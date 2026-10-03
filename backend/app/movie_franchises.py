"""Private, manually curated movie franchises. Never publication authority."""
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from threading import RLock
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
import psycopg
from psycopg.types.json import Jsonb

from app.auth import AuthenticatedUsername, authenticated_user_id
from app.config import get_database_conninfo
from app.movie_chronology import resolve_reference, tmdb_movie_id
from app.movies import (
    MovieSummary, _asset_is_exclusive_movie, _is_movie_library_title,
    _optional_int, _optional_string, _owned_artwork_url,
)
from app.vault_master import CataloguedAsset, VaultMasterStore, get_vault_master_store


Order = Literal["timeline", "release"]
router = APIRouter(prefix="/api/movie-franchises", tags=["movie-franchises"])


@dataclass
class Franchise:
    id: UUID
    user_id: UUID
    name: str
    description: str = ""
    selected_order: Order | None = None
    chronology: dict = field(default_factory=dict)
    # The source key prevents combining unrelated chronologies into one order.
    members: dict[UUID, tuple[int | None, str | None]] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryMovieFranchiseStore:
    def __init__(self):
        self.entries: dict[UUID, Franchise] = {}
        self.lock = RLock()

    def create(self, user_id, name, description):
        with self.lock:
            entry = Franchise(uuid4(), user_id, name, description)
            self.entries[entry.id] = entry
            return deepcopy(entry)

    def list(self, user_id):
        with self.lock:
            return deepcopy([entry for entry in self.entries.values() if entry.user_id == user_id])

    def get(self, user_id, franchise_id):
        with self.lock:
            entry = self.entries.get(franchise_id)
            return deepcopy(entry) if entry and entry.user_id == user_id else None

    def mutate(self, user_id, franchise_id, update):
        with self.lock:
            entry = self.get(user_id, franchise_id)
            if entry is None:
                return None
            update(entry)
            entry.updated_at = datetime.now(timezone.utc)
            self.entries[entry.id] = entry
            return deepcopy(entry)

    def delete(self, user_id, franchise_id):
        with self.lock:
            if self.get(user_id, franchise_id) is None:
                return False
            del self.entries[franchise_id]
            return True


class PostgresMovieFranchiseStore:
    def __init__(self, conninfo):
        self.conninfo = conninfo

    def initialize(self):
        with psycopg.connect(self.conninfo) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS movie_franchises (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES auth_accounts(user_id) ON DELETE CASCADE,
                name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 160),
                description TEXT NOT NULL DEFAULT '',
                selected_order TEXT CHECK (selected_order IN ('timeline','release')),
                chronology JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute("CREATE INDEX IF NOT EXISTS movie_franchises_user_idx ON movie_franchises(user_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS movie_franchise_members (
                franchise_id UUID NOT NULL REFERENCES movie_franchises(id) ON DELETE CASCADE,
                asset_id UUID NOT NULL REFERENCES vault_assets(id) ON DELETE CASCADE,
                timeline_position INTEGER CHECK (timeline_position > 0),
                chronology_key TEXT,
                PRIMARY KEY (franchise_id, asset_id))""")
            connection.execute("CREATE INDEX IF NOT EXISTS movie_franchise_members_asset_idx ON movie_franchise_members(asset_id)")

    def create(self, user_id, name, description):
        entry = Franchise(uuid4(), user_id, name, description)
        with psycopg.connect(self.conninfo) as connection:
            connection.execute("INSERT INTO movie_franchises(id,user_id,name,description) VALUES (%s,%s,%s,%s)",
                               (entry.id, user_id, name, description))
        return self.get(user_id, entry.id)

    def _get(self, connection, user_id, franchise_id, lock=False):
        row = connection.execute("""SELECT id,user_id,name,description,selected_order,chronology,
            created_at,updated_at FROM movie_franchises WHERE user_id=%s AND id=%s""" +
            (" FOR UPDATE" if lock else ""), (user_id, franchise_id)).fetchone()
        if row is None:
            return None
        # Dataclass members are not part of the franchise table.
        entry = Franchise(*row[:6], created_at=row[6], updated_at=row[7])
        entry.members = {asset: (position, key) for asset, position, key in connection.execute(
            "SELECT asset_id,timeline_position,chronology_key FROM movie_franchise_members WHERE franchise_id=%s", (entry.id,))}
        return entry

    def get(self, user_id, franchise_id):
        with psycopg.connect(self.conninfo) as connection:
            return self._get(connection, user_id, franchise_id)

    def list(self, user_id):
        with psycopg.connect(self.conninfo) as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM movie_franchises WHERE user_id=%s ORDER BY name,id", (user_id,))]
            return [entry for franchise_id in ids
                    if (entry := self._get(connection, user_id, franchise_id)) is not None]

    def mutate(self, user_id, franchise_id, update: Callable[[Franchise], None]):
        with psycopg.connect(self.conninfo) as connection:
            entry = self._get(connection, user_id, franchise_id, lock=True)
            if entry is None:
                return None
            previous_members = set(entry.members)
            update(entry)
            entry.updated_at = datetime.now(timezone.utc)
            connection.execute("""UPDATE movie_franchises SET name=%s,description=%s,
                selected_order=%s,chronology=%s,updated_at=%s WHERE id=%s AND user_id=%s""",
                (entry.name, entry.description, entry.selected_order, Jsonb(entry.chronology), entry.updated_at, entry.id, user_id))
            for asset_id in previous_members - set(entry.members):
                connection.execute("DELETE FROM movie_franchise_members WHERE franchise_id=%s AND asset_id=%s", (entry.id, asset_id))
            for asset_id, (position, key) in entry.members.items():
                connection.execute("""INSERT INTO movie_franchise_members VALUES (%s,%s,%s,%s)
                    ON CONFLICT (franchise_id,asset_id) DO UPDATE SET
                    timeline_position=EXCLUDED.timeline_position,chronology_key=EXCLUDED.chronology_key""",
                    (entry.id, asset_id, position, key))
            return entry

    def delete(self, user_id, franchise_id):
        with psycopg.connect(self.conninfo) as connection:
            return connection.execute("DELETE FROM movie_franchises WHERE id=%s AND user_id=%s", (franchise_id, user_id)).rowcount == 1


def get_movie_franchise_store():
    return PostgresMovieFranchiseStore(get_database_conninfo())


FranchiseStore = Annotated[PostgresMovieFranchiseStore, Depends(get_movie_franchise_store)]
Catalogue = Annotated[VaultMasterStore, Depends(get_vault_master_store)]


class FranchiseEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a franchise name")
        return value


class FranchiseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: Order


class AddMovies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: list[UUID] = Field(min_length=1, max_length=500)


class FranchiseView(BaseModel):
    id: UUID
    name: str
    description: str
    selected_order: Order | None
    effective_order: Order
    timeline_available: bool
    timeline_status: Literal["resolved", "unresolved"]
    chronology_source: str | None
    chronology_url: str | None
    chronology_version: str | None
    chronology_resolved_at: str | None
    member_count: int
    poster_url: str | None
    member_ids: list[UUID]
    movies: list[MovieSummary]
    created_at: datetime
    updated_at: datetime


def visible_movies(catalogue, username):
    return {asset.id: asset for asset in catalogue.list_visible_movie_assets(username)
            if asset.lifecycle_state == "active" and _is_movie_library_title(asset)
            and asset.asset_type.casefold() in {"movie", "movies"}}


def release_key(asset: CataloguedAsset):
    metadata = asset.effective_metadata
    value = metadata.get("release_date")
    try:
        released = date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except ValueError:
        released = None
    year = metadata.get("release_year")
    year = year if type(year) is int and 1 <= year <= 9999 else None
    # Year-only records follow precisely dated records within that same year;
    # fully unknown releases sort last. UUID ties survive title/metadata changes.
    return (released.year if released else year or 10000,
            released.month if released else 13,
            released.day if released else 32, str(asset.id))


def present(entry: Franchise, visible: dict[UUID, CataloguedAsset], include_movies=True):
    assets = [visible[asset_id] for asset_id in entry.members if asset_id in visible]
    available = bool(assets) and bool(entry.chronology) and all(
        entry.members[asset.id][0] is not None and
        entry.members[asset.id][1] == entry.chronology.get("key") for asset in assets)
    order = entry.selected_order or ("timeline" if available else "release")
    effective = order if order != "timeline" or available else "release"
    assets.sort(key=(lambda asset: (entry.members[asset.id][0], str(asset.id)))
                if effective == "timeline" else release_key)
    movies = [MovieSummary(id=str(asset.id), asset_id=str(asset.id),
                          title=_optional_string(asset.effective_metadata.get("display_title")) or asset.display_title,
                          year=_optional_int(asset.effective_metadata.get("release_year")),
                          poster_url=_owned_artwork_url(asset, "poster"),
                          is_exclusive_movie=_asset_is_exclusive_movie(asset)) for asset in assets]
    return FranchiseView(
        id=entry.id, name=entry.name, description=entry.description,
        selected_order=entry.selected_order, effective_order=effective,
        timeline_available=available, timeline_status="resolved" if available else "unresolved",
        chronology_source=entry.chronology.get("label"), chronology_url=entry.chronology.get("url"),
        chronology_version=entry.chronology.get("version"), chronology_resolved_at=entry.chronology.get("resolved_at"),
        member_count=len(movies), poster_url=next((movie.poster_url for movie in movies if movie.poster_url), None),
        member_ids=[asset.id for asset in assets], movies=movies if include_movies else [],
        created_at=entry.created_at, updated_at=entry.updated_at)


def require_entry(entry):
    if entry is None:
        raise HTTPException(404, "Franchise not found")
    return entry


def resolve_members(entry: Franchise, visible, refresh=False):
    assets = [visible[asset_id] for asset_id in entry.members if asset_id in visible]
    if refresh or not entry.chronology:
        resolved = resolve_reference([asset.effective_metadata for asset in assets])
        # Failed refresh retains the previous durable reference; no network
        # dependency and no automatic deletion of a user's existing resolution.
        if resolved:
            resolved["resolved_at"] = datetime.now(timezone.utc).isoformat()
            entry.chronology = resolved
    positions = entry.chronology.get("positions", {})
    for asset in assets:
        previous = entry.members[asset.id]
        if previous[0] is not None and not refresh:
            continue
        position = positions.get(tmdb_movie_id(asset.effective_metadata))
        entry.members[asset.id] = (position, entry.chronology.get("key") if position else None)


@router.get("", response_model=list[FranchiseView])
def list_franchises(username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    visible = visible_movies(catalogue, username)
    return [present(entry, visible, include_movies=False) for entry in store.list(authenticated_user_id(username))]


@router.post("", response_model=FranchiseView, status_code=201)
def create_franchise(body: FranchiseEdit, username: AuthenticatedUsername, store: FranchiseStore):
    return present(store.create(authenticated_user_id(username), body.name, body.description), {})


@router.get("/{franchise_id}", response_model=FranchiseView)
def get_franchise(franchise_id: UUID, username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    return present(require_entry(store.get(authenticated_user_id(username), franchise_id)), visible_movies(catalogue, username))


@router.patch("/{franchise_id}", response_model=FranchiseView)
def edit_franchise(franchise_id: UUID, body: FranchiseEdit, username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    def update(entry):
        entry.name, entry.description = body.name, body.description
    return present(require_entry(store.mutate(authenticated_user_id(username), franchise_id, update)), visible_movies(catalogue, username))


@router.delete("/{franchise_id}", status_code=204)
def delete_franchise(franchise_id: UUID, username: AuthenticatedUsername, store: FranchiseStore):
    if not store.delete(authenticated_user_id(username), franchise_id):
        raise HTTPException(404, "Franchise not found")
    return Response(status_code=204)


@router.put("/{franchise_id}/order", response_model=FranchiseView)
def select_order(franchise_id: UUID, body: FranchiseOrder, username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    def update(entry):
        entry.selected_order = body.order
    return present(require_entry(store.mutate(authenticated_user_id(username), franchise_id, update)), visible_movies(catalogue, username))


@router.post("/{franchise_id}/members", response_model=FranchiseView)
def add_movies(franchise_id: UUID, body: AddMovies, username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    visible = visible_movies(catalogue, username)
    if any(asset_id not in visible for asset_id in body.asset_ids):
        raise HTTPException(404, "Movie not found")
    def update(entry):
        for asset_id in body.asset_ids:
            entry.members.setdefault(asset_id, (None, None))
        resolve_members(entry, visible)
    return present(require_entry(store.mutate(authenticated_user_id(username), franchise_id, update)), visible)


@router.delete("/{franchise_id}/members/{asset_id}", response_model=FranchiseView)
def remove_movie(franchise_id: UUID, asset_id: UUID, username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    def update(entry):
        entry.members.pop(asset_id, None)
    return present(require_entry(store.mutate(authenticated_user_id(username), franchise_id, update)), visible_movies(catalogue, username))


@router.post("/{franchise_id}/chronology/refresh", response_model=FranchiseView)
def refresh_chronology(franchise_id: UUID, username: AuthenticatedUsername, store: FranchiseStore, catalogue: Catalogue):
    visible = visible_movies(catalogue, username)
    def update(entry):
        resolve_members(entry, visible, refresh=True)
    return present(require_entry(store.mutate(authenticated_user_id(username), franchise_id, update)), visible)
