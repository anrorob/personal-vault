"""Owner/asset-scoped KEN revisions. No canonical metadata writes."""
from uuid import uuid4
from contextlib import nullcontext
from psycopg.types.json import Jsonb
from app import ken_config as ken
from app.ken_attestation import digest_json


def initialize(conn):
    conn.execute('''DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ken_correction_binding'
                       AND conrelid='vault_ken_runs'::regclass) THEN
            ALTER TABLE vault_ken_runs ADD CONSTRAINT ken_correction_binding CHECK (
                result IS NULL OR configuration->>'correction_version' IS DISTINCT FROM 'ken-corrections-v1'
                OR ((result->>'correction_fingerprint'=configuration->>'correction_fingerprint') IS TRUE));
        END IF;
    END $$''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS ken_run_owner_identity
                    ON vault_ken_runs(id,asset_id,owner_user_id)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS vault_ken_corrections (
        id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id),
        owner_user_id UUID NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>0),
        text TEXT NOT NULL CHECK(length(text)>0), created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        model_id TEXT NOT NULL, model_revision TEXT NOT NULL, run_id UUID NOT NULL,
        UNIQUE(asset_id,owner_user_id,sequence), UNIQUE(run_id),
        FOREIGN KEY(run_id,asset_id,owner_user_id) REFERENCES vault_ken_runs(id,asset_id,owner_user_id) ON DELETE CASCADE
    )''')


class CorrectionsStore:
    def corrections(self, asset_id, owner_id):
        with self.connect() as conn:
            return conn.execute('''SELECT * FROM vault_ken_corrections
                WHERE asset_id=%s AND owner_user_id=%s ORDER BY sequence''', (asset_id, owner_id)).fetchall()

    def queue_ken(self, asset_id, owner_id, config, *, text=None, parent_run_id=None, connection=None):
        with (self.connect() if connection is None else nullcontext(connection)) as conn:
            conn.execute('SELECT pg_advisory_xact_lock(761003)')
            asset = conn.execute('SELECT id FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE',
                                 (asset_id, owner_id)).fetchone()
            if asset is None:
                raise ValueError('Video owner is unavailable')
            active = conn.execute('''SELECT * FROM vault_ken_runs WHERE asset_id=%s AND owner_user_id=%s
                AND status IN ('queued','preparing_input','analysing','saving_result')''', (asset_id, owner_id)).fetchone()
            if active:
                if text is not None:
                    raise ValueError('Wait for the current KEN run before adjusting it')
                return active
            if conn.execute("SELECT count(*) AS n FROM vault_ken_runs WHERE owner_user_id=%s AND status='queued'",
                            (owner_id,)).fetchone()['n'] >= 32:
                raise ValueError('Too many queued analyses')
            previous = conn.execute('''SELECT id,result->>'description' AS description FROM vault_ken_runs
                WHERE asset_id=%s AND owner_user_id=%s AND status='completed'
                AND configuration->>'correction_version'=%s ORDER BY created_at DESC,id DESC LIMIT 1''',
                (asset_id, owner_id, ken.PHASE)).fetchone()
            rows = conn.execute('''SELECT * FROM vault_ken_corrections WHERE asset_id=%s AND owner_user_id=%s
                                   ORDER BY sequence''', (asset_id, owner_id)).fetchall()
            run_id = uuid4()
            correction = None
            if text is not None:
                if not text.strip() or len(text.encode('utf-8')) > 2000:
                    raise ValueError('Enter a shorter, non-empty correction')
                if previous is None or previous['id'] != parent_run_id:
                    raise ValueError('The draft changed; refresh before adjusting it')
                correction = {'id': uuid4(), 'sequence': max((r['sequence'] for r in rows), default=0)+1, 'text': text.strip()}
                rows.append(correction)
            from app.ken_titles import trusted_people, trusted_location
            context = ken.correction_context(ken.clean_generated_output(previous['description']) if previous and rows else '', rows,
                                             trusted_people(conn, asset_id, owner_id), trusted_location(conn, asset_id, owner_id))
            from app import ken_owner_style
            context['owner_style'] = ken_owner_style.context(conn, owner_id)
            if len(ken.grounded_prompt(context)) > 11500:
                raise ValueError('KEN context is full; shorten video corrections')
            config = {**config, 'correction_context': context, 'correction_fingerprint': digest_json(context),
                      'draft_state': 'user_adjusted' if rows else 'generated',
                      'owner_style_fingerprint': digest_json(context['owner_style']),
                      'grounded_prompt_version': ken.GROUNDED_VERSION,
                      'grounded_prompt_sha256': __import__('hashlib').sha256(ken.grounded_prompt(context).encode()).hexdigest(),
                      'parent_run_id': str(previous['id']) if previous and rows else None}
            run = conn.execute('''INSERT INTO vault_ken_runs
                (id,asset_id,owner_user_id,input_mode,status,configuration)
                VALUES(%s,%s,%s,%s,'queued',%s) RETURNING *''',
                (run_id,asset_id,owner_id,config['input_mode'],Jsonb(config))).fetchone()
            if correction:
                conn.execute('''INSERT INTO vault_ken_corrections
                    (id,asset_id,owner_user_id,sequence,text,model_id,model_revision,run_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (correction['id'],asset_id,owner_id,correction['sequence'],correction['text'],
                     config['model_id'],config['model_revision'],run_id))
            return run
