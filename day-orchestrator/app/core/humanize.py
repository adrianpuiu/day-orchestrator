"""Owner-facing date/time formatting: DD/MM/YYYY - HH:MM (local time).

Internal state stays ISO; only display surfaces (brief lines, approval cards,
chat) use this.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

FORMAT = "%d/%m/%Y - %H:%M"


def fmt_dt(value: Any) -> str:
    """ISO-ish datetime -> 'DD/MM/YYYY - HH:MM'; unparseable -> raw string.

    Exactly-midnight values (date-only dues) render as 'DD/MM/YYYY'.
    """
    try:
        dt = datetime.fromisoformat(str(value)).astimezone()
    except (TypeError, ValueError):
        return str(value)
    if dt.hour == 0 and dt.minute == 0:
        return dt.strftime("%d/%m/%Y")
    return dt.strftime(FORMAT)


def fmt_span(start_iso: Any, end_iso: Any) -> str:
    """'DD/MM/YYYY - HH:MM → HH:MM' (full date again only when days differ)."""
    start = datetime.fromisoformat(str(start_iso)).astimezone()
    end = datetime.fromisoformat(str(end_iso)).astimezone()
    start_txt = start.strftime(FORMAT)
    end_txt = (
        end.strftime("%H:%M")
        if end.date() == start.date()
        else end.strftime(FORMAT)
    )
    return f"{start_txt} → {end_txt}"
