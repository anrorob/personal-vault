"""Authenticated Jellyfin playback adapter for canonical TV Episodes."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from app.auth import AuthenticatedUsername, authenticated_user_id
from app.config import get_jellyfin_api_key, get_jellyfin_url
from app.jellyfin import HlsResourceStore, JellyfinClient, JellyfinMovie, JellyfinStream, JellyfinUnavailableError, rewrite_hls_playlist
from app.jellyfin_paths import JellyfinPathMappingError, jellyfin_media_path
from app.tv_shows import PostgresTvShowStore, TvStore
from app.playback_diagnostics import playback_info, require_development
from app.playback_policy import PlaybackPlanRequest, create_plan, require_playback_enabled

router = APIRouter(prefix="/api/tv-shows/episodes", tags=["tv playback"])
private_resources = HlsResourceStore()


def get_jellyfin_client() -> JellyfinClient:
    return JellyfinClient(base_url=get_jellyfin_url(), api_key=get_jellyfin_api_key())


JellyfinPlaybackClient = Annotated[JellyfinClient, Depends(get_jellyfin_client)]


def _source_path(episode_id: UUID, username: str, store: PostgresTvShowStore) -> Path:
    source = store.visible_episode_source(episode_id, authenticated_user_id(username))
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
    from app.storage_placement import resolve_metadata_placement

    root = Path(os.getenv("PV_TV_SHOWS_PATH", "/media/tv")).resolve(strict=False)
    logical = PurePosixPath(source.vault_path)
    try:
        relative = logical.relative_to("/vault/Theatre/TV Shows")
    except ValueError:
        raise HTTPException(status_code=404, detail="Episode not found") from None
    if source.storage_placement is not None:
        if source.storage_placement.get("relative_path") != str(logical.relative_to("/vault")):
            raise HTTPException(status_code=404, detail="Episode not found")
        try:
            placed = resolve_metadata_placement({"storage_placement": source.storage_placement})
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Episode not found") from None
        if placed is None or not placed.is_file():
            raise HTTPException(status_code=404, detail="Episode not found")
        return placed
    path = root.joinpath(*relative.parts).resolve(strict=False)
    if not path.is_relative_to(root):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
    return path


def _playback_episode(episode_id: UUID, username: str, store: PostgresTvShowStore, client: JellyfinClient) -> JellyfinMovie:
    source = _source_path(episode_id, username, store)
    try:
        episode = client.find_episode_by_path(jellyfin_media_path(source, "tv"))
    except (JellyfinUnavailableError, JellyfinPathMappingError):
        raise HTTPException(status_code=503, detail="Playback service is unavailable") from None
    if episode is None:
        raise HTTPException(status_code=503, detail="Episode is not available for playback")
    return episode


def _proxy(episode_id: UUID, user_id: UUID, stream: JellyfinStream) -> PlainTextResponse | StreamingResponse:
    is_playlist = stream.url.partition("?")[0].endswith(".m3u8") or (stream.content_type and "mpegurl" in stream.content_type.casefold())
    if is_playlist:
        try:
            playlist = stream.response.read().decode("utf-8")
        finally:
            stream.response.close()
        return PlainTextResponse(
            rewrite_hls_playlist(playlist, stream.url, lambda url: f"/api/tv-shows/episodes/{episode_id}/hls/{private_resources.issue(user_id, url, scope=str(episode_id))}"),
            status_code=stream.status_code, media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    return StreamingResponse(stream.iter_bytes(), status_code=stream.status_code, media_type=stream.content_type or "video/mp2t", headers={**stream.headers, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{episode_id}/playback")
def playback_readiness(episode_id: UUID, username: AuthenticatedUsername, store: TvStore, client: JellyfinPlaybackClient) -> dict[str, object]:
    episode = _playback_episode(episode_id, username, store, client)
    return {"episode_id": str(episode_id), "status": "ready", "container": episode.container,
            "subtitles": [{"index": track.index, "label": track.display_title or track.title or track.language or f"Subtitle {track.index}"} for track in episode.subtitle_tracks]}


@router.get("/{episode_id}/playback-info")
def episode_playback_info(episode_id: UUID, username: AuthenticatedUsername, store: TvStore, subtitle_index: int | None = Query(default=None, ge=0), h264_supported: bool | None = Query(default=None)) -> dict[str, object]:
    require_development()
    source = _source_path(episode_id, username, store)
    if os.getenv("PV_JELLYFIN_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return {"availability": "jellyfin_disabled", "source": None, "playback": None}
    client = get_jellyfin_client()
    try:
        episode = client.find_episode_by_path(jellyfin_media_path(source, "tv"))
    except (JellyfinUnavailableError, JellyfinPathMappingError):
        raise HTTPException(status_code=503, detail="Playback diagnostics are unavailable") from None
    if episode is None:
        raise HTTPException(status_code=503, detail="Episode is not indexed by the playback service")
    try:
        source_info = client.playback_source_info(episode)
        decision = client.playback_decision(episode, h264_supported=h264_supported is True)
    except JellyfinUnavailableError:
        raise HTTPException(status_code=503, detail="Playback diagnostics are unavailable") from None
    return {"availability": "ready", **playback_info(episode, source_info, subtitle_index, decision, h264_supported)}


@router.post("/{episode_id}/playback-plan")
def episode_playback_plan(episode_id: UUID, payload: PlaybackPlanRequest, username: AuthenticatedUsername,
                         store: TvStore, client: JellyfinPlaybackClient) -> dict:
    _source_path(episode_id, username, store)
    require_playback_enabled()
    item = _playback_episode(episode_id, username, store, client)
    return create_plan(client, item, payload, private_resources, authenticated_user_id(username),
                       str(episode_id), f"/api/tv-shows/episodes/{episode_id}/hls")


@router.get("/{episode_id}/stream.mp4")
def stream_episode(episode_id: UUID, request: Request, username: AuthenticatedUsername, store: TvStore, client: JellyfinPlaybackClient) -> StreamingResponse:
    episode = _playback_episode(episode_id, username, store, client)
    try:
        stream = client.open_browser_stream(episode, range_header=request.headers.get("Range"))
    except JellyfinUnavailableError:
        raise HTTPException(status_code=503, detail="Playback stream is unavailable") from None
    return StreamingResponse(stream.iter_bytes(), status_code=stream.status_code, media_type="video/mp4", headers={**stream.headers, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{episode_id}/download/original")
def download_episode(episode_id: UUID, username: AuthenticatedUsername, store: TvStore) -> FileResponse:
    path = _source_path(episode_id, username, store)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Episode not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{episode_id}/hls/master.m3u8", response_model=None)
def hls_master(episode_id: UUID, username: AuthenticatedUsername, store: TvStore, client: JellyfinPlaybackClient, subtitle_index: int | None = Query(default=None, ge=0)) -> PlainTextResponse | StreamingResponse:
    episode = _playback_episode(episode_id, username, store, client)
    try:
        if subtitle_index is not None and subtitle_index not in {track.index for track in episode.subtitle_tracks}:
            raise HTTPException(status_code=404, detail="Subtitle not found")
        return _proxy(episode_id, authenticated_user_id(username), client.open_hls_master(episode, subtitle_stream_index=subtitle_index))
    except JellyfinUnavailableError:
        raise HTTPException(status_code=503, detail="Playback stream is unavailable") from None


@router.get("/{episode_id}/hls/{resource_token}", response_model=None)
def hls_resource(episode_id: UUID, resource_token: str, request: Request, username: AuthenticatedUsername, store: TvStore, client: JellyfinPlaybackClient) -> PlainTextResponse | StreamingResponse:
    user_id = authenticated_user_id(username)
    # Authorize the requested canonical Episode before resolving an opaque
    # resource token; a UUID path cannot turn another Show's token into access.
    _source_path(episode_id, username, store)
    url = private_resources.resolve(user_id, resource_token, scope=str(episode_id))
    if url is None:
        raise HTTPException(status_code=404, detail="Playback resource not found")
    try:
        return _proxy(episode_id, user_id, client.open_resource(url, range_header=request.headers.get("Range")))
    except JellyfinUnavailableError:
        raise HTTPException(status_code=503, detail="Playback stream is unavailable") from None
