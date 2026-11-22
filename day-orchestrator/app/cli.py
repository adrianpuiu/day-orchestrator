"""Owner-side setup and ops CLI. Secrets never transit chat: run this locally."""

from __future__ import annotations

import argparse
import asyncio
import getpass
from datetime import datetime
from pathlib import Path

from .core.config import PROJECT_ROOT, Config
from .core.secrets import default_secrets

KNOWN_SECRETS = {
    "TELEGRAM_BOT_TOKEN": "BotFather token (pair via /start after first run)",
    "GMAIL_ADDRESS": "the Gmail address to manage",
    "GMAIL_APP_PASSWORD": "Google app password — needs 2FA (docs/adr/ADR-002)",
}  # stored in the project .env (git-ignored), Keychain as fallback


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
    sub.add_parser("eval-nightly", help="run live agent evals; alert Telegram on failure")
    sub.add_parser("smoke", help="offline proof of the approval gate")

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
    elif args.cmd == "run":
        import logging

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        from .runtime import Runtime

        asyncio.run(Runtime().start())
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
    elif args.cmd == "smoke":
        from .smoke import run_smoke

        asyncio.run(run_smoke())


if __name__ == "__main__":
    main()
