"""Explicit, manifest-bound reconciliation of published Supplier album selections.

Never imported by startup. Dry-run is read-only and works before group schema
exists. Apply requires the promoted schema, an exact build and later operator
approval. Private manifests and output belong outside source control.
"""
import argparse
import hashlib
import json
import os
import re
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.music_groups import supplier_manual_album, declared_album, ensure_album, bind_members
from app.music_order import metadata_sequence

VERSION = 'music-legacy-groups-v1'
MEMBER_FIELDS = ('asset_id', 'file_id', 'sha256', 'size_bytes', 'vault_path', 'metadata_fingerprint')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def inspect_group(cursor, group, vault_id):
    """Prove the entire selected root, not a caller-selected filename subset."""
    owner, installation, source = (UUID(group[key]) for key in ('owner_user_id', 'installation_id', 'source_id'))
    transfers = cursor.execute("""SELECT transfer_id, vault_id, state, expected_sha256,
        total_size, media_type, source_context FROM vault_supplier_transfer_sessions
        WHERE user_id=%s AND installation_id=%s AND source_context->>'source_id'=%s
        ORDER BY transfer_id""", (owner, installation, str(source))).fetchall()
    expected_transfers = group['transfer_ids']
    if (not transfers or len(expected_transfers) != len(set(expected_transfers))
            or set(expected_transfers) != {str(t['transfer_id']) for t in transfers}):
        raise ValueError('Selected-root transfer set changed or is empty')
    members, intents, metadata = [], [], []
    for transfer in transfers:
        context = transfer['source_context']
        if (transfer['vault_id'] != vault_id or transfer['state'] != 'finalized'
                or context.get('source_kind') != 'manual_upload'
                or context.get('source_label') != group['source_label']
                or (transfer['media_type'] is not None and not transfer['media_type'].startswith('audio/'))):
            raise ValueError('Transfer is not a completed local manual audio selection')
        rows = cursor.execute("""SELECT a.id asset_id, f.id file_id, f.sha256,
            f.size_bytes, f.vault_path, a.owner_user_id, a.origin_vault_id,
            a.asset_type, a.lifecycle_state, f.mime_type,
            jsonb_build_object('disc_number',a.effective_metadata->'disc_number',
                'track_number',a.effective_metadata->'track_number') coordinates,
            md5(jsonb_build_array(a.metadata,a.detected_metadata,a.imported_metadata,
                a.user_overrides,a.effective_metadata)::text) metadata_fingerprint,
            (SELECT count(*) FROM vault_files p WHERE p.asset_id=a.id
                AND p.file_role='primary') primary_count,
            EXISTS(SELECT 1 FROM vault_asset_history h WHERE h.asset_id=a.id
                AND h.action='permanently_deleted') deleted
            FROM vault_files f JOIN vault_assets a ON a.id=f.asset_id
            WHERE f.sha256=%s AND f.file_role='primary'""", (transfer['expected_sha256'],)).fetchall()
        if len(rows) != 1:
            raise ValueError('Transfer must resolve to exactly one surviving canonical file')
        row = rows[0]
        if (row['owner_user_id'] != owner or row['origin_vault_id'] != vault_id
                or row['asset_type'] != 'Music' or row['lifecycle_state'] != 'active'
                or row['deleted'] or row['primary_count'] != 1
                or not row['mime_type'].startswith('audio/')
                or row['size_bytes'] != transfer['total_size']):
            raise ValueError('Canonical member is deleted, ambiguous, foreign or incompatible')
        # Older Supplier sessions may omit the optional media_type. The existing
        # canonical file's detected audio MIME is required independently above.
        adapted = supplier_manual_album(context, row['mime_type'], installation)
        if 'music_album' not in adapted:
            raise ValueError('No authoritative album declaration')
        intents.append(adapted['music_album'])
        members.append({key: str(row[key]) if isinstance(row[key], UUID) else row[key] for key in MEMBER_FIELDS})
        metadata.append((row['asset_id'], row['coordinates']))
    if any(intent != intents[0] for intent in intents):
        raise ValueError('Conflicting declarations within selected root')
    expected = group['members']
    ids = [m['asset_id'] for m in members]
    if (len(ids) != len(set(ids)) or len(expected) != len(members)
            or sorted(expected, key=lambda m: m['asset_id']) != sorted(members, key=lambda m: m['asset_id'])):
        raise ValueError('Audited canonical identities, paths, checksums or metadata changed')
    sequence = list(map(str, metadata_sequence(metadata) or []))
    if sequence != group['expected_order']:
        raise ValueError('Audited order evidence changed; review required')
    return declared_album(owner, intents[0]), [UUID(i) for i in ids], sequence


def reconcile(conninfo, manifest, *, apply=False):
    if manifest.get('version') != VERSION or not manifest.get('groups'):
        raise ValueError('A versioned, nonempty private manifest is required')
    vault_id = UUID(manifest['vault_id'])
    with psycopg.connect(conninfo, row_factory=dict_row) as connection:
        if apply:
            connection.execute("SET LOCAL lock_timeout='10s'")
            connection.execute("SET LOCAL statement_timeout='60s'")
            # Fixed table names. Block concurrent intake, catalogue and owner edits
            # during this short operation; ordinary reads remain available.
            connection.execute("""LOCK TABLE vault_assets, vault_files,
                vault_asset_history, vault_supplier_transfer_sessions,
                vault_music_albums, vault_music_album_members,
                vault_music_album_history IN SHARE ROW EXCLUSIVE MODE""")
        else:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor = connection.cursor()
        local = cursor.execute('SELECT vault_id FROM vaults WHERE is_local').fetchall()
        if len(local) != 1 or local[0]['vault_id'] != vault_id:
            raise ValueError('Manifest does not identify this local Vault')
        schema = cursor.execute("SELECT to_regclass('vault_music_albums') present").fetchone()['present'] is not None
        plans, seen = [], set()
        for group in manifest['groups']:
            album, ids, sequence = inspect_group(cursor, group, vault_id)
            if seen.intersection(ids):
                raise ValueError('Manifest groups overlap')
            seen.update(ids)
            digest = fingerprint(group)
            completed = False
            if schema:
                existing = cursor.execute('SELECT * FROM vault_music_albums WHERE id=%s', (album.id,)).fetchone()
                if existing:
                    audit = cursor.execute("""SELECT details FROM vault_music_album_history
                        WHERE album_id=%s AND action='legacy_reconciled'""", (album.id,)).fetchall()
                    actual = cursor.execute('SELECT asset_id,owner_user_id FROM vault_music_album_members WHERE album_id=%s', (album.id,)).fetchall()
                    if (len(audit) != 1 or audit[0]['details'] != {'version': VERSION, 'manifest_group_sha256': digest}
                            or {r['asset_id'] for r in actual} != set(ids)
                            or any(r['owner_user_id'] != album.owner_user_id for r in actual)
                            or existing['owner_user_id'] != album.owner_user_id
                            or existing['import_group_id'] != album.import_group_id):
                        raise ValueError('Existing group needs review; no automatic merge or overwrite')
                    completed = True  # Preserve later owner album identity/order corrections.
                elif cursor.execute('SELECT 1 FROM vault_music_album_members WHERE asset_id=ANY(%s)', (ids,)).fetchone():
                    raise ValueError('A target already belongs to another group')
            plans.append((album, ids, sequence, digest, completed))
        # All groups are proven before any write. Failure rolls back the whole set.
        report = []
        for album, ids, sequence, digest, completed in plans:
            if apply and not completed:
                ensure_album(cursor, album)
                bind_members(cursor, album, ids)  # Existing canonical order resolver.
                cursor.execute("""INSERT INTO vault_music_album_history
                    (album_id,actor_user_id,action,details) VALUES(%s,%s,'legacy_reconciled',%s)""",
                    (album.id, album.owner_user_id, Jsonb({'version': VERSION, 'manifest_group_sha256': digest})))
            report.append({'album_id': str(album.id), 'members': len(ids),
                           'audited_order': 'ready' if sequence else 'unresolved',
                           'already_applied': completed})
        return {'version': VERSION, 'mode': 'apply' if apply else 'read-only',
                'environment': manifest['environment'], 'schema_present': schema, 'groups': report}


def connection_from_environment(manifest, environment, sha):
    if (environment not in ('production', 'development') or manifest['environment'] != environment
            or os.environ.get('PV_ENVIRONMENT') != environment
            or not re.fullmatch('[0-9a-f]{40}', sha) or os.environ.get('PV_COMMIT') != sha
            or os.environ.get('POSTGRES_HOST') != manifest['database']['host']
            or os.environ.get('POSTGRES_DB') != manifest['database']['name']):
        raise ValueError('Explicit environment, database and exact deployed build must match')
    from psycopg.conninfo import make_conninfo
    return make_conninfo(**{key: os.environ[name] for key, name in {
        'host': 'POSTGRES_HOST', 'port': 'POSTGRES_PORT', 'dbname': 'POSTGRES_DB',
        'user': 'POSTGRES_USER', 'password': 'POSTGRES_PASSWORD'}.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, help='External private JSON, or - for stdin')
    parser.add_argument('--environment', required=True, choices=('production', 'development'))
    parser.add_argument('--expect-sha', required=True)
    parser.add_argument('--apply', action='store_true', help='Apply only after explicit operator approval and backup')
    args = parser.parse_args()
    import sys
    if args.manifest == '-':
        manifest = json.load(sys.stdin)
    else:
        with open(args.manifest, encoding='utf-8-sig') as source:
            manifest = json.load(source)
    conninfo = connection_from_environment(manifest, args.environment, args.expect_sha)
    print(json.dumps(reconcile(conninfo, manifest, apply=args.apply), indent=2))


if __name__ == '__main__':
    main()
