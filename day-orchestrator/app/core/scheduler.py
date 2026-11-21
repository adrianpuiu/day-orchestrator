"""APScheduler wiring (ADR-005): date-scoped job ids, claims in action_log.

Persistent jobstore survives restarts; a job missed while the Mac slept fires
once on wake within misfire_grace_time (coalesce), never twice — handlers
additionally claim their dedup key in the action log.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from .db import Database
from .events import REMINDER_DUE, Event, EventBus

log = logging.getLogger(__name__)

GRACE = timedelta(hours=6)

Handler = Callable[[str], Awaitable[None]]


def local_tzinfo(timezone_cfg: str):
    if timezone_cfg == "local":
        return datetime.now().astimezone().tzinfo
    from zoneinfo import ZoneInfo

    return ZoneInfo(timezone_cfg)


class EffectScheduler:
    def __init__(
        self,
        db_path: Path,
        timezone_cfg: str,
        handlers: dict[str, Handler],
        bus: EventBus,
    ) -> None:
        self._handlers = handlers
        self._bus = bus
        self._tz = local_tzinfo(timezone_cfg)
        self._db_path = db_path
        self._sched = AsyncIOScheduler(
            jobstores={"main": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")},
            timezone=self._tz,
        )

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

    def ensure_daily(self, kind: str, at_hm: str, now: datetime | None = None) -> None:
        """Idempotently schedule today's run at HH:MM local. Date-scoped job id."""
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
            misfire_grace_time=GRACE,
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

    async def _tick_reminders(self) -> None:
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
