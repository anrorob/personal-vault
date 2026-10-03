"""Owner-declared album identity, independent of filenames and enrichment."""
from dataclasses import dataclass, replace
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from app.music_order import initialize_music_order, membership_changed, metadata_sequence, sequence_result, write_sequence


@dataclass(frozen=True)
class MusicAlbum:
    id: UUID
    owner_user_id: UUID
    import_group_id: UUID
    artist_name: str
    album_title: str


def album_intent(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"import_group_id", "artist_name", "album_title"}:
        raise ValueError("Music grouping requires import_group_id, artist_name and album_title")
    result = {"import_group_id": str(UUID(str(value["import_group_id"])))}
    for field in ("artist_name", "album_title"):
        text = value[field]
        if not isinstance(text, str) or len(text) > 300 or any(ord(c) < 32 for c in text):
            raise ValueError(f"Music {field} is invalid")
        result[field] = text.strip()
    return result


def supplier_manual_album(context, media_type, installation_id):
    """Adapt the current Supplier's per-selection root UUID, never a watched root.

    ManualUploadFolderScanner.ScanDetailed creates SourceGroupId for each root
    on each new preparation; retries retain it. Individually selected files and
    Automatic Sources do not carry this album-selection authority.
    """
    if context.get("music_album") is not None or not (media_type or "").startswith("audio/"):
        return context
    if context.get("source_kind") != "manual_upload" or not context.get("source_id"):
        return context
    source = UUID(str(context["source_id"]))
    installation = UUID(str(installation_id))
    label, relative = context.get("source_label"), context.get("relative_path")
    if not isinstance(label, str) or not label.strip() or not isinstance(relative, str) or "/" in relative or "\\" in relative:
        raise ValueError("Manual Music folder provenance is incomplete")
    parts = label.split(" - ")
    artist, title = (part.strip() for part in parts) if len(parts) == 2 and all(part.strip() for part in parts) else ("", "")
    return {**context, "music_album": {
        "import_group_id": str(uuid5(installation, f"pv-manual-music-import-v1:{source}")),
        "artist_name": artist, "album_title": title,
    }}


def declared_album(owner: UUID, intent: object) -> MusicAlbum:
    if not isinstance(owner, UUID):
        raise ValueError("Music grouping requires an immutable owner")
    value = album_intent(intent)
    source = UUID(value["import_group_id"])
    return MusicAlbum(uuid5(owner, f"pv-music-album-v1:{source}"), owner, source, value["artist_name"], value["album_title"])


def item_album(item):
    context = item.metadata.get("source_context")
    value = context.get("music_album") if isinstance(context, dict) else None
    if value is None:
        return None
    if item.proposed_category != "Music" or not item.mime_type.startswith("audio/"):
        raise ValueError("Declared Music album members must publish as Music audio")
    return declared_album(item.owner_user_id, value)


def initialize_music_groups(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_music_albums (
        id UUID PRIMARY KEY, owner_user_id UUID NOT NULL, import_group_id UUID NOT NULL,
        artist_name TEXT NOT NULL, album_title TEXT NOT NULL,
        import_intent JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_user_id, import_group_id), UNIQUE(id, owner_user_id))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_music_album_members (
        asset_id UUID PRIMARY KEY REFERENCES vault_assets(id) ON DELETE CASCADE,
        album_id UUID NOT NULL, owner_user_id UUID NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(album_id, owner_user_id) REFERENCES vault_music_albums(id, owner_user_id))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS vault_music_album_history (
        id BIGSERIAL PRIMARY KEY, album_id UUID NOT NULL REFERENCES vault_music_albums(id),
        actor_user_id UUID NOT NULL, action TEXT NOT NULL, details JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    initialize_music_order(cursor)


def migrate_music_local_visibility(cursor, local_vault_id):
    """One-time, audited permission migration; never infer historical albums."""
    cursor.execute("CREATE TABLE IF NOT EXISTS vault_music_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    cursor.execute("INSERT INTO vault_music_migrations(version) VALUES('local-default-v1') ON CONFLICT DO NOTHING RETURNING version")
    if cursor.fetchone() is None:
        return
    cursor.execute("""UPDATE vault_assets asset SET visibility='vault-wide', updated_at=CURRENT_TIMESTAMP
        WHERE asset_type='Music' AND visibility='private' AND lifecycle_state='active'
          AND owner_user_id IS NOT NULL AND origin_vault_id=%s
          AND EXISTS (SELECT 1 FROM vault_files file
                      WHERE file.asset_id=asset.id AND file.file_role='primary')
          AND NOT EXISTS (SELECT 1 FROM vault_share_grants grant_record WHERE grant_record.asset_id=asset.id)
          AND NOT EXISTS (SELECT 1 FROM vault_asset_history history
                          WHERE history.asset_id=asset.id
                            AND history.action IN ('access_policy_updated', 'permanently_deleted'))
        RETURNING id, owner_user_id""", (local_vault_id,))
    for row in cursor.fetchall():
        cursor.execute("""INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values)
            VALUES(%s,%s,'music_local_default_migrated','Music grouping migration',%s,%s)""",
            (uuid5(row["id"], "music-local-default-v1"), row["id"], Jsonb({"visibility": "private"}), Jsonb({"visibility": "vault-wide", "owner_user_id": str(row["owner_user_id"])})))


def album_from_row(row):
    return MusicAlbum(**{key: row[key] for key in MusicAlbum.__dataclass_fields__}) if row else None


def ensure_album(cursor, album):
    intent = {"artist_name": album.artist_name, "album_title": album.album_title}
    cursor.execute("""INSERT INTO vault_music_albums(id,owner_user_id,import_group_id,artist_name,album_title,import_intent)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id""",
        (album.id, album.owner_user_id, album.import_group_id, album.artist_name, album.album_title, Jsonb(intent)))
    created = cursor.fetchone() is not None
    cursor.execute("SELECT * FROM vault_music_albums WHERE id=%s FOR UPDATE", (album.id,))
    row = cursor.fetchone()
    if row["owner_user_id"] != album.owner_user_id or row["import_intent"] != intent:
        raise ValueError("This Music import group already has different declared identity")
    if created:
        cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'declared',%s)", (album.id, album.owner_user_id, Jsonb(intent)))
    return album_from_row(row)


def bind_members(cursor, album, asset_ids):
    cursor.execute("SELECT id, owner_user_id, asset_type, EXISTS (SELECT 1 FROM vault_files file WHERE file.asset_id=vault_assets.id AND file.mime_type LIKE 'audio/%%') AS audio FROM vault_assets WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (asset_ids,))
    rows = cursor.fetchall()
    if len(rows) != len(set(asset_ids)) or any(row["owner_user_id"] != album.owner_user_id or row["asset_type"] != "Music" or not row["audio"] for row in rows):
        raise ValueError("Album membership requires owned Music assets")
    changed = False
    for asset_id in asset_ids:
        cursor.execute("INSERT INTO vault_music_album_members(asset_id,album_id,owner_user_id) VALUES(%s,%s,%s) ON CONFLICT(asset_id) DO NOTHING RETURNING asset_id", (asset_id, album.id, album.owner_user_id))
        created = cursor.fetchone() is not None
        cursor.execute("SELECT album_id FROM vault_music_album_members WHERE asset_id=%s", (asset_id,))
        if cursor.fetchone()["album_id"] != album.id:
            raise ValueError("A track already belongs to another explicit album")
        if created:
            changed = True
            cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'member_added',%s)", (album.id, album.owner_user_id, Jsonb({"asset_id": str(asset_id)})))

    if changed:
        membership_changed(cursor, album)


class PostgresMusicGroups:
    def declare_music_album(self, owner, intent):
        with self._connect() as connection, connection.cursor() as cursor:
            return ensure_album(cursor, declared_album(owner, intent))

    def get_music_album(self, album_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_music_albums WHERE id=%s", (album_id,))
            return album_from_row(cursor.fetchone())

    def get_asset_music_album(self, asset_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT album.* FROM vault_music_albums album JOIN vault_music_album_members member ON member.album_id=album.id JOIN vault_assets asset ON asset.id=member.asset_id AND asset.owner_user_id=member.owner_user_id WHERE member.asset_id=%s", (asset_id,))
            return album_from_row(cursor.fetchone())

    def music_album_asset_ids(self, album_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT asset_id FROM vault_music_album_members WHERE album_id=%s ORDER BY asset_id", (album_id,))
            return [row["asset_id"] for row in cursor.fetchall()]

    def bind_music_album(self, album_id, owner, asset_ids):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_music_albums WHERE id=%s AND owner_user_id=%s FOR UPDATE", (album_id, owner))
            album = album_from_row(cursor.fetchone())
            if album is None:
                raise ValueError("Music album not found")
            bind_members(cursor, album, asset_ids)

    def get_music_album_order(self, album_id):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT asset_id,playback_position FROM vault_music_album_members WHERE album_id=%s", (album_id,))
            result = sequence_result([(row['asset_id'], row['playback_position']) for row in cursor.fetchall()])
            cursor.execute("""SELECT 1 FROM vault_master_items item JOIN vault_music_albums album
                ON item.owner_user_id=album.owner_user_id
                AND item.metadata->'source_context'->'music_album'->>'import_group_id'=album.import_group_id::text
                WHERE album.id=%s AND item.source_kind='incoming'
                AND item.state NOT IN ('moved','arrival_removed','duplicate_removed') LIMIT 1""", (album_id,))
            if cursor.fetchone():
                result.update(state='unresolved', asset_ids=[])
            return result

    def set_music_album_order(self, album_id, owner, asset_ids):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM vault_music_albums WHERE id=%s AND owner_user_id=%s FOR UPDATE", (album_id, owner))
            if cursor.fetchone() is None:
                raise ValueError("Music album not found")
            write_sequence(cursor, album_id, owner, asset_ids, 'explicit')
        return self.get_music_album_order(album_id)

    def correct_music_album(self, album_id, owner, artist, title):
        album_intent({"import_group_id": str(album_id), "artist_name": artist, "album_title": title})
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_music_albums SET artist_name=%s, album_title=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s RETURNING *", (artist.strip(), title.strip(), album_id, owner))
            album = album_from_row(cursor.fetchone())
            if album is None:
                raise ValueError("Music album not found")
            cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'identity_corrected',%s)", (album.id, owner, Jsonb({"artist_name": album.artist_name, "album_title": album.album_title})))
            return album


class MemoryMusicGroups:
    def _music_state(self):
        if not hasattr(self, "_music_albums"):
            self._music_albums, self._music_intents, self._music_members, self.music_album_history = {}, {}, {}, []
            self._music_positions, self._music_order_sources = {}, {}

    def declare_music_album(self, owner, intent):
        self._music_state()
        album = declared_album(owner, intent)
        if album.id in self._music_albums:
            if self._music_intents[album.id] != album:
                raise ValueError("This Music import group already has different declared identity")
            return self._music_albums[album.id]
        self._music_albums[album.id] = self._music_intents[album.id] = album
        self.music_album_history.append((album.id, owner, "declared"))
        return album

    def get_music_album(self, album_id):
        self._music_state()
        return self._music_albums.get(album_id)

    def get_asset_music_album(self, asset_id):
        self._music_state()
        return self._music_albums.get(self._music_members.get(asset_id))

    def music_album_asset_ids(self, album_id):
        self._music_state()
        return [asset_id for asset_id, member_album in self._music_members.items() if member_album == album_id]

    def bind_music_album(self, album_id, owner, asset_ids):
        album = self.get_music_album(album_id)
        if album is None or album.owner_user_id != owner:
            raise ValueError("Music album not found")
        for asset_id in asset_ids:
            asset = self.get_catalogued_asset_by_id(asset_id)
            if asset is None or asset.owner_user_id != owner or asset.asset_type != "Music" or not asset.mime_type.startswith("audio/"):
                raise ValueError("Album membership requires owned Music assets")
            if self._music_members.get(asset_id, album_id) != album_id:
                raise ValueError("A track already belongs to another explicit album")
        changed = False
        for asset_id in asset_ids:
            if asset_id not in self._music_members:
                changed = True
                self._music_members[asset_id] = album_id
                self.music_album_history.append((album_id, owner, "member_added", asset_id))

        if changed:
            members = self.music_album_asset_ids(album_id)
            for asset_id in members:
                self._music_positions[asset_id] = None
            if self._music_order_sources.get(album_id) != 'explicit':
                sequence = metadata_sequence([(i, self.get_catalogued_asset_by_id(i).effective_metadata) for i in members])
                self._music_order_sources[album_id] = 'metadata' if sequence else 'unresolved'
                for position, asset_id in enumerate(sequence or [], 1):
                    self._music_positions[asset_id] = position
            self.music_album_history.append((album_id, owner, 'order_membership_changed'))

    def get_music_album_order(self, album_id):
        members = self.music_album_asset_ids(album_id)
        result = sequence_result([(i, self._music_positions.get(i)) for i in members])
        album = self.get_music_album(album_id)
        if album and any(item.source_kind == 'incoming' and item.owner_user_id == album.owner_user_id
               and item.state not in {'moved', 'arrival_removed', 'duplicate_removed'}
               and item.metadata.get('source_context', {}).get('music_album', {}).get('import_group_id') == str(album.import_group_id)
               for item in self.list_items()):
            result.update(state='unresolved', asset_ids=[])
        return result

    def set_music_album_order(self, album_id, owner, asset_ids):
        album = self.get_music_album(album_id)
        if album is None or album.owner_user_id != owner:
            raise ValueError("Music album not found")
        if not asset_ids or len(asset_ids) != len(set(asset_ids)) or set(asset_ids) != set(self.music_album_asset_ids(album_id)):
            raise ValueError("Album order must include every current member exactly once")
        if self.get_music_album_order(album_id)['asset_ids'] != asset_ids or self._music_order_sources.get(album_id) != 'explicit':
            for position, asset_id in enumerate(asset_ids, 1):
                self._music_positions[asset_id] = position
            self._music_order_sources[album_id] = 'explicit'
            self.music_album_history.append((album_id, owner, 'order_established', list(asset_ids)))
        return self.get_music_album_order(album_id)

    def correct_music_album(self, album_id, owner, artist, title):
        album = self.get_music_album(album_id)
        if album is None or album.owner_user_id != owner:
            raise ValueError("Music album not found")
        album_intent({"import_group_id": str(album_id), "artist_name": artist, "album_title": title})
        album = replace(album, artist_name=artist.strip(), album_title=title.strip())
        self._music_albums[album_id] = album
        self.music_album_history.append((album_id, owner, "identity_corrected", artist, title))
        return album
