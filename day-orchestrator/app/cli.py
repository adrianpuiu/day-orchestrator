"""Owner-side setup and ops CLI. Secrets never transit chat: run this locally."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .core.config import PROJECT_ROOT, Config
from .core.secrets import default_secrets

KNOWN_SECRETS = {
    "TELEGRAM_BOT_TOKEN": "BotFather token (pair via /start after first run)",
    "GMAIL_ADDRESS": "the Gmail address to manage",
    "GMAIL_APP_PASSWORD": "Google app password — needs 2FA (docs/adr/ADR-002)",
}  # stored in the project .env (git-ignored), Keychain as fallback

AGENT_LABEL = "com.agp.day-orchestrator"


def _render_agent_plist(project_root: Path, data_dir: Path) -> dict:
    """launchd agent definition with this checkout's real paths (ADR-005/009:
    the agent runs as the bundle's main process so TCC attributes EventKit
    and geocoding consent to com.agp.day-orchestrator)."""
    bundle = (project_root / "DayOrchestrator.app" / "Contents" / "MacOS"
              / "DayOrchestrator")
    log_dir = data_dir / "logs"
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(bundle), "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 300,
        "StandardOutPath": str(log_dir / "stdout.log"),
        "StandardErrorPath": str(log_dir / "stderr.log"),
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
    }


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, **kw)


def cmd_install() -> None:
    if sys.platform != "darwin":
        raise SystemExit("installer supports macOS only (EventKit + launchd).")
    print("== Day Orchestrator installer ==")

    if shutil.which("uv") is None:
        raise SystemExit(
            "uv is required. Install it with:\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            "then re-run this installer."
        )

    print("-- 1/5 dependencies (uv sync)")
    if _run(["uv", "sync"]).returncode != 0:
        raise SystemExit("uv sync failed — fix the error above and re-run.")

    print("-- 2/5 config")
    example = PROJECT_ROOT / "config.toml.example"
    live = PROJECT_ROOT / "config.toml"
    if live.exists():
        print("   config.toml already exists — keeping it.")
    elif example.exists():
        live.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print("   created config.toml from config.toml.example "
              "(empty model_api_base = Gemini API).")
    else:
        print("   no config.toml.example found — using built-in defaults.")

    print("-- 3/5 secrets (input hidden; stored in .env)")
    secrets = default_secrets(PROJECT_ROOT)
    for name, help_text in KNOWN_SECRETS.items():
        if secrets.get(name):
            print(f"   {name} already set — skipping.")
            continue
        value = getpass.getpass(f"   {name} ({help_text}): ")
        if value.strip():
            secrets.set(name, value)
        else:
            print(f"   skipped {name} — you can set it later with "
                  f"`uv run python -m app.cli set-secret {name}`")

    print("-- 4/5 calendar consent (a macOS dialog may pop up — click OK)")
    if sys.platform == "darwin":
        try:
            from .channels.calendar_apple import AppleCalendarClient

            AppleCalendarClient().get_events(1)
            print("   calendar access OK.")
        except Exception as exc:
            print(f"   calendar consent not completed ({exc}); "
                  "re-run `uv run python -m app.cli auth-calendar` later.")

    print("-- 5/5 always-on service (launchd)")
    cfg = Config.load()
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = (Path.home() / "Library" / "LaunchAgents"
                  / f"{AGENT_LABEL}.plist")
    plist_path.write_bytes(
        plistlib.dumps(_render_agent_plist(PROJECT_ROOT, cfg.data_dir),
                       fmt=plistlib.FMT_XML)
    )
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"])
    result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if result.returncode != 0:
        print(f"   bootstrap failed ({result.returncode}); start it with "
              f"`launchctl kickstart -k gui/{uid}/{AGENT_LABEL}` after fixing.")
    else:
        print(f"   agent installed and started ({plist_path}).")

    print(
        "\n== Done. Last step (interactive): ==\n"
        "1. Run `uv run python -m app.cli pair` for the one-time code,\n"
        "   then send `/start <code>` to your bot on Telegram.\n"
        "2. Tomorrow " + str(cfg.brief_time) + " you get the first brief.\n"
        "Useful: `uv run python -m app.cli secrets` (list), "
        "`... auth-calendar` (re-consent), `... smoke` (offline proof)."
    )


def cmd_pair() -> None:
    """Print the one-time pairing code (rotated by reset-pairing)."""

    from .core import memory
    from .core.config import Config as _Cfg
    from .core.db import Database

    cfg = _Cfg.load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.init_schema()
    override = memory.get_value(db, "pairing_code")
    db.close()
    token = default_secrets(PROJECT_ROOT).get("TELEGRAM_BOT_TOKEN")
    code = override or (token[-8:] if token else None)
    if not code:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set (app.cli set-secret first).")
    print("On Telegram, send this to your bot:\n")
    print(f"    /start {code}")


def cmd_reset_pairing() -> None:
    """Forget the paired chat and rotate the one-time pairing code."""
    import secrets as _secrets
    import string

    from .core import memory
    from .core.config import Config as _Cfg
    from .core.db import Database

    cfg = _Cfg.load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.init_schema()
    memory.set_value(db, "paired_chat", None)
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(_secrets.choice(alphabet) for _ in range(8))
    memory.set_value(db, "pairing_code", code)
    db.close()
    print("pairing reset and code rotated — new code:")
    print(f"\n    /start {code}\n")
    print("(the running daemon reads the rotated code on the next /start)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="day-orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_secret = sub.add_parser("set-secret", help="store a secret in the .env file")
    p_secret.add_argument("name", choices=sorted(KNOWN_SECRETS))

    sub.add_parser("secrets", help="list known secret names")

    p_auth = sub.add_parser(
        "auth-calendar",
        help="grant calendar access (apple: TCC prompt; google: OAuth consent)",
    )
    p_auth.add_argument(
        "--client-secret",
        default=None,
        help="Google OAuth client secret JSON (google backend only)",
    )

    sub.add_parser("run", help="run the orchestrator (foreground; launchd wraps it)")
    sub.add_parser(
        "install",
        help="guided installer: deps, config, secrets, calendar, launchd",
    )
    sub.add_parser("eval-nightly", help="run live agent evals; alert Telegram on failure")
    sub.add_parser("smoke", help="offline proof of the approval gate")
    sub.add_parser("pair", help="print the one-time Telegram pairing code")
    sub.add_parser("reset-pairing", help="forget the paired Telegram chat")

    args = parser.parse_args()

    if args.cmd == "secrets":
        for name, help_text in KNOWN_SECRETS.items():
            print(f"{name:20} {help_text}")
    elif args.cmd == "set-secret":
        value = getpass.getpass(f"value for {args.name}: ")
        if not value:
            raise SystemExit("empty value")
        default_secrets(PROJECT_ROOT).set(args.name, value)
        print(f"stored {args.name} in {PROJECT_ROOT / '.env'} (git-ignored).")
    elif args.cmd == "auth-calendar":
        cfg = Config.load()
        if cfg.calendar_backend == "apple":
            from .channels.calendar_apple import AppleCalendarClient

            print("A macOS consent prompt will appear — click OK to allow "
                  "calendar access (up to 2 minutes).")
            events = AppleCalendarClient().get_events(1)
            print(f"calendar OK: {len(events)} event(s) in the next 24h.")
        else:
            if not args.client_secret:
                raise SystemExit(
                    "google backend needs --client-secret (or switch "
                    "calendar_backend to 'apple' in config.toml)"
                )
            from .channels.calendar import CalendarClient

            client = CalendarClient(
                cfg.data_dir / "calendar_token.json", Path(args.client_secret)
            )
            events = client.get_events(1)
            print(f"calendar OK: {len(events)} event(s) in the next day.")
    elif args.cmd == "pair":
        cmd_pair()
    elif args.cmd == "reset-pairing":
        cmd_reset_pairing()
    elif args.cmd == "run":
        import logging
        import signal
        from logging.handlers import RotatingFileHandler

        rotating = RotatingFileHandler(
            Config.load().data_dir / "logs" / "agent.log",
            maxBytes=2_000_000,
            backupCount=5,
        )
        rotating.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.StreamHandler(), rotating],
        )
        from .runtime import Runtime

        async def _serve() -> None:
            rt = Runtime()
            loop = asyncio.get_running_loop()
            # launchd sends SIGTERM on unload/logout: drain cleanly instead
            # of dying mid-effect (watcher, telegram, scheduler, db close).
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, rt.request_stop)
            await rt.start()

        asyncio.run(_serve())
    elif args.cmd == "eval-nightly":
        import logging
        import os
        import subprocess
        import sys

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        log = logging.getLogger("eval-nightly")
        data_dir = Config.load().data_dir
        log_path = data_dir / "logs" / "evals.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        env = {**os.environ, "RUN_LIVE": "1"}
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"=== {datetime.now().isoformat(timespec='seconds')} nightly evals ===\n")
            log_file.flush()
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/eval", "-q"],
                stdout=log_file, stderr=subprocess.STDOUT, env=env, cwd=PROJECT_ROOT,
            )
        if proc.returncode == 0:
            log.info("nightly evals PASS")
            return
        log.error("nightly evals FAILED — alerting owner")
        tail = log_path.read_text(encoding="utf-8")[-1500:].replace("\n", " ")
        token = default_secrets(PROJECT_ROOT).get("TELEGRAM_BOT_TOKEN")
        row = None
        try:
            from .core.db import Database

            db = Database(Config.load().db_path)
            row = db.query_one(
                "SELECT value FROM memory WHERE key='paired_chat'"
            )
            db.close()
        except Exception:
            log.warning("could not read paired chat", exc_info=True)
        chat = row["value"] if row else None
        if token and chat:
            import html as _html

            import httpx

            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": str(chat),
                    "parse_mode": "HTML",
                    "text": "🌙 Nightly evals FAILED — model behavior may have "
                    f"drifted.<pre>{_html.escape(tail)}</pre>",
                },
                timeout=15,
            )
            log.info("failure alert sent to chat %s", chat)
    elif args.cmd == "install":
        cmd_install()
    elif args.cmd == "smoke":
        from .smoke import run_smoke

        asyncio.run(run_smoke())


if __name__ == "__main__":
    main()
