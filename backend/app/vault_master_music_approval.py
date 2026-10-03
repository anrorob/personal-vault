"""Atomic catalogue-only approval of an owner-reviewed Music mapping."""
from dataclasses import replace
from psycopg.types.json import Jsonb


def persist_mapping(store, album_id, owner, originals, updates, evidence, sequence, apply_order, expected_order):
    from app.vault_master import _catalogued_asset_from_row
    from app.music_order import write_sequence, sequence_result
    expected={a.id:a for a in originals}
    if hasattr(store, "catalogued_assets"):
        album=store.get_music_album(album_id)
        if album is None or album.owner_user_id!=owner or set(store.music_album_asset_ids(album_id))!=set(expected):
            raise ValueError("Album membership changed; review again")
        if store.get_music_album_order(album_id)!=expected_order:
            raise ValueError("Album order changed; review again")
        if any(store.get_catalogued_asset_by_id(k)!=a for k,a in expected.items()):
            raise ValueError("Album metadata changed; review again")
        for updated in updates: store.catalogued_assets[updated.vault_path]=updated
        if apply_order:
            if sequence: store.set_music_album_order(album_id,owner,sequence)
            else:
                for k in expected: store._music_positions[k]=None
                store._music_order_sources[album_id]='explicit'
        record=(album_id,owner,'mapping_approved',evidence)
        if not store.music_album_history or store.music_album_history[-1]!=record:
            store.music_album_history.append(record)
    else:
        with store._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM vault_music_albums WHERE id=%s AND owner_user_id=%s FOR UPDATE",(album_id,owner))
            if cursor.fetchone() is None: raise ValueError("Music album not found")
            cursor.execute("SELECT asset_id,playback_position FROM vault_music_album_members WHERE album_id=%s FOR UPDATE",(album_id,))
            members=cursor.fetchall()
            if {r['asset_id'] for r in members}!=set(expected):
                raise ValueError("Album membership changed; review again")
            current_order=sequence_result([(row['asset_id'],row['playback_position']) for row in members])
            if current_order!=expected_order:
                raise ValueError("Album order changed or import is incomplete; review again")
            for asset_id in sorted(expected,key=str):
                cursor.execute("SELECT asset.*, file.vault_path,file.filename,file.size_bytes,file.mime_type,file.sha256 FROM vault_assets asset JOIN vault_files file ON file.asset_id=asset.id WHERE asset.id=%s ORDER BY file.file_role='primary' DESC,file.created_at LIMIT 1 FOR UPDATE OF asset",(asset_id,))
                row=cursor.fetchone()
                if not row or _catalogued_asset_from_row(row)!=expected[asset_id]:
                    raise ValueError("Album metadata changed; review again")
            for a in updates:
                cursor.execute("UPDATE vault_assets SET display_title=%s,captured_on=%s,location=%s,metadata_provenance=%s,imported_metadata=%s,effective_metadata=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(a.display_title,a.captured_on,a.location,Jsonb(a.metadata_provenance),Jsonb(a.imported_metadata),Jsonb(a.effective_metadata),a.id))
            if apply_order:
                if sequence: write_sequence(cursor,album_id,owner,sequence,'explicit')
                else:
                    cursor.execute("UPDATE vault_music_album_members SET playback_position=NULL WHERE album_id=%s",(album_id,))
                    cursor.execute("UPDATE vault_music_albums SET order_source='explicit' WHERE id=%s",(album_id,))
            cursor.execute("SELECT details FROM vault_music_album_history WHERE album_id=%s AND action='mapping_approved' ORDER BY id DESC LIMIT 1",(album_id,))
            previous=cursor.fetchone()
            if not previous or previous['details']!=evidence:
                cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'mapping_approved',%s)",(album_id,owner,Jsonb(evidence)))
    return all([store._export_sidecar(a) for a in updates])


def imported_update(asset, metadata):
    from app.vault_master import apply_imported_asset_metadata
    # Remove only values previously owned by this importer, never manual layers.
    previous=asset.imported_metadata.get('music_match',{})
    imported=dict(asset.imported_metadata)
    for key in previous.get('owned_fields',[]):
        if key != 'artwork': imported.pop(key,None)
    return apply_imported_asset_metadata(replace(asset,imported_metadata=imported),metadata,'musicbrainz')
