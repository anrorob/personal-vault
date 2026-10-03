"""Synthetic recoverable Delete contract tests; no persistent Vault state."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg.types.json import Jsonb

from app.auth import SESSION_COOKIE_NAME, get_authentication_store, get_passkey_store
from app.passkeys import PasskeyCredential
from app.vault_master import asset_is_visible_to
from app.vault_master_api import get_catalogue_preview_roots
from app.federation import FederationStore
from app.gallery import scan_gallery
import app.vault_master_api as vault_master_api
from tests.test_gallery import authenticate, catalogue_image, configure_gallery, create_image, synthetic_recipient
from tests.test_vault_master_api import authenticate_regular_user
from tests.test_postgres_vault_master import (
    postgres_conninfo, postgres_store, _catalogued_asset, _arrival_hall_owner_user_id,
)


def test_recoverable_delete_requires_passkey_and_restores_same_asset(
    client, tmp_path: Path, monkeypatch,
) -> None:
    store = configure_gallery(tmp_path)
    source = create_image(tmp_path, "Synthetic-Nokia-receipt.jpg", b"synthetic bytes")
    asset = catalogue_image(store, tmp_path, source)
    image_id = scan_gallery(tmp_path)[0].id
    asset = replace(asset, metadata={"original_filename": "IMG_123.jpg",
                                     "description": "Nokia phone receipt",
                                     "ocr_text": "Example receipt for Nokia phone"},
                    visibility="shared", shared_with=("recipient",),
                    shared_with_user_ids=(synthetic_recipient().user_id,))
    store.catalogued_assets[asset.vault_path] = asset
    client.app.dependency_overrides[get_catalogue_preview_roots] = lambda: {
        "/vault/Gallery": tmp_path,
    }
    authenticate(client)
    endpoint = f"/api/vault-master/assets/{asset.id}/lifecycle/delete"
    assert client.post(endpoint).status_code == 422
    assert client.post(f"{endpoint}/options").status_code == 400
    assert store.get_catalogued_asset_by_id(asset.id).lifecycle_state == "active"

    keys = client.app.dependency_overrides[get_passkey_store]()
    keys.create_credential(PasskeyCredential(
        uuid4(), asset.owner_user_id, b"synthetic-credential", b"public-key", 0,
        (), "platform", "Synthetic", datetime.now(timezone.utc), None,
    ))
    challenge = client.post(f"{endpoint}/options").json()
    body = {"challenge_id": challenge["challenge_id"],
            "credential": {"id": "c3ludGhldGljLWNyZWRlbnRpYWw",
                           "rawId": "c3ludGhldGljLWNyZWRlbnRpYWw", "response": {}}}
    assert client.post(endpoint, json=body).status_code == 401
    assert store.get_catalogued_asset_by_id(asset.id).lifecycle_state == "active"
    challenge = client.post(f"{endpoint}/options").json()
    body["challenge_id"] = challenge["challenge_id"]
    monkeypatch.setattr(vault_master_api, "verify_authentication_response",
                        lambda **kwargs: SimpleNamespace(new_sign_count=1))
    assert client.post(endpoint, json=body).status_code == 200
    assert client.post(endpoint, json=body).status_code == 404

    deleted = store.get_catalogued_asset_by_id(asset.id)
    assert deleted.id == asset.id
    assert deleted.vault_path == asset.vault_path
    assert deleted.metadata == asset.metadata
    assert deleted.owner_user_id == asset.owner_user_id
    assert deleted.lifecycle_state == "deleted"
    assert deleted.visibility == "private" and deleted.shared_with == ()
    assert source.read_bytes() == b"synthetic bytes"
    assert not asset_is_visible_to(deleted, synthetic_recipient())
    assert client.get(f"/api/gallery/{image_id}").status_code == 404
    authentication_store = client.app.dependency_overrides[get_authentication_store]()
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token and authentication_store.authorize_hidden_photos_session(token, asset.owner_user_id)
    assert all(entry["asset_id"] != str(asset.id)
               for entry in client.get("/api/gallery").json())
    assert all(entry["asset_id"] != str(asset.id)
               for entry in client.get("/api/gallery?include_hidden=true").json())
    assert client.get("/api/vault-master/assets/search", params={"query": "Nokia"}).json()["assets"] == []
    for query in ("Nokia", "IMG_123", "receipt Nokia phone", "Gallery"):
        found = client.get("/api/vault-master/assets/recovery/search", params={"query": query})
        assert found.status_code == 200
        assert [entry["id"] for entry in found.json()["assets"]] == [str(asset.id)]
        assert found.json()["assets"][0]["current_location"] == "Deleted"
        assert found.json()["assets"][0]["original_section"] == "Gallery"

    authenticate_regular_user(client, authentication_store)
    assert client.get("/api/vault-master/assets/recovery/search",
                      params={"query": "Nokia"}).json()["assets"] == []
    assert client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/restore-deleted").status_code == 404
    assert client.get(f"/api/gallery/{image_id}").status_code == 404
    authenticate(client)

    restored = client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/restore-deleted")
    assert restored.status_code == 200
    current = store.get_catalogued_asset_by_id(asset.id)
    assert current.id == asset.id and current.lifecycle_state == "active"
    assert current.metadata == asset.metadata and current.vault_path == asset.vault_path
    assert current.visibility == "private" and current.shared_with == ()
    assert source.read_bytes() == b"synthetic bytes"
    assert [entry["action"] for entry in store.list_catalogued_asset_history(asset.id)][:2] == [
        "asset_restored", "asset_deleted",
    ]


def test_hidden_is_distinct_and_recovery_search_is_owner_scoped(client, tmp_path: Path) -> None:
    store = configure_gallery(tmp_path)
    source = create_image(tmp_path, "Synthetic-hidden.jpg")
    asset = catalogue_image(store, tmp_path, source)
    image_id = scan_gallery(tmp_path)[0].id
    authenticate(client)
    hidden = store.set_catalogued_asset_lifecycle_state(
        asset.id, asset.owner_user_id, asset.owner_username, "hidden",
    )
    assert hidden.lifecycle_state == "hidden"
    auth_store = client.app.dependency_overrides[get_authentication_store]()
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token and auth_store.authorize_hidden_photos_session(token, asset.owner_user_id)
    assert [entry["asset_id"] for entry in client.get("/api/gallery?include_hidden=true").json()] == [str(asset.id)]
    assert client.get(f"/api/gallery/{image_id}").status_code == 200
    result = client.get("/api/vault-master/assets/recovery/search", params={"query": "Synthetic"})
    assert result.json()["assets"][0]["current_location"] == "Hidden"
    recipient = synthetic_recipient()
    assert not asset_is_visible_to(store.set_catalogued_asset_deleted(
        asset.id, asset.owner_user_id, asset.owner_username,
    ), recipient)
    assert store.get_visible_catalogued_asset_by_id(asset.id, recipient) is None
    assert store.search_recoverable_catalogued_assets("Synthetic", recipient.user_id) == []


def test_postgres_delete_revokes_direct_and_collection_access_without_losing_identity(
    postgres_store, postgres_conninfo, monkeypatch,
) -> None:
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://synthetic-origin.test")
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(uuid4(), "/vault/Gallery/Synthetic-Nokia.jpg", "owner"), "owner",
    )
    owner_id = _arrival_hall_owner_user_id(postgres_conninfo)
    other_id = _arrival_hall_owner_user_id(postgres_conninfo, "son")
    postgres_store.update_catalogued_asset_access(asset.id, "shared", ("son",), "owner")
    with psycopg.connect(postgres_conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT origin_vault_id FROM vault_assets WHERE id=%s", (asset.id,))
        origin_vault_id = cursor.fetchone()[0]
        collection_id = uuid4()
        cursor.execute("""INSERT INTO vault_shared_collections
                       (collection_id,owner_user_id,origin_vault_id,name)
                       VALUES (%s,%s,%s,'Synthetic Collection')""",
                       (collection_id, owner_id, origin_vault_id))
        cursor.execute("""INSERT INTO vault_shared_collection_members(collection_id,asset_id)
                       VALUES (%s,%s)""", (collection_id, asset.id))
        cursor.execute("""INSERT INTO vault_collection_share_grants
                       (grant_id,collection_id,grantor_user_id,origin_vault_id,
                        target_type,state,activated_at)
                       VALUES (%s,%s,%s,%s,'local_all','active',CURRENT_TIMESTAMP)""",
                       (uuid4(), collection_id, owner_id, origin_vault_id))
        cursor.execute("""UPDATE vault_assets SET metadata=%s WHERE id=%s""",
                       (Jsonb({"original_filename": "IMG_123.jpg",
                               "description": "Nokia phone receipt"}), asset.id))
        term_id = uuid4()
        cursor.execute("""INSERT INTO vault_metadata_terms
                       (id,namespace,slug,display_name)
                       VALUES (%s,'content_tag','synthetic-blue-tag','Synthetic Blue Tag')""",
                       (term_id,))
        cursor.execute("""INSERT INTO vault_asset_metadata_assignments
                       (id,asset_id,term_id,source)
                       VALUES (%s,%s,%s,'user')""", (uuid4(), asset.id, term_id))
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is not None
    federation = FederationStore(postgres_conninfo)
    peer = federation.pair_vault(uuid4(), "Synthetic Peer", "https://synthetic-peer.test", "s" * 32)
    direct_share_id = federation.create_outgoing_shares(
        owner_id, [asset.id], peer.remote_vault_id, "quick",
    )[0]
    collection_share_id = federation.create_outgoing_collection_share(
        owner_id, collection_id, peer.remote_vault_id, "quick",
    )
    deleted = postgres_store.set_catalogued_asset_deleted(asset.id, owner_id, "owner")
    assert deleted is not None and deleted.lifecycle_state == "deleted"
    assert deleted.id == asset.id and deleted.vault_path == asset.vault_path
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "owner") is None
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is None
    assert postgres_store.search_visible_catalogued_assets("Nokia", "owner") == []
    for query in ("IMG_123", "receipt Nokia phone", "Synthetic Blue Tag"):
        assert [item.id for item in postgres_store.search_recoverable_catalogued_assets(
            query, owner_id,
        )] == [asset.id]
        assert postgres_store.search_recoverable_catalogued_assets(query, other_id) == []
    with psycopg.connect(postgres_conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM vault_share_grants WHERE asset_id=%s", (asset.id,))
        assert {row[0] for row in cursor.fetchall()} == {"revoked"}
        cursor.execute("SELECT 1 FROM vault_shared_collection_members WHERE asset_id=%s", (asset.id,))
        assert cursor.fetchone() is None
        cursor.execute("SELECT state FROM vault_federation_outgoing_shares WHERE federation_share_id=%s",
                       (direct_share_id,))
        assert cursor.fetchone() == ("revoked",)
        cursor.execute("""SELECT event_type FROM vault_federation_deliveries
                       WHERE federation_share_id=%s ORDER BY created_at DESC LIMIT 1""",
                       (direct_share_id,))
        assert cursor.fetchone() == ("share_revoked",)
        cursor.execute("""SELECT payload FROM vault_federation_collection_deliveries
                       WHERE federation_collection_share_id=%s ORDER BY created_at DESC LIMIT 1""",
                       (collection_share_id,))
        assert cursor.fetchone()[0]["members"] == []
        cursor.execute("""SELECT action,actor_user_id FROM vault_asset_history
                       WHERE asset_id=%s AND action='asset_deleted'""", (asset.id,))
        assert cursor.fetchone() == ("asset_deleted", owner_id)
    restored = postgres_store.restore_catalogued_asset_deleted(asset.id, owner_id, "owner")
    assert restored is not None and restored.id == asset.id
    assert restored.lifecycle_state == "active" and restored.visibility == "private"
    assert restored.metadata["description"] == "Nokia phone receipt"
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is None
    with psycopg.connect(postgres_conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM vault_share_grants WHERE asset_id=%s", (asset.id,))
        assert {row[0] for row in cursor.fetchall()} == {"revoked"}
        cursor.execute("SELECT 1 FROM vault_shared_collection_members WHERE asset_id=%s", (asset.id,))
        assert cursor.fetchone() is None
        cursor.execute("SELECT state FROM vault_federation_outgoing_shares WHERE federation_share_id=%s",
                       (direct_share_id,))
        assert cursor.fetchone() == ("revoked",)
