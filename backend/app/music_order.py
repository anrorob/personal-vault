"""Durable playback order on existing Music album membership."""
import re

from psycopg.types.json import Jsonb


def metadata_sequence(rows):
    """No sequence unless every disc/track coordinate is positive and unique."""
    numbered = []
    for asset_id, metadata in rows:
        coordinate = []
        for key in ("disc_number", "track_number"):
            value = metadata.get(key)
            match = re.fullmatch(r"(0*[1-9][0-9]*)(?:/0*[1-9][0-9]*)?", str(value).strip())
            if not match:
                return None
            coordinate.append(int(match[1]))
        numbered.append((tuple(coordinate), asset_id))
    if not numbered or len({key for key, _ in numbered}) != len(numbered):
        return None
    return [asset_id for _, asset_id in sorted(numbered, key=lambda pair: pair[0])]


def sequence_result(rows):
    positions = [position for _, position in rows]
    ready = bool(rows) and None not in positions and sorted(positions) == list(range(1, len(rows) + 1))
    return {
        "state": "ready" if ready else "unresolved",
        "member_count": len(rows),
        "asset_ids": [asset_id for asset_id, _ in sorted(rows, key=lambda row: row[1])] if ready else [],
    }


def write_sequence(cursor, album_id, owner, asset_ids, source):
    cursor.execute("SELECT asset_id, playback_position FROM vault_music_album_members WHERE album_id=%s FOR UPDATE", (album_id,))
    rows = cursor.fetchall()
    if len(asset_ids) != len(set(asset_ids)) or set(asset_ids) != {row['asset_id'] for row in rows} or not asset_ids:
        raise ValueError("Album order must include every current member exactly once")
    previous = sequence_result([(row['asset_id'], row['playback_position']) for row in rows])
    cursor.execute("SELECT order_source FROM vault_music_albums WHERE id=%s", (album_id,))
    if previous['asset_ids'] == asset_ids and cursor.fetchone()['order_source'] == source:
        return
    cursor.execute("UPDATE vault_music_album_members SET playback_position=NULL WHERE album_id=%s", (album_id,))
    for position, asset_id in enumerate(asset_ids, 1):
        cursor.execute("UPDATE vault_music_album_members SET playback_position=%s WHERE album_id=%s AND asset_id=%s", (position, album_id, asset_id))
    cursor.execute("UPDATE vault_music_albums SET order_source=%s WHERE id=%s", (source, album_id))
    cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'order_established',%s)",
        (album_id, owner, Jsonb({'source': source, 'asset_ids': list(map(str, asset_ids))})))


def backfill_sequence(cursor, album_id, owner):
    cursor.execute("SELECT order_source FROM vault_music_albums WHERE id=%s FOR UPDATE", (album_id,))
    if cursor.fetchone()['order_source'] != 'unresolved':
        return
    cursor.execute("SELECT a.id,a.effective_metadata FROM vault_music_album_members m JOIN vault_assets a ON a.id=m.asset_id AND a.owner_user_id=m.owner_user_id WHERE m.album_id=%s", (album_id,))
    sequence = metadata_sequence([(row['id'], row['effective_metadata']) for row in cursor.fetchall()])
    if sequence:
        write_sequence(cursor, album_id, owner, sequence, 'metadata')


def membership_changed(cursor, album):
    cursor.execute("SELECT order_source FROM vault_music_albums WHERE id=%s FOR UPDATE", (album.id,))
    source = cursor.fetchone()['order_source']
    cursor.execute("UPDATE vault_music_album_members SET playback_position=NULL WHERE album_id=%s", (album.id,))
    # An explicit sequence never becomes a metadata-derived sequence on later arrivals.
    if source != 'explicit':
        cursor.execute("UPDATE vault_music_albums SET order_source='unresolved' WHERE id=%s", (album.id,))
        backfill_sequence(cursor, album.id, album.owner_user_id)
    cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'order_membership_changed',%s)",
        (album.id, album.owner_user_id, Jsonb({'previous_source': source})))


def initialize_music_order(cursor):
    cursor.execute("ALTER TABLE vault_music_album_members ADD COLUMN IF NOT EXISTS playback_position INTEGER CHECK(playback_position > 0)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS music_album_playback_position ON vault_music_album_members(album_id,playback_position) WHERE playback_position IS NOT NULL")
    cursor.execute("ALTER TABLE vault_music_albums ADD COLUMN IF NOT EXISTS order_source TEXT NOT NULL DEFAULT 'unresolved' CHECK(order_source IN ('unresolved','metadata','explicit'))")
    cursor.execute("CREATE TABLE IF NOT EXISTS vault_music_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    cursor.execute("INSERT INTO vault_music_migrations(version) VALUES('membership-order-v1') ON CONFLICT DO NOTHING RETURNING version")
    if cursor.fetchone() is None:
        return
    cursor.execute("SELECT id,owner_user_id FROM vault_music_albums ORDER BY id FOR UPDATE")
    for row in cursor.fetchall():
        backfill_sequence(cursor, row['id'], row['owner_user_id'])
