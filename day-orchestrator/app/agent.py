# ruff: noqa
"""ADK entrypoint: the W5 coordinator as `root_agent`.

Programmatic runs (briefings, triage, sweeps) go through `app.runtime`.
This module exists for `adk web` / `agents-cli playground` debugging of the
coordinator conversation with the full tool set.
"""

from google.adk.apps import App

from .channels.calendar import CalendarClient
from .channels.gmail import GmailClient
from .core.config import PROJECT_ROOT, Config
from .core.db import Database
from .core.secrets import default_secrets
from .tools import ToolDeps, make_tools
from .runtime import NullNotifier
from .workflows.agents import make_coordinator


def _build_root_agent():
    cfg = Config.load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.init_schema()
    secrets = default_secrets(PROJECT_ROOT)
    if cfg.calendar_backend == "apple":
        from .channels.calendar_apple import AppleCalendarClient

        calendar = AppleCalendarClient(target_calendar=cfg.calendar_target)
    else:
        calendar = CalendarClient(cfg.data_dir / "calendar_token.json")
    deps = ToolDeps(
        db=db,
        gmail=GmailClient(
            secrets.get("GMAIL_ADDRESS") or "", secrets.get("GMAIL_APP_PASSWORD") or ""
        ),
        calendar=calendar,
        notifier=NullNotifier(),  # playground must not push to Telegram
        budgets=cfg.budgets,
        timezone=cfg.timezone,
    )
    return make_coordinator(cfg, make_tools(deps))


root_agent = _build_root_agent()
app = App(name="app", root_agent=root_agent)
