"""Owner-side setup and ops CLI. Secrets never transit chat: run this locally."""

from __future__ import annotations

import argparse
import asyncio
import getpass
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

    p_auth = sub.add_parser("auth-calendar", help="run the one-time calendar consent")
    p_auth.add_argument("--client-secret", required=True,
                        help="path to Google OAuth client secret JSON")

    sub.add_parser("run", help="run the orchestrator (foreground; launchd wraps it)")
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
    elif args.cmd == "smoke":
        from .smoke import run_smoke

        asyncio.run(run_smoke())


if __name__ == "__main__":
    main()
