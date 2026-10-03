"""Canonical Music Video projection and authorised reusable video playback."""
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.video_thumbnails import get_video_thumbnail_cache_path, video_thumbnail
from app.auth import AuthenticatedUsername, authenticated_user_id
from app.asset_favorites import get_asset_favorites
from app.home_video_playback import get_home_video_playback_cache_path, playback_status, playback_file
from app.music_video_identity import ASSET_TYPE, CONTENT_TYPE, ROOT
from app.storage_placement import resolve_metadata_placement
from app.vault_master import get_vault_master_store, asset_is_editable_by

router = APIRouter(prefix="/api/music-videos", tags=["music"])
HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def visible_asset(asset_id, username, store):
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if (asset is None or asset.owner_user_id is None or asset.asset_type != ASSET_TYPE
            or asset.lifecycle_state != "active" or not asset.vault_path.startswith(ROOT + "/")
            or not asset.mime_type.startswith("video/")):
        raise HTTPException(404, "Music Video was not found")
    return asset


def summary(asset, username):
    context = asset.metadata.get("source_context", {})
    context = context if isinstance(context, dict) and context.get("content_type") == CONTENT_TYPE else {}
    # Home Video KEN titles are retained as history, never projected here.
    artist = asset.user_overrides.get("artist") or context.get("artist")
    title = asset.user_overrides.get("display_title") or context.get("title")
    if not title and asset.metadata_provenance.get("display_title") in {"user_override", "imported"}:
        title = asset.display_title
    return {"asset_id": str(asset.id), "owner_user_id": str(asset.owner_user_id),
            "content_type": CONTENT_TYPE, "artist": artist or None, "title": title or None,
            "can_edit": asset_is_editable_by(asset, username),
            "playback_url": f"/api/music-videos/{asset.id}/playback",
            "thumbnail_url": f"/api/music-videos/{asset.id}/thumbnail"}


@router.get("")
def list_videos(response: Response, username: AuthenticatedUsername, store=Depends(get_vault_master_store),
                favorites=Depends(get_asset_favorites)):
    response.headers.update(HEADERS)
    starred = favorites.list(authenticated_user_id(username))
    items = []
    for candidate in store.list_catalogued_assets_by_vault_path_prefix(ROOT + "/"):
        try:
            items.append({**summary(visible_asset(candidate.id, username, store), username),
                          "favorite": candidate.id in starred})
        except HTTPException as error:
            if error.status_code != 404:
                raise
    return items


class MetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    artist: str | None = Field(default=None, max_length=240)
    title: str | None = Field(default=None, max_length=240)


@router.patch("/{asset_id}/metadata")
def edit_metadata(asset_id: UUID, edit: MetadataEdit, response: Response,
                  username: AuthenticatedUsername, store=Depends(get_vault_master_store)):
    asset = visible_asset(asset_id, username, store)
    if not asset_is_editable_by(asset, username):
        raise HTTPException(404)
    if not edit.model_fields_set:
        raise HTTPException(422, "At least one field is required")
    changes = {"display_title" if key == "title" else key: value
               for key, value in edit.model_dump(exclude_unset=True).items()}
    updated = store.update_catalogued_asset_metadata(asset.id, changes, username)
    if updated is None:
        raise HTTPException(404)
    response.headers.update(HEADERS)
    return summary(updated, username)


def media_source(asset):
    try:
        path = resolve_metadata_placement(asset.metadata)
        if path is None or not path.is_file():
            raise ValueError()
        return path
    except (OSError, ValueError):
        raise HTTPException(409, "Music Video storage is unavailable") from None


@router.get("/{asset_id}/playback")
def playback(asset_id: UUID, response: Response, username: AuthenticatedUsername,
             retry: bool = False, store=Depends(get_vault_master_store),
             cache: Path = Depends(get_home_video_playback_cache_path)):
    asset = visible_asset(asset_id, username, store)
    state = playback_status(media_source(asset), asset.id, asset.sha256, cache, retry=retry)
    response.headers.update(HEADERS)
    return {"status": state, "playback_url": f"/api/music-videos/{asset.id}/content"
            if state in {"direct", "ready"} else None}


@router.get("/{asset_id}/content")
def content(asset_id: UUID, username: AuthenticatedUsername,
            store=Depends(get_vault_master_store), cache: Path = Depends(get_home_video_playback_cache_path)):
    asset = visible_asset(asset_id, username, store)
    media = playback_file(media_source(asset), asset.id, asset.sha256, cache)
    if media is None:
        raise HTTPException(409, "Playback is not ready", headers=HEADERS)
    return FileResponse(media, media_type="video/mp4", headers={**HEADERS, "Content-Disposition": "inline"})


@router.get("/{asset_id}/thumbnail")
def thumbnail(asset_id: UUID, username: AuthenticatedUsername,
              store=Depends(get_vault_master_store), cache: Path = Depends(get_video_thumbnail_cache_path)):
    asset = visible_asset(asset_id, username, store)
    image = video_thumbnail(media_source(asset), asset.id, cache)
    if image is None:
        raise HTTPException(503, "Thumbnail is unavailable", headers=HEADERS)
    return FileResponse(image, media_type="image/jpeg", headers=HEADERS)


class FavoriteEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    favorite: bool


@router.put("/{asset_id}/favorite")
def set_favorite(asset_id: UUID, edit: FavoriteEdit, response: Response,
                 username: AuthenticatedUsername, store=Depends(get_vault_master_store),
                 favorites=Depends(get_asset_favorites)):
    user_id = authenticated_user_id(username)
    visible_asset(asset_id, username, store)
    favorites.set(user_id, asset_id, edit.favorite)
    response.headers.update(HEADERS)
    return {"asset_id": str(asset_id), "favorite": edit.favorite}
