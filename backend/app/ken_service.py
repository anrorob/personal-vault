"""Environment-local analyser queue, including opt-in KEN metadata delivery."""

import asyncio
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
import hashlib
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_database_conninfo
from app.runtime_identity import source_repository_allowed
from app.home_videos import get_home_videos_path
from app.ken_integrity import seal_input, verify_input
from app.ken_binding import RESULT_BINDING_VERSION, verify_result
from app.ken_attestation import expect_input, enforce_attestation, digest_json
from app import ken_config as ken
from app import ken_corrections
from app.ken_repetition import warn_on_repetition
from app.ken_native import prepare_native, publish_native, cleanup_abandoned_inputs, file_sha256
from app.video_intelligence import probe_video_duration_ms

TASK_VERSION = 'ken-analysis-v1'

ACTIVE = ("preparing_input", "analysing", "saving_result")
LOG = logging.getLogger("pv.ken")


def enabled() -> bool:
    if os.getenv("PV_KEN_ENABLED") != "true" or not source_repository_allowed():
        return False
    environment = os.getenv("PV_ENVIRONMENT")
    if environment == "development":
        return True
    if environment == "production":
        # Production must never inherit the Development endpoint or scratch.
        if not os.getenv("PV_KEN_URL") or not os.getenv("PV_KEN_WORK_ROOT"):
            raise RuntimeError("Production KEN requires an explicit endpoint and work root")
        return True
    return False


@dataclass(frozen=True)
class Engine:
    display_name: str = 'Qwen3-VL 8B'
    model_id: str = ken.MODEL_ID
    model_revision: str = ken.REVISION
    runtime: str = ken.RUNTIME
    supported_input_modes: tuple[str,...] = ('native_video',)

ENGINE = Engine()

class KenFailure(Exception):
    """A bounded user-safe KEN failure."""

class LocalModelAdapter:
    def __init__(self, selected: Engine = ENGINE):
        self.selected = selected

    def endpoint(self) -> str:
        # Configuration is operator-owned, never accepted from a browser request.
        endpoint = os.getenv("PV_KEN_URL")
        environment = os.getenv("PV_ENVIRONMENT")
        if environment not in {"development", "production"} or not source_repository_allowed():
            raise ValueError("KEN environment/repository is not configured")
        if not endpoint:
            raise ValueError("KEN endpoint must be explicitly configured")
        return endpoint.rstrip("/")

    def health(self) -> dict:
        try:
            with urlopen(self.endpoint() + "/health", timeout=2) as response:
                data = json.loads(response.read(16384))
            if data.get("model_revision") != self.selected.model_revision or data.get("runtime") != self.selected.runtime:
                return {"status": "unavailable"}
            return {"status": data["status"]} if data.get("status") in ("available", "busy", "loading", "failed") else {"status": "unavailable"}
        except (OSError, ValueError, KeyError):
            return {"status": "unavailable"}

    def generate_title(self, payload: dict) -> dict:
        request = Request(self.endpoint() + '/title', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
        try:
            with urlopen(request, timeout=1860) as response:
                return json.loads(response.read(32768))
        except (OSError, ValueError) as error:
            raise KenFailure('KEN title generation failed or is busy') from error

    def analyse(self, payload: dict) -> dict:
        request = Request(self.endpoint() + "/analyse", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=36000) as response:
                data = json.loads(response.read(2 * 1024 * 1024))
        except HTTPError as error:
            failure=KenFailure("Analyser is busy; retry when available" if error.code==409 else "Model execution failed; technical diagnostics recorded")
            failure.service_diagnostic={'http_status':error.code}
            raise failure from error
        except (TimeoutError, URLError) as error:
            raise KenFailure("Analyser unavailable or inference timeout") from error
        if data.get("model_revision") != self.selected.model_revision or data.get("runtime") != self.selected.runtime:
            raise KenFailure("Analyser version does not match the configured engine")
        if any(data.get(key) != payload.get(key) or not payload.get(key) for key in ("asset_id", "run_id", "input_fingerprint")) or data.get("input_mode") != payload.get("input_mode"):
            raise KenFailure("Analyser result identity does not match the requested run")
        data = enforce_attestation(payload, data)
        if data.get("error"):
            data["error"] = data["error"] if data["error"] in ("Inference timeout", "Model execution failed", "Model returned no description", "Input integrity verification failed") else "Model execution failed"
            return data
        data["description"] = ken.clean_generated_output(data.get("description"))
        if not isinstance(data.get("description"), str) or not data["description"].strip():
            raise KenFailure("Model returned no description")
        return data


ADAPTER = LocalModelAdapter()


def configuration() -> dict:
    return {**asdict(ENGINE), 'input_mode':'native_video', 'task_version':TASK_VERSION,
            'result_binding_version':RESULT_BINDING_VERSION, 'prompt_version':ken.PROMPT_VERSION,
            'prompt':ken.PROMPT, 'prompt_sha256':hashlib.sha256(ken.PROMPT.encode()).hexdigest(),
            'parameters':deepcopy(ken.PARAMETERS),'temporal_policy':ken.VERSION,
            'correction_version':ken.PHASE,'role':'KEN','metadata_destination':'home-video-ken-v1'}


from app import ken_titles


class KenStore(ken_corrections.CorrectionsStore, ken_titles.TitlesStore):
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def connect(self):
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def initialize(self, connection=None):
        with (self.connect() if connection is None else nullcontext(connection)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS vault_ken_runs (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id),
                owner_user_id UUID NOT NULL, input_mode TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','preparing_input','analysing','saving_result','completed','failed')),
                configuration JSONB NOT NULL, result JSONB, error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, processing_ms BIGINT
            )""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS vault_ken_active
                ON vault_ken_runs(asset_id)
                WHERE status IN ('queued','preparing_input','analysing','saving_result')""")
            conn.execute("CREATE INDEX IF NOT EXISTS vault_ken_history ON vault_ken_runs(asset_id,created_at DESC)")
            conn.execute("""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ken_result_binding'
                               AND conrelid='vault_ken_runs'::regclass) THEN
                    ALTER TABLE vault_ken_runs ADD CONSTRAINT ken_result_binding CHECK (
                        result IS NULL OR NOT (configuration ? 'result_binding_version') OR
                        ((result->>'asset_id'=asset_id::text AND result->>'run_id'=id::text
                          AND result->>'input_fingerprint'=configuration #>> '{input_integrity,input_fingerprint}') IS TRUE));
                END IF;
            END $$""")
            conn.execute("""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ken_completed_attestation_v5'
                               AND conrelid='vault_ken_runs'::regclass) THEN
                    ALTER TABLE vault_ken_runs ADD CONSTRAINT ken_completed_attestation_v5 CHECK (
                        status <> 'completed' OR NOT (configuration ? 'attestation_version') OR
                        ((result #>> '{input_attestation,status}'='verified'
                          AND result #>> '{input_attestation,version}'=configuration->>'attestation_version'
                          AND result #>> '{input_attestation,asset_id}'=asset_id::text
                          AND result #>> '{input_attestation,run_id}'=id::text
                          AND result #>> '{input_attestation,request_nonce}'=configuration->>'request_nonce'
                          AND result #>> '{input_attestation,input_fingerprint}'=configuration #>> '{input_integrity,input_fingerprint}'
                          AND result #>> '{input_attestation,resolved_input_sha256}'=configuration->>'expected_input_sha256'
                          AND result #> '{input_attestation,resolved_input_size}'=configuration->'expected_input_size'
                          AND length(result #>> '{input_attestation,decoded_input_fingerprint}')=64
                          AND (result #>> '{input_attestation,decoded_manifest,frame_count}')::int BETWEEN 1 AND
                              LEAST(1802,COALESCE((configuration #>> '{runtime_video,max_decoded_frames}')::int,16))
                          AND (input_mode <> 'native_video' OR
                               (result #>> '{input_attestation,runtime_opened_sha256}'=configuration->>'expected_input_sha256'
                                AND result #> '{input_attestation,runtime_opened_size}'=configuration->'expected_input_size'))) IS TRUE));
                END IF;
            END $$""")

            from app import home_video_backfill
            home_video_backfill.initialize(conn)
            ken_corrections.initialize(conn)
            ken_titles.initialize(conn)
            from app import ken_owner_style
            ken_owner_style.initialize(conn)

    def queue(self, asset_id, owner_id, config):
        return self.queue_ken(asset_id,owner_id,config)

    def history(self, asset_id: UUID, owner_id: UUID) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM vault_ken_runs WHERE asset_id=%s AND owner_user_id=%s ORDER BY created_at DESC,id DESC LIMIT 50", (asset_id, owner_id)).fetchall()

    def get(self, run_id: UUID) -> dict | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM vault_ken_runs WHERE id=%s", (run_id,)).fetchone()

    def bindings(self, asset_id: UUID, owner_id: UUID) -> list[dict]:
        # This diagnostic projection must never select description/raw response JSON.
        with self.connect() as conn:
            return conn.execute("""SELECT asset_id,id AS run_id,
                COALESCE(configuration #>> '{source,verified_sha256}',configuration #>> '{source,catalogue_sha256}') AS source_fingerprint,
                configuration #>> '{input_integrity,input_fingerprint}' AS input_fingerprint,
                configuration->>'expected_input_sha256' AS expected_input_sha256,
                configuration->'expected_input_size' AS expected_input_size,
                result #>> '{input_attestation,status}' AS attestation_status,
                result #>> '{input_attestation,resolved_input_sha256}' AS resolved_input_sha256,
                result #> '{input_attestation,resolved_input_size}' AS resolved_input_size,
                result #>> '{input_attestation,runtime_opened_sha256}' AS runtime_opened_sha256,
                result #>> '{input_attestation,decoded_input_fingerprint}' AS decoded_input_fingerprint,
                CASE WHEN result IS NOT NULL THEN asset_id END AS persisted_result_asset_id,
                CASE WHEN result IS NOT NULL THEN id END AS persisted_result_run_id,
                result->>'asset_id' AS embedded_result_asset_id,result->>'run_id' AS embedded_result_run_id
                FROM vault_ken_runs WHERE asset_id=%s AND owner_user_id=%s
                ORDER BY created_at DESC,id DESC LIMIT 50""", (asset_id, owner_id)).fetchall()

    @contextmanager
    def worker_lock(self):
        # A session lock spans input preparation AND heavy inference, across processes.
        with psycopg.connect(self.conninfo, row_factory=dict_row, autocommit=True) as conn:
            acquired = conn.execute("SELECT pg_try_advisory_lock(761001)").fetchone()["pg_try_advisory_lock"]
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute("SELECT pg_advisory_unlock(761001)")

    def has_active_runs(self):
        with self.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM vault_ken_runs WHERE status IN ('preparing_input','analysing','saving_result') LIMIT 1").fetchone())

    def recover(self):
        # Called only by the holder of the global worker lock. Never races a live worker.
        with self.connect() as conn:
            conn.execute("""UPDATE vault_ken_runs SET status='failed',error='Interrupted by worker restart',
                configuration=jsonb_set(configuration,'{failure_diagnostic}',jsonb_build_object(
                    'stage',status,'code','worker_interrupted','retryable',true)),
                completed_at=CURRENT_TIMESTAMP,processing_ms=(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-started_at))*1000)::bigint
                WHERE status IN ('preparing_input','analysing','saving_result')""")

    def claim(self) -> dict | None:
        with self.connect() as conn:
            return conn.execute("""WITH next AS (SELECT id FROM vault_ken_runs WHERE status='queued'
                ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE vault_ken_runs SET status='preparing_input',started_at=CURRENT_TIMESTAMP
                WHERE id IN (SELECT id FROM next) RETURNING *""").fetchone()

    def update(self, run_id: UUID, status: str, *, config=None, result=None, error=None, processing_ms=None):
        with self.connect() as conn:
            if status in ('completed', 'failed'):
                conn.execute('SELECT pg_advisory_xact_lock(761003)')
            saved = conn.execute("""UPDATE vault_ken_runs SET status=%s,
                configuration=COALESCE(%s,configuration),result=COALESCE(%s,result),error=%s,
                processing_ms=COALESCE(%s,processing_ms),
                completed_at=CASE WHEN %s IN ('completed','failed') THEN CURRENT_TIMESTAMP ELSE completed_at END
                WHERE id=%s RETURNING *""", (status, Jsonb(config) if config is not None else None,
                Jsonb(result) if result is not None else None, error, processing_ms, status, run_id)).fetchone()
            if status == 'completed' and saved:
                from app.home_video_ken import publish_description
                publish_description(conn, saved)


@lru_cache
def get_ken_store() -> KenStore:
    return KenStore(get_database_conninfo())


def prepare_input(asset, config: dict, work: Path) -> dict:
    config["prompt_sha256"] = hashlib.sha256(config["prompt"].encode()).hexdigest()
    root = get_home_videos_path().resolve()
    prefix = "/vault/Home Videos/"
    if asset.asset_type != "Home Videos" or not asset.vault_path.startswith(prefix):
        raise KenFailure("Published Home Video is unavailable")
    source = (root / asset.vault_path.removeprefix(prefix)).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise KenFailure("Published Home Video is unavailable")
    before = source.stat()
    duration = probe_video_duration_ms(source)
    config["source"] = {"asset_id": str(asset.id), "size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
                        "catalogue_sha256": getattr(asset, "sha256", None), "duration_ms": duration}
    payload = {"asset_id": str(asset.id), "run_id": config.get("run_id"), "prompt": config["prompt"],
               "input_mode": config["input_mode"], "parameters": config["parameters"]}
    if config.get('correction_version') == ken.PHASE:
        context = config.setdefault('correction_context', ken.correction_context('', []))
        config['correction_fingerprint'] = digest_json(context)
        payload.update(correction_context=context, correction_fingerprint=config['correction_fingerprint'])
    if config["input_mode"] == "native_video":
        payload.update(prepare_native(source, root, config, work))
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise KenFailure("Video changed during native input preparation; retry")
        seal_input(asset.id, config, payload)
        publish_native(config, payload, work)
        reference = payload["native_sha256"]
        prepared = work / "prepared.mp4" if config["runtime_video"]["prepared_kind"] == "prepared_native_video" else source
        expect_input(config, payload, reference, prepared.stat().st_size)
        return payload
    raise KenFailure('Only native-video KEN input is supported')


def export_normal_metadata(store, vault_store, run_id):
    from app.home_video_ken import DESTINATION
    if not hasattr(vault_store, '_export_sidecar'):
        return
    try:
        run = store.get(run_id)
        if run and run['status'] == 'completed' and run['configuration'].get('metadata_destination') == DESTINATION:
            vault_store._export_sidecar(vault_store.get_catalogued_asset_by_id(run['asset_id']))
    except Exception as error:
        # Canonical DB completion remains valid; normal sidecar refresh can retry.
        LOG.warning('KEN sidecar export failed for run %s (%s)', run_id, type(error).__name__)


def process_next(store, vault_store) -> UUID | None:
    with store.worker_lock() as acquired:
        if not acquired:
            return None
        work_root = os.getenv("PV_KEN_WORK_ROOT")
        if work_root:
            # A gateway inference can outlive a restarted backend. Keep its video
            # capability until it finishes; do not overlap preparation with it.
            states = [adapter.health().get("status") for adapter in [ADAPTER]]
            if any(state in ('busy','loading') for state in states):
                return None
            if any(state != 'available' for state in states) and (not hasattr(store,'has_active_runs') or store.has_active_runs()):
                return None
            cleanup_abandoned_inputs(Path(work_root))
        store.recover()
        if isinstance(store, KenStore):
            from app.home_video_backfill import admit_next
            admit_next(store, vault_store, configuration())
        if hasattr(store, 'process_pending_title'):
            title_run = store.process_pending_title(ADAPTER)
            if title_run is not None:
                export_normal_metadata(store, vault_store, title_run)
                return title_run
        run = store.claim()
        if run is None:
            return None
        start = time.monotonic()
        config = deepcopy(run["configuration"])
        config["run_id"] = str(run["id"])
        config["result_binding_version"] = RESULT_BINDING_VERSION
        stage = 'asset_resolution'
        try:
            asset = vault_store.get_catalogued_asset_by_id(run["asset_id"])
            if asset is None or asset.owner_user_id != run["owner_user_id"]:
                raise KenFailure("Published Home Video is unavailable for the owner")
            stage = 'engine_validation'
            selected = ENGINE
            if config["model_revision"] != selected.model_revision or config["runtime"] != selected.runtime:
                raise KenFailure("KEN configuration changed; create a new run")
            if work_root:
                Path(work_root).mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"pv-ken-{run['id']}-", dir=work_root) as directory:
                stage = 'input_preparation'
                payload = prepare_input(asset, config, Path(directory))
                stage = 'input_integrity'
                verify_input(run["asset_id"], config, payload)
                payload["run_id"] = str(run["id"])
                store.update(run["id"], "analysing", config=config)
                stage = 'inference'
                result = ADAPTER.analyse(payload)
                stage = 'result_integrity'
                verify_result(run["asset_id"], run["id"], config, result)
                result = enforce_attestation(payload, result)
                result = warn_on_repetition(result)
                stage = 'result_persistence'
                store.update(run["id"], "saving_result", result=result)
                if result.get("error"):
                    raise KenFailure(result["error"])
            config['automatic_title'] = {'status':'pending','attempts':0}
            store.update(run["id"], "completed", config=config, processing_ms=round((time.monotonic()-start)*1000))
        except Exception as error:
            # Persist code locations, never exception locals, arbitrary messages or
            # request/result text. Ordinary UI keeps a bounded friendly message.
            trace = error.__traceback__
            while trace and trace.tb_next:
                trace = trace.tb_next
            config['failure_diagnostic'] = {'stage': stage, 'exception_type': type(error).__name__,
                'function': trace.tb_frame.f_code.co_name if trace else None,
                'line': trace.tb_lineno if trace else None}
            if hasattr(error,'service_diagnostic'):
                config['failure_diagnostic']['service']=error.service_diagnostic
            known_policy_error = isinstance(error, ValueError) and str(error) == ken.DURATION_LIMIT_ERROR
            if known_policy_error:
                config['failure_diagnostic']['code'] = 'ken_duration_limit'
            from app.ken_failure import failure_info
            failure = failure_info({'configuration':config, 'error':str(error) if isinstance(error, KenFailure) else None})
            config['failure_diagnostic'].update(code=failure['code'], retryable=failure['retryable'])
            LOG.warning("KEN run %s failed at %s (%s)", run["id"], stage, type(error).__name__)
            store.update(run["id"], "failed", config=config, error=str(error) if isinstance(error, KenFailure) or known_policy_error else "KEN analysis failed; source remains unchanged",
                         processing_ms=round((time.monotonic()-start)*1000))
        export_normal_metadata(store, vault_store, run['id'])
        return run["id"]


async def run_worker():
    from app.vault_master import get_vault_master_store
    while True:
        try:
            await asyncio.to_thread(process_next, get_ken_store(), get_vault_master_store())
        except Exception:
            LOG.exception("KEN worker iteration failed")
        await asyncio.sleep(2)
