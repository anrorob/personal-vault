"""Stage B People persistence; specialist-free and routing-free by design."""
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from app.config import get_database_conninfo

AssociationSource = Literal["vault_master", "user", "user_face", "imported"]
AssociationDecision = Literal["include", "exclude"]


@dataclass(frozen=True)
class VaultPerson:
    id: UUID
    owner_username: str
    display_name: str
    active: bool
    created_at: datetime
    updated_at: datetime
    owner_user_id: UUID | None = None
    full_name: str = ""
    preferred_name: str | None = None
    aliases: tuple[str, ...] = ()
    date_of_birth: date | None = None
    profile_asset_id: UUID | None = None
    profile_frame: dict[str, float] | None = None


@dataclass(frozen=True)
class PersonRelationship:
    id: UUID
    owner_user_id: UUID
    subject_person_id: UUID
    related_person_id: UUID
    relationship_label: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AssetPerson:
    asset_id: UUID
    person_id: UUID
    display_name: str
    source: str


@dataclass(frozen=True)
class PersonReference:
    person_id: UUID
    embedding: bytes
    embedding_model: str
    embedding_revision: str | None


@dataclass(frozen=True)
class FaceDetection:
    id: UUID
    asset_id: UUID
    bounding_box: dict[str, float]
    person_id: UUID | None
    person_name: str | None
    user_confirmed: bool


def _person_values(
    full_name: str | None,
    display_name: str | None,
    preferred_name: str | None,
    aliases: tuple[str, ...] | list[str],
) -> tuple[str, str, str | None, tuple[str, ...]]:
    canonical_full_name = (full_name or display_name or "").strip()
    canonical_display_name = (display_name or canonical_full_name).strip()
    canonical_preferred_name = preferred_name.strip() if preferred_name and preferred_name.strip() else None
    canonical_aliases = tuple(
        dict.fromkeys(alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip())
    )
    if not canonical_full_name or not canonical_display_name:
        raise ValueError("Full name is required")
    return canonical_full_name, canonical_display_name, canonical_preferred_name, canonical_aliases


class MemoryGalleryPeopleStore:
    def __init__(self) -> None:
        self.people: dict[UUID, VaultPerson] = {}
        self.associations: dict[tuple[UUID, UUID, str, UUID | None], None] = {}
        self.association_owner_user_ids: dict[tuple[UUID, UUID, str, UUID | None], UUID] = {}
        self.decisions: dict[tuple[UUID, UUID], str] = {}
        self.decision_sources: dict[tuple[UUID, UUID], str] = {}
        self.decision_owner_user_ids: dict[tuple[UUID, UUID], UUID] = {}
        self.face_detections: dict[UUID, dict[str, object]] = {}
        self.person_detections: dict[UUID, dict[str, object]] = {}
        self.me_people: dict[UUID, UUID] = {}
        self.relationships: dict[tuple[UUID, UUID, UUID], PersonRelationship] = {}

    def create_person(
        self,
        owner: str,
        display_name: str | None = None,
        owner_user_id: UUID | None = None,
        *,
        full_name: str | None = None,
        preferred_name: str | None = None,
        aliases: tuple[str, ...] | list[str] = (),
        date_of_birth: date | None = None,
        profile_asset_id: UUID | None = None,
        profile_frame: dict[str, float] | None = None,
    ) -> VaultPerson:
        full_name, display_name, preferred_name, aliases = _person_values(
            full_name, display_name, preferred_name, aliases
        )
        now = datetime.now(timezone.utc)
        person = VaultPerson(
            uuid4(), owner, display_name, True, now, now,
            owner_user_id or uuid5(NAMESPACE_URL, f"personal-vault-test:{owner}"),
            full_name, preferred_name, aliases, date_of_birth, profile_asset_id,
            profile_frame,
        )
        self.people[person.id] = person
        return person

    def get_person(self, person_id: UUID, owner: UUID | str) -> VaultPerson | None:
        person = self.people.get(person_id)
        return person if person and (person.owner_user_id == owner if isinstance(owner, UUID) else person.owner_username == owner) else None

    def list_people(self, owner: UUID | str, include_inactive: bool = False) -> list[VaultPerson]:
        return sorted((person for person in self.people.values() if (person.owner_user_id == owner if isinstance(owner, UUID) else person.owner_username == owner) and (person.active or include_inactive)), key=lambda person: person.display_name.casefold())

    def update_person(
        self, person_id: UUID, owner: UUID | str, display_name: str | None = None,
        active: bool | None = None, *, full_name: str | None = None,
        preferred_name: str | None = None, aliases: tuple[str, ...] | list[str] | None = None,
        date_of_birth: date | None = None, profile_asset_id: UUID | None = None,
        clear_date_of_birth: bool = False, clear_profile_asset: bool = False,
        profile_frame: dict[str, float] | None = None, clear_profile_frame: bool = False,
    ) -> VaultPerson | None:
        person = self.get_person(person_id, owner)
        if person is None:
            return None
        updated_full_name, updated_display_name, updated_preferred_name, updated_aliases = _person_values(
            full_name if full_name is not None else person.full_name,
            display_name if display_name is not None else person.display_name,
            preferred_name if preferred_name is not None else person.preferred_name,
            aliases if aliases is not None else person.aliases,
        )
        updated = replace(
            person, full_name=updated_full_name, display_name=updated_display_name,
            preferred_name=updated_preferred_name, aliases=updated_aliases,
            date_of_birth=None if clear_date_of_birth else (date_of_birth if date_of_birth is not None else person.date_of_birth),
            profile_asset_id=None if clear_profile_asset else (profile_asset_id if profile_asset_id is not None else person.profile_asset_id),
            active=active if active is not None else person.active, updated_at=datetime.now(timezone.utc),
            profile_frame=None if clear_profile_frame or clear_profile_asset else (profile_frame if profile_frame is not None else person.profile_frame),
        )
        self.people[person_id] = updated
        return updated

    def associate(self, asset_id: UUID, person_id: UUID, source: AssociationSource, face_detection_id: UUID | None = None, created_by: str = "vault_master") -> None:
        del created_by
        person = self.people.get(person_id)
        if person is None or person.owner_user_id is None:
            raise ValueError("Person requires an immutable owner ID")
        key = (asset_id, person_id, source, face_detection_id)
        self.associations[key] = None
        self.association_owner_user_ids[key] = person.owner_user_id

    def decide(self, asset_id: UUID, person_id: UUID, decision: AssociationDecision, owner: UUID | str, source: str = "photo_presence") -> None:
        if self.get_person(person_id, owner) is None:
            raise ValueError("Person was not found")
        person = self.people[person_id]
        self.decisions[(asset_id, person_id)] = decision
        self.decision_sources[(asset_id, person_id)] = source
        self.decision_owner_user_ids[(asset_id, person_id)] = person.owner_user_id  # type: ignore[assignment]

    def effective_people(self, asset_id: UUID, owner: UUID | str) -> list[AssetPerson]:
        ids = {person_id for candidate_asset, person_id, _, _ in self.associations if candidate_asset == asset_id} | {person_id for (candidate_asset, person_id), value in self.decisions.items() if candidate_asset == asset_id and value == "include"}
        values = []
        for person_id in ids:
            person = self.get_person(person_id, owner)
            if person and person.active and self.decisions.get((asset_id, person_id)) != "exclude":
                sources = [source for candidate_asset, candidate_person, source, _ in self.associations if candidate_asset == asset_id and candidate_person == person_id]
                values.append(AssetPerson(asset_id, person_id, person.display_name, "user" if self.decisions.get((asset_id, person_id)) == "include" else (sources[-1] if sources else "user")))
        return sorted(values, key=lambda person: person.display_name.casefold())

    def add_face_detection(self, asset_id: UUID, **evidence: object) -> UUID:
        detection_id = uuid4()
        self.face_detections[detection_id] = {"id": detection_id, "asset_id": asset_id, **evidence}
        return detection_id

    def add_person_detection(self, asset_id: UUID, **evidence: object) -> UUID:
        detection_id = uuid4()
        self.person_detections[detection_id] = {"id": detection_id, "asset_id": asset_id, **evidence}
        return detection_id

    def reference_embeddings(self, owner: str) -> list[PersonReference]:
        return [PersonReference(UUID(str(row["reference_person_id"])), bytes(row["embedding"]), str(row.get("embedding_model") or "facenet512"), str(row["embedding_revision"]) if row.get("embedding_revision") else None) for row in self.face_detections.values() if row.get("reference_person_id") and row.get("embedding") and self.get_person(UUID(str(row["reference_person_id"])), owner)]

    def reference_embeddings_by_user_id(self, owner_user_id: UUID | None) -> list[PersonReference]:
        if owner_user_id is None:
            return []
        return [PersonReference(UUID(str(row["reference_person_id"])), bytes(row["embedding"]), str(row.get("embedding_model") or "facenet512"), str(row["embedding_revision"]) if row.get("embedding_revision") else None) for row in self.face_detections.values() if row.get("reference_person_id") and row.get("embedding") and self.get_person(UUID(str(row["reference_person_id"])), owner_user_id)]

    def unknown_face_detections(
        self, asset_id: UUID, producing_job_id: UUID | None = None, legacy_since: datetime | None = None
    ) -> list[UUID]:
        return [
            face.id
            for face in self.face_detections_for_asset(asset_id, "", producing_job_id, legacy_since)
            if face.person_id is None
        ]

    def face_detections_for_asset(
        self,
        asset_id: UUID,
        owner: str,
        producing_job_id: UUID | None = None,
        legacy_since: datetime | None = None,
    ) -> list[FaceDetection]:
        rows = [
            (detection_id, row)
            for detection_id, row in self.face_detections.items()
            if row["asset_id"] == asset_id
        ]
        if producing_job_id is not None:
            current = [item for item in rows if item[1].get("producing_job_id") == producing_job_id]
            if current:
                rows = current
            elif legacy_since is not None:
                rows = [
                    item
                    for item in rows
                    if item[1].get("producing_job_id") is None
                    and item[1].get("created_at", legacy_since) >= legacy_since
                ]
        values = []
        for detection_id, row in rows:
            person_id = row.get("reference_person_id") or row.get("recognition_candidate_person_id")
            person = self.get_person(UUID(str(person_id)), owner) if person_id else None
            values.append(FaceDetection(detection_id, asset_id, dict(row.get("bounding_box") or {}), person.id if person else None, person.display_name if person else None, bool(row.get("reference_person_id"))))
        return values

    def confirm_face_reference(self, detection_id: UUID, person_id: UUID, owner: UUID | str) -> None:
        if self.get_person(person_id, owner) is None or detection_id not in self.face_detections:
            raise ValueError("Face evidence or Person was not found")
        self.face_detections[detection_id]["reference_person_id"] = person_id
        self.face_detections[detection_id]["recognition_result"] = "known"

    def identify_face(self, asset_id: UUID, detection_id: UUID, person_id: UUID, owner: UUID | str) -> None:
        if self.face_detections.get(detection_id, {}).get("asset_id") != asset_id:
            raise ValueError("Face evidence was not found")
        previous_person_id = self.face_detections[detection_id].get("reference_person_id")
        self.confirm_face_reference(detection_id, person_id, owner)
        for key in list(self.associations):
            if key[0] == asset_id and key[2] == "user_face" and key[3] == detection_id:
                del self.associations[key]
                self.association_owner_user_ids.pop(key, None)
        self.associate(asset_id, person_id, "user_face", detection_id, owner)
        # A later explicit identification is also an explicit assertion that
        # this Person is present in the photo. It supersedes an earlier user
        # exclusion, while later explicit removal can still exclude them.
        self.decide(asset_id, person_id, "include", owner, "face_identification")
        if previous_person_id and previous_person_id != person_id:
            has_other_face = any(
                key[0] == asset_id and key[1] == previous_person_id and key[2] == "user_face"
                for key in self.associations
            )
            if not has_other_face and self.decision_sources.get((asset_id, previous_person_id)) == "face_identification":
                self.decisions.pop((asset_id, previous_person_id), None)
                self.decision_sources.pop((asset_id, previous_person_id), None)
                self.decision_owner_user_ids.pop((asset_id, previous_person_id), None)

    def clear_face_identity(self, asset_id: UUID, detection_id: UUID, owner: UUID | str) -> None:
        if self.face_detections.get(detection_id, {}).get("asset_id") != asset_id:
            raise ValueError("Face evidence was not found")
        self.face_detections[detection_id]["reference_person_id"] = None
        self.face_detections[detection_id]["recognition_result"] = "unknown"
        for key in list(self.associations):
            if key[0] == asset_id and key[2] == "user_face" and key[3] == detection_id:
                del self.associations[key]
                self.association_owner_user_ids.pop(key, None)

    def matching_asset_ids(self, person_ids: tuple[UUID, ...], owner: UUID | str) -> set[UUID]:
        matching: set[UUID] = set()
        for asset_id, person_id, _, _ in self.associations:
            if person_id in person_ids and self.get_person(person_id, owner) and self.decisions.get((asset_id, person_id)) != "exclude":
                matching.add(asset_id)
        for (asset_id, person_id), decision in self.decisions.items():
            if person_id in person_ids and decision == "include" and self.get_person(person_id, owner):
                matching.add(asset_id)
        return matching

    def search_people(self, owner: UUID | str, query: str) -> list[VaultPerson]:
        needle = query.strip().casefold()
        if not needle:
            return self.list_people(owner)
        return [
            person for person in self.list_people(owner)
            if needle in person.full_name.casefold()
            or (person.preferred_name is not None and needle in person.preferred_name.casefold())
            or any(needle in alias.casefold() for alias in person.aliases)
        ]

    def resolve_me_person(self, user_id: UUID) -> VaultPerson | None:
        person_id = self.me_people.get(user_id)
        return self.get_person(person_id, user_id) if person_id else None

    def set_me_person(self, user_id: UUID, person_id: UUID) -> VaultPerson:
        person = self.get_person(person_id, user_id)
        if person is None or not person.active:
            raise ValueError("Person was not found")
        self.me_people[user_id] = person_id
        return person

    def clear_me_person(self, user_id: UUID) -> None:
        self.me_people.pop(user_id, None)

    def set_relationship(
        self, owner_user_id: UUID, subject_person_id: UUID, related_person_id: UUID,
        relationship_label: str,
    ) -> PersonRelationship:
        subject = self.get_person(subject_person_id, owner_user_id)
        related = self.get_person(related_person_id, owner_user_id)
        label = relationship_label.strip()
        if subject is None or related is None or not label:
            raise ValueError("People relationship was not found")
        now = datetime.now(timezone.utc)
        key = (owner_user_id, subject_person_id, related_person_id)
        current = self.relationships.get(key)
        relationship = PersonRelationship(
            current.id if current else uuid4(), owner_user_id, subject_person_id,
            related_person_id, label, current.created_at if current else now, now,
        )
        self.relationships[key] = relationship
        return relationship

    def relationships_for_person(self, owner_user_id: UUID, subject_person_id: UUID) -> list[PersonRelationship]:
        if self.get_person(subject_person_id, owner_user_id) is None:
            return []
        return sorted(
            (relationship for relationship in self.relationships.values()
             if relationship.owner_user_id == owner_user_id and relationship.subject_person_id == subject_person_id),
            key=lambda relationship: (relationship.relationship_label.casefold(), str(relationship.related_person_id)),
        )

    def clear_relationship(self, owner_user_id: UUID, subject_person_id: UUID, related_person_id: UUID) -> None:
        self.relationships.pop((owner_user_id, subject_person_id, related_person_id), None)

    def merge_people(self, source_person_id: UUID, target_person_id: UUID, owner_user_id: UUID) -> VaultPerson:
        """Move compatible identity evidence to the retained Person without deleting history."""
        source = self.get_person(source_person_id, owner_user_id)
        target = self.get_person(target_person_id, owner_user_id)
        if source is None or target is None or source.id == target.id or not source.active or not target.active:
            raise ValueError("People merge was not found")
        for (asset_id, person_id), decision in self.decisions.items():
            if person_id != source.id:
                continue
            target_decision = self.decisions.get((asset_id, target.id))
            if target_decision is not None and target_decision != decision:
                raise ValueError("People merge has conflicting user decisions")
        remapped: dict[tuple[UUID, UUID, UUID], PersonRelationship] = {}
        for relationship in self.relationships.values():
            if relationship.owner_user_id != owner_user_id:
                continue
            subject = target.id if relationship.subject_person_id == source.id else relationship.subject_person_id
            related = target.id if relationship.related_person_id == source.id else relationship.related_person_id
            if subject == related:
                continue
            key = (owner_user_id, subject, related)
            current = remapped.get(key)
            if current and current.relationship_label != relationship.relationship_label:
                raise ValueError("People merge has conflicting relationships")
            remapped[key] = replace(relationship, subject_person_id=subject, related_person_id=related)
        for key in [key for key in self.associations if key[1] == source.id]:
            asset_id, _, association_source, face_id = key
            new_key = (asset_id, target.id, association_source, face_id)
            self.associations[new_key] = None
            self.association_owner_user_ids[new_key] = owner_user_id
        for (asset_id, person_id), decision in list(self.decisions.items()):
            if person_id == source.id and (asset_id, target.id) not in self.decisions:
                self.decisions[(asset_id, target.id)] = decision
                self.decision_sources[(asset_id, target.id)] = self.decision_sources[(asset_id, person_id)]
                self.decision_owner_user_ids[(asset_id, target.id)] = owner_user_id
        for face in self.face_detections.values():
            for field in ("reference_person_id", "recognition_candidate_person_id"):
                if face.get(field) == source.id:
                    face[field] = target.id
        if self.me_people.get(owner_user_id) == source.id:
            self.me_people[owner_user_id] = target.id
        self.relationships = {
            key: relationship for key, relationship in self.relationships.items()
            if relationship.owner_user_id != owner_user_id
        }
        self.relationships.update(remapped)
        self.people[source.id] = replace(source, active=False, updated_at=datetime.now(timezone.utc))
        return target

    def person_detection_count(self, asset_id: UUID, producing_job_id: UUID | None = None) -> int:
        return sum(1 for row in self.person_detections.values() if row["asset_id"] == asset_id and (producing_job_id is None or row.get("producing_job_id") == producing_job_id))


class PostgresGalleryPeopleStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_people (id UUID PRIMARY KEY, owner_username TEXT NOT NULL, display_name TEXT NOT NULL, aliases JSONB NOT NULL DEFAULT '[]'::jsonb, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(owner_username, display_name))""")
            cursor.execute("ALTER TABLE vault_people ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_people ADD COLUMN IF NOT EXISTS full_name TEXT")
            cursor.execute("ALTER TABLE vault_people ADD COLUMN IF NOT EXISTS preferred_name TEXT")
            cursor.execute("ALTER TABLE vault_people ADD COLUMN IF NOT EXISTS date_of_birth DATE")
            cursor.execute("ALTER TABLE vault_people ADD COLUMN IF NOT EXISTS profile_asset_id UUID REFERENCES vault_assets(id)")
            cursor.execute("ALTER TABLE vault_people ADD COLUMN IF NOT EXISTS profile_frame JSONB")
            cursor.execute("UPDATE vault_people SET full_name=display_name WHERE full_name IS NULL OR btrim(full_name)=''" )
            cursor.execute("ALTER TABLE vault_people ALTER COLUMN full_name SET NOT NULL")
            cursor.execute("""DO $$ DECLARE legacy_constraint TEXT; BEGIN
                FOR legacy_constraint IN
                    SELECT conname FROM pg_constraint
                    WHERE conrelid='vault_people'::regclass AND contype='u'
                      AND conkey=ARRAY[
                        (SELECT attnum FROM pg_attribute WHERE attrelid='vault_people'::regclass AND attname='owner_username'),
                        (SELECT attnum FROM pg_attribute WHERE attrelid='vault_people'::regclass AND attname='display_name')
                      ]::smallint[]
                LOOP
                    EXECUTE format('ALTER TABLE vault_people DROP CONSTRAINT %I', legacy_constraint);
                END LOOP;
            END $$""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_people_owner_full_name_idx ON vault_people(owner_user_id, lower(full_name))")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_people_owner_preferred_name_idx ON vault_people(owner_user_id, lower(preferred_name)) WHERE preferred_name IS NOT NULL")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_people_aliases_idx ON vault_people USING GIN(aliases)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS user_me_people (
                user_id UUID PRIMARY KEY REFERENCES auth_accounts(user_id),
                person_id UUID NOT NULL REFERENCES vault_people(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            cursor.execute("CREATE INDEX IF NOT EXISTS user_me_people_person_idx ON user_me_people(person_id)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS person_relationships (
                id UUID PRIMARY KEY,
                owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                subject_person_id UUID NOT NULL REFERENCES vault_people(id),
                related_person_id UUID NOT NULL REFERENCES vault_people(id),
                relationship_label TEXT NOT NULL CHECK(btrim(relationship_label) <> ''),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_user_id, subject_person_id, related_person_id)
            )""")
            cursor.execute("CREATE INDEX IF NOT EXISTS person_relationships_subject_idx ON person_relationships(owner_user_id, subject_person_id)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS person_merge_history (
                source_person_id UUID PRIMARY KEY REFERENCES vault_people(id),
                target_person_id UUID NOT NULL REFERENCES vault_people(id),
                owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                merged_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(source_person_id <> target_person_id)
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_face_detections (id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), producing_job_id UUID REFERENCES vault_gallery_intelligence_jobs(id), bounding_box JSONB NOT NULL, detector_provider TEXT NOT NULL, detector_model TEXT NOT NULL, detector_revision TEXT, task_version TEXT NOT NULL, embedding BYTEA, embedding_dimension INTEGER, embedding_model TEXT, embedding_revision TEXT, reference_person_id UUID REFERENCES vault_people(id), recognition_candidate_person_id UUID REFERENCES vault_people(id), native_distance DOUBLE PRECISION, recognition_result TEXT CHECK(recognition_result IN ('known','unknown','unmatched')), raw_evidence JSONB NOT NULL DEFAULT '{}'::jsonb, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("ALTER TABLE vault_face_detections ADD COLUMN IF NOT EXISTS reference_person_id UUID REFERENCES vault_people(id)")
            cursor.execute("ALTER TABLE vault_face_detections ADD COLUMN IF NOT EXISTS producing_job_id UUID REFERENCES vault_gallery_intelligence_jobs(id)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_person_detections (id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), producing_job_id UUID REFERENCES vault_gallery_intelligence_jobs(id), bounding_box JSONB NOT NULL, detector_provider TEXT NOT NULL, detector_model TEXT NOT NULL, detector_revision TEXT, task_version TEXT NOT NULL, raw_evidence JSONB NOT NULL DEFAULT '{}'::jsonb, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("ALTER TABLE vault_person_detections ADD COLUMN IF NOT EXISTS producing_job_id UUID REFERENCES vault_gallery_intelligence_jobs(id)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_asset_people (id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), person_id UUID NOT NULL REFERENCES vault_people(id), source TEXT NOT NULL CHECK(source IN ('vault_master','user','user_face','imported')), supporting_face_detection_id UUID REFERENCES vault_face_detections(id), active BOOLEAN NOT NULL DEFAULT TRUE, created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(asset_id,person_id,source))""")
            cursor.execute("ALTER TABLE vault_asset_people DROP CONSTRAINT IF EXISTS vault_asset_people_source_check")
            cursor.execute("ALTER TABLE vault_asset_people ADD CONSTRAINT vault_asset_people_source_check CHECK(source IN ('vault_master','user','user_face','imported'))")
            cursor.execute("ALTER TABLE vault_asset_people DROP CONSTRAINT IF EXISTS vault_asset_people_asset_id_person_id_source_key")
            cursor.execute("""DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='vault_asset_people_asset_person_source_face_key'
                      AND conrelid='vault_asset_people'::regclass
                ) THEN
                    ALTER TABLE vault_asset_people
                    ADD CONSTRAINT vault_asset_people_asset_person_source_face_key
                    UNIQUE(asset_id,person_id,source,supporting_face_detection_id);
                END IF;
            END $$""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_asset_people_decisions (id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), person_id UUID NOT NULL REFERENCES vault_people(id), decision TEXT NOT NULL CHECK(decision IN ('include','exclude')), decision_source TEXT, decided_by TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(asset_id,person_id))""")
            cursor.execute("ALTER TABLE vault_asset_people_decisions ADD COLUMN IF NOT EXISTS decision_source TEXT")
            cursor.execute("""DO $$ DECLARE foreign_key RECORD; BEGIN
                FOR foreign_key IN
                    SELECT constraint_row.conname, table_row.relname AS table_name
                    FROM pg_constraint constraint_row
                    JOIN pg_class table_row ON table_row.oid=constraint_row.conrelid
                    JOIN pg_attribute column_row ON column_row.attrelid=constraint_row.conrelid
                        AND column_row.attnum=ANY(constraint_row.conkey)
                    WHERE constraint_row.contype='f'
                      AND constraint_row.confrelid='vault_assets'::regclass
                      AND table_row.relname IN ('vault_face_detections','vault_person_detections','vault_asset_people','vault_asset_people_decisions')
                      AND column_row.attname='asset_id'
                      AND constraint_row.confdeltype <> 'c'
                LOOP
                    EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', foreign_key.table_name, foreign_key.conname);
                    EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (asset_id) REFERENCES vault_assets(id) ON DELETE CASCADE', foreign_key.table_name, foreign_key.conname);
                END LOOP;
            END $$""")
            for table in ("vault_face_detections", "vault_person_detections", "vault_asset_people", "vault_asset_people_decisions"):
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
                cursor.execute(f"""UPDATE {table} AS records SET owner_user_id=assets.owner_user_id FROM vault_assets AS assets
                    WHERE records.owner_user_id IS NULL AND records.asset_id=assets.id AND assets.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_people AS people SET owner_user_id=accounts.user_id FROM auth_accounts AS accounts
                WHERE people.owner_user_id IS NULL AND people.owner_username=accounts.username""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_asset_people_asset_idx ON vault_asset_people(asset_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_face_detections_asset_idx ON vault_face_detections(asset_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_face_detections_producing_job_idx ON vault_face_detections(producing_job_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_person_detections_asset_idx ON vault_person_detections(asset_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_person_detections_producing_job_idx ON vault_person_detections(producing_job_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_face_detections_owner_asset_idx ON vault_face_detections(owner_user_id,asset_id)")
            self._repair_legacy_detection_provenance(cursor, "vault_face_detections")
            self._repair_legacy_detection_provenance(cursor, "vault_person_detections")

    @staticmethod
    def _repair_legacy_detection_provenance(cursor, table_name: str) -> None:
        """Link only unambiguous legacy Stage B evidence to its completed job.

        Earlier releases retained detections without a producing-job reference.
        This narrow additive repair only links a row when one and only one
        completed People task could have produced it in the immediate
        post-completion window. Ambiguous evidence remains untouched and is
        handled by the read-only compatibility projection.
        """
        cursor.execute(f"""UPDATE {table_name} detections
            SET producing_job_id = (
                SELECT jobs.id
                FROM vault_gallery_intelligence_jobs jobs
                WHERE jobs.asset_id=detections.asset_id
                  AND jobs.people_status='completed'
                  AND jobs.completed_at IS NOT NULL
                  AND detections.created_at >= jobs.completed_at
                  AND detections.created_at < jobs.completed_at + INTERVAL '5 minutes'
            )
            WHERE detections.producing_job_id IS NULL
              AND detections.task_version='gallery-people-v2'
              AND 1 = (
                  SELECT COUNT(*)
                  FROM vault_gallery_intelligence_jobs jobs
                  WHERE jobs.asset_id=detections.asset_id
                    AND jobs.people_status='completed'
                    AND jobs.completed_at IS NOT NULL
                    AND detections.created_at >= jobs.completed_at
                    AND detections.created_at < jobs.completed_at + INTERVAL '5 minutes'
              )""")

    @staticmethod
    def _person(row: dict[str, object]) -> VaultPerson:
        aliases = row.get("aliases") if isinstance(row.get("aliases"), list) else []
        return VaultPerson(
            UUID(str(row["id"])), str(row["owner_username"]), str(row["display_name"]),
            bool(row["active"]), row["created_at"], row["updated_at"],
            UUID(str(row["owner_user_id"])) if row.get("owner_user_id") else None,
            str(row.get("full_name") or row["display_name"]),
            str(row["preferred_name"]) if row.get("preferred_name") else None,
            tuple(str(alias) for alias in aliases), row.get("date_of_birth"),
            UUID(str(row["profile_asset_id"])) if row.get("profile_asset_id") else None,
            dict(row["profile_frame"]) if isinstance(row.get("profile_frame"), dict) else None,
        )  # type: ignore[arg-type]

    @staticmethod
    def _owner_user_id(cursor, owner_username: str) -> UUID:
        cursor.execute("SELECT user_id FROM auth_accounts WHERE username=%s", (owner_username,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Person requires an immutable owner ID")
        return UUID(str(row["user_id"]))

    @staticmethod
    def _validate_profile_asset(cursor, owner_user_id: UUID, profile_asset_id: UUID | None) -> None:
        if profile_asset_id is None:
            return
        cursor.execute("SELECT id FROM vault_assets WHERE id=%s AND owner_user_id=%s", (profile_asset_id, owner_user_id))
        if cursor.fetchone() is None:
            raise ValueError("Profile asset was not found")

    def create_person(
        self, owner: str, display_name: str | None = None, owner_user_id: UUID | None = None,
        *, full_name: str | None = None, preferred_name: str | None = None,
        aliases: tuple[str, ...] | list[str] = (), date_of_birth: date | None = None,
        profile_asset_id: UUID | None = None,
        profile_frame: dict[str, float] | None = None,
    ) -> VaultPerson:
        full_name, display_name, preferred_name, aliases = _person_values(
            full_name, display_name, preferred_name, aliases
        )
        with self._connect() as connection, connection.cursor() as cursor:
            resolved_owner = owner_user_id or self._owner_user_id(cursor, owner)
            self._validate_profile_asset(cursor, resolved_owner, profile_asset_id)
            cursor.execute("""INSERT INTO vault_people(id,owner_username,owner_user_id,display_name,full_name,preferred_name,aliases,date_of_birth,profile_asset_id,profile_frame)
                VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb) RETURNING *""", (
                uuid4(), owner, resolved_owner, display_name, full_name, preferred_name,
                __import__("json").dumps(aliases), date_of_birth, profile_asset_id, __import__("json").dumps(profile_frame) if profile_frame else None,
            ))
            return self._person(cursor.fetchone())

    def get_person(self, person_id: UUID, owner: UUID | str) -> VaultPerson | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_people WHERE id=%s AND " + ("owner_user_id=%s" if isinstance(owner, UUID) else "owner_username=%s"), (person_id, owner))
            row = cursor.fetchone()
        return self._person(row) if row else None

    def list_people(self, owner: UUID | str, include_inactive: bool = False) -> list[VaultPerson]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_people WHERE " + ("owner_user_id=%s" if isinstance(owner, UUID) else "owner_username=%s") + " AND (%s OR active) ORDER BY lower(display_name)", (owner, include_inactive))
            return [self._person(row) for row in cursor.fetchall()]

    def update_person(
        self, person_id: UUID, owner: UUID | str, display_name: str | None = None,
        active: bool | None = None, *, full_name: str | None = None,
        preferred_name: str | None = None, aliases: tuple[str, ...] | list[str] | None = None,
        date_of_birth: date | None = None, profile_asset_id: UUID | None = None,
        clear_date_of_birth: bool = False, clear_profile_asset: bool = False,
        profile_frame: dict[str, float] | None = None, clear_profile_frame: bool = False,
    ) -> VaultPerson | None:
        current = self.get_person(person_id, owner)
        if current is None:
            return None
        updated_full_name, updated_display_name, updated_preferred_name, updated_aliases = _person_values(
            full_name if full_name is not None else current.full_name,
            display_name if display_name is not None else current.display_name,
            preferred_name if preferred_name is not None else current.preferred_name,
            aliases if aliases is not None else current.aliases,
        )
        owner_user_id = current.owner_user_id
        if owner_user_id is None:
            raise ValueError("Person requires an immutable owner ID")
        with self._connect() as connection, connection.cursor() as cursor:
            selected_profile = None if clear_profile_asset else (profile_asset_id if profile_asset_id is not None else current.profile_asset_id)
            self._validate_profile_asset(cursor, owner_user_id, selected_profile)
            cursor.execute("""UPDATE vault_people SET display_name=%s,full_name=%s,preferred_name=%s,aliases=%s::jsonb,
                date_of_birth=%s,profile_asset_id=%s,profile_frame=%s::jsonb,active=COALESCE(%s,active),updated_at=CURRENT_TIMESTAMP
                WHERE id=%s AND owner_user_id=%s RETURNING *""", (
                updated_display_name, updated_full_name, updated_preferred_name,
                __import__("json").dumps(updated_aliases),
                None if clear_date_of_birth else (date_of_birth if date_of_birth is not None else current.date_of_birth),
                selected_profile,
                __import__("json").dumps(None if clear_profile_frame or clear_profile_asset else (profile_frame if profile_frame is not None else current.profile_frame)),
                active, person_id, owner_user_id,
            ))
            row = cursor.fetchone()
        return self._person(row) if row else None

    def associate(self, asset_id: UUID, person_id: UUID, source: AssociationSource, face_detection_id: UUID | None = None, created_by: str = "vault_master") -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_asset_people(id,asset_id,person_id,source,supporting_face_detection_id,created_by,owner_user_id)
                SELECT %s,assets.id,%s,%s,%s,%s,assets.owner_user_id FROM vault_assets assets
                WHERE assets.id=%s AND assets.owner_user_id IS NOT NULL
                ON CONFLICT(asset_id,person_id,source,supporting_face_detection_id) DO UPDATE SET active=TRUE,updated_at=CURRENT_TIMESTAMP""", (uuid4(), person_id, source, face_detection_id, created_by, asset_id))

    def decide(self, asset_id: UUID, person_id: UUID, decision: AssociationDecision, owner: UUID | str, source: str = "photo_presence") -> None:
        if self.get_person(person_id, owner) is None:
            raise ValueError("Person was not found")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_asset_people_decisions(id,asset_id,person_id,decision,decision_source,decided_by,owner_user_id)
                SELECT %s,assets.id,%s,%s,%s,%s,assets.owner_user_id FROM vault_assets assets
                WHERE assets.id=%s AND assets.owner_user_id IS NOT NULL
                ON CONFLICT(asset_id,person_id) DO UPDATE SET decision=EXCLUDED.decision,decision_source=EXCLUDED.decision_source,decided_by=EXCLUDED.decided_by,active=TRUE,updated_at=CURRENT_TIMESTAMP""", (uuid4(), person_id, decision, source, str(owner), asset_id))

    def effective_people(self, asset_id: UUID, owner: UUID | str) -> list[AssetPerson]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT DISTINCT ON (people.id) people.id,people.display_name,COALESCE(decisions.decision,associations.source) source FROM vault_people people LEFT JOIN vault_asset_people associations ON associations.person_id=people.id AND associations.asset_id=%s AND associations.active AND associations.owner_user_id=people.owner_user_id LEFT JOIN vault_asset_people_decisions decisions ON decisions.person_id=people.id AND decisions.asset_id=%s AND decisions.active AND decisions.owner_user_id=people.owner_user_id WHERE """ + ("people.owner_user_id=%s" if isinstance(owner, UUID) else "people.owner_username=%s") + " AND people.active AND (associations.id IS NOT NULL OR decisions.decision='include') AND COALESCE(decisions.decision,'') <> 'exclude' ORDER BY people.id, associations.updated_at DESC NULLS LAST", (asset_id, asset_id, owner))
            return [AssetPerson(asset_id, UUID(str(row["id"])), str(row["display_name"]), str(row["source"])) for row in cursor.fetchall()]

    def add_face_detection(self, asset_id: UUID, **evidence: object) -> UUID:
        detection_id = uuid4()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_face_detections(id,asset_id,producing_job_id,bounding_box,detector_provider,detector_model,detector_revision,task_version,embedding,embedding_dimension,embedding_model,embedding_revision,reference_person_id,recognition_candidate_person_id,native_distance,recognition_result,raw_evidence,owner_user_id) SELECT %s,assets.id,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,assets.owner_user_id FROM vault_assets assets WHERE assets.id=%s AND assets.owner_user_id IS NOT NULL""", (detection_id, evidence.get("producing_job_id"), __import__("json").dumps(evidence.get("bounding_box", {})), evidence.get("detector_provider", "yunet"), evidence.get("detector_model", "unknown"), evidence.get("detector_revision"), evidence.get("task_version", "gallery-people-v1"), evidence.get("embedding"), evidence.get("embedding_dimension"), evidence.get("embedding_model"), evidence.get("embedding_revision"), evidence.get("reference_person_id"), evidence.get("recognition_candidate_person_id"), evidence.get("native_distance"), evidence.get("recognition_result", "unknown"), __import__("json").dumps(evidence.get("raw_evidence", {})), asset_id))
        return detection_id

    def add_person_detection(self, asset_id: UUID, **evidence: object) -> UUID:
        detection_id = uuid4()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_person_detections(id,asset_id,producing_job_id,bounding_box,detector_provider,detector_model,detector_revision,task_version,raw_evidence,owner_user_id) SELECT %s,assets.id,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,assets.owner_user_id FROM vault_assets assets WHERE assets.id=%s AND assets.owner_user_id IS NOT NULL""", (detection_id, evidence.get("producing_job_id"), __import__("json").dumps(evidence.get("bounding_box", {})), evidence.get("detector_provider", "yolox"), evidence.get("detector_model", "unknown"), evidence.get("detector_revision"), evidence.get("task_version", "gallery-people-v1"), __import__("json").dumps(evidence.get("raw_evidence", {})), asset_id))
        return detection_id

    def reference_embeddings(self, owner: str) -> list[PersonReference]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT faces.reference_person_id,faces.embedding,faces.embedding_model,faces.embedding_revision FROM vault_face_detections faces JOIN vault_people people ON people.id=faces.reference_person_id WHERE people.owner_username=%s AND people.active AND faces.active AND faces.embedding IS NOT NULL", (owner,))
            return [PersonReference(UUID(str(row["reference_person_id"])), bytes(row["embedding"]), str(row["embedding_model"]), str(row["embedding_revision"]) if row["embedding_revision"] else None) for row in cursor.fetchall()]

    def reference_embeddings_by_user_id(self, owner_user_id: UUID | None) -> list[PersonReference]:
        if owner_user_id is None:
            return []
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT faces.reference_person_id,faces.embedding,faces.embedding_model,faces.embedding_revision
                FROM vault_face_detections faces JOIN vault_people people ON people.id=faces.reference_person_id
                WHERE people.owner_user_id=%s AND faces.owner_user_id=%s AND people.active AND faces.active
                AND faces.embedding IS NOT NULL""", (owner_user_id, owner_user_id))
            return [PersonReference(UUID(str(row["reference_person_id"])), bytes(row["embedding"]), str(row["embedding_model"]), str(row["embedding_revision"]) if row["embedding_revision"] else None) for row in cursor.fetchall()]

    def unknown_face_detections(
        self, asset_id: UUID, producing_job_id: UUID | None = None, legacy_since: datetime | None = None
    ) -> list[UUID]:
        return [
            face.id
            for face in self.face_detections_for_asset(asset_id, "", producing_job_id, legacy_since)
            if face.person_id is None
        ]

    def face_detections_for_asset(
        self,
        asset_id: UUID,
        owner: UUID | str,
        producing_job_id: UUID | None = None,
        legacy_since: datetime | None = None,
    ) -> list[FaceDetection]:
        with self._connect() as connection, connection.cursor() as cursor:
            owner_join = (
                "people.owner_user_id=%s AND faces.owner_user_id=%s"
                if isinstance(owner, UUID)
                else "people.owner_username=%s"
            )
            owner_params = (owner, owner) if isinstance(owner, UUID) else (owner,)
            if producing_job_id is not None:
                cursor.execute("""SELECT faces.id,faces.asset_id,faces.bounding_box,faces.reference_person_id,faces.recognition_candidate_person_id,people.display_name
                    FROM vault_face_detections faces
                    LEFT JOIN vault_people people ON people.id=COALESCE(faces.reference_person_id,faces.recognition_candidate_person_id) AND """ + owner_join + """
                    WHERE faces.asset_id=%s AND faces.active AND faces.producing_job_id=%s ORDER BY faces.created_at""", owner_params + (asset_id, producing_job_id))
                rows = cursor.fetchall()
                if rows:
                    return [FaceDetection(UUID(str(row["id"])), UUID(str(row["asset_id"])), dict(row["bounding_box"]), UUID(str(row["reference_person_id"] or row["recognition_candidate_person_id"])) if row["reference_person_id"] or row["recognition_candidate_person_id"] else None, str(row["display_name"]) if row["display_name"] else None, bool(row["reference_person_id"])) for row in rows]
            if legacy_since is not None:
                cursor.execute("""SELECT faces.id,faces.asset_id,faces.bounding_box,faces.reference_person_id,faces.recognition_candidate_person_id,people.display_name
                    FROM vault_face_detections faces
                    LEFT JOIN vault_people people ON people.id=COALESCE(faces.reference_person_id,faces.recognition_candidate_person_id) AND """ + owner_join + """
                    WHERE faces.asset_id=%s AND faces.active AND faces.producing_job_id IS NULL AND faces.created_at >= %s
                    ORDER BY faces.created_at""", owner_params + (asset_id, legacy_since))
            else:
                cursor.execute("""SELECT faces.id,faces.asset_id,faces.bounding_box,faces.reference_person_id,faces.recognition_candidate_person_id,people.display_name
                FROM vault_face_detections faces
                LEFT JOIN vault_people people ON people.id=COALESCE(faces.reference_person_id,faces.recognition_candidate_person_id) AND """ + owner_join + """
                WHERE faces.asset_id=%s AND faces.active ORDER BY faces.created_at""", owner_params + (asset_id,))
            return [FaceDetection(UUID(str(row["id"])), UUID(str(row["asset_id"])), dict(row["bounding_box"]), UUID(str(row["reference_person_id"] or row["recognition_candidate_person_id"])) if row["reference_person_id"] or row["recognition_candidate_person_id"] else None, str(row["display_name"]) if row["display_name"] else None, bool(row["reference_person_id"])) for row in cursor.fetchall()]

    def confirm_face_reference(self, detection_id: UUID, person_id: UUID, owner: UUID | str) -> None:
        if self.get_person(person_id, owner) is None:
            raise ValueError("Person was not found")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_face_detections SET reference_person_id=%s,recognition_result='known' WHERE id=%s AND active RETURNING id", (person_id, detection_id))
            if cursor.fetchone() is None:
                raise ValueError("Face evidence was not found")

    def identify_face(self, asset_id: UUID, detection_id: UUID, person_id: UUID, owner: UUID | str) -> None:
        if not isinstance(owner, UUID):
            raise ValueError("Face identification requires an immutable owner ID")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT owner_user_id FROM vault_assets WHERE id=%s AND owner_user_id=%s", (asset_id, owner))
            asset = cursor.fetchone()
            if asset is None:
                raise ValueError("Asset owner was not found")
            asset_owner_user_id = UUID(str(asset["owner_user_id"]))
            cursor.execute("SELECT id FROM vault_people WHERE id=%s AND owner_user_id=%s AND active", (person_id, asset_owner_user_id))
            if cursor.fetchone() is None:
                raise ValueError("Person was not found")
            cursor.execute("SELECT reference_person_id FROM vault_face_detections WHERE id=%s AND asset_id=%s AND owner_user_id=%s AND active", (detection_id, asset_id, asset_owner_user_id))
            previous = cursor.fetchone()
            if previous is None:
                raise ValueError("Face evidence was not found")
            previous_person_id = previous["reference_person_id"]
            cursor.execute("UPDATE vault_face_detections SET reference_person_id=%s,recognition_result='known' WHERE id=%s AND owner_user_id=%s", (person_id, detection_id, asset_owner_user_id))
            cursor.execute("UPDATE vault_asset_people SET active=FALSE,updated_at=CURRENT_TIMESTAMP WHERE asset_id=%s AND owner_user_id=%s AND source='user_face' AND supporting_face_detection_id=%s", (asset_id, asset_owner_user_id, detection_id))
            cursor.execute("""INSERT INTO vault_asset_people(id,asset_id,person_id,source,supporting_face_detection_id,created_by,owner_user_id)
                SELECT %s,assets.id,%s,'user_face',%s,%s,assets.owner_user_id FROM vault_assets assets
                WHERE assets.id=%s AND assets.owner_user_id=%s
                ON CONFLICT(asset_id,person_id,source,supporting_face_detection_id) DO UPDATE SET active=TRUE,updated_at=CURRENT_TIMESTAMP""", (uuid4(), person_id, detection_id, str(owner), asset_id, asset_owner_user_id))
            # Exact face identification is a later explicit user assertion of
            # photo-level presence, so it replaces any prior include/exclude
            # decision for this asset and Person.
            cursor.execute("""INSERT INTO vault_asset_people_decisions(id,asset_id,person_id,decision,decision_source,decided_by,owner_user_id)
                SELECT %s,assets.id,%s,'include','face_identification',%s,assets.owner_user_id FROM vault_assets assets
                WHERE assets.id=%s AND assets.owner_user_id=%s
                ON CONFLICT(asset_id,person_id) DO UPDATE SET decision='include',decision_source='face_identification',decided_by=EXCLUDED.decided_by,active=TRUE,updated_at=CURRENT_TIMESTAMP""", (uuid4(), person_id, str(owner), asset_id, asset_owner_user_id))
            if previous_person_id and previous_person_id != person_id:
                cursor.execute("""UPDATE vault_asset_people_decisions decisions SET active=FALSE,updated_at=CURRENT_TIMESTAMP
                    WHERE decisions.asset_id=%s AND decisions.person_id=%s
                      AND decisions.decision_source='face_identification'
                      AND NOT EXISTS (
                        SELECT 1 FROM vault_asset_people associations
                        WHERE associations.asset_id=%s AND associations.person_id=%s
                          AND associations.source='user_face' AND associations.active
                      )""", (asset_id, previous_person_id, asset_id, previous_person_id))

    def clear_face_identity(self, asset_id: UUID, detection_id: UUID, owner: UUID | str) -> None:
        if not isinstance(owner, UUID):
            raise ValueError("Face identification requires an immutable owner ID")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT faces.id FROM vault_face_detections faces WHERE faces.id=%s AND faces.asset_id=%s AND faces.owner_user_id=%s AND faces.active", (detection_id, asset_id, owner))
            if cursor.fetchone() is None:
                raise ValueError("Face evidence was not found")
            cursor.execute("UPDATE vault_face_detections SET reference_person_id=NULL,recognition_result='unknown' WHERE id=%s AND owner_user_id=%s", (detection_id, owner))
            cursor.execute("UPDATE vault_asset_people SET active=FALSE,updated_at=CURRENT_TIMESTAMP WHERE asset_id=%s AND owner_user_id=%s AND source='user_face' AND supporting_face_detection_id=%s", (asset_id, owner, detection_id))

    def matching_asset_ids(self, person_ids: tuple[UUID, ...], owner: UUID) -> set[UUID]:
        if not person_ids:
            return set()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT DISTINCT asset_id FROM (
                SELECT associations.asset_id, associations.person_id, decisions.decision
                FROM vault_asset_people associations
                LEFT JOIN vault_asset_people_decisions decisions ON decisions.asset_id=associations.asset_id AND decisions.person_id=associations.person_id AND decisions.active
                WHERE associations.active
                UNION
                SELECT decisions.asset_id, decisions.person_id, decisions.decision
                FROM vault_asset_people_decisions decisions WHERE decisions.active
            ) candidates
            JOIN vault_people people ON people.id=candidates.person_id
            WHERE people.owner_user_id=%s AND people.active AND candidates.person_id = ANY(%s) AND COALESCE(candidates.decision,'') <> 'exclude'""", (owner, list(person_ids)))
            return {UUID(str(row["asset_id"])) for row in cursor.fetchall()}

    def search_people(self, owner: UUID | str, query: str) -> list[VaultPerson]:
        needle = query.strip()
        if not needle:
            return self.list_people(owner)
        owner_clause = "owner_user_id=%s" if isinstance(owner, UUID) else "owner_username=%s"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vault_people WHERE " + owner_clause + " AND active AND ("
                "full_name ILIKE %s OR preferred_name ILIKE %s OR EXISTS ("
                "SELECT 1 FROM jsonb_array_elements_text(aliases) alias WHERE alias ILIKE %s"
                ")) ORDER BY lower(full_name), id",
                (owner, f"%{needle}%", f"%{needle}%", f"%{needle}%"),
            )
            return [self._person(row) for row in cursor.fetchall()]

    def resolve_me_person(self, user_id: UUID) -> VaultPerson | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT people.* FROM user_me_people mapping
                JOIN vault_people people ON people.id=mapping.person_id
                WHERE mapping.user_id=%s AND people.owner_user_id=%s AND people.active""", (user_id, user_id))
            row = cursor.fetchone()
        return self._person(row) if row else None

    def set_me_person(self, user_id: UUID, person_id: UUID) -> VaultPerson:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_people WHERE id=%s AND owner_user_id=%s AND active", (person_id, user_id))
            row = cursor.fetchone()
            if row is None:
                raise ValueError("Person was not found")
            cursor.execute("""INSERT INTO user_me_people(user_id,person_id)
                VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE
                SET person_id=EXCLUDED.person_id,updated_at=CURRENT_TIMESTAMP""", (user_id, person_id))
        return self._person(row)

    def clear_me_person(self, user_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_me_people WHERE user_id=%s", (user_id,))

    @staticmethod
    def _relationship(row: dict[str, object]) -> PersonRelationship:
        return PersonRelationship(
            UUID(str(row["id"])), UUID(str(row["owner_user_id"])),
            UUID(str(row["subject_person_id"])), UUID(str(row["related_person_id"])),
            str(row["relationship_label"]), row["created_at"], row["updated_at"],
        )  # type: ignore[arg-type]

    def set_relationship(
        self, owner_user_id: UUID, subject_person_id: UUID, related_person_id: UUID,
        relationship_label: str,
    ) -> PersonRelationship:
        label = relationship_label.strip()
        if not label:
            raise ValueError("Relationship label is required")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS count FROM vault_people WHERE owner_user_id=%s AND active AND id=ANY(%s)", (owner_user_id, [subject_person_id, related_person_id]))
            if int(cursor.fetchone()["count"]) != 2:
                raise ValueError("People relationship was not found")
            cursor.execute("""INSERT INTO person_relationships(id,owner_user_id,subject_person_id,related_person_id,relationship_label)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(owner_user_id,subject_person_id,related_person_id)
                DO UPDATE SET relationship_label=EXCLUDED.relationship_label,updated_at=CURRENT_TIMESTAMP RETURNING *""", (
                uuid4(), owner_user_id, subject_person_id, related_person_id, label,
            ))
            return self._relationship(cursor.fetchone())

    def relationships_for_person(self, owner_user_id: UUID, subject_person_id: UUID) -> list[PersonRelationship]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT relationships.* FROM person_relationships relationships
                JOIN vault_people subject ON subject.id=relationships.subject_person_id
                WHERE relationships.owner_user_id=%s AND relationships.subject_person_id=%s
                AND subject.owner_user_id=%s ORDER BY lower(relationships.relationship_label),relationships.related_person_id""", (
                owner_user_id, subject_person_id, owner_user_id,
            ))
            return [self._relationship(row) for row in cursor.fetchall()]

    def clear_relationship(self, owner_user_id: UUID, subject_person_id: UUID, related_person_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""DELETE FROM person_relationships WHERE owner_user_id=%s
                AND subject_person_id=%s AND related_person_id=%s""", (
                owner_user_id, subject_person_id, related_person_id,
            ))

    def merge_people(self, source_person_id: UUID, target_person_id: UUID, owner_user_id: UUID) -> VaultPerson:
        if source_person_id == target_person_id:
            raise ValueError("A Person cannot be merged into itself")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_people WHERE id=ANY(%s) AND owner_user_id=%s AND active FOR UPDATE", ([source_person_id, target_person_id], owner_user_id))
            rows = {UUID(str(row["id"])): row for row in cursor.fetchall()}
            if set(rows) != {source_person_id, target_person_id}:
                raise ValueError("People merge was not found")
            cursor.execute("""SELECT source.asset_id FROM vault_asset_people_decisions source
                JOIN vault_asset_people_decisions target ON target.asset_id=source.asset_id AND target.person_id=%s AND target.active
                WHERE source.person_id=%s AND source.active AND source.decision <> target.decision LIMIT 1""", (target_person_id, source_person_id))
            if cursor.fetchone() is not None:
                raise ValueError("People merge has conflicting user decisions")
            cursor.execute("""SELECT 1 FROM person_relationships source
                JOIN person_relationships target ON target.owner_user_id=source.owner_user_id
                    AND target.subject_person_id=CASE WHEN source.subject_person_id=%s THEN %s ELSE source.subject_person_id END
                    AND target.related_person_id=CASE WHEN source.related_person_id=%s THEN %s ELSE source.related_person_id END
                WHERE source.owner_user_id=%s AND (source.subject_person_id=%s OR source.related_person_id=%s)
                    AND source.relationship_label <> target.relationship_label LIMIT 1""", (source_person_id, target_person_id, source_person_id, target_person_id, owner_user_id, source_person_id, source_person_id))
            if cursor.fetchone() is not None:
                raise ValueError("People merge has conflicting relationships")
            cursor.execute("""DELETE FROM vault_asset_people target USING vault_asset_people source
                WHERE target.person_id=%s AND source.person_id=%s AND target.asset_id=source.asset_id
                  AND source.active AND target.source=source.source AND target.supporting_face_detection_id IS NOT DISTINCT FROM source.supporting_face_detection_id""", (target_person_id, source_person_id))
            cursor.execute("UPDATE vault_asset_people SET person_id=%s,updated_at=CURRENT_TIMESTAMP WHERE person_id=%s AND active", (target_person_id, source_person_id))
            cursor.execute("DELETE FROM vault_asset_people_decisions target USING vault_asset_people_decisions source WHERE target.person_id=%s AND source.person_id=%s AND source.active AND target.asset_id=source.asset_id", (target_person_id, source_person_id))
            cursor.execute("UPDATE vault_asset_people_decisions SET person_id=%s,updated_at=CURRENT_TIMESTAMP WHERE person_id=%s AND active", (target_person_id, source_person_id))
            cursor.execute("UPDATE vault_face_detections SET reference_person_id=%s WHERE reference_person_id=%s", (target_person_id, source_person_id))
            cursor.execute("UPDATE vault_face_detections SET recognition_candidate_person_id=%s WHERE recognition_candidate_person_id=%s", (target_person_id, source_person_id))
            cursor.execute("UPDATE user_me_people SET person_id=%s,updated_at=CURRENT_TIMESTAMP WHERE user_id=%s AND person_id=%s", (target_person_id, owner_user_id, source_person_id))
            cursor.execute("DELETE FROM person_relationships WHERE owner_user_id=%s AND subject_person_id=%s AND related_person_id=%s", (owner_user_id, source_person_id, target_person_id))
            cursor.execute("DELETE FROM person_relationships WHERE owner_user_id=%s AND subject_person_id=%s AND related_person_id=%s", (owner_user_id, target_person_id, source_person_id))
            cursor.execute("UPDATE person_relationships SET subject_person_id=%s WHERE owner_user_id=%s AND subject_person_id=%s", (target_person_id, owner_user_id, source_person_id))
            cursor.execute("UPDATE person_relationships SET related_person_id=%s WHERE owner_user_id=%s AND related_person_id=%s", (target_person_id, owner_user_id, source_person_id))
            cursor.execute("INSERT INTO person_merge_history(source_person_id,target_person_id,owner_user_id) VALUES(%s,%s,%s)", (source_person_id, target_person_id, owner_user_id))
            cursor.execute("UPDATE vault_people SET active=FALSE,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s", (source_person_id, owner_user_id))
            return self._person(rows[target_person_id])

    def person_detection_count(self, asset_id: UUID, producing_job_id: UUID | None = None) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS count FROM vault_person_detections WHERE asset_id=%s "
                + ("AND producing_job_id=%s" if producing_job_id else ""),
                (asset_id, producing_job_id) if producing_job_id else (asset_id,),
            )
            return int(cursor.fetchone()["count"])


@lru_cache
def get_gallery_people_store():
    return PostgresGalleryPeopleStore(get_database_conninfo())
