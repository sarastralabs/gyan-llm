"""Command-line key management, for use on the server:

    docker compose exec gateway gyan-gateway create-key --name n8n --rpm 30 --daily 1000
    docker compose exec gateway gyan-gateway list-keys
    docker compose exec gateway gyan-gateway revoke 3
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .store import Store


def _store() -> Store:
    return Store(os.getenv("DATABASE_PATH", "data/gateway.db"))


async def _run(args: argparse.Namespace) -> int:
    store = _store()
    try:
        if args.command == "create-key":
            rpm = args.rpm or int(os.getenv("DEFAULT_RPM", "20"))
            daily = args.daily or int(os.getenv("DEFAULT_DAILY", "500"))
            key, plain = await store.create_key(args.name, rpm, daily)
            print(f"Created key #{key.id} '{key.name}' ({rpm}/min, {daily}/day)")
            print("Copy it now. It is shown only once:")
            print(plain)
        elif args.command == "list-keys":
            for k in await store.list_keys():
                state = "active" if k.active else "revoked"
                print(f"#{k.id:<4} {k.name:<24} {k.prefix}...  {k.rpm}/min {k.daily}/day  {state}")
        elif args.command in {"revoke", "restore"}:
            ok = await store.set_active(args.id, args.command == "restore")
            if not ok:
                print(f"No key with id {args.id}", file=sys.stderr)
                return 1
            print(f"Key #{args.id} {'restored' if args.command == 'restore' else 'revoked'}")
    finally:
        store.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="gyan-gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-key", help="create an API key (shown once)")
    create.add_argument("--name", required=True)
    create.add_argument("--rpm", type=int)
    create.add_argument("--daily", type=int)

    sub.add_parser("list-keys", help="list keys (never shows the secret)")
    for name in ("revoke", "restore"):
        p = sub.add_parser(name, help=f"{name} a key by id")
        p.add_argument("id", type=int)

    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
