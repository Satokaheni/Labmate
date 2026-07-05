"""Admin CLI: create or reset a Labmate user directly in the local SQLite auth
store (auth_users). Registration is closed (no signup UI), so this is how you add
the 2nd/3rd user and rotate passwords. Headless — no running gateway required.

Usage:
  python -m services.ws_gateway.seed_user --email a@b.c --password secret [--role admin|user] [--display-name "Name"]
  python -m services.ws_gateway.seed_user --email a@b.c --password new --reset-password
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from argon2 import PasswordHasher

from services.orchestrator.local_store import get_local_store
from services.ws_gateway.user_store import SqliteUserStore


async def _run(args: argparse.Namespace) -> int:
    store = get_local_store()
    # Always close the store before returning: aiosqlite keeps a background
    # connection thread alive, so a headless one-shot CLI that never closes it
    # hangs on process exit waiting for that thread. (Unit tests don't observe
    # this because the pytest process keeps running.)
    try:
        us = SqliteUserStore(store)
        ph = PasswordHasher()
        pw_hash = ph.hash(args.password)
        existing = await us.find_by_email(args.email)
        if existing is not None:
            if not args.reset_password:
                print(
                    f"user {args.email!r} already exists — pass --reset-password to update the password",
                    file=sys.stderr,
                )
                return 1
            await store.auth_user_set_password(args.email, pw_hash)
            print(f"password reset for {args.email}")
            return 0
        doc = await us.create(
            email=args.email,
            display_name=args.display_name or "",
            password_hash=pw_hash,
            role=args.role,
        )
        print(f"created {args.role} user {doc['email']} (id {doc['id']})")
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m services.ws_gateway.seed_user",
        description="Create or reset a Labmate user in the local SQLite auth store.",
    )
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--role", choices=["admin", "user"], default="user")
    p.add_argument("--display-name", default="")
    p.add_argument(
        "--reset-password",
        action="store_true",
        help="if the email already exists, update its password instead of erroring",
    )
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
