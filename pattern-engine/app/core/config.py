"""Configuration: config.toml + env, mirroring the day-orchestrator pattern."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    # Model serving: model_api_base set -> every model string is served
    # OpenAI-compatible via LiteLLM (ds41 on the LAN); research plane only —
    # the trading plane makes zero LLM calls.
    model_api_base: str = "http://192.168.0.206:8000/v1"
    model_api_key: str = "EMPTY"
    research_model: str = "ds41"

    # Data
    market: str = "SPY"
    data_dir: str = "data"
    fixture_path: str = "tests/fixtures/spy_daily.parquet"

    # Risk (DESIGN.md: even bet, unit per $100k, ATR-normalized)
    unit_equity_usd: float = 100_000.0
    risk_frac_per_trade: float = 0.01      # 1% of unit equity at risk
    max_stop_atr: float = 2.5              # skip signals needing more room
    cost_bp_side: float = 0.5              # paper fills carry assumed friction

    # Circuit breakers
    missed_run_limit: int = 2              # process degradation -> halve size

    # Hyperliquid (shadow mode: read-only, address only)
    hl_account: str = ""
    hl_testnet: bool = False
    confirm_first_n: int = 5             # first N live orders need approval

    # Paths
    db_dir: str = "var"

    @property
    def data_path(self) -> Path:
        return PROJECT_ROOT / self.data_dir / f"{self.market.lower()}_daily.parquet"

    @property
    def registry_db(self) -> Path:
        return PROJECT_ROOT / self.db_dir / "registry.db"

    @property
    def journal_db(self) -> Path:
        return PROJECT_ROOT / self.db_dir / "journal.db"

    @property
    def session_db(self) -> str:
        return f"sqlite+aiosqlite:///{PROJECT_ROOT / self.db_dir / 'sessions.db'}"


def load_config(path: Path | None = None) -> Config:
    toml_path = path or (PROJECT_ROOT / "config.toml")
    _load_dotenv()
    values: dict = {}
    if toml_path.exists():
        with open(toml_path, "rb") as fh:
            values = tomllib.load(fh)
    # .env-canonical credentials override (day-orchestrator convention)
    env_keys = {"MODEL_API_BASE": "model_api_base",
                "MODEL_API_KEY": "model_api_key",
                "HL_ACCOUNT_ADDRESS": "hl_account"}
    for key, field in env_keys.items():
        if os.environ.get(key):
            values[field] = os.environ[key]
    known = Config.__dataclass_fields__.keys()
    return Config(**{k: v for k, v in values.items() if k in known})


def _load_dotenv() -> None:
    """Reads PROJECT_ROOT/.env into os.environ (no override)."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
