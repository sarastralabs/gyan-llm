from __future__ import annotations

import argparse

from gyan_gateway import cli
from gyan_gateway.store import Store, hash_key


async def test_cli_create_list_revoke(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "cli.db")
    monkeypatch.setenv("DATABASE_PATH", db)

    ns = argparse.Namespace(command="create-key", name="zapier", rpm=5, daily=50)
    assert await cli._run(ns) == 0
    out = capsys.readouterr().out
    plain = out.strip().splitlines()[-1]
    assert plain.startswith("sk-gyan-")

    store = Store(db)
    key = store._find_by_key(plain)
    assert key and key.name == "zapier" and key.rpm == 5 and key.daily == 50
    assert key.active and hash_key(plain) != plain
    store.close()

    assert await cli._run(argparse.Namespace(command="list-keys")) == 0
    assert "zapier" in capsys.readouterr().out

    assert await cli._run(argparse.Namespace(command="revoke", id=key.id)) == 0
    store = Store(db)
    assert store._find_by_key(plain).active is False
    store.close()

    assert await cli._run(argparse.Namespace(command="revoke", id=999)) == 1
