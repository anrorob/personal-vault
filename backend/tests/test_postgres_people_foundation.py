from datetime import date
import os
from uuid import uuid4

import psycopg
import pytest

from app.auth_store import PostgresAuthenticationStore
from app.gallery_intelligence import PostgresGalleryIntelligenceStore
from app.gallery_people import PostgresGalleryPeopleStore
from app.vault_master import CataloguedAsset, PostgresVaultMasterStore


@pytest.fixture
def postgres_people_store(tmp_path):
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    authentication = PostgresAuthenticationStore(conninfo)
    authentication.initialize()
    for username in ("people-owner", "people-other"):
        authentication.ensure_initial_administrator(username, "test-hash")
    vault = PostgresVaultMasterStore(conninfo, sidecar_root=tmp_path / "metadata")
    vault.initialize()
    PostgresGalleryIntelligenceStore(conninfo).initialize()
    people = PostgresGalleryPeopleStore(conninfo)
    people.initialize()
    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("TRUNCATE vault_people CASCADE")
    yield people, authentication, conninfo, vault
    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("TRUNCATE vault_people CASCADE")


def test_postgres_people_foundation_backfills_legacy_uuid_and_persists_owner_scoped_behaviour(postgres_people_store) -> None:
    people, authentication, conninfo, _ = postgres_people_store
    owner = authentication.get_account("people-owner")
    other = authentication.get_account("people-other")
    assert owner is not None and other is not None

    # Simulate the deployed People table before P2, then run the real additive initializer.
    legacy_person_id = uuid4()
    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("DROP TABLE vault_people CASCADE")
        cursor.execute("""CREATE TABLE vault_people (
            id UUID PRIMARY KEY, owner_username TEXT NOT NULL, display_name TEXT NOT NULL,
            aliases JSONB NOT NULL DEFAULT '[]'::jsonb, active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_username, display_name)
        )""")
        cursor.execute("INSERT INTO vault_people(id,owner_username,display_name) VALUES(%s,%s,%s)", (legacy_person_id, owner.username, "Owner"))
    people.initialize()

    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT constraint_row.confdeltype FROM pg_constraint constraint_row
            JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid
            JOIN pg_attribute column_row ON column_row.attrelid=constraint_row.conrelid
                AND column_row.attnum=ANY(constraint_row.conkey)
            WHERE constraint_row.contype='f' AND constraint_row.confrelid='vault_assets'::regclass
              AND table_row.relname IN ('vault_face_detections','vault_person_detections','vault_asset_people','vault_asset_people_decisions')
              AND column_row.attname='asset_id'""")
        assert {row[0] for row in cursor.fetchall()} == {"c"}

    migrated = people.get_person(legacy_person_id, owner.user_id)
    assert migrated is not None
    assert migrated.id == legacy_person_id and migrated.full_name == "Owner"

    first = people.create_person(
        owner.username, "Owner", owner.user_id, full_name="Owner Kowalski",
        preferred_name="Rob", aliases=["Bobby"], date_of_birth=date(1980, 1, 2),
    )
    duplicate = people.create_person(owner.username, "Owner", owner.user_id, full_name="Owner Kowalski", preferred_name="Rob")
    other_person = people.create_person(other.username, "Owner", other.user_id, full_name="Owner Kowalski")
    assert first.id != duplicate.id
    assert [person.id for person in people.search_people(owner.user_id, "bobby")] == [first.id]
    assert {person.id for person in people.search_people(owner.user_id, "kowalski")} == {first.id, duplicate.id}
    updated = people.update_person(
        first.id, owner.user_id, preferred_name="Owner", aliases=["Bob"]
    )
    assert updated is not None and updated.id == first.id
    assert [person.id for person in people.search_people(owner.user_id, "bob")] == [first.id]

    assert people.set_me_person(owner.user_id, first.id).id == first.id
    assert people.set_me_person(owner.user_id, duplicate.id).id == duplicate.id
    assert people.resolve_me_person(owner.user_id).id == duplicate.id  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        people.set_me_person(owner.user_id, other_person.id)
    assert people.resolve_me_person(other.user_id) is None

    relationship = people.set_relationship(owner.user_id, first.id, duplicate.id, "friend")
    assert people.set_relationship(owner.user_id, first.id, duplicate.id, "colleague").id == relationship.id
    assert people.relationships_for_person(owner.user_id, first.id)[0].relationship_label == "colleague"
    people.clear_relationship(owner.user_id, first.id, duplicate.id)
    assert people.relationships_for_person(owner.user_id, first.id) == []
    with pytest.raises(ValueError):
        people.set_relationship(other.user_id, other_person.id, first.id, "friend")


def test_postgres_people_merge_is_transactional_and_preserves_identity_references(postgres_people_store) -> None:
    people, authentication, conninfo, vault = postgres_people_store
    owner = authentication.get_account("people-owner")
    assert owner is not None
    retained = people.create_person(owner.username, "Retained", owner.user_id, full_name="Retained")
    duplicate = people.create_person(owner.username, "Duplicate", owner.user_id, full_name="Duplicate")
    related = people.create_person(owner.username, "Related", owner.user_id, full_name="Related")
    asset = vault.restore_catalogued_asset(CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="merge", captured_on=None, location=None,
        vault_path="/vault/Gallery/merge.jpg", filename="merge.jpg", size_bytes=8, mime_type="image/jpeg",
        sha256="a" * 64, metadata={}, metadata_provenance={}, owner_username=owner.username, owner_user_id=owner.user_id,
    ), owner.username)
    people.associate(asset.id, duplicate.id, "user")
    people.decide(asset.id, duplicate.id, "include", owner.user_id, "people_correction")
    face_id = people.add_face_detection(asset.id, reference_person_id=duplicate.id)
    people.set_me_person(owner.user_id, duplicate.id)
    people.set_relationship(owner.user_id, duplicate.id, related.id, "friend")

    assert people.merge_people(duplicate.id, retained.id, owner.user_id).id == retained.id
    assert people.get_person(duplicate.id, owner.user_id) is not None
    assert people.get_person(duplicate.id, owner.user_id).active is False  # type: ignore[union-attr]
    assert people.resolve_me_person(owner.user_id).id == retained.id  # type: ignore[union-attr]
    assert people.matching_asset_ids((retained.id,), owner.user_id) == {asset.id}
    assert people.relationships_for_person(owner.user_id, retained.id)[0].related_person_id == related.id
    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT target_person_id FROM person_merge_history WHERE source_person_id=%s", (duplicate.id,))
        assert cursor.fetchone()[0] == retained.id
        cursor.execute("SELECT reference_person_id FROM vault_face_detections WHERE id=%s", (face_id,))
        assert cursor.fetchone()[0] == retained.id
