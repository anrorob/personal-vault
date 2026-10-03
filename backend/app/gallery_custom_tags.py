"""Private, user-owned Gallery custom tags.

Generated Gallery Intelligence terms deliberately remain in their established
shared vocabulary.  This layer is scoped by immutable user UUIDs and is never
returned outside the requesting user's current asset access boundary.
"""
from dataclasses import dataclass
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from app.gallery_intelligence import custom_tag_identity


@dataclass(frozen=True)
class CustomTag:
    id: UUID
    owner_user_id: UUID
    slug: str
    display_name: str


class MemoryGalleryCustomTagStore:
    def __init__(self) -> None:
        self.tags: dict[UUID, CustomTag] = {}
        self.assignments: set[tuple[UUID, UUID, UUID]] = set()
    def matching_asset_ids(self, user_id: UUID, tag_ids: tuple[UUID, ...]) -> set[UUID]:
        owned = {tag.id for tag in self.list(user_id)}
        if not set(tag_ids) <= owned:
            return set()
        return {asset for owner, tag, asset in self.assignments if owner == user_id and tag in tag_ids}
    def initialize(self) -> None: pass
    def list(self, user_id: UUID) -> list[CustomTag]: return sorted((tag for tag in self.tags.values() if tag.owner_user_id == user_id), key=lambda tag: (tag.display_name.casefold(), str(tag.id)))
    def create(self, user_id: UUID, name: str) -> CustomTag:
        slug, display = custom_tag_identity(name)
        existing = next((tag for tag in self.tags.values() if tag.owner_user_id == user_id and tag.slug == slug), None)
        tag = CustomTag(existing.id if existing else uuid4(), user_id, slug, display)
        self.tags[tag.id] = tag
        return tag
    def assign(self, user_id: UUID, tag_id: UUID, asset_id: UUID) -> None:
        if tag_id not in self.tags or self.tags[tag_id].owner_user_id != user_id: raise ValueError("Custom tag was not found")
        self.assignments.add((user_id, tag_id, asset_id))
    def rename(self, user_id: UUID, tag_id: UUID, name: str) -> CustomTag:
        tag = self.tags.get(tag_id)
        if not tag or tag.owner_user_id != user_id: raise ValueError("Custom tag was not found")
        slug, display = custom_tag_identity(name); updated = CustomTag(tag.id, user_id, slug, display); self.tags[tag_id] = updated; return updated
    def delete(self, user_id: UUID, tag_id: UUID) -> None:
        tag = self.tags.get(tag_id)
        if not tag or tag.owner_user_id != user_id: raise ValueError("Custom tag was not found")
        self.tags.pop(tag_id); self.assignments = {item for item in self.assignments if item[1] != tag_id}
    def unassign(self, user_id: UUID, tag_id: UUID, asset_id: UUID) -> None: self.assignments.discard((user_id, tag_id, asset_id))
    def for_asset(self, user_id: UUID, asset_id: UUID) -> "list[CustomTag]": return [self.tags[tag_id] for owner, tag_id, assigned_asset in self.assignments if owner == user_id and assigned_asset == asset_id]


class PostgresGalleryCustomTagStore:
    def __init__(self, conninfo: str) -> None: self._conninfo = conninfo
    def _connect(self): return psycopg.connect(self._conninfo, row_factory=dict_row)
    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS user_gallery_custom_tags (
                id UUID PRIMARY KEY, owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                slug TEXT NOT NULL, display_name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(owner_user_id, slug))""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS user_gallery_custom_tag_assignments (
                tag_id UUID NOT NULL REFERENCES user_gallery_custom_tags(id) ON DELETE CASCADE,
                asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE,
                owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id), created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(tag_id, asset_id), CHECK (owner_user_id IS NOT NULL))""")
            cursor.execute("CREATE INDEX IF NOT EXISTS user_gallery_custom_tag_assignments_owner_asset_idx ON user_gallery_custom_tag_assignments(owner_user_id, asset_id)")
    def matching_asset_ids(self, user_id: UUID, tag_ids: tuple[UUID, ...]) -> set[UUID]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM user_gallery_custom_tags WHERE owner_user_id=%s AND id=ANY(%s)", (user_id, list(tag_ids)))
            if {row["id"] for row in cursor.fetchall()} != set(tag_ids):
                return set()
            cursor.execute("""SELECT DISTINCT assignment.asset_id FROM user_gallery_custom_tag_assignments assignment
                JOIN user_gallery_custom_tags tag ON tag.id=assignment.tag_id
                WHERE assignment.owner_user_id=%s AND tag.owner_user_id=%s AND tag.id=ANY(%s)""", (user_id, user_id, list(tag_ids)))
            return {row["asset_id"] for row in cursor.fetchall()}
    def list(self, user_id: UUID) -> list[CustomTag]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,owner_user_id,slug,display_name FROM user_gallery_custom_tags WHERE owner_user_id=%s ORDER BY lower(display_name),id", (user_id,))
            return [CustomTag(UUID(str(r['id'])), UUID(str(r['owner_user_id'])), str(r['slug']), str(r['display_name'])) for r in cursor.fetchall()]
    def create(self, user_id: UUID, name: str) -> CustomTag:
        slug, display = custom_tag_identity(name)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO user_gallery_custom_tags(id,owner_user_id,slug,display_name) VALUES(%s,%s,%s,%s)
                ON CONFLICT(owner_user_id,slug) DO UPDATE SET display_name=EXCLUDED.display_name,updated_at=CURRENT_TIMESTAMP
                RETURNING id,owner_user_id,slug,display_name""", (uuid4(), user_id, slug, display))
            r=cursor.fetchone(); return CustomTag(UUID(str(r['id'])),UUID(str(r['owner_user_id'])),str(r['slug']),str(r['display_name']))
    def assign(self, user_id: UUID, tag_id: UUID, asset_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO user_gallery_custom_tag_assignments(tag_id,asset_id,owner_user_id) SELECT id,%s,%s FROM user_gallery_custom_tags WHERE id=%s AND owner_user_id=%s ON CONFLICT DO NOTHING", (asset_id,user_id,tag_id,user_id))
            if cursor.rowcount != 1: raise ValueError("Custom tag was not found")
    def rename(self, user_id: UUID, tag_id: UUID, name: str) -> CustomTag:
        slug, display = custom_tag_identity(name)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE user_gallery_custom_tags SET slug=%s,display_name=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s RETURNING id,owner_user_id,slug,display_name", (slug,display,tag_id,user_id))
            row=cursor.fetchone()
            if not row: raise ValueError("Custom tag was not found")
            return CustomTag(UUID(str(row['id'])),UUID(str(row['owner_user_id'])),str(row['slug']),str(row['display_name']))
    def delete(self, user_id: UUID, tag_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_gallery_custom_tags WHERE id=%s AND owner_user_id=%s", (tag_id,user_id))
            if cursor.rowcount != 1: raise ValueError("Custom tag was not found")
    def unassign(self, user_id: UUID, tag_id: UUID, asset_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_gallery_custom_tag_assignments WHERE tag_id=%s AND asset_id=%s AND owner_user_id=%s", (tag_id,asset_id,user_id))
    def for_asset(self, user_id: UUID, asset_id: UUID) -> "list[CustomTag]":
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT tag.id,tag.owner_user_id,tag.slug,tag.display_name FROM user_gallery_custom_tags tag JOIN user_gallery_custom_tag_assignments assignment ON assignment.tag_id=tag.id WHERE assignment.asset_id=%s AND assignment.owner_user_id=%s AND tag.owner_user_id=%s ORDER BY lower(tag.display_name),tag.id", (asset_id,user_id,user_id))
            return [CustomTag(UUID(str(r['id'])),UUID(str(r['owner_user_id'])),str(r['slug']),str(r['display_name'])) for r in cursor.fetchall()]


def get_gallery_custom_tag_store():
    from app.config import get_database_conninfo
    return PostgresGalleryCustomTagStore(get_database_conninfo())
