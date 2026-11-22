"""Configuration: one frozen dataclass, TOML override, sane defaults.

Constraint C5: loading is pure; nothing calls the LLM or network here.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "day-orchestrator"
    return Path.home() / ".local" / "share" / "day-orchestrator"


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    timezone: str = "local"  # "local" = system zone; else IANA name
    brief_time: str = "07:00"  # local wall clock, HH:MM
    sweep_times: tuple[str, ...] = ("13:00", "18:00")
    triage_model: str = "gemini-3.8-flash"
    planner_model: str = "gemini-3.8-pro"
    coordinator_model: str = "gemini-3.8-flash"
    # Empty → Gemini model strings as-is; set → OpenAI-compatible via LiteLLM
    # (architecture.md evolution step 2), specs are bare model ids.
    model_api_base: str = ""
    model_api_key: str = "EMPTY"
    calendar_backend: str = "apple"  # "apple" (EventKit, ADR-009) | "google"
    gmail_poll_seconds: int = 180  # IDLE fallback poll (ADR-002)
    max_overnight_emails: int = 50
    # C10 notification budget: max proactive pushes per category per day
    budgets: dict[str, int] = field(
        default_factory=lambda: {"briefing": 1, "triage_alerts": 5, "reminders": 20}
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "day.db"

    @classmethod
    def load(cls, root: Path = PROJECT_ROOT) -> Config:
        """Defaults, overridden by config.toml at the project root if present."""
        values: dict = {}
        toml_path = root / "config.toml"
        if toml_path.exists():
            values = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        sweep = values.pop("sweep_times", None)
        budgets = values.pop("budgets", None)
        cfg = cls(**values)
        if sweep is not None:
            object.__setattr__(cfg, "sweep_times", tuple(sweep))
        if budgets is not None:
            object.__setattr__(cfg, "budgets", budgets)
        return cfg
