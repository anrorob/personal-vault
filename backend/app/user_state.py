from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, computed_field, ConfigDict

from app.auth import AuthenticatedUser, get_authentication_store
from app.auth_store import AuthenticationStore, EpisodeProgress, GalleryState, MovieProgress


router = APIRouter(prefix="/api/user-state", tags=["user state"])
UserStateStore = Annotated[AuthenticationStore, Depends(get_authentication_store)]


class GalleryStatePayload(BaseModel):
    sort: Literal["newest", "oldest"] = "newest"
    anchor_id: str | None = Field(default=None, max_length=200)
    anchor_offset: int = Field(default=0, ge=-2000, le=2000)


class MovieProgressPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    position_seconds: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    completed: bool = False


class PlaybackState(MovieProgressPayload):
    @computed_field
    @property
    def state(self) -> Literal["unwatched", "in_progress", "watched"]:
        if self.completed:
            return "watched"
        meaningful = min(30, self.duration_seconds * 0.05) if self.duration_seconds > 0 else 30
        return "in_progress" if self.position_seconds > 0 and self.position_seconds >= meaningful else "unwatched"


class WatchedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    watched: bool


def playback_state(progress):
    return PlaybackState(position_seconds=progress.position_seconds, duration_seconds=progress.duration_seconds,
                         completed=progress.completed) if progress else None


def save_progress(store, user_id, identity, payload, *, episode=False, watched=None):
    cls = EpisodeProgress if episode else MovieProgress
    read = store.get_episode_progress if episode else store.get_movie_progress
    save = store.save_episode_progress if episode else store.save_movie_progress
    if watched is not None:
        previous = read(user_id, identity)
        progress = cls(identity, previous.position_seconds if previous else 0,
                       previous.duration_seconds if previous else 0, watched)
    else:
        position = min(payload.position_seconds, payload.duration_seconds) if payload.duration_seconds else payload.position_seconds
        # Keep the established last-30-seconds policy; avoid instant completion on short clips.
        near_end = payload.duration_seconds > 0 and position >= payload.duration_seconds - min(30, payload.duration_seconds * 0.05)
        progress = cls(identity, position, payload.duration_seconds, payload.completed or near_end)
    return playback_state(save(user_id, progress, preserve_completed=watched is None))


@router.get("/gallery", response_model=GalleryStatePayload)
def get_gallery_state(
    user: AuthenticatedUser,
    store: UserStateStore,
) -> GalleryStatePayload:
    state = store.get_gallery_state(user.user_id)
    return (
        GalleryStatePayload(
            sort=state.sort,
            anchor_id=state.anchor_id,
            anchor_offset=state.anchor_offset,
        )
        if state
        else GalleryStatePayload()
    )


@router.put("/gallery", response_model=GalleryStatePayload)
def save_gallery_state(
    payload: GalleryStatePayload,
    user: AuthenticatedUser,
    store: UserStateStore,
) -> GalleryStatePayload:
    store.save_gallery_state(
        user.user_id,
        GalleryState(payload.sort, payload.anchor_id, payload.anchor_offset),
    )
    return payload


@router.get("/movies", response_model=dict[str, PlaybackState])
def list_movie_progress(user: AuthenticatedUser, store: UserStateStore):
    return {p.movie_id: playback_state(p) for p in store.list_movie_progress(user.user_id)}


@router.get("/tv-episodes", response_model=dict[str, PlaybackState])
def list_episode_progress(user: AuthenticatedUser, store: UserStateStore):
    return {str(p.episode_id): playback_state(p) for p in store.list_episode_progress(user.user_id)}


@router.get("/movies/{movie_id}", response_model=PlaybackState | None)
def get_movie_progress(movie_id: str, user: AuthenticatedUser, store: UserStateStore):
    return playback_state(store.get_movie_progress(user.user_id, movie_id))


@router.put("/movies/{movie_id}", response_model=PlaybackState)
def save_movie_progress(movie_id: str, payload: MovieProgressPayload, user: AuthenticatedUser, store: UserStateStore):
    return save_progress(store, user.user_id, movie_id, payload)


@router.put("/movies/{movie_id}/watched", response_model=PlaybackState)
def mark_movie_watched(movie_id: str, payload: WatchedPayload, user: AuthenticatedUser, store: UserStateStore):
    return save_progress(store, user.user_id, movie_id, None, watched=payload.watched)


@router.get("/tv-episodes/{episode_id}", response_model=PlaybackState | None)
def get_episode_progress(episode_id: UUID, user: AuthenticatedUser, store: UserStateStore):
    return playback_state(store.get_episode_progress(user.user_id, episode_id))


@router.put("/tv-episodes/{episode_id}", response_model=PlaybackState)
def save_episode_progress(episode_id: UUID, payload: MovieProgressPayload, user: AuthenticatedUser, store: UserStateStore):
    return save_progress(store, user.user_id, episode_id, payload, episode=True)


@router.put("/tv-episodes/{episode_id}/watched", response_model=PlaybackState)
def mark_episode_watched(episode_id: UUID, payload: WatchedPayload, user: AuthenticatedUser, store: UserStateStore):
    return save_progress(store, user.user_id, episode_id, None, episode=True, watched=payload.watched)
