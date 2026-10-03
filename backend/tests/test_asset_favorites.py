import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app.main import app
from app.auth import require_authenticated_user
from app.asset_favorites import PostgresAssetFavorites
from tests.test_music_videos import video_fixture
from tests.test_music import authenticate


def test_video_favorites_are_private_idempotent_and_do_not_change_asset(client, tmp_path):
    store, asset = video_fixture(tmp_path)
    url = f"/api/music-videos/{asset.id}/favorite"
    assert client.put(url, json={"favorite": True}).status_code == 401
    authenticate(client)
    original = store.get_catalogued_asset_by_id(asset.id)
    assert client.put(url, json={"favorite": True}).json()["favorite"] is True
    assert client.put(url, json={"favorite": True}).status_code == 200
    assert client.get("/api/music-videos").json()[0]["favorite"] is True
    user_b = SimpleNamespace(user_id=uuid4())
    app.dependency_overrides[require_authenticated_user] = lambda: user_b
    assert client.get("/api/music-videos").json()[0]["favorite"] is False
    assert client.put(url, json={"favorite": True}).status_code == 200
    assert client.put(url, json={"favorite": False}).status_code == 200
    assert client.patch(f"/api/music-videos/{asset.id}/metadata", json={"title": "Denied"}).status_code == 404
    app.dependency_overrides.pop(require_authenticated_user)
    assert client.get("/api/music-videos").json()[0]["favorite"] is True
    assert client.put(url, json={"favorite": False}).status_code == 200
    assert client.get("/api/music-videos").json()[0]["favorite"] is False
    assert store.get_catalogued_asset_by_id(asset.id) == original
    assert client.put(url, json={"favorite": True, "user_id": str(uuid4())}).status_code == 422
    store.catalogued_assets[asset.vault_path] = replace(asset, lifecycle_state="hidden")
    assert client.put(url, json={"favorite": True}).status_code == 404


def test_favorites_postgres_persistence_and_foreign_keys():
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("Disposable PostgreSQL is supplied by required CI")
    schema = "test_favorites_" + uuid4().hex
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(conninfo, options=f"-c search_path={schema}")
    try:
        a, b, asset = uuid4(), uuid4(), uuid4()
        with psycopg.connect(scoped) as connection:
            connection.execute("CREATE TABLE auth_accounts(user_id UUID PRIMARY KEY)")
            connection.execute("CREATE TABLE vault_assets(id UUID PRIMARY KEY)")
            connection.execute("INSERT INTO auth_accounts VALUES (%s),(%s)", (a,b))
            connection.execute("INSERT INTO vault_assets VALUES (%s)", (asset,))
        store = PostgresAssetFavorites(scoped)
        store.initialize(); store.initialize()
        store.set(a, asset, True); store.set(a, asset, True)
        restored = PostgresAssetFavorites(scoped)
        assert restored.list(a) == {asset}
        assert restored.list(b) == set()
        restored.set(b, asset, False)
        assert restored.list(a) == {asset}
        restored.set(a, asset, False)
        assert restored.list(a) == set()
        restored.set(a, asset, True)
        with psycopg.connect(scoped) as connection:
            connection.execute("DELETE FROM vault_assets WHERE id=%s", (asset,))
        assert restored.list(a) == set()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            restored.set(a, uuid4(), True)
    finally:
        with psycopg.connect(conninfo, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
