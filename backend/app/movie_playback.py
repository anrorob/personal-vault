from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.auth import AuthenticatedUsername, authenticated_user_id
from app.config import get_jellyfin_api_key, get_jellyfin_url
from app.jellyfin import (
    HlsResourceStore,
    JellyfinClient,
    JellyfinMovie,
    JellyfinStream,
    JellyfinUnavailableError,
    rewrite_hls_playlist,
)
from app.movies import (
    MoviesLibraryPath,
    MovieLibraryRoots,
    MOVIE_VAULT_ROOT,
    VaultMasterCatalogue,
    _asset_for_movie_id,
    resolve_catalogued_movie_path,
)


router = APIRouter(prefix="/api/movies", tags=["movie playback"])
logger = logging.getLogger("pv.movie_playback")
private_resources = HlsResourceStore()


class MoviePlaybackSubtitleTrack(BaseModel):
    index: int
    title: str | None
    display_title: str | None
    language: str | None
    codec: str | None
    is_external: bool
    is_default: bool
    is_forced: bool
    is_hearing_impaired: bool


class MoviePlaybackReadiness(BaseModel):
    movie_id: str
    status: Literal["ready"]
    container: str | None
    video_codec: str | None
    audio_codecs: list[str]
    subtitles: list[MoviePlaybackSubtitleTrack]


def get_jellyfin_client() -> JellyfinClient:
    return JellyfinClient(
        base_url=get_jellyfin_url(),
        api_key=get_jellyfin_api_key(),
    )


JellyfinPlaybackClient = Annotated[
    JellyfinClient,
    Depends(get_jellyfin_client),
]


def _resolve_playback_movie(
    movie_id: str,
    username: str,
    library_path: Path,
    library_roots: dict,
    jellyfin_client: JellyfinClient,
    store: VaultMasterCatalogue,
) -> JellyfinMovie:
    try:
        asset = _asset_for_movie_id(movie_id, username, library_path, store)
    except OSError:
        logger.exception(
            "Movie library lookup failed for authenticated user=%r",
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Movie library is unavailable",
        ) from None

    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Movie not found",
        )

    try:
        source_path = resolve_catalogued_movie_path(
            asset, {**library_roots, MOVIE_VAULT_ROOT: library_path}
        )
    except (OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Movie not found",
        )

    try:
        playback_movie = jellyfin_client.find_movie_by_path(
            source_path,
        )
    except JellyfinUnavailableError:
        logger.exception(
            "Playback lookup failed for movie_id=%s user=%r",
            movie_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playback service is unavailable",
        ) from None

    if playback_movie is None:
        logger.warning(
            "Movie is not indexed by playback service movie_id=%s user=%r",
            movie_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Movie is not available for playback",
        )

    return playback_movie


def _resolve_original_movie_path(
    movie_id: str,
    username: str,
    library_path: Path,
    library_roots: dict,
    store: VaultMasterCatalogue,
) -> Path:
    asset = _asset_for_movie_id(movie_id, username, library_path, store)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found")
    try:
        return resolve_catalogued_movie_path(
            asset, {**library_roots, MOVIE_VAULT_ROOT: library_path}
        )
    except (OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found")


def _resolve_playback_feature(
    movie_id: str,
    feature_id: str,
    username: str,
    library_path: Path,
    jellyfin_client: JellyfinClient,
    store,
) -> JellyfinMovie:
    asset = _asset_for_movie_id(movie_id, username, library_path, store)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Movie not found",
        )
    provider_item_id: str | None = None
    for field in ("extras", "trailers"):
        entries = asset.effective_metadata.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("provider_item_id")
            if (
                isinstance(candidate, str)
                and hashlib.sha256(
                    candidate.encode("utf-8")
                ).hexdigest()[:16] == feature_id
            ):
                provider_item_id = candidate
                break
    if provider_item_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Movie feature not found",
        )
    try:
        playback_feature = jellyfin_client.get_video_by_id(provider_item_id)
    except JellyfinUnavailableError:
        logger.exception(
            "Feature playback lookup failed movie_id=%s feature_id=%s user=%r",
            movie_id,
            feature_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playback service is unavailable",
        ) from None
    if playback_feature is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Movie feature is not available for playback",
        )
    return playback_feature


@router.get(
    "/{movie_id}/playback",
    response_model=MoviePlaybackReadiness,
)
def get_movie_playback_readiness(
    movie_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    library_roots: MovieLibraryRoots,
    jellyfin_client: JellyfinPlaybackClient,
    store: VaultMasterCatalogue,
) -> MoviePlaybackReadiness:
    playback_movie = _resolve_playback_movie(
        movie_id=movie_id,
        username=username,
        library_path=library_path,
        library_roots=library_roots,
        jellyfin_client=jellyfin_client,
        store=store,
    )

    return MoviePlaybackReadiness(
        movie_id=movie_id,
        status="ready",
        container=playback_movie.container,
        video_codec=playback_movie.video_codec,
        audio_codecs=list(playback_movie.audio_codecs),
        subtitles=[
            MoviePlaybackSubtitleTrack(
                index=track.index,
                title=track.title,
                display_title=track.display_title,
                language=track.language,
                codec=track.codec,
                is_external=track.is_external,
                is_default=track.is_default,
                is_forced=track.is_forced,
                is_hearing_impaired=track.is_hearing_impaired,
            )
            for track in playback_movie.subtitle_tracks
        ],
    )


@router.get("/{movie_id}/stream.mp4")
def stream_movie(
    movie_id: str,
    request: Request,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    library_roots: MovieLibraryRoots,
    jellyfin_client: JellyfinPlaybackClient,
    store: VaultMasterCatalogue,
) -> StreamingResponse:
    playback_movie = _resolve_playback_movie(
        movie_id=movie_id,
        username=username,
        library_path=library_path,
        library_roots=library_roots,
        jellyfin_client=jellyfin_client,
        store=store,
    )

    try:
        stream = jellyfin_client.open_browser_stream(
            playback_movie,
            range_header=request.headers.get("Range"),
        )
    except JellyfinUnavailableError:
        logger.exception(
            "Playback stream failed for movie_id=%s user=%r",
            movie_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playback stream is unavailable",
        ) from None

    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        media_type="video/mp4",
        headers={
            **stream.headers,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{movie_id}/download/original")
def download_original_movie(
    movie_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    library_roots: MovieLibraryRoots,
    store: VaultMasterCatalogue,
) -> FileResponse:
    source_path = _resolve_original_movie_path(movie_id, username, library_path, library_roots, store)
    return FileResponse(
        source_path,
        filename=source_path.name,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{movie_id}/download/compressed.mp4")
def download_compressed_movie(
    movie_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    library_roots: MovieLibraryRoots,
    jellyfin_client: JellyfinPlaybackClient,
    store: VaultMasterCatalogue,
) -> StreamingResponse:
    source_path = _resolve_original_movie_path(movie_id, username, library_path, library_roots, store)
    playback_movie = _resolve_playback_movie(
        movie_id, username, library_path, library_roots, jellyfin_client, store
    )
    try:
        stream = jellyfin_client.open_browser_stream(playback_movie, range_header=None)
    except JellyfinUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Compressed download is unavailable",
        ) from None
    safe_stem = "".join(character for character in source_path.stem if character.isalnum() or character in " -_").strip() or "movie"
    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        media_type="video/mp4",
        headers={
            **stream.headers,
            "Content-Disposition": f'attachment; filename="{safe_stem}.mp4"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _proxy_hls_response(
    movie_id: str,
    user_id: UUID,
    stream: JellyfinStream,
    diagnostic_headers: dict[str, str] | None = None,
) -> PlainTextResponse | StreamingResponse:
    content_type = stream.content_type
    stream_url = stream.url
    is_playlist = (
        stream_url.partition("?")[0].casefold().endswith(".m3u8")
        or (
            isinstance(content_type, str)
            and "mpegurl" in content_type.casefold()
        )
    )

    if is_playlist:
        try:
            playlist = stream.response.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise JellyfinUnavailableError(
                "Playback playlist is unavailable"
            ) from None
        finally:
            stream.response.close()

        def proxy_url_for(upstream_url: str) -> str:
            token = private_resources.issue(user_id, upstream_url)
            return f"/api/movies/{movie_id}/hls/{token}"

        return PlainTextResponse(
            rewrite_hls_playlist(
                playlist,
                stream_url,
                proxy_url_for,
            ),
            status_code=stream.status_code,
            media_type="application/vnd.apple.mpegurl",
            headers={
                **(diagnostic_headers or {}),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        media_type=content_type or "video/mp2t",
        headers={
            **stream.headers,
            **(diagnostic_headers or {}),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{movie_id}/hls/master.m3u8", response_model=None)
def stream_movie_hls(
    movie_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    library_roots: MovieLibraryRoots,
    jellyfin_client: JellyfinPlaybackClient,
    store: VaultMasterCatalogue,
    subtitle_index: Annotated[int | None, Query(ge=0)] = None,
) -> PlainTextResponse | StreamingResponse:
    user_id = authenticated_user_id(username)
    playback_movie = _resolve_playback_movie(
        movie_id=movie_id,
        username=username,
        library_path=library_path,
        library_roots=library_roots,
        jellyfin_client=jellyfin_client,
        store=store,
    )

    selected_subtitle = next(
        (
            track
            for track in playback_movie.subtitle_tracks
            if track.index == subtitle_index
        ),
        None,
    )
    if subtitle_index is not None and selected_subtitle is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Subtitle track is not available",
        )

    try:
        subtitle_evidence = {
            "X-PV-Subtitle-Stream-Index": (
                str(selected_subtitle.index)
                if selected_subtitle is not None
                else "off"
            ),
            "X-PV-Subtitle-Delivery-Method": (
                "encode" if selected_subtitle is not None else "off"
            ),
        }
        logger.info(
            "HLS subtitle selection movie_id=%s media_source_id=%s "
            "stream_index=%s codec=%s delivery_method=%s",
            movie_id,
            playback_movie.media_source_id,
            selected_subtitle.index if selected_subtitle is not None else None,
            selected_subtitle.codec if selected_subtitle is not None else None,
            "encode" if selected_subtitle is not None else "off",
        )
        stream = jellyfin_client.open_hls_master(
            playback_movie,
            subtitle_stream_index=subtitle_index,
        )
        logger.info(
            "HLS subtitle request accepted movie_id=%s stream_index=%s "
            "delivery_method=%s upstream_status=%s upstream_url=%s",
            movie_id,
            selected_subtitle.index if selected_subtitle is not None else None,
            "encode" if selected_subtitle is not None else "off",
            stream.status_code,
            stream.url,
        )
        return _proxy_hls_response(
            movie_id,
            user_id,
            stream,
            diagnostic_headers=subtitle_evidence,
        )
    except JellyfinUnavailableError:
        logger.exception(
            "HLS playback failed for movie_id=%s user=%r",
            movie_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playback stream is unavailable",
        ) from None


@router.get(
    "/{movie_id}/features/{feature_id}/hls/master.m3u8",
    response_model=None,
)
def stream_movie_feature_hls(
    movie_id: str,
    feature_id: str,
    username: AuthenticatedUsername,
    library_path: MoviesLibraryPath,
    jellyfin_client: JellyfinPlaybackClient,
    store: VaultMasterCatalogue,
) -> PlainTextResponse | StreamingResponse:
    user_id = authenticated_user_id(username)
    playback_feature = _resolve_playback_feature(
        movie_id,
        feature_id,
        username,
        library_path,
        jellyfin_client,
        store,
    )
    try:
        stream = jellyfin_client.open_hls_master(playback_feature)
        return _proxy_hls_response(movie_id, user_id, stream)
    except JellyfinUnavailableError:
        logger.exception(
            "Feature HLS playback failed movie_id=%s feature_id=%s user=%r",
            movie_id,
            feature_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playback stream is unavailable",
        ) from None


@router.get("/{movie_id}/hls/{resource_token}", response_model=None)
def stream_movie_hls_resource(
    movie_id: str,
    resource_token: str,
    username: AuthenticatedUsername,
    jellyfin_client: JellyfinPlaybackClient,
) -> PlainTextResponse | StreamingResponse:
    user_id = authenticated_user_id(username)
    upstream_url = private_resources.resolve(user_id, resource_token)
    if upstream_url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Playback resource not found",
        )

    try:
        stream = jellyfin_client.open_hls_resource(upstream_url)
        return _proxy_hls_response(movie_id, user_id, stream)
    except JellyfinUnavailableError:
        logger.exception(
            "HLS resource failed for movie_id=%s user=%r",
            movie_id,
            username,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playback stream is unavailable",
        ) from None
