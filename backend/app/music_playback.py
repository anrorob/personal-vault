from typing import Annotated
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.auth import AuthenticatedUsername
from app.jellyfin import JellyfinAudio, JellyfinClient, JellyfinUnavailableError
from app.movie_playback import get_jellyfin_client
from app.music import MusicCatalogue, MusicLibraryPath, resolve_visible_track


router = APIRouter(prefix="/api/music", tags=["music playback"])
MusicPlaybackClient = Annotated[JellyfinClient, Depends(get_jellyfin_client)]


def resolve_audio_with_index_retry(
    client: JellyfinClient,
    path: Path,
    *,
    attempts: int = 3,
    delay_seconds: float = 1,
) -> JellyfinAudio | None:
    audio = client.find_audio_by_path(path)
    if audio is not None:
        return audio
    client.notify_media_updated((path,))
    client.refresh_library()
    for _ in range(attempts):
        if delay_seconds:
            time.sleep(delay_seconds)
        audio = client.find_audio_by_path(path)
        if audio is not None:
            return audio
    return None


@router.get("/{track_id}/stream")
def stream_music(
    track_id: str,
    request: Request,
    username: AuthenticatedUsername,
    library_path: MusicLibraryPath,
    store: MusicCatalogue,
    client: MusicPlaybackClient,
) -> StreamingResponse:
    path, _ = resolve_visible_track(track_id, username, library_path, store)
    try:
        audio = resolve_audio_with_index_retry(client, path)
        if audio is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Track is not indexed by the playback service",
            )
        stream = client.open_audio_stream(audio, request.headers.get("Range"))
    except JellyfinUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Music playback is unavailable",
        ) from None
    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        media_type=stream.content_type or "audio/mpeg",
        headers={
            **stream.headers,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
