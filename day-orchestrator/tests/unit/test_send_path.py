"""CI send-path + import-discipline gates (ADR-006): AST scans, not review.

Enforces the trust boundary mechanically:
- only app/channels/gmail.py imports smtplib / imapclient;
- only app/channels/telegram.py imports aiogram;
- only app/channels/calendar.py imports google api client libs;
- only app/tools.py calls approval.require_resolved (the send gate);
- channels never import each other.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def iter_py() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def calls_in(path: Path, names: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            hits.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in names:
            hits.append(node.id)
    return hits


def _assert_single_owner(matches: dict[Path, set[str]], label: str, owner: str) -> None:
    offenders = {str(p): sorted(m) for p, m in matches.items() if m}
    unexpected = {k: v for k, v in offenders.items() if not k.endswith(owner)}
    assert not unexpected, f"{label} imported outside {owner}: {unexpected}"


def test_only_gmail_module_imports_mail_transports() -> None:
    matches: dict[Path, set[str]] = {}
    for path in iter_py():
        mods = imports_of(path)
        hits = {m for m in mods if m in {"smtplib", "imapclient"} or
                m.startswith("imapclient")}
        if hits:
            matches[path] = hits
    assert matches, "gmail transport imports expected in app/channels/gmail.py"
    _assert_single_owner(matches, "mail transport", "channels/gmail.py")


def test_only_telegram_module_imports_aiogram() -> None:
    matches: dict[Path, set[str]] = {}
    for path in iter_py():
        hits = {m for m in imports_of(path) if m == "aiogram" or
                m.startswith("aiogram.")}
        if hits:
            matches[path] = hits
    assert matches
    _assert_single_owner(matches, "aiogram", "channels/telegram.py")


def test_only_calendar_modules_import_calendar_clients() -> None:
    matches: dict[Path, set[str]] = {}
    for path in iter_py():
        hits = {
            m
            for m in imports_of(path)
            if m.startswith(("googleapiclient", "google_auth_oauthlib", "google.oauth2",
                             "google.auth.transport", "google.auth.exceptions"))
            or m == "EventKit"
        }
        if hits:
            matches[path] = hits
    if matches:  # calendar deps optional at runtime; if present, only these files
        unexpected = {
            str(p): sorted(m)
            for p, m in matches.items()
            if not (str(p).endswith("channels/calendar.py")
                    or str(p).endswith("channels/calendar_helper.py"))
        }
        assert not unexpected, f"calendar clients imported outside adapters: {unexpected}"


def test_send_gate_called_only_in_tools_layer() -> None:
    matches: dict[Path, list[str]] = {}
    for path in iter_py():
        hits = calls_in(path, {"require_resolved"})
        if hits:
            matches[path] = hits
    assert matches, "approval.require_resolved must gate the send path"
    unexpected = {str(p): v for p, v in matches.items()
                  if not str(p).endswith("tools.py")}
    assert not unexpected, f"send gate used outside app/tools.py: {unexpected}"


def test_channels_do_not_import_each_other() -> None:
    channel_files = [p for p in (APP / "channels").glob("*.py")
                     if p.name != "__init__.py"]
    for path in channel_files:
        own = f"app.channels.{path.stem}"
        for mod in imports_of(path):
            if mod.startswith("app.channels."):
                assert mod == own, (
                    f"{path.name} imports sibling channel {mod} (ADR-006)"
                )
