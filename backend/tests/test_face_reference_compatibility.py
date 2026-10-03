"""Synthetic upgrade coverage for the canonical reusable-reference store."""
from dataclasses import replace
import struct
from uuid import uuid4

import psycopg
import pytest

from app.gallery_people import MemoryGalleryPeopleStore
from app.vault_master import CataloguedAsset
from tests.test_postgres_people_foundation import postgres_people_store


VECTOR = struct.pack('<512f', 1.0, *([0.0] * 511))


def reference_rows(conninfo):
    with psycopg.connect(conninfo) as c:
        return c.execute('SELECT to_jsonb(r) FROM person_face_references r ORDER BY id').fetchall()


def asset(vault, owner):
    identity = uuid4()
    return vault.restore_catalogued_asset(CataloguedAsset(
        id=identity, asset_type='Gallery', display_title='Example photo', captured_on=None,
        location=None, vault_path=f'/vault/Gallery/synthetic-{identity}.jpg',
        filename=f'synthetic-{identity}.jpg', size_bytes=8, mime_type='image/jpeg',
        sha256=identity.hex * 2, metadata={}, metadata_provenance={},
        owner_username=owner.username, owner_user_id=owner.user_id), owner.username)


def test_existing_reference_rows_survive_bootstrap_without_identity_or_state_changes(postgres_people_store):
    people, auth, conninfo, _ = postgres_people_store
    owner, other = auth.get_account('people-owner'), auth.get_account('people-other')
    person = people.create_person(owner.username, 'Example Person', owner.user_id)
    relative = people.create_person(owner.username, 'Example Relative', owner.user_id)
    inactive_person = people.create_person(owner.username, 'Example Inactive', owner.user_id)
    people.update_person(inactive_person.id, owner.user_id, active=False)
    foreign = people.create_person(other.username, 'Example Person', other.user_id)
    people.set_me_person(owner.user_id, person.id)
    people.set_relationship(owner.user_id, person.id, relative.id, 'friend')
    # Existing dedicated identities need not equal the derived detection UUID.
    # Source provenance deliberately has no corresponding asset/detection rows.
    definitions = [(person.id,owner.user_id,True), (person.id,owner.user_id,False),
                   (foreign.id,other.user_id,True), (person.id,other.user_id,True),
                   (inactive_person.id,owner.user_id,True)]
    with psycopg.connect(conninfo) as c:
        for person_id, owner_id, active in definitions:
            c.execute("""INSERT INTO person_face_references
                (id,person_id,owner_user_id,embedding,embedding_dimension,embedding_model,
                 embedding_revision,source_asset_id,source_face_detection_id,active)
                VALUES(%s,%s,%s,%s,512,'facenet512',NULL,%s,%s,%s)""",
                (uuid4(),person_id,owner_id,VECTOR,uuid4(),uuid4(),active))
        people_before = c.execute('SELECT to_jsonb(p) FROM vault_people p ORDER BY id').fetchall()
        # Older schema variants used NO ACTION; repair must not rebuild rows.
        c.execute('ALTER TABLE person_face_references DROP CONSTRAINT person_face_references_person_id_fkey')
        c.execute('ALTER TABLE person_face_references ADD CONSTRAINT person_face_references_person_id_fkey FOREIGN KEY(person_id) REFERENCES vault_people(id)')
    before = reference_rows(conninfo)
    people.initialize()
    people.initialize()
    assert reference_rows(conninfo) == before
    refs = people.reference_embeddings_by_user_id(owner.user_id)
    assert [(r.person_id,r.embedding,r.embedding_model,r.embedding_revision) for r in refs] == [(person.id,VECTOR,'facenet512',None)]
    assert [r.person_id for r in people.reference_embeddings_by_user_id(other.user_id)] == [foreign.id]
    assert people.reference_embeddings_by_user_id(None) == []
    assert people.reference_embeddings_by_user_id(uuid4()) == []
    assert people.resolve_me_person(owner.user_id).id == person.id
    assert people.relationships_for_person(owner.user_id, person.id)[0].related_person_id == relative.id
    with psycopg.connect(conninfo) as c:
        assert c.execute("SELECT confdeltype FROM pg_constraint WHERE conrelid='person_face_references'::regclass AND conname='person_face_references_person_id_fkey'").fetchone()[0] == 'c'
        assert c.execute('SELECT to_jsonb(p) FROM vault_people p ORDER BY id').fetchall() == people_before
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with c.transaction():
                c.execute('UPDATE person_face_references SET person_id=%s WHERE person_id=%s', (uuid4(),person.id))


def test_explicit_identification_updates_same_reference_and_clear_stays_inactive(postgres_people_store):
    people, auth, conninfo, vault = postgres_people_store
    owner, other = auth.get_account('people-owner'), auth.get_account('people-other')
    first = people.create_person(owner.username, 'Example A', owner.user_id)
    second = people.create_person(owner.username, 'Example B', owner.user_id)
    foreign = people.create_person(other.username, 'Example C', other.user_id)
    photo = asset(vault, owner)
    face = people.add_face_detection(photo.id, embedding=VECTOR, embedding_dimension=512,
        embedding_model='facenet512', recognition_result='unknown')
    with pytest.raises(ValueError):
        people.identify_face(photo.id, face, foreign.id, other.user_id)
    assert reference_rows(conninfo) == []
    people.identify_face(photo.id, face, first.id, owner.user_id)
    original = reference_rows(conninfo)[0][0]
    people.identify_face(photo.id, face, second.id, owner.user_id)
    updated = reference_rows(conninfo)
    assert len(updated) == 1 and updated[0][0]['id'] == original['id']
    assert updated[0][0]['person_id'] == str(second.id)
    assert [r.person_id for r in people.reference_embeddings_by_user_id(owner.user_id)] == [second.id]
    people.clear_face_identity(photo.id, face, owner.user_id)
    cleared = reference_rows(conninfo)
    assert cleared[0][0]['active'] is False
    assert cleared[0][0]['embedding'] == original['embedding']
    # Even stale legacy confirmation cannot reactivate an existing dedicated row.
    with psycopg.connect(conninfo) as c:
        c.execute('UPDATE vault_face_detections SET reference_person_id=%s WHERE id=%s', (second.id,face))
    people.initialize()
    assert reference_rows(conninfo) == cleared
    assert people.reference_embeddings_by_user_id(owner.user_id) == []


def test_legacy_copy_is_idempotent_not_runtime_fallback_and_survives_asset_cleanup(postgres_people_store):
    people, auth, conninfo, vault = postgres_people_store
    owner = auth.get_account('people-owner')
    person = people.create_person(owner.username, 'Example Reference', owner.user_id)
    photo = asset(vault, owner)
    face = people.add_face_detection(photo.id, embedding=VECTOR, embedding_dimension=512,
        embedding_model='facenet512', reference_person_id=person.id, recognition_result='known')
    assert people.reference_embeddings_by_user_id(owner.user_id) == []
    people.initialize()
    before = reference_rows(conninfo)
    assert len(before) == 1 and before[0][0]['source_face_detection_id'] == str(face)
    people.initialize()
    assert reference_rows(conninfo) == before
    with psycopg.connect(conninfo) as c:
        c.execute('DELETE FROM vault_files WHERE asset_id=%s', (photo.id,))
        c.execute('DELETE FROM vault_assets WHERE id=%s', (photo.id,))
        assert c.execute('SELECT count(*) FROM vault_face_detections WHERE id=%s', (face,)).fetchone()[0] == 0
    assert reference_rows(conninfo) == before
    assert people.reference_embeddings_by_user_id(owner.user_id)[0].embedding == VECTOR
    with psycopg.connect(conninfo) as c:
        c.execute('DELETE FROM vault_people WHERE id=%s', (person.id,))
        assert c.execute('SELECT count(*) FROM person_face_references WHERE person_id=%s', (person.id,)).fetchone()[0] == 0


def test_memory_store_uses_dedicated_owner_scoped_active_references_only():
    people = MemoryGalleryPeopleStore()
    owner, other = uuid4(), uuid4()
    person = people.create_person('example-owner', 'Example Person', owner)
    face = people.add_face_detection(uuid4(), embedding=VECTOR, embedding_dimension=512,
        embedding_model='facenet512', reference_person_id=person.id)
    assert people.reference_embeddings_by_user_id(owner) == []
    people.confirm_face_reference(face, person.id, owner)
    assert len(people.reference_embeddings_by_user_id(owner)) == 1
    assert people.reference_embeddings_by_user_id(other) == []
    assert people.reference_embeddings_by_user_id(None) == []
    people.face_detections.clear()  # Dedicated references outlive detection lifecycle.
    assert people.reference_embeddings_by_user_id(owner)[0].embedding == VECTOR
    people.update_person(person.id, owner, active=False)
    assert people.reference_embeddings_by_user_id(owner) == []
    assert people.reference_embeddings('example-owner') == []
    people.update_person(person.id, owner, active=True)
    reference = next(iter(people.face_references.values()))
    people.face_references[reference.id] = replace(reference, owner_user_id=other)
    assert people.reference_embeddings_by_user_id(owner) == []
    assert people.reference_embeddings('example-owner') == []
