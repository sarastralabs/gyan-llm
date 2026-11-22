from __future__ import annotations

import glob
import os

from conftest import ADMIN, bearer, new_key


def test_healthz_needs_no_key(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_models_require_a_key(client):
    resp = client.get("/v1/models")
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "invalid_api_key" and err["type"] == "authentication_error"


def test_wrong_key_rejected(client):
    resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-gyan-nope"})
    assert resp.status_code == 401


def test_admin_requires_its_own_token(client):
    assert client.get("/admin/keys").status_code == 401
    assert client.get("/admin/keys", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/admin/keys", headers=ADMIN).status_code == 200


def test_api_key_is_not_an_admin_token(client):
    key = new_key(client)
    assert client.get("/admin/keys", headers=bearer(key)).status_code == 401


def test_created_key_works(client):
    key = new_key(client, "n8n")
    assert key["key"].startswith("sk-gyan-") and key["name"] == "n8n"
    resp = client.get("/v1/models", headers=bearer(key))
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert "qwen2.5:3b" in ids and resp.json()["object"] == "list"


def test_revoke_and_restore(client):
    key = new_key(client)
    assert client.post(f"/admin/keys/{key['id']}/revoke", headers=ADMIN).json()["active"] is False
    assert client.get("/v1/models", headers=bearer(key)).status_code == 401
    client.post(f"/admin/keys/{key['id']}/restore", headers=ADMIN)
    assert client.get("/v1/models", headers=bearer(key)).status_code == 200


def test_revoking_unknown_key_is_404(client):
    assert client.post("/admin/keys/999/revoke", headers=ADMIN).status_code == 404


def test_plain_key_is_never_stored(client, settings):
    key = new_key(client)
    client.get("/v1/models", headers=bearer(key))
    for path in glob.glob(settings.database_path + "*"):
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                assert key["key"].encode() not in fh.read(), path


def test_listing_keys_never_shows_secrets(client):
    key = new_key(client)
    listing = client.get("/admin/keys", headers=ADMIN).json()["data"]
    assert listing and all("key" not in k for k in listing)
    assert listing[0]["prefix"] == key["key"][:12]


def test_repeated_bad_keys_are_blocked(client):
    statuses = [
        client.get("/v1/models", headers={"Authorization": "Bearer bad"}).status_code
        for _ in range(25)
    ]
    assert statuses[:20] == [401] * 20
    assert set(statuses[20:]) == {429}
