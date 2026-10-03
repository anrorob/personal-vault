"""Native Music playback with private, disposable lossless preparation."""
from typing import Annotated
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth import AuthenticatedUsername
from app.music import MusicCatalogue, MusicLibraryPath, resolve_visible_track
from app.music_playback_cache import get_cache_root, playback_file

router = APIRouter(prefix="/api/music", tags=["music playback"])
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class MusicResponse(FileResponse):
    def __init__(self, path, mime, claim):
        super().__init__(path, media_type=mime, headers=PRIVATE_HEADERS)
        self.claim = claim

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self.claim is not None:
                self.claim.close()


@router.get("/{track_id}/stream")
def stream_music(
    track_id: str,
    username: AuthenticatedUsername,
    library_path: MusicLibraryPath,
    store: MusicCatalogue,
    cache_root: Annotated[Path, Depends(get_cache_root)],
) -> FileResponse:
    # The same current UUID visibility and commissioned placement resolver guards
    # both canonical sources and cache hits. Cache keys are never public URLs.
    source, asset = resolve_visible_track(track_id, username, library_path, store)
    try:
        path, mime, claim = playback_file(source, asset.id, asset.sha256, cache_root)
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(503, "Music playback preparation is unavailable. Please try again.") from None
    return MusicResponse(path, mime, claim)
