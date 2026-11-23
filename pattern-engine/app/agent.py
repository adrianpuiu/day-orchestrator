"""Root agent: the deterministic trading plane.

Phase 1+2: PatternMonitor only (zero LLM). Phase 3 adds the research loop
as siblings under a coordinator — the trading plane stays untouched.
"""

from __future__ import annotations

from .core import market
from .core.config import load_config
from .core.market import load_eod
from .core.registry import Registry
from .exec.paper import PaperGateway
from .exec.risk import RiskManager
from .workflows.monitor import PatternMonitor


def build_monitor(config=None, df=None):
    cfg = config or load_config()
    if df is None:
        if not cfg.data_path.exists():
            market.ingest_eod(cfg.market, cfg.data_path)
        data = load_eod(cfg.data_path)
    else:
        data = df
    registry = Registry(cfg.registry_db)
    from .strategies.seeds import seed_specs
    for spec in seed_specs():
        registry.upsert(spec)
    gateway = PaperGateway(cost_bp_side=cfg.cost_bp_side)
    risk = RiskManager(
        unit_equity_usd=cfg.unit_equity_usd,
        risk_frac_per_trade=cfg.risk_frac_per_trade,
        missed_run_limit=cfg.missed_run_limit,
    )
    return PatternMonitor(
        name="pattern_engine",           # keep in sync with agents-cli-manifest
        registry=registry, gateway=gateway, risk=risk, df=data,
    )


def __getattr__(name: str):
    if name == "root_agent":
        return build_monitor()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
