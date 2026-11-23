"""Runtime: autonomous daemon, one-shot modes, cycle smoke.

Modes:
  once    # ingest + quality gate + process newest bar (ADK Runner + sessions)
  replay  # full-history paper replay + stats
  research# one regeneration-loop round (ds41 proposes, code grades)
  decay   # one DecayWatcher pass (trailing-window re-grade)
  report  # write today's markdown report
  cycle   # once -> decay -> research -> report, synchronously (smoke/kick)
  serve   # the daemon: flock single-instance + APScheduler
          #   EOD     weekdays 22:15 UTC (17:15 America/New_York)
          #   decay   daily    23:30 UTC
          #   research weekly   Sat 12:00 UTC
          #   report  daily    23:35 UTC
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import sys
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types as gtypes

from .agent import build_monitor
from .core import backtest, labels, market
from .core.config import load_config
from .core.journal import JournalStore
from .core.registry import Registry
from .core.report import write_report
from .strategies.seeds import seed_specs

log = logging.getLogger("pattern-engine")


def _seed_registry(cfg) -> Registry:
    registry = Registry(cfg.registry_db)
    for spec in seed_specs():
        registry.upsert(spec)
    return registry


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
    flat = "flat" if monitor.gateway.flat else "in position"
    return (f"{events[-1].content.parts[0].text if events else 'no events'}"
            f"; book {flat}")


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


async def run_research(cfg, iterations: int = 3) -> dict:
    from .core.indicators import with_indicators
    from .research.agents import build_research_loop

    df = with_indicators(market.load_eod(cfg.data_path))
    registry = _seed_registry(cfg)
    loop = build_research_loop(cfg, registry, df, max_iterations=iterations)
    runner = make_runner(cfg, loop)
    session = await runner.session_service.create_session(
        app_name="pattern-engine", user_id="local",
        state={"registry_state": registry.as_dicts()})
    async for event in runner.run_async(
            user_id="local", session_id=session.id,
            new_message=gtypes.Content(role="user",
                                       parts=[gtypes.Part(text="run research")])):
        log.debug("research event: %s", event.author)
    fresh = await runner.session_service.get_session(
        app_name="pattern-engine", user_id="local", session_id=session.id)
    grade = fresh.state.get("grade")
    log.info("research grade: %s", grade)
    return {"grade": grade, "registry": registry.as_dicts()}


async def run_decay(cfg) -> dict:
    from .core.indicators import with_indicators
    from .research.decay import evaluate_decay

    df = with_indicators(market.load_eod(cfg.data_path))
    registry = _seed_registry(cfg)
    return evaluate_decay(registry, df)


async def run_report(cfg, decay: dict | None = None) -> Path:
    registry = _seed_registry(cfg)
    journal = JournalStore(cfg.journal_db)
    path = write_report(Path(cfg.db_dir) / "reports", registry, journal,
                        decay=decay)
    journal.close()
    log.info("report written: %s", path)
    return path


async def run_hl_ingest(cfg) -> dict:
    """Ingests the Hyperliquid wishlist; returns per-market quality."""
    from .core.hyperliquid import WISHLIST, ingest_hl
    from .core.market import quality_gate

    out: dict = {}
    for coin in WISHLIST:
        df = ingest_hl(coin, Path(cfg.data_dir))
        q = quality_gate(df)
        out[coin] = {"days": len(df), "ok": q.ok, "problems": q.problems}
        log.info("[hl-ingest] %s: %s days ok=%s", coin, len(df), q.ok)
    return out


async def run_hl_research(cfg, iterations: int = 2) -> dict:
    """Reseeds the research loop on every quality-passing HL market."""
    from .core.hyperliquid import WISHLIST, load_hl
    from .core.indicators import with_indicators
    from .core.market import quality_gate
    from .research.agents import build_research_loop

    registry = _seed_registry(cfg)
    out: dict = {}
    for coin in WISHLIST:
        df = load_hl(coin, Path(cfg.data_dir))
        if not quality_gate(df).ok:
            log.info("[hl-research] %s: data gate failed, skipped", coin)
            out[coin] = "skipped: data gate"
            continue
        loop = build_research_loop(cfg, registry, with_indicators(df),
                                   iterations, market=coin)
        runner = make_runner(cfg, loop)
        session = await runner.session_service.create_session(
            app_name="pattern-engine", user_id="local",
            state={"registry_state": [
                r for r in registry.as_dicts() if r["market"] == coin]})
        async for _e in runner.run_async(
                user_id="local", session_id=session.id,
                new_message=gtypes.Content(role="user",
                                           parts=[gtypes.Part(text="research")])):
            pass
        fresh = await runner.session_service.get_session(
            app_name="pattern-engine", user_id="local", session_id=session.id)
        out[coin] = str(fresh.state.get("grade"))[:200]
        log.info("[hl-research] %s: %s", coin, out[coin])
    return out


async def run_cycle(cfg, with_research: bool = True) -> dict:
    """One synchronous full pass — smoke verification and manual kick."""
    out: dict = {}
    out["once"] = await run_once(cfg)
    out["decay"] = await run_decay(cfg)
    if with_research:
        out["research"] = await run_research(cfg, iterations=1)
    out["report"] = str(await run_report(cfg, decay=out["decay"]))
    for key, val in out.items():
        print(f"[cycle:{key}] {val}")
    return out


def _acquire_instance_lock(lock_path: Path):
    """flock single instance; a second daemon parks (never double-trades)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        log.warning("another instance holds %s; parking until it exits",
                    lock_path)
        fcntl.flock(fh, fcntl.LOCK_EX)     # blocking: wait it out
        return fh


async def run_serve(cfg) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    # misfire_grace_time must be int seconds (timedelta raises TypeError).
    scheduler.add_job(lambda: asyncio.ensure_future(_job(cfg, "eod", run_once)),
                      "cron", day_of_week="mon-fri", hour=22, minute=15,
                      timezone="UTC", misfire_grace_time=3600,
                      max_instances=1, id="eod_run")
    scheduler.add_job(lambda: asyncio.ensure_future(
                          _job(cfg, "decay", run_decay)),
                      "cron", hour=23, minute=30, timezone="UTC",
                      misfire_grace_time=3600, max_instances=1, id="decay_run")
    scheduler.add_job(lambda: asyncio.ensure_future(
                          _job(cfg, "research", run_research)),
                      "cron", day_of_week="sat", hour=12, timezone="UTC",
                      misfire_grace_time=3600, max_instances=1,
                      id="research_run")
    scheduler.add_job(lambda: asyncio.ensure_future(
                          _job(cfg, "report", run_report)),
                      "cron", hour=23, minute=35, timezone="UTC",
                      misfire_grace_time=3600, max_instances=1, id="report_run")
    scheduler.start()
    log.info("scheduler running: eod(mon-fri 22:15Z), decay(23:30Z), "
             "research(sat 12:00Z), report(23:35Z)")
    await asyncio.Event().wait()


async def _job(cfg, name: str, fn) -> None:
    try:
        result = await fn(cfg)
        log.info("[%s] ok: %s", name, str(result)[:300])
    except Exception:  # a daemon must log and survive
        log.exception("[%s] failed", name)


MODES = {
    "once": run_once, "replay": run_replay, "research": run_research,
    "decay": run_decay, "report": run_report, "cycle": run_cycle,
    "hl-ingest": run_hl_ingest, "hl-research": run_hl_research,
    "serve": run_serve,
}


async def main(argv: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    mode = argv[1] if len(argv) > 1 else "once"
    if mode not in MODES:
        print(__doc__)
        return
    if mode == "serve":
        lock = _acquire_instance_lock(Path(cfg.db_dir) / "daemon.lock")
        try:
            await run_serve(cfg)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        return
    await MODES[mode](cfg)


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
