"""Owner-scoped trusted context and KEN titles.
Automatic post-analysis titles and explicit edits share catalogue title authority.
"""
import json
from uuid import uuid4
from psycopg.types.json import Jsonb
from app import ken_config as ken
from app.ken_attestation import digest_json
from app.ken_binding import verify_stored_run


def trusted_people(conn, asset_id, owner_id):
    # Accepted associations only. Detection/candidate tables are never queried.
    rows = conn.execute('''SELECT DISTINCT p.id,p.display_name FROM vault_people p
        JOIN vault_assets a ON a.id=%s AND a.owner_user_id=p.owner_user_id
        LEFT JOIN vault_asset_people x ON x.asset_id=a.id AND x.person_id=p.id
            AND x.owner_user_id=a.owner_user_id AND x.active
        LEFT JOIN vault_asset_people_decisions d ON d.asset_id=a.id AND d.person_id=p.id
            AND d.owner_user_id=a.owner_user_id AND d.active
        WHERE p.owner_user_id=%s AND p.active AND COALESCE(d.decision,'')<>'exclude'
        AND (d.decision='include' OR x.source IN ('user','user_face','imported','vault_master'))
        ORDER BY p.id''', (asset_id,owner_id)).fetchall()
    return [{'person_id':str(r['id']), 'name':r['display_name'], 'source':'accepted_people_association'} for r in rows]


def trusted_location(conn, asset_id, owner_id):
    # Canonical metadata only, with provenance and immutable owner enforcement.
    asset = conn.execute("SELECT location,metadata_provenance,to_jsonb(a)->'metadata' AS metadata,to_jsonb(a)->'detected_metadata' AS detected_metadata,to_jsonb(a)->'imported_metadata' AS imported_metadata,to_jsonb(a)->'user_overrides' AS user_overrides FROM vault_assets a WHERE id=%s AND owner_user_id=%s",
                         (asset_id, owner_id)).fetchone()
    if asset is None:
        return None
    source = (asset['metadata_provenance'] or {}).get('location', '')
    name = asset['location']
    if not isinstance(name, str) or not name or not isinstance(source, str) or not (source in ('user_override', 'embedded') or source.startswith('import:')):
        return None
    from app.video_location import ken_location_display
    for key in ('user_overrides','imported_metadata','detected_metadata','metadata'):
        display = ken_location_display(name, asset.get(key))
        if display != name:
            return {'name': display, 'source': source}
    return {'name': name, 'source': source}


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS vault_ken_titles (
        id UUID PRIMARY KEY, asset_id UUID NOT NULL, owner_user_id UUID NOT NULL, run_id UUID NOT NULL,
        context JSONB NOT NULL, context_fingerprint TEXT NOT NULL, title TEXT NOT NULL,
        model_revision TEXT NOT NULL, prompt_version TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, accepted_at TIMESTAMPTZ,
        FOREIGN KEY(run_id,asset_id,owner_user_id) REFERENCES vault_ken_runs(id,asset_id,owner_user_id) ON DELETE CASCADE,
        CHECK(length(title) BETWEEN 1 AND 120), CHECK(length(context_fingerprint)=64))''')
    conn.execute('CREATE INDEX IF NOT EXISTS ken_titles_asset ON vault_ken_titles(asset_id,owner_user_id,created_at DESC)')


def title_context(conn, asset_id, owner_id, run_id):
    asset = conn.execute('SELECT user_overrides,display_title,metadata_provenance FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE', (asset_id,owner_id)).fetchone()
    if asset is None:
        raise ValueError('Video owner is unavailable')
    run = conn.execute('''SELECT * FROM vault_ken_runs WHERE asset_id=%s AND owner_user_id=%s
        AND status='completed' AND configuration->>'correction_version'=%s ORDER BY created_at DESC,id DESC LIMIT 1''',
        (asset_id,owner_id,ken.PHASE)).fetchone()
    if run is None or run['id'] != run_id:
        raise ValueError('The draft changed; refresh before generating a title')
    verify_stored_run(run,asset_id,owner_id)
    if conn.execute("SELECT 1 FROM vault_ken_runs WHERE asset_id=%s AND status IN ('queued','preparing_input','analysing','saving_result')", (asset_id,)).fetchone():
        raise ValueError('Wait for the current KEN run')
    corrections = conn.execute('SELECT id,sequence,text FROM vault_ken_corrections WHERE asset_id=%s AND owner_user_id=%s ORDER BY sequence', (asset_id,owner_id)).fetchall()
    manual = next((asset['user_overrides'].get(k) for k in ('video_narrative','narrative') if asset['user_overrides'].get(k)),None)
    description = manual or ken.clean_generated_output(run['result']['description'])
    if not isinstance(description,str) or len(description.encode())>8192:
        raise ValueError('Accepted description is too large for the experimental title context')
    context = {'accepted_description':description,'description_source':'manual_final' if manual else 'user_selected_ken_draft',
        'run_id':str(run_id),'trusted_people':trusted_people(conn,asset_id,owner_id),
        'trusted_location':trusted_location(conn,asset_id,owner_id),
        'corrections':[{'id':str(c['id']),'sequence':c['sequence'],'text':c['text']} for c in corrections],
        # Dates remain explicit user metadata; location uses canonical provenance above.
        'trusted_metadata':{k:asset['user_overrides'][k] for k in ('captured_on','captured_at') if asset['user_overrides'].get(k)}}
    if len(json.dumps(context).encode())>20000:
        raise ValueError('Title context is too large')
    return context


class TitlesStore:
    def titles(self,asset_id,owner_id):
        with self.connect() as conn:
            rows = conn.execute('''SELECT id,asset_id,run_id,title,model_revision,prompt_version,created_at,accepted_at
                FROM vault_ken_titles WHERE asset_id=%s AND owner_user_id=%s ORDER BY created_at DESC,id DESC LIMIT 20''', (asset_id,owner_id)).fetchall()
            return [{**row, 'title': ken.clean_generated_output(row['title'])} for row in rows]

    def generate_title(self,asset_id,owner_id,run_id,adapter):
        # Same global admission as video inference; never run two heavy models.
        with self.worker_lock() as acquired:
            if not acquired:
                raise ValueError('KEN is busy; retry when available')
            return self._generate_title_locked(asset_id,owner_id,run_id,adapter)

    def _generate_title_locked(self,asset_id,owner_id,run_id,adapter,automatic=False):
        with self.connect() as conn:
            asset = conn.execute('SELECT * FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE',(asset_id,owner_id)).fetchone()
            if asset is None: raise ValueError('Video owner is unavailable')
            if automatic and manual_title_exists(asset): return None
            context = title_context(conn,asset_id,owner_id,run_id)
            if automatic:
                existing=conn.execute('SELECT * FROM vault_ken_titles WHERE asset_id=%s AND owner_user_id=%s AND run_id=%s AND context_fingerprint=%s AND prompt_version=%s ORDER BY created_at DESC LIMIT 1',(asset_id,owner_id,run_id,digest_json(context),ken.TITLE_VERSION)).fetchone()
                if existing:
                    # A valid historical suggestion is sufficient: promote it without inference.
                    if not existing['accepted_at'] or asset['display_title'] != existing['title']:
                        write_title(conn,asset,ken.validate_title(existing['title']),'ken_generated','system:ken')
                        conn.execute('UPDATE vault_ken_titles SET accepted_at=CURRENT_TIMESTAMP WHERE id=%s',(existing['id'],))
                    return existing
        request = {'asset_id':str(asset_id),'run_id':str(run_id),'request_id':str(uuid4()),
            'context':context,'context_fingerprint':digest_json(context),'prompt_version':ken.TITLE_VERSION}
        result = adapter.generate_title(request)
        if any(result.get(k)!=request[k] for k in ('asset_id','run_id','request_id','context_fingerprint','prompt_version')) or result.get('model_revision')!=ken.REVISION or result.get('runtime')!=ken.RUNTIME:
            raise ValueError('Title result identity mismatch')
        title = ken.validate_title(result.get('title'))
        with self.connect() as conn:
            if digest_json(title_context(conn,asset_id,owner_id,run_id))!=request['context_fingerprint']:
                raise ValueError('Video context changed; regenerate the title')
            suggestion = conn.execute('''INSERT INTO vault_ken_titles
                (id,asset_id,owner_user_id,run_id,context,context_fingerprint,title,model_revision,prompt_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id,asset_id,run_id,title,model_revision,prompt_version,created_at,accepted_at''',
                (request['request_id'],asset_id,owner_id,run_id,Jsonb(context),request['context_fingerprint'],title,ken.REVISION,ken.TITLE_VERSION)).fetchone()
            if automatic:
                asset = conn.execute('SELECT * FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE',(asset_id,owner_id)).fetchone()
                if not manual_title_exists(asset):
                    write_title(conn,asset,title,'ken_generated','system:ken')
                    conn.execute('UPDATE vault_ken_titles SET accepted_at=CURRENT_TIMESTAMP WHERE id=%s',(suggestion['id'],))
            return suggestion

    def process_pending_title(self,adapter):
        with self.connect() as conn:
            run = conn.execute("""SELECT id,asset_id,owner_user_id,configuration FROM vault_ken_runs
                WHERE status='completed' AND configuration->>'correction_version'=%s
                AND configuration->'automatic_title'->>'status'='pending'
                ORDER BY completed_at,id LIMIT 1""",(ken.PHASE,)).fetchone()
        if run is None: return None
        step=run['configuration']['automatic_title']
        attempts=step.get('attempts',0)+1
        try:
            title=self._generate_title_locked(run['asset_id'],run['owner_user_id'],run['id'],adapter,automatic=True)
            state={**step,'status':'completed' if title else 'manual_title_preserved','attempts':attempts}
        except Exception:
            # Never log description/title text or invalidate successful video evidence.
            state={**step,'status':'pending' if attempts<3 else 'failed','attempts':attempts}
        with self.connect() as conn:
            conn.execute("UPDATE vault_ken_runs SET configuration=jsonb_set(configuration,'{automatic_title}',%s) WHERE id=%s",
                         (Jsonb(state),run['id']))
        return run['id']

    def set_video_title(self,asset_id,owner_id,username,*,suggestion_id=None,manual_title=None):
        with self.connect() as conn:
            asset = conn.execute('SELECT * FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE', (asset_id,owner_id)).fetchone()
            if asset is None:
                raise ValueError('Video owner is unavailable')
            overrides = dict(asset['user_overrides'])
            if suggestion_id:
                if overrides.get('display_title') or asset['metadata_provenance'].get('display_title')=='user_override':
                    raise ValueError('A manual title is authoritative; edit it explicitly instead')
                suggestion = conn.execute('SELECT * FROM vault_ken_titles WHERE id=%s AND asset_id=%s AND owner_user_id=%s', (suggestion_id,asset_id,owner_id)).fetchone()
                if suggestion is None or digest_json(title_context(conn,asset_id,owner_id,suggestion['run_id']))!=suggestion['context_fingerprint']:
                    raise ValueError('Title suggestion is stale; regenerate it')
                title = ken.validate_title(suggestion['title'])
                provenance='ken_accepted'
                conn.execute('UPDATE vault_ken_titles SET accepted_at=CURRENT_TIMESTAMP WHERE id=%s',(suggestion_id,))
            else:
                title = (manual_title or '').strip()
                if not title or len(title)>160 or any(ord(c)<32 for c in title):
                    raise ValueError('Enter a non-empty single-line title of at most 160 characters')
                overrides['display_title']=title
                provenance='user_override'
            return write_title(conn,asset,title,provenance,username)



def manual_title_exists(asset):
    return bool(asset['user_overrides'].get('display_title') or asset['metadata_provenance'].get('display_title')=='user_override')

def write_title(conn,asset,title,provenance,actor):
    previous=asset['display_title']
    overrides=dict(asset['user_overrides'])
    if provenance=='user_override': overrides['display_title']=title
    metadata = dict(asset['metadata']); effective = dict(asset['effective_metadata'])
    if provenance != 'user_override':
        metadata['ken_accepted_title'] = title
        metadata['ken_title_source'] = provenance
    effective['display_title']=title
    sources={**asset['metadata_provenance'],'display_title':provenance}
    conn.execute('''UPDATE vault_assets SET display_title=%s,metadata=%s,effective_metadata=%s,
        metadata_provenance=%s,user_overrides=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s''',
        (title,Jsonb(metadata),Jsonb(effective),Jsonb(sources),Jsonb(overrides),asset['id'],asset['owner_user_id']))
    conn.execute('''INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values)
        VALUES(%s,%s,'metadata_updated',%s,%s,%s)''',(uuid4(),asset['id'],str(actor),Jsonb({'display_title':previous}),Jsonb({'display_title':title,'source':provenance})))
    return {'asset_id':str(asset['id']),'display_title':title,'title_source':provenance}
