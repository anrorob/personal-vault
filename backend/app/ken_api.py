"""KEN routes are development-only and enforce immutable asset ownership."""

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUsername, authenticated_user_id
from app.vault_master import VaultMasterStore, get_vault_master_store, asset_is_editable_by
from app.ken_service import ADAPTER, KenFailure, configuration, enabled, get_ken_store
from app.ken_binding import verify_stored_run
from app.ken_service import ENGINE
from app import ken_config as ken



def public_run(run):
    if run.get('status') == 'failed':
        from app.ken_failure import failure_info
        run = {**run, 'failure':failure_info(run)}
    # Clean generated display text without rewriting historical records/fingerprints.
    if run.get('configuration', {}).get('metadata_destination') == 'home-video-ken-v1':
        run = {**run, 'error': (run.get('error') or '').replace('Experimental analysis', 'KEN analysis') or None}
    if run.get('configuration', {}).get('correction_version') == ken.PHASE and run.get('result'):
        return {**run, 'result': {**run['result'], 'description': ken.clean_generated_output(run['result'].get('description'))}}
    return run


def guard(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    if not enabled():
        raise HTTPException(404, "KEN is unavailable")


ken_router = APIRouter(prefix='/api/personal-videos/ken', tags=['home-video-ken'], dependencies=[Depends(guard)])


@ken_router.get('/engine')
def ken_engine(identity: AuthenticatedUsername):
    authenticated_user_id(identity)
    return {**configuration(), **ADAPTER.health()}


def queue_normal_ken(asset_id, identity, vault, store, *, text=None, parent_run_id=None):
    from app.home_video_ken import DESTINATION
    asset = owner_asset(asset_id, identity, vault)
    if ADAPTER.health()['status'] not in ('available', 'busy'):
        raise HTTPException(503, 'KEN is unavailable')
    config = {**configuration(), 'metadata_destination': DESTINATION}
    try:
        return store.queue_ken(asset.id, asset.owner_user_id, config, text=text, parent_run_id=parent_run_id)
    except (KenFailure, ValueError) as error:
        raise HTTPException(409, str(error).replace('experiments', 'analyses')) from error


@ken_router.post('/assets/{asset_id}/runs', status_code=202)
def queue_home_video_ken(asset_id: UUID, identity: AuthenticatedUsername,
                         vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    return queue_normal_ken(asset_id, identity, vault, store)


@ken_router.post('/assets/{asset_id}/runs/{run_id}/retry', status_code=202)
def retry_home_video_ken(asset_id: UUID, run_id: UUID, identity: AuthenticatedUsername,
                        vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    from app.home_video_backfill import retry_failed
    asset = owner_asset(asset_id, identity, vault)
    if ADAPTER.health()['status'] not in ('available', 'busy'):
        raise HTTPException(503, 'KEN is unavailable')
    try:
        return public_run(retry_failed(store, asset, run_id, configuration()))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@ken_router.get('/assets/{asset_id}/runs')
def home_video_ken_history(asset_id: UUID, identity: AuthenticatedUsername,
                           vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store), binding_only: bool = False):
    if binding_only:
        return history(asset_id, identity, vault, store, binding_only=True)
    from app.home_video_ken import DESTINATION
    return [run for run in history(asset_id, identity, vault, store)
            if run['configuration'].get('metadata_destination') == DESTINATION]


def owner_asset(asset_id, identity, vault):
    asset = vault.get_catalogued_asset_by_id(asset_id)
    if asset is None or not asset_is_editable_by(asset, identity) or asset.asset_type != "Home Videos" or not asset.vault_path.startswith("/vault/Home Videos/"):
        raise HTTPException(404, "Video was not found")
    return asset


def history(asset_id: UUID, identity: AuthenticatedUsername,
            vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store), binding_only: bool = False):
    asset = owner_asset(asset_id, identity, vault)
    if binding_only:
        return store.bindings(asset.id, asset.owner_user_id)
    try:
        runs = store.history(asset.id, asset.owner_user_id)
        return [public_run(verify_stored_run(run, asset.id, asset.owner_user_id)) for run in runs]
    except ValueError as error:
        raise HTTPException(409, "KEN result identity mismatch") from error


@ken_router.get('/runs/{run_id}')
def get_run(run_id: UUID, identity: AuthenticatedUsername,
            vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store), asset_id: UUID | None = None):
    run = store.get(run_id)
    if run is None or run["owner_user_id"] != authenticated_user_id(identity) or (asset_id is not None and run["asset_id"] != asset_id):
        raise HTTPException(404, "Run was not found")
    owner_asset(run["asset_id"], identity, vault)
    try:
        return public_run(verify_stored_run(run, run["asset_id"], run["owner_user_id"]))
    except ValueError as error:
        raise HTTPException(409, "KEN result identity mismatch") from error


class AdjustmentRequest(BaseModel):
    parent_run_id: UUID
    text: str = Field(min_length=1, max_length=2000)


@ken_router.post('/assets/{asset_id}/corrections', status_code=202)
def adjust_home_video_ken(asset_id: UUID, body: AdjustmentRequest, identity: AuthenticatedUsername,
                          vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    return queue_normal_ken(asset_id, identity, vault, store, text=body.text, parent_run_id=body.parent_run_id)


@ken_router.get('/assets/{asset_id}/corrections')
def corrections(asset_id: UUID, identity: AuthenticatedUsername,
                vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    asset = owner_asset(asset_id, identity, vault)
    return store.corrections(asset.id, asset.owner_user_id)


class TitleRequest(BaseModel):
    run_id: UUID


class ManualTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)


@ken_router.get('/assets/{asset_id}/titles')
def titles(asset_id: UUID, identity: AuthenticatedUsername,
           vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    asset = owner_asset(asset_id, identity, vault)
    return {'asset_id':str(asset.id),'display_title':asset.display_title,
            'manual_title':bool(asset.user_overrides.get('display_title') or asset.metadata_provenance.get('display_title')=='user_override'),
            'suggestions':store.titles(asset.id,asset.owner_user_id)}


@ken_router.post('/assets/{asset_id}/titles')
def generate_title(asset_id: UUID, body: TitleRequest, identity: AuthenticatedUsername,
                   vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    asset = owner_asset(asset_id, identity, vault)
    try:
        return store.generate_title(asset.id,asset.owner_user_id,body.run_id,ADAPTER)
    except (ValueError,KenFailure) as error:
        raise HTTPException(409,str(error)) from error


def export_title(vault, asset_id):
    # Same owner sidecar export used by existing catalogue edits; original media is not rewritten.
    exporter = getattr(vault,'_export_sidecar',None)
    if exporter:
        exporter(vault.get_catalogued_asset_by_id(asset_id))


@ken_router.post('/assets/{asset_id}/titles/{suggestion_id}/accept')
def accept_title(asset_id: UUID, suggestion_id: UUID, identity: AuthenticatedUsername,
                 vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    asset = owner_asset(asset_id,identity,vault)
    try:
        result=store.set_video_title(asset.id,asset.owner_user_id,identity,suggestion_id=suggestion_id)
    except ValueError as error:
        raise HTTPException(409,str(error)) from error
    export_title(vault,asset.id)
    return result


@ken_router.patch('/assets/{asset_id}/title')
def edit_title(asset_id: UUID, body: ManualTitleRequest, identity: AuthenticatedUsername,
               vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    asset=owner_asset(asset_id,identity,vault)
    try:
        result=store.set_video_title(asset.id,asset.owner_user_id,identity,manual_title=body.title)
    except ValueError as error:
        raise HTTPException(409,str(error)) from error
    export_title(vault,asset.id)
    return result

class KenPreferencesRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    preferences: dict


@ken_router.get('/preferences')
def get_ken_preferences(identity: AuthenticatedUsername, store=Depends(get_ken_store)):
    from app.ken_owner_style import preferences
    with store.connect() as conn:
        return preferences(conn, authenticated_user_id(identity))


@ken_router.put('/preferences')
def put_ken_preferences(body: KenPreferencesRequest, identity: AuthenticatedUsername, store=Depends(get_ken_store)):
    from app.ken_owner_style import save_preferences, preferences
    try:
        with store.connect() as conn:
            owner = authenticated_user_id(identity)
            save_preferences(conn, owner, body.preferences)
            return preferences(conn, owner)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@ken_router.post('/backfill', status_code=202)
def backfill_home_videos(identity: AuthenticatedUsername,
    vault: VaultMasterStore = Depends(get_vault_master_store), store=Depends(get_ken_store)):
    from app.home_video_backfill import enqueue
    from app.media_formats import VIDEO_EXTENSIONS
    from pathlib import Path
    owner_id = authenticated_user_id(identity)
    if ADAPTER.health()['status'] not in ('available', 'busy'):
        raise HTTPException(503, 'KEN is unavailable')
    assets = [asset for asset in vault.list_owned_catalogued_assets_by_user_id(owner_id)
              if asset.asset_type == 'Home Videos' and asset.lifecycle_state == 'active'
              and asset.vault_path.startswith('/vault/Home Videos/')
              and Path(asset.filename).suffix.casefold() in VIDEO_EXTENSIONS]
    return enqueue(store, assets, owner_id, configuration())


@ken_router.get('/backfill')
def home_video_backfill_progress(identity: AuthenticatedUsername, store=Depends(get_ken_store)):
    from app.home_video_backfill import progress
    return progress(store, authenticated_user_id(identity), configuration())
