"""Normal Home Video description authority; inference configuration stays in KEN."""
from psycopg.types.json import Jsonb

from app import ken_config as ken
from app.ken_binding import verify_stored_run

DESTINATION = 'home-video-ken-v1'


def publish_description(conn, run):
    """Called in the same transaction as successful run completion."""
    config = run['configuration']
    if config.get('metadata_destination') != DESTINATION:
        return
    if (run['status'] != 'completed' or config.get('correction_version') != ken.PHASE
            or config.get('model_id') != ken.MODEL_ID or config.get('model_revision') != ken.REVISION):
        raise ValueError('Invalid KEN metadata producer')
    verify_stored_run(run, run['asset_id'], run['owner_user_id'])
    result = run['result'] or {}
    text = ken.clean_generated_output(result.get('description'))
    if not text or result.get('error') or not result.get('input_fingerprint'):
        raise ValueError('KEN description is incomplete')
    asset = conn.execute('''SELECT metadata FROM vault_assets
        WHERE id=%s AND owner_user_id=%s AND asset_type='Home Videos' FOR UPDATE''',
        (run['asset_id'], run['owner_user_id'])).fetchone()
    if asset is None:
        raise ValueError('KEN metadata owner is unavailable')
    previous = (asset['metadata'] or {}).get('ken_description', {})
    source = 'ken_adjusted' if config.get('draft_state') == 'user_adjusted' else 'ken_generated'
    # A replay/older initial draft cannot demote an adjusted version.
    if (previous.get('source') == 'ken_adjusted' and source == 'ken_generated'):
        return
    created_at = run['created_at'].isoformat()
    if previous.get('created_at', '') > created_at:
        return
    record = {'version': DESTINATION, 'asset_id': str(run['asset_id']),
              'owner_user_id': str(run['owner_user_id']), 'run_id': str(run['id']),
              'description': text, 'source': source, 'created_at': created_at,
              'model_id': config['model_id'], 'model_revision': config['model_revision'],
              'prompt_version': config['prompt_version'],
              'input_fingerprint': result['input_fingerprint'],
              'correction_fingerprint': config['correction_fingerprint']}
    # Generated evidence has its own key; never write manual overrides or Florence.
    conn.execute('''UPDATE vault_assets SET
        metadata=jsonb_set(metadata,'{ken_description}',%s),
        detected_metadata=jsonb_set(detected_metadata,'{ken_description}',%s),
        effective_metadata=jsonb_set(effective_metadata,'{ken_description}',%s),
        metadata_provenance=jsonb_set(metadata_provenance,'{ken_description}',%s),
        updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s''',
        (Jsonb(record), Jsonb(record), Jsonb(record), Jsonb(source), run['asset_id'], run['owner_user_id']))


def description_for(asset, fallback=None):
    for key in ('video_narrative', 'narrative'):
        manual = asset.user_overrides.get(key)
        if isinstance(manual, str) and manual.strip():
            return manual, 'user'
    record = asset.metadata.get('ken_description')
    if (isinstance(record, dict) and record.get('version') == DESTINATION
            and record.get('asset_id') == str(asset.id)
            and record.get('owner_user_id') == str(asset.owner_user_id)
            and record.get('source') in ('ken_adjusted', 'ken_generated')
            and isinstance(record.get('description'), str) and record['description'].strip()):
        return record['description'], record['source']
    return fallback, 'vault_master' if fallback else 'none'
