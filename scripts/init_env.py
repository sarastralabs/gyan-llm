"""Create .env from .env.example, filling every secret with a fresh random value.

    python scripts/init_env.py                          # local use (http://localhost:3080)
    python scripts/init_env.py --host gyan.example.com  # a server behind nginx + HTTPS

Safe to re-run: values already in .env are kept and only blank secrets are filled in. With
--host, DOMAIN_CLIENT and TRUST_PROXY are (re)set for that public address.
"""
from __future__ import annotations

import argparse
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name -> how to generate it
GENERATED = {
    "ADMIN_TOKEN": lambda: secrets.token_urlsafe(32),
    "SEARXNG_SECRET": lambda: secrets.token_hex(32),
    "JWT_SECRET": lambda: secrets.token_hex(32),
    "JWT_REFRESH_SECRET": lambda: secrets.token_hex(32),
    "CREDS_KEY": lambda: secrets.token_hex(32),   # 64 hex characters
    "CREDS_IV": lambda: secrets.token_hex(16),    # 32 hex characters
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", help="public hostname, e.g. gyan.example.com")
    parser.add_argument("--http", action="store_true", help="with --host: use http:// (no TLS yet)")
    args = parser.parse_args()

    overrides: dict[str, str] = {}
    if args.host:
        scheme = "http" if args.http else "https"
        overrides = {"DOMAIN_CLIENT": f"{scheme}://{args.host}", "TRUST_PROXY": "true"}

    example = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    target = ROOT / ".env"
    existing: dict[str, str] = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, _, value = line.partition("=")
                existing[name.strip()] = value

    lines, filled = [], []
    for line in example:
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            name = name.strip()
            value = existing.get(name, value)
            value = overrides.get(name, value)
            if not value and name in GENERATED:
                value = GENERATED[name]()
                filled.append(name)
            line = f"{name}={value}"
        lines.append(line)

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)  # secrets: owner-only on Linux and macOS
    except OSError:
        pass
    print(f"Wrote {target}")
    print("Generated:", ", ".join(filled) if filled else "nothing new")
    if overrides:
        print("Set:", ", ".join(f"{k}={v}" for k, v in overrides.items()))
    if not existing.get("GYAN_API_KEY"):
        print("Still to do: create the UI's gateway key (scripts/vm-bootstrap.sh does it).")


if __name__ == "__main__":
    main()
