"""Command line: `uv run python -m leadgen.cli <command>`.

  serve [--port 8765]            run the app on 127.0.0.1
  set-password <username>        create the user or change its password (prompts, never echoes)
  backup                         copy the database to data/backups/ with a timestamp
  dev-user                       local testing only: user 'owner' with a random password in data/dev-credentials.txt
"""
import argparse
import getpass
import json
import sqlite3
import sys
from datetime import datetime

from leadgen.config import get_settings
from leadgen.db import Database
from leadgen.migrate import upgrade


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leadgen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    pw = sub.add_parser("set-password")
    pw.add_argument("username")
    sub.add_parser("backup")
    sub.add_parser("dev-user")
    ev = sub.add_parser("eval")
    ev.add_argument("--quick", action="store_true", help="3 cases, 1 draft each")
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.cmd == "serve":
        import uvicorn
        from leadgen.web.app import create_app
        # Bound to loopback only: exposing it on a network needs HTTPS and a deliberate decision.
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
        return 0

    if args.cmd == "eval":
        from leadgen import evals
        from leadgen.ai.provider import make_provider
        provider = make_provider(settings)
        ok, msg = provider.status()
        if not ok:
            print(f"AI unavailable: {msg}", file=sys.stderr)
            return 1
        report = evals.run(provider, quick=args.quick)
        print(json.dumps(report["summary"], indent=1))
        print(f"Report: {report['path']}")
        return 0

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade(settings.db_url)
    if args.cmd == "set-password":
        from leadgen.auth import set_password
        password = getpass.getpass("New password (min 10 chars): ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
        with Database(settings.db_url).sessionmaker() as s:
            try:
                set_password(s, args.username, password)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
        print(f"Password set for {args.username}.")
        return 0

    if args.cmd == "dev-user":
        import secrets
        from leadgen.auth import set_password
        password = secrets.token_urlsafe(12)
        with Database(settings.db_url).sessionmaker() as s:
            set_password(s, "owner", password)
        (settings.data_dir / "dev-credentials.txt").write_text(f"username=owner\npassword={password}\n", encoding="utf-8")
        print("Dev user 'owner' ready; password stored in data/dev-credentials.txt")
        return 0

    if args.cmd == "backup":
        src = settings.db_url.removeprefix("sqlite:///")
        dest_dir = settings.data_dir / "backups"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"leadgen-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
        with sqlite3.connect(src) as a, sqlite3.connect(dest) as b:
            a.backup(b)
        print(f"Backup written to {dest}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
