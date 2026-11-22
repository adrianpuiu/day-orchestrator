"""Installer: plist generation and config example sanity."""

from __future__ import annotations

import plistlib
import tomllib
from pathlib import Path

from app.cli import AGENT_LABEL, _render_agent_plist

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_render_agent_plist_uses_real_paths() -> None:
    data_dir = Path.home() / "Library" / "Application Support" / "day-orchestrator"
    plist = _render_agent_plist(PROJECT_ROOT, data_dir)

    assert plist["Label"] == AGENT_LABEL
    args = plist["ProgramArguments"]
    assert args[0].endswith("DayOrchestrator.app/Contents/MacOS/DayOrchestrator")
    assert Path(args[0]).exists(), "bundle executable missing"
    assert args[1] == "serve"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["StandardErrorPath"].startswith(str(data_dir))

    raw = plistlib.dumps(plist, fmt=plistlib.FMT_XML)
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == AGENT_LABEL, "must be valid launchd XML"


def test_config_example_exists_and_is_sane() -> None:
    example = PROJECT_ROOT / "config.toml.example"
    assert example.exists(), "installer copies config.toml.example"
    values = tomllib.loads(example.read_text(encoding="utf-8"))
    assert values.get("model_api_base") == "", (
        "tracked example must not point at a private LAN model server"
    )
    assert values.get("calendar_backend") == "apple"
