"""Durable, post-publication Florence recovery for canonical Gallery assets."""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import mimetypes
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.config import get_database_conninfo

GALLERY_FLORENCE_TASK_VERSION = "gallery-florence-recovery-v1"

@dataclass(frozen=True)
class GalleryFlorenceJob:
    id: UUID; asset_id: UUID; owner_user_id: UUID; requested_by: str; status: str; attempts: int
    error: str | None; created_at: datetime; started_at: datetime | None = None; completed_at: datetime | None = None

@dataclass(frozen=True)
class GalleryFlorenceEvidence:
    id: UUID; job_id: UUID; asset_id: UUID; owner_user_id: UUID; caption: str; ocr_text: str
    model_id: str; model_revision: str; task_version: str; processing_ms: int; created_at: datetime

class PostgresGalleryFlorenceStore:
    def __init__(self, conninfo: str): self._conninfo = conninfo
    def _connect(self): return psycopg.connect(self._conninfo, row_factory=dict_row)
    def initialize(self):
        with self._connect() as c, c.cursor() as x:
            x.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_florence_jobs (
              id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE,
              owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id), requested_by TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('queued','processing','completed','failed')), attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ)""")
            x.execute("CREATE UNIQUE INDEX IF NOT EXISTS vault_gallery_florence_active_asset_idx ON vault_gallery_florence_jobs(asset_id) WHERE status IN ('queued','processing')")
            x.execute("CREATE INDEX IF NOT EXISTS vault_gallery_florence_asset_idx ON vault_gallery_florence_jobs(asset_id,created_at DESC)")
            x.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_florence_evidence (
              id UUID PRIMARY KEY, job_id UUID NOT NULL UNIQUE REFERENCES vault_gallery_florence_jobs(id) ON DELETE CASCADE,
              asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE, owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
              caption TEXT NOT NULL, ocr_text TEXT NOT NULL, model_id TEXT NOT NULL, model_revision TEXT NOT NULL,
              task_version TEXT NOT NULL, processing_ms INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            x.execute("UPDATE vault_gallery_florence_jobs SET status='queued',started_at=NULL,error='Recovered after worker restart' WHERE status='processing'")
    def _job(self,row): return GalleryFlorenceJob(**row)
    def queue(self, asset_id, owner_user_id, requested_by):
        from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
        with self._connect() as c, c.cursor() as x:
            x.execute("SELECT id FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE", (asset_id, owner_user_id))
            if x.fetchone() is None:
                raise ValueError('Gallery Florence recovery requires the canonical asset owner')
            x.execute("""SELECT id FROM vault_gallery_florence_evidence WHERE asset_id=%s
                AND owner_user_id=%s AND model_id=%s AND model_revision=%s AND task_version=%s
                AND length(trim(caption))>0 LIMIT 1""",
                (asset_id, owner_user_id, AI_MODEL_ID, AI_MODEL_REVISION, GALLERY_FLORENCE_TASK_VERSION))
            if x.fetchone() is not None:
                return None
            x.execute("SELECT * FROM vault_gallery_florence_jobs WHERE asset_id=%s AND status IN ('queued','processing') ORDER BY created_at DESC LIMIT 1",(asset_id,)); row=x.fetchone()
            if not row:
                x.execute("INSERT INTO vault_gallery_florence_jobs(id,asset_id,owner_user_id,requested_by,status) SELECT %s,id,owner_user_id,%s,'queued' FROM vault_assets WHERE id=%s AND owner_user_id=%s RETURNING *",(uuid4(),requested_by,asset_id,owner_user_id)); row=x.fetchone()
            if not row: raise ValueError('Gallery Florence recovery requires the canonical asset owner')
            return self._job(row)
    def latest_evidence(self,asset_id,owner_user_id):
        with self._connect() as c, c.cursor() as x:
            x.execute("SELECT * FROM vault_gallery_florence_evidence WHERE asset_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT 1",(asset_id,owner_user_id)); row=x.fetchone()
        return GalleryFlorenceEvidence(**row) if row else None
    def active_or_latest_job(self,asset_id):
        with self._connect() as c, c.cursor() as x:
            x.execute("SELECT * FROM vault_gallery_florence_jobs WHERE asset_id=%s ORDER BY created_at DESC LIMIT 1",(asset_id,)); row=x.fetchone()
        return self._job(row) if row else None
    def claim_next_job(self):
        with self._connect() as c, c.cursor() as x:
            x.execute("""WITH next AS (SELECT id FROM vault_gallery_florence_jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
              UPDATE vault_gallery_florence_jobs j SET status='processing',attempts=attempts+1,started_at=CURRENT_TIMESTAMP,error=NULL WHERE j.id IN (SELECT id FROM next) RETURNING j.*"""); row=x.fetchone()
        return self._job(row) if row else None
    def complete(self,job,caption,ocr_text,processing_ms):
        from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
        with self._connect() as c, c.cursor() as x:
            x.execute("INSERT INTO vault_gallery_florence_evidence(id,job_id,asset_id,owner_user_id,caption,ocr_text,model_id,model_revision,task_version,processing_ms) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET caption=EXCLUDED.caption,ocr_text=EXCLUDED.ocr_text,processing_ms=EXCLUDED.processing_ms",(uuid4(),job.id,job.asset_id,job.owner_user_id,caption,ocr_text,AI_MODEL_ID,AI_MODEL_REVISION,GALLERY_FLORENCE_TASK_VERSION,processing_ms))
            x.execute("UPDATE vault_gallery_florence_jobs SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE id=%s",(job.id,))
    def fail(self,job_id,error):
        with self._connect() as c, c.cursor() as x: x.execute("UPDATE vault_gallery_florence_jobs SET status='failed',error=%s,completed_at=CURRENT_TIMESTAMP WHERE id=%s",(str(error)[:2000],job_id))

def gallery_source(asset):
    prefix='/vault/Gallery/'
    if not asset or not asset.vault_path.startswith(prefix): return None
    return Path(os.getenv('PV_GALLERY_PATH','/media/gallery')) / asset.vault_path.removeprefix(prefix)

def process_next_gallery_florence_job(store,vault_store):
    job=store.claim_next_job()
    if not job: return None
    try:
        source=gallery_source(vault_store.get_catalogued_asset_by_id(job.asset_id))
        if source is None or not source.is_file(): raise ValueError('Canonical Gallery source is unavailable')
        from app.vault_master_ingestion_ai import request_florence_analysis
        caption,text,elapsed=request_florence_analysis(source)
        if not caption: raise ValueError('Florence returned an empty Gallery caption')
        store.complete(job,caption,text,elapsed)
    except Exception as error: store.fail(job.id,error)
    return job.id

@lru_cache
def get_gallery_florence_store(): return PostgresGalleryFlorenceStore(get_database_conninfo())
