"""Disposable PostgreSQL proof for private-tag store and explicit legacy migration."""
import os
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.gallery_custom_tags import PostgresGalleryCustomTagStore
from app.gallery_intelligence import PostgresGalleryIntelligenceStore
from app.gallery_tag_migration import reconcile_legacy_tags


@pytest.fixture
def tag_database():
    base = os.getenv("PV_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Disposable PostgreSQL is not configured")
    schema = "gallery_tags_test_" + uuid4().hex
    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(base, options=f"-c search_path={schema}")
    owner, second, asset, another = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        with psycopg.connect(scoped) as connection:
            connection.execute("CREATE TABLE auth_accounts(user_id UUID PRIMARY KEY, username TEXT)")
            connection.execute("INSERT INTO auth_accounts VALUES(%s,'same-name'),(%s,'same-name')", (owner, second))
            connection.execute("CREATE TABLE vault_assets(id UUID PRIMARY KEY,owner_user_id UUID)")
            connection.execute("INSERT INTO vault_assets VALUES(%s,%s),(%s,%s)", (asset, owner, another, second))
        intelligence = PostgresGalleryIntelligenceStore(scoped)
        intelligence.initialize()
        tags = PostgresGalleryCustomTagStore(scoped)
        tags.initialize()
        yield scoped, intelligence, tags, owner, second, asset, another
    finally:
        with psycopg.connect(base) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_migration_preserves_names_assignments_and_is_restart_safe(tag_database):
    conninfo, intelligence, tags, owner, second, asset, another = tag_database
    intelligence.create_custom_tag(asset, owner, "Bikes")
    intelligence.create_custom_tag(another, second, "Bikes")
    dry = reconcile_legacy_tags(conninfo)
    assert dry["blocked_records"] == 0 and dry["migrated_owners"] == 0
    assert tags.list(owner) == []
    with psycopg.connect(conninfo) as connection:
        assert connection.execute("SELECT to_regclass('user_gallery_legacy_tag_migrations')").fetchone()[0] is None
    assert reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)["migrated_owners"] == 1
    assert tags.list(second) == []
    first = tags.list(owner)[0]
    assert first.display_name == "Bikes" and tags.matching_asset_ids(owner, (first.id,)) == {asset}
    assert reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)["migrated_owners"] == 0
    assert reconcile_legacy_tags(conninfo, apply=True, owner_user_id=second)["migrated_owners"] == 1
    other = tags.list(second)[0]
    assert first.id != other.id and tags.matching_asset_ids(second, (other.id,)) == {another}
    assert tags.matching_asset_ids(owner, (other.id,)) == set()
    with psycopg.connect(conninfo, row_factory=dict_row) as connection:
        assert connection.execute("SELECT count(*) AS n FROM vault_asset_metadata_decisions").fetchone()["n"] == 2
        row = connection.execute("SELECT evidence FROM user_gallery_legacy_tag_migrations WHERE owner_user_id=%s", (owner,)).fetchone()
        assert row["evidence"]["decisions"][0]["decided_by"] == str(owner)
        assert row["evidence"]["added_asset_ids"] == [str(asset)]
    # Later user edits/deletion are not overwritten or resurrected by reruns.
    tags.rename(owner, first.id, "Cycling")
    tags.delete(owner, first.id)
    assert reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)["migrated_owners"] == 0
    assert tags.list(owner) == [] and tags.list(second)[0].display_name == "Bikes"


def test_migration_ambiguous_or_changed_evidence_fails_closed(tag_database):
    conninfo, intelligence, tags, owner, second, asset, another = tag_database
    intelligence.create_custom_tag(asset, owner, "Proven")
    intelligence.create_custom_tag(another, second, "Ambiguous")
    with psycopg.connect(conninfo) as connection:
        connection.execute("UPDATE vault_asset_metadata_decisions SET decided_by='same-name' WHERE asset_id=%s", (another,))
        connection.execute("INSERT INTO vault_metadata_terms(id,namespace,slug,display_name) VALUES(%s,'content_tag','orphan','Orphan')", (uuid4(),))
    report = reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)
    assert report["blocked_records"] == 2 and report["migrated_owners"] == 1
    assert [tag.display_name for tag in tags.list(owner)] == ["Proven"]
    assert tags.list(second) == []
    with psycopg.connect(conninfo) as connection:
        assert connection.execute("SELECT decided_by FROM vault_asset_metadata_decisions WHERE asset_id=%s", (another,)).fetchone()[0] == "same-name"
        connection.execute("UPDATE vault_asset_metadata_decisions SET decision='exclude' WHERE asset_id=%s", (asset,))
    report = reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)
    assert report["blocked_records"] == 3 and report["migrated_owners"] == 0
    assert len(tags.for_asset(owner, asset)) == 1  # Review, never destroy a migrated relationship.


def test_migration_collision_and_missing_owner_are_safe(tag_database):
    conninfo, intelligence, tags, owner, second, asset, another = tag_database
    intelligence.create_custom_tag(asset, owner, "Bikes")
    existing = tags.create(owner, "BIKES")
    with pytest.raises(ValueError):
        reconcile_legacy_tags(conninfo, apply=True)
    result = reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)
    assert result["blocked_records"] == 1 and result["migrated_owners"] == 0
    assert tags.list(owner) == [existing] and tags.for_asset(owner, asset) == []


def test_private_store_database_identity_unused_and_shared_assignments(tag_database):
    _, _, tags, owner, second, asset, _ = tag_database
    first = tags.create(owner, "Bikes")
    other = tags.create(second, "Bikes")
    unused = tags.create(owner, "Unused")
    tags.assign(owner, first.id, asset)
    tags.assign(second, other.id, asset)
    assert {tag.id for tag in tags.list(owner)} == {first.id, unused.id}
    assert tags.matching_asset_ids(owner, (unused.id,)) == set()
    assert tags.matching_asset_ids(owner, (first.id,)) == {asset}
    assert tags.matching_asset_ids(owner, (first.id, other.id)) == set()
    tags.rename(owner, first.id, "Cycling")
    tags.delete(owner, first.id)
    assert tags.for_asset(second, asset) == [other]


def test_postgres_private_tag_filter_through_gallery_routes(tag_database, client, tmp_path, authentication_store):
    from dataclasses import replace
    from app.auth import AuthenticatedIdentity, require_authenticated_user
    from app.gallery_custom_tags import get_gallery_custom_tag_store
    from app.main import app
    from tests.conftest import TEST_USERNAME
    from tests.test_gallery import authenticate, catalogue_image, configure_gallery, create_image
    _, _, tags, owner, second, asset_id, _ = tag_database
    vault = configure_gallery(tmp_path)
    asset = catalogue_image(vault, tmp_path, create_image(tmp_path, "synthetic.jpg"))
    vault.catalogued_assets[asset.vault_path] = replace(asset, id=asset_id, owner_user_id=owner)
    authenticate(client)
    account = replace(authentication_store.get_account(TEST_USERNAME), user_id=owner)
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(account)
    app.dependency_overrides[get_gallery_custom_tag_store] = lambda: tags
    card = client.get("/api/gallery").json()[0]
    created = client.post("/api/gallery/custom-tags", json={"display_name": "Bikes"}).json()
    assert client.get("/api/gallery", params={"private_tag": created["id"]}).json() == []
    assert client.put(f"/api/gallery/{card['id']}/custom-tags/{created['id']}").status_code == 204
    assert client.get("/api/gallery", params={"private_tag": created["id"]}).json()[0]["id"] == card["id"]
    assert client.get(f"/api/gallery/{card['id']}", params={"private_tag": created["id"]}).json()["custom_tags"] == [created]
    assert client.get("/api/gallery/custom-tags").headers["cache-control"] == "private, no-store"
