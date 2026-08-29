from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUser, get_authentication_store
from app.auth_store import AuthenticationStore, EpisodeProgress, GalleryState, MovieProgress


router = APIRouter(prefix="/api/user-state", tags=["user state"])
UserStateStore = Annotated[AuthenticationStore, Depends(get_authentication_store)]


class GalleryStatePayload(BaseModel):
    sort: Literal["newest", "oldest"] = "newest"
    anchor_id: str | None = Field(default=None, max_length=200)
    anchor_offset: int = Field(default=0, ge=-2000, le=2000)


class MovieProgressPayload(BaseModel):
    position_seconds: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    completed: bool = False


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


@router.get("/movies/{movie_id}", response_model=MovieProgressPayload | None)
def get_movie_progress(
    movie_id: str,
    user: AuthenticatedUser,
    store: UserStateStore,
) -> MovieProgressPayload | None:
    progress = store.get_movie_progress(user.user_id, movie_id)
    return (
        MovieProgressPayload(
            position_seconds=progress.position_seconds,
            duration_seconds=progress.duration_seconds,
            completed=progress.completed,
        )
        if progress
        else None
    )


@router.put("/movies/{movie_id}", response_model=MovieProgressPayload)
def save_movie_progress(
    movie_id: str,
    payload: MovieProgressPayload,
    user: AuthenticatedUser,
    store: UserStateStore,
) -> MovieProgressPayload:
    completed = payload.completed or (
        payload.duration_seconds > 0
        and payload.position_seconds >= payload.duration_seconds - 30
    )
    stored = MovieProgressPayload(
        position_seconds=0 if completed else payload.position_seconds,
        duration_seconds=payload.duration_seconds,
        completed=completed,
    )
    store.save_movie_progress(
        user.user_id,
        MovieProgress(movie_id, stored.position_seconds, stored.duration_seconds, stored.completed),
    )
    return stored


@router.get("/tv-episodes/{episode_id}", response_model=MovieProgressPayload | None)
def get_episode_progress(
    episode_id: str,
    user: AuthenticatedUser,
    store: UserStateStore,
) -> MovieProgressPayload | None:
    from uuid import UUID
    progress = store.get_episode_progress(user.user_id, UUID(episode_id))
    return MovieProgressPayload(
        position_seconds=progress.position_seconds,
        duration_seconds=progress.duration_seconds,
        completed=progress.completed,
    ) if progress else None


@router.put("/tv-episodes/{episode_id}", response_model=MovieProgressPayload)
def save_episode_progress(
    episode_id: str,
    payload: MovieProgressPayload,
    user: AuthenticatedUser,
    store: UserStateStore,
) -> MovieProgressPayload:
    from uuid import UUID
    completed = payload.completed or (
        payload.duration_seconds > 0 and payload.position_seconds >= payload.duration_seconds - 30
    )
    stored = MovieProgressPayload(
        position_seconds=0 if completed else payload.position_seconds,
        duration_seconds=payload.duration_seconds,
        completed=completed,
    )
    store.save_episode_progress(
        user.user_id,
        EpisodeProgress(UUID(episode_id), stored.position_seconds, stored.duration_seconds, stored.completed),
    )
    return stored
