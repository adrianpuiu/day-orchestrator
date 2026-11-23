"""Runtime: session service, one-shot EOD run, replay, scheduler loop.

Usage:
  python -m app.runtime once    # ingest + quality gate + process newest bar
  python -m app.runtime replay  # full-history paper replay + stats
  python -m app.runtime serve   # APScheduler loop (no jobstore: schedule is
                                # re-derived at boot, DB rows are the truth)
"""

from __future__ import annotations

import asyncio
import sys

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types as gtypes

from .agent import build_monitor
from .core import backtest, labels, market
from .core.config import load_config


def make_runner(cfg, agent) -> Runner:
    svc = DatabaseSessionService(cfg.session_db)   # sqlite+aiosqlite (async driver)
    return Runner(agent=agent, app_name="pattern-engine", session_service=svc)


async def run_once(cfg) -> str:
    market.ingest_eod(cfg.market, cfg.data_path)
    monitor = build_monitor(cfg)
    runner = make_runner(cfg, monitor)
    session = await runner.session_service.create_session(
        app_name="pattern-engine", user_id="local")
    events = []
    async for event in runner.run_async(
            user_id="local", session_id=session.id,
            new_message=gtypes.Content(role="user",
                                       parts=[gtypes.Part(text="eod run")])):
        events.append(event)
    return events[-1].content if events else "no events"


async def run_replay(cfg) -> dict:
    df = market.load_eod(cfg.data_path)
    monitor = build_monitor(cfg, df=df)
    closed = monitor.replay()
    print(f"closed trades: {len(closed)}")
    by_id: dict[str, list[dict]] = {}
    for t in closed:
        by_id.setdefault(t["strategy_id"], []).append(t)
    report = {}
    for sid, trades in by_id.items():
        stats = backtest.summarize(trades)
        stats["excursions"] = labels.label_report(labels.excursions(monitor.df, trades))
        report[sid] = stats
        print(sid, stats)
    return report


async def run_serve(cfg) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    # EOD after US close (17:15 America/New_York); misfire_grace_time must be
    # int seconds (timedelta raises TypeError inside add_job).
    scheduler.add_job(
        lambda: asyncio.ensure_future(run_once(cfg)), "cron",
        hour=22, minute=15, timezone="UTC",
        misfire_grace_time=3600,
        id="eod_run",
    )
    scheduler.start()
    print("scheduler running; ctrl-c to stop")
    await asyncio.Event().wait()


async def main(argv: list[str]) -> None:
    cfg = load_config()
    mode = argv[1] if len(argv) > 1 else "once"
    if mode == "once":
        print(await run_once(cfg))
    elif mode == "replay":
        await run_replay(cfg)
    elif mode == "serve":
        await run_serve(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
