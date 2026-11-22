"""APScheduler wiring (ADR-005): date-scoped job ids, claims in action_log.

The scheduler uses the in-memory store BY DESIGN: every scheduled effect is
re-derived from config on every boot (`ensure_daily` re-adds date-scoped
jobs), the at-most-once latch lives in the action_log claims, and reminder
truth lives in the `reminders` table. A persistent jobstore adds nothing here
— missed effects within the grace window fire once on start; older ones are
skipped and logged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from .db import Database
from .events import REMINDER_DUE, Event, EventBus

log = logging.getLogger(__name__)

GRACE_SECONDS = 6 * 3600  # APScheduler 3.x wants misfire_grace_time in seconds

Handler = Callable[[str], Awaitable[None]]


def local_tzinfo(timezone_cfg: str):
    if timezone_cfg == "local":
        return datetime.now().astimezone().tzinfo
    from zoneinfo import ZoneInfo

    return ZoneInfo(timezone_cfg)


class EffectScheduler:
    def __init__(
        self,
        timezone_cfg: str,
        handlers: dict[str, Handler],
        bus: EventBus,
    ) -> None:
        self._handlers = handlers
        self._bus = bus
        self._tz = local_tzinfo(timezone_cfg)
        self._sched = AsyncIOScheduler(timezone=self._tz)
        # kind -> HH:MM list registered via ensure_daily; the midnight re-arm
        # job replays them so a long-lived process keeps scheduling future
        # days (launchd KeepAlive runs us for weeks, not one day).
        self._daily_specs: dict[str, list[str]] = {}
        self._last_armed_day: date | None = None

    async def start(self) -> None:
        self._sched.start(paused=False)

        # Reminder ticks: rows are the truth (ADR-005); jobstore is a cache.
        self._sched.add_job(
            self._tick_reminders,
            "interval",
            seconds=60,
            id="reminders_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        # Re-arm tomorrow's date-scoped jobs at local midnight: ensure_daily
        # jobs are one-shot for *today* only; without this the scheduler is
        # empty from day 2 of uptime onward (briefs and sweeps die silently).
        self._sched.add_job(
            self._rearm_daily,
            "cron",
            hour=0,
            minute=0,
            id="rearm_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=GRACE_SECONDS,
        )

    def ensure_daily(self, kind: str, at_hm: str, now: datetime | None = None) -> None:
        """Idempotently schedule today's run at HH:MM local. Date-scoped job id."""
        if at_hm not in self._daily_specs.setdefault(kind, []):
            self._daily_specs[kind].append(at_hm)
        now = now or datetime.now(self._tz)
        hour, minute = (int(p) for p in at_hm.split(":"))
        run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        job_id = f"{kind}:{now:%Y-%m-%d}"
        self._sched.add_job(
            self._fire,
            DateTrigger(run_date=run_at),
            id=job_id,
            args=[kind, job_id],
            replace_existing=True,
            misfire_grace_time=GRACE_SECONDS,
            coalesce=True,
            max_instances=1,
        )
        if run_at < now:
            log.info("scheduled %s was missed at %s; catch-up within grace", job_id, run_at)

    async def _fire(self, kind: str, dedup_key: str) -> None:
        handler = self._handlers.get(kind)
        if handler is None:
            log.error("no handler for scheduled effect %r", kind)
            return
        await handler(dedup_key)

    async def _rearm_daily(self) -> None:
        """Re-schedule every registered daily effect for the new local day."""
        for kind, times in self._daily_specs.items():
            for at_hm in times:
                self.ensure_daily(kind, at_hm)
        log.info("daily effects re-armed: %s", dict(self._daily_specs))

    async def _tick_reminders(self) -> None:
        # Day-change reconcile: if the midnight re-arm was missed (a Mac can
        # sleep far past its misfire grace), the next 60s tick re-arms today.
        today = datetime.now(self._tz).date()
        if today != self._last_armed_day:
            self._last_armed_day = today
            await self._rearm_daily()
        # Caller passes db lazily via set_database(); kept optional for tests.
        if self._db is None:
            return
        now_iso = datetime.now(self._tz).isoformat(timespec="seconds")
        due = self._db.query(
            "SELECT id, text FROM reminders WHERE status='scheduled' AND fire_at<=?",
            (now_iso,),
        )
        for row in due:
            cur = self._db.execute(
                "UPDATE reminders SET status='fired', fired_at=? WHERE id=? "
                "AND status='scheduled'",
                (now_iso, row["id"]),
            )
            if cur.rowcount == 1:  # atomic claim: exactly one firer
                await self._bus.publish(
                    Event(type=REMINDER_DUE, payload={"id": row["id"], "text": row["text"]})
                )

    _db: Database | None = None

    def set_database(self, db: Database) -> None:
        self._db = db

    async def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
