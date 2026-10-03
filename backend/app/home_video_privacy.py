"""Home Video lifecycle/share interlocks within the caller's transaction."""


def require_shareable(cursor, asset_ids):
    cursor.execute("""SELECT lifecycle_state, asset_type FROM vault_assets
        WHERE id=ANY(%s) ORDER BY id FOR UPDATE""", (list(asset_ids),))
    blocked = [row for row in cursor.fetchall() if row['lifecycle_state'] != 'active']
    if any(row['asset_type'] == 'Home Videos' and row['lifecycle_state'] == 'hidden'
           for row in blocked):
        raise ValueError('Hidden videos must be restored before sharing.')
    if blocked:
        raise ValueError('Only active assets can be shared.')


def require_unshared(cursor, asset_id):
    # The caller holds the asset row lock also used by every grant-creation path.
    # Pending permissions matter: they must not activate after the asset is hidden.
    cursor.execute("""SELECT
        EXISTS(SELECT 1 FROM vault_share_grants WHERE asset_id=%s AND state IN ('active','pending'))
        OR EXISTS(SELECT 1 FROM vault_shared_collection_members m
            JOIN vault_collection_share_grants g ON g.collection_id=m.collection_id
            WHERE m.asset_id=%s AND g.state IN ('active','pending'))
        OR EXISTS(SELECT 1 FROM vault_federation_outgoing_shares WHERE origin_asset_id=%s AND state IN ('active','pending'))
        OR EXISTS(SELECT 1 FROM vault_shared_collection_members m
            JOIN vault_federation_outgoing_collection_shares g ON g.origin_collection_id=m.collection_id
            WHERE m.asset_id=%s AND g.state IN ('active','pending')) AS shared""",
        (asset_id, asset_id, asset_id, asset_id))
    if cursor.fetchone()['shared']:
        raise ValueError('This video is shared. Unshare it before hiding.')


def require_collection_shareable(cursor, collection_id):
    # Membership writers lock this same collection before locking member assets.
    cursor.execute('SELECT collection_id FROM vault_shared_collections WHERE collection_id=%s FOR UPDATE', (collection_id,))
    cursor.fetchone()
    cursor.execute('SELECT asset_id FROM vault_shared_collection_members WHERE collection_id=%s', (collection_id,))
    require_shareable(cursor, [row['asset_id'] for row in cursor.fetchall()])
