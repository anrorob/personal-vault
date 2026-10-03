"""Explicit incident-scoped use of the existing Gallery enrichment backfill."""
import argparse
import json
from pathlib import Path
from uuid import UUID

from app.gallery_publication import queue_missing_gallery_florence, current_florence


def scoped_florence_backfill(vault_store, ingestion_store, florence_store, manifest, *, queue=False, limit=50):
    if manifest.get('schema') != 'personal-vault.gallery-intelligence-backfill.v1' or not 1 <= limit <= 500:
        raise ValueError('Explicit bounded Gallery backfill manifest required')
    rows=manifest['assets']
    if not rows or len({row['asset_id'] for row in rows}) != len(rows):
        raise ValueError('Backfill requires unique canonical asset identities')
    assets=[]
    # Validate the whole manifest before queuing anything.
    for row in rows:
        asset=vault_store.get_catalogued_asset_by_id(UUID(row['asset_id']))
        if (asset is None or asset.owner_user_id != UUID(row['owner_user_id'])
                or asset.asset_type != 'Gallery' or not asset.vault_path.startswith('/vault/Gallery/')
                or asset.sha256 != row['sha256'] or asset.size_bytes != row['size_bytes']):
            raise ValueError('Canonical backfill identity changed')
        assets.append(asset)
    from app.gallery_reconciliation import published_source_items
    counts=dict(selected=len(assets),current=0,active=0,missing_or_outdated=0,queued=0)
    for asset in assets:
        current=current_florence(florence_store.latest_evidence(asset.id,asset.owner_user_id)) or any(
            current_florence(evidence) for item in published_source_items(vault_store,asset)
            for evidence in ingestion_store.list_evidence(item.id,asset.owner_user_id))
        job=florence_store.active_or_latest_job(asset.id)
        if current:
            counts['current']+=1
        elif job and job.status in {'queued','processing'}:
            counts['active']+=1
        else:
            counts['missing_or_outdated']+=1
            if queue and counts['queued'] < limit and queue_missing_gallery_florence(
                    vault_store,ingestion_store,florence_store,asset,'Explicit Gallery incident backfill'):
                counts['queued']+=1
    return counts


def main():
    from app.vault_master import get_vault_master_store
    from app.vault_master_ingestion_ai import get_ingestion_ai_store
    from app.gallery_florence import get_gallery_florence_store
    parser=argparse.ArgumentParser(description='Preview or queue exact published Gallery assets; never republishes')
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--queue',action='store_true')
    parser.add_argument('--limit',type=int,default=50)
    args=parser.parse_args()
    print(json.dumps(scoped_florence_backfill(get_vault_master_store(),get_ingestion_ai_store(),
        get_gallery_florence_store(),json.loads(args.manifest.read_text(encoding='utf-8')),
        queue=args.queue,limit=args.limit),sort_keys=True))


if __name__=='__main__':
    main()
