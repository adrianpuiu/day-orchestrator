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
    # SPY incumbents are locked to the fixture; both windows are the same
    # verified series so daily semantics are unchanged.
    windows = {"d": {"research": df, "confirm": df}}
    loop = build_research_loop(cfg, registry, windows, max_iterations=iterations)
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
    """Dual-window reseeding per HL market: long Yahoo-proxy research
    window + HL-native confirmation window, daily and 8h timeframes."""
    from .core.hyperliquid import WISHLIST, ingest_hl_8h, load_hl
    from .core.indicators import with_indicators
    from .core.market import quality_gate
    from .core.yahoo import YAHOO_MAP, load_yahoo
    from .research.agents import build_research_loop

    registry = _seed_registry(cfg)
    out: dict = {}
    for coin in WISHLIST:
        daily = load_hl(coin, Path(cfg.data_dir))
        bars8 = ingest_hl_8h(coin, Path(cfg.data_dir))
        if not quality_gate(daily).ok and len(bars8) < 300:
            log.info("[hl-research] %s: data gate failed, skipped", coin)
            out[coin] = "skipped: data gate"
            continue
        windows: dict = {}
        if coin in YAHOO_MAP:
            windows["d"] = {"research": with_indicators(load_yahoo(coin,
                                              Path(cfg.data_dir))),
                            "confirm": with_indicators(daily)}
        elif quality_gate(daily).ok:      # XYZ100: HL-only, thin but honest
            windows["d"] = {"research": with_indicators(daily),
                            "confirm": with_indicators(daily)}
        if len(bars8) >= 300:
            windows["8h"] = {"research": with_indicators(bars8),
                             "confirm": with_indicators(bars8.tail(1095))}
        if not windows:
            out[coin] = "skipped: no window"
            continue
        loop = build_research_loop(cfg, registry, windows, iterations,
                                   market=coin)
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
        out[coin] = str(fresh.state.get("grade"))[:400]
        log.info("[hl-research] %s: %s", coin, out[coin])
    return out

async def run_hl_shadow(cfg, address: str | None = None) -> dict:
    """Shadow mode: paper-run every live HL strategy on fresh bars, mirror
    the real account read-only, reconcile the two books. No orders, ever."""
    import json

    from .core.hyperliquid import ingest_hl
    from .core.indicators import with_indicators
    from .core.registry import Registry
    from .exec.hyperliquid_gateway import HyperliquidGateway
    from .exec.paper import PaperGateway
    from .exec.risk import RiskManager
    from .workflows.monitor import PatternMonitor

    addr = address or cfg.hl_account
    if not addr:
        raise SystemExit("hl-shadow: set HL_ACCOUNT_ADDRESS in .env "
                         "or pass an address argument")
    registry = _seed_registry(cfg)
    gw = HyperliquidGateway(addr)

    paper: dict = {}
    for spec in [s for s in registry.live_specs() if s.market != "SPY"]:
        df = with_indicators(ingest_hl(spec.market, Path(cfg.data_dir)))
        mreg = Registry(Path(cfg.db_dir) /
                        f"shadow_{spec.market.replace(':', '_')}.db")
        mreg.upsert(spec)
        mon = PatternMonitor(
            name=f"shadow_{spec.market.replace(':', '_')}", registry=mreg,
            gateway=PaperGateway(cfg.cost_bp_side),
            risk=RiskManager(unit_equity_usd=cfg.unit_equity_usd,
                             risk_frac_per_trade=cfg.risk_frac_per_trade),
            df=df)
        trades = mon.replay()
        pos = None if mon.gateway.flat else {
            "side": mon.gateway.position.signal.side,
            "entry": round(mon.gateway.position.entry_fill.price, 2),
            "entry_ts": str(mon.gateway.position.entry_fill.ts),
        }
        paper[spec.market] = {
            "strategy": spec.id, "trades": len(trades),
            "avg_r": round(sum(t["r"] for t in trades) / len(trades), 3)
            if trades else None,
            "open_position": pos,
            "last_bar": str(df.index[-1])}
        mreg.close()

    acct = gw.snapshot()
    acct_pos = {p["coin"]: p["szi"] for p in acct["positions"]}
    recon = []
    for mkt, book in paper.items():
        held = acct_pos.get(mkt, 0.0)
        want = (book["open_position"] or {}).get("side", 0)
        if (held > 0) == (want > 0) and held * want > 0:
            state = "ALIGNED"
        elif held == 0 and want == 0:
            state = "ALIGNED (flat)"
        else:
            state = f"DIVERGENT (paper {want:+d}, account {held:+g})"
        recon.append(f"{mkt:>12}: {state}")

    out = {"paper": paper, "account": {
        "as_of": acct["as_of"],
        "account_value": acct["margin_summary"].get("accountValue"),
        "positions": acct["positions"],
        "recent_fills": acct["recent_fills"]},
        "reconciliation": recon}
    dest = Path(cfg.db_dir) / "shadow_last.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str))
    # append-only record: the reconciliation history builds itself
    record = {"as_of": out["account"]["as_of"],
              "account_value": out["account"]["account_value"],
              "paper": {m: {"open": bool(b["open_position"]),
                            "trades": b["trades"]}
                        for m, b in paper.items()},
              "reconciliation": recon}
    hist = Path(cfg.db_dir) / "shadow_history.jsonl"

    def _append_history() -> None:
        with hist.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    await asyncio.to_thread(_append_history)
    for line in recon:
        log.info("[hl-shadow] %s", line)
    log.info("[hl-shadow] account %s @ %s, artifact %s",
             acct["margin_summary"].get("accountValue"), acct["as_of"], dest)
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
                      misfire_grace_time=3600, max_instances=1,
                      id="report_run")
    scheduler.add_job(lambda: asyncio.ensure_future(
                          _job(cfg, "hl-shadow", run_hl_shadow)),
                      "cron", hour=0, minute=5, timezone="UTC",   # 24/7: after the UTC daily mark
                      misfire_grace_time=3600, max_instances=1,
                      id="shadow_run")
    scheduler.start()
    log.info("scheduler running: eod(mon-fri 22:15Z), decay(23:30Z), "
             "research(sat 12:00Z), report(23:35Z), "
             "hl-shadow(daily 00:05Z)")
    await asyncio.Event().wait()


async def _job(cfg, name: str, fn) -> None:
    try:
        result = await fn(cfg)
        log.info("[%s] ok: %s", name, str(result)[:300])
    except Exception:  # a daemon must log and survive
        log.exception("[%s] failed", name)


def _live_guards(cfg):
    from .exec.guards import ConfirmationGate, KillSwitch

    base = Path(cfg.db_dir)
    kill = KillSwitch(base / "KILL")
    gate = ConfirmationGate(base / "live_state.json",
                            confirm_first_n=cfg.confirm_first_n)
    return kill, gate


def run_preflight(cfg) -> dict:
    """Human checklist before arming: guards, credentials, account."""
    from .exec.hyperliquid_gateway import HyperliquidGateway
    from .exec.live_gateway import LiveGateway

    kill, gate = _live_guards(cfg)
    gw = LiveGateway(kill, gate, cfg.hl_account, testnet=cfg.hl_testnet)
    out = gw.preflight()
    if cfg.hl_account:
        try:
            acct = HyperliquidGateway(cfg.hl_account).snapshot()
            out["account_value"] = acct["account_value"]
            out["account_positions"] = [p["coin"] for p in acct["positions"]]
        except (OSError, ValueError, KeyError) as e:  # read must not hide state
            out["account_read_error"] = str(e)[:120]
    for k, v in out.items():
        print(f"{k:>20}: {v}")
    return out


def run_approve(cfg, intent_id=None) -> dict:
    """Executes a pending intent through the same guarded path."""
    from .exec.live_gateway import LiveGateway

    kill, gate = _live_guards(cfg)
    gw = LiveGateway(kill, gate, cfg.hl_account, testnet=cfg.hl_testnet)
    pending = gate.pending()
    if not intent_id:
        print("pending intents:")
        for iid, intent in pending.items():
            print(f"  {iid}: {intent['kind']} {intent['coin']} "
                  f"side={intent['side']} qty={intent['qty']} "
                  f"@~{intent.get('price_hint')}")
        return {"pending": list(pending)}
    intent = gate.take_approved(intent_id)
    if intent is None:
        raise SystemExit(f"no pending intent {intent_id!r}")
    # approval bypasses re-enqueue (the human already said yes) but NOT the
    # kill switch or the armed check
    result = gate.guarded_send(kill, intent, gw._send, approved=True)
    gate.note_confirmed_order()
    print(f"approved+sent {intent_id}: {result}")
    return result


async def run_live_once(cfg) -> dict:
    """One live step over every live HL strategy — the gated counterpart of
    hl-shadow. Refuses to run unless armed and not killed."""
    from .core.hyperliquid import ingest_hl
    from .core.indicators import with_indicators
    from .core.registry import Registry
    from .exec.guards import TradingHalted
    from .exec.live_gateway import LiveGateway
    from .exec.risk import RiskManager
    from .workflows.monitor import PatternMonitor

    kill, gate = _live_guards(cfg)
    if kill.tripped or not gate.armed:
        raise SystemExit(f"live-once: not armed or killed "
                         f"(tripped={kill.tripped}, armed={gate.armed})")
    registry = _seed_registry(cfg)
    out: dict = {}
    for spec in [s for s in registry.live_specs() if s.market != "SPY"]:
        df = with_indicators(ingest_hl(spec.market, Path(cfg.data_dir)))
        mreg = Registry(Path(cfg.db_dir) /
                        f"live_{spec.market.replace(':', '_')}.db")
        mreg.upsert(spec)
        gw = LiveGateway(kill, gate, cfg.hl_account,
                         cost_bp_side=cfg.cost_bp_side,
                         testnet=cfg.hl_testnet,
                         coin_of=lambda sid, m=spec.market: m)
        mon = PatternMonitor(
            name=f"live_{spec.market.replace(':', '_')}", registry=mreg,
            gateway=gw,
            risk=RiskManager(unit_equity_usd=cfg.unit_equity_usd,
                             risk_frac_per_trade=cfg.risk_frac_per_trade),
            df=df)
        try:
            trades = mon.replay(end=len(df) - 1)      # all but newest bar
            out[spec.market] = {
                "closed": len(trades),
                "open": None if gw.flat else {
                    "entry": gw.position.entry_fill.price,
                    "ts": str(gw.position.entry_fill.ts)},
                "pending": list(gate.pending())}
        except TradingHalted as e:
            out[spec.market] = {"halted": str(e)}
            log.error("[live-once] %s halted: %s", spec.market, e)
            break                        # a halt stops every market
        mreg.close()
    return out


MODES = {
    "once": run_once, "replay": run_replay, "research": run_research,
    "decay": run_decay, "report": run_report, "cycle": run_cycle,
    "hl-ingest": run_hl_ingest, "hl-research": run_hl_research,
    "hl-shadow": run_hl_shadow, "serve": run_serve,
    "live-once": run_live_once, "preflight": run_preflight,
    "approve": run_approve,
}


async def main(argv: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    mode = argv[1] if len(argv) > 1 else "once"
    if mode in ("arm", "kill"):
        _run_guard_cli(cfg, mode)
        return
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
    if mode == "hl-shadow":
        await run_hl_shadow(cfg, *argv[2:])
        return
    if mode == "approve":
        run_approve(cfg, *argv[2:])
        return
    result = MODES[mode](cfg)
    if asyncio.iscoroutine(result):
        await result


def _run_guard_cli(cfg, mode: str) -> None:
    kill, gate = _live_guards(cfg)
    if mode == "kill":
        kill.trip(reason="cli")
        gate.disarm()
        print("KILL switch tripped; gateway disarmed. "
              "Manual re-arm required.")
        return
    if kill.tripped:
        raise SystemExit("kill switch tripped — remove var/KILL "
                         "deliberately before arming")
    gate.arm()
    print(f"armed. confirm_first_n={gate.confirm_first_n} — the "
          f"first {gate.confirm_first_n} live orders need `approve`.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
