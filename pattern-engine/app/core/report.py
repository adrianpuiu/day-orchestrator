"""Daily markdown report — the autonomous system's audit trail."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .journal import JournalStore
from .registry import Registry


def write_report(report_dir: Path, registry: Registry,
                 journal: JournalStore, decay: dict | None = None,
                 open_position: str = "flat",
                 today: date | None = None) -> Path:
    today = today or date.today()
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{today.isoformat()}.md"
    js = journal.summary()
    lines = [
        f"# pattern-engine report — {today.isoformat()}",
        "",
        "## Book",
        f"- journal: {js['trades']} closed trades, total {js['total_r']}R",
        f"- open position: {open_position}",
        "",
        "## Live strategies",
        "| id | status | n | pf | avg_r |",
        "|---|---|---|---|---|",
    ]
    for row in registry.as_dicts():
        s = row.get("stats", {})
        lines.append(f"| {row['id']} | {row['status']} | {s.get('n', '')} "
                     f"| {s.get('pf', '')} | {s.get('avg_r', '')} |")
    lines += ["", "## Recent trades",
              "| strategy | entry | exit | r | kind |",
              "|---|---|---|---|---|"]
    for t in journal.recent(limit=10):
        lines.append(f"| {t['strategy_id']} | {t['entry_ts'][:10]} "
                     f"| {t['exit_ts'][:10]} | {t['r']:+.2f} "
                     f"| {t['exit_kind']} |")
    if decay:
        lines += ["", "## Decay watch"]
        for spec_id, w in decay.get("windows", {}).items():
            lines.append(f"- {spec_id}: n={w.get('n')} "
                         f"avg_r={w.get('avg_r')} — {w.get('verdict')}")
        if decay.get("retired"):
            lines.append(f"- RETIRED this run: {', '.join(decay['retired'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
