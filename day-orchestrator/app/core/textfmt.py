"""Text formatting shared across modules (channels and workflows both import core)."""

from __future__ import annotations

import re


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def markdown_to_telegram_html(text: str) -> str:
    """Minimal markdown → Telegram HTML: headers/bold/code/bullets. Rest escaped."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            out.append("")
            continue
        header = re.match(r"^#{1,6}\s+(.*)$", line)
        if header:
            out.append(f"<b>{escape_html(header.group(1))}</b>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet:
            line = "• " + bullet.group(1)
        elif line.startswith("#"):
            line = line.lstrip("#").strip()
        line = escape_html(line)
        line = _BOLD.sub(r"<b>\1</b>", line)
        line = _CODE.sub(r"<code>\1</code>", line)
        out.append(line)
    return "\n".join(out)
