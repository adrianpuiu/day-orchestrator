"""Fence-tolerant JSON extraction (local engines wrap JSON in prose/fences)."""

from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict | None:
    """First JSON object in `text`, tolerant of markdown fences and prose."""
    if not text:
        return None
    stripped = re.sub(r"```(?:json)?", "", text)
    start = stripped.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(stripped)):
        ch = stripped[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(stripped[start:k + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None
