"""Canonical catalogue and read-only API for Theatre TV Shows.

Shows own audience. Seasons and Episodes are structural records; an Episode
references exactly one canonical Vault asset/file and can never widen access.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import json
import os
import re
from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import AuthenticatedUsername, authenticated_user_id
from app.config import get_database_conninfo, get_metadata_storage_root

TV_ROOT = PurePosixPath("/vault/Theatre/TV Shows")
EPISODE_PATTERN = re.compile(r"^(?P<title>.+?)[ ._-]+S(?P<season>\d{1,2})E(?P<episode>\d{1,3})$", re.I)


@dataclass(frozen=True)
class ParsedEpisode:
    show_title: str
    season_number: int
    episode_number: int


def parse_reviewed_episode(relative_path: str, filename: str) -> ParsedEpisode | None:
    """Parse only explicit SnnEnn evidence; staging folders are context, not authority."""
    match = EPISODE_PATTERN.fullmatch(Path(filename).stem)
    if not match:
        return None
    title = re.sub(r"[._-]+", " ", match.group("title")).strip()
    if not title:
        return None
    season, episode = int(match.group("season")), int(match.group("episode"))
    if season <= 0 or episode <= 0:
        return None
    return ParsedEpisode(title, season, episode)


class TvShowSummary(BaseModel):
    id: UUID
    title: str
    season_count: int
    poster_url: str | None = None


class TvEpisode(BaseModel):
    id: UUID
    asset_id: UUID
    episode_number: int
    title: str
    runtime_minutes: int | None = None
    artwork_url: str | None = None
    playback_url: str


class TvSeason(BaseModel):
    id: UUID
    season_number: int
    poster_url: str | None = None
    episodes: list[TvEpisode]


class TvShowDetails(BaseModel):
    id: UUID
    title: str
    poster_url: str | None = None
    seasons: list[TvSeason]


@dataclass(frozen=True)
class TvEpisodeSource:
    id: UUID
    asset_id: UUID
    vault_path: str
    owner_user_id: UUID
    visibility: str


@dataclass(frozen=True)
class PendingTvEpisode:
    id: UUID
    asset_id: UUID
    vault_path: str
    episode_number: int


class PostgresTvShowStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self.conninfo, row_factory=psycopg.rows.dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_shows (
                    id UUID PRIMARY KEY,
                    owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                    title TEXT NOT NULL CHECK (length(btrim(title)) > 0),
                    visibility TEXT NOT NULL CHECK (visibility IN ('private', 'vault-wide')),
                    provider_ids JSONB NOT NULL DEFAULT '{}'::jsonb,
                    imported_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, title)
                )
            """)
            cursor.execute("ALTER TABLE vault_tv_shows ADD COLUMN IF NOT EXISTS owned_artwork JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_seasons (
                    id UUID PRIMARY KEY,
                    show_id UUID NOT NULL REFERENCES vault_tv_shows(id) ON DELETE RESTRICT,
                    season_number INTEGER NOT NULL CHECK (season_number > 0),
                    provider_ids JSONB NOT NULL DEFAULT '{}'::jsonb,
                    imported_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(show_id, season_number)
                )
            """)
            cursor.execute("ALTER TABLE vault_tv_seasons ADD COLUMN IF NOT EXISTS owned_artwork JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_episodes (
                    id UUID PRIMARY KEY,
                    show_id UUID NOT NULL REFERENCES vault_tv_shows(id) ON DELETE RESTRICT,
                    season_id UUID NOT NULL REFERENCES vault_tv_seasons(id) ON DELETE RESTRICT,
                    asset_id UUID NOT NULL UNIQUE REFERENCES vault_assets(id) ON DELETE RESTRICT,
                    episode_number INTEGER NOT NULL CHECK (episode_number > 0),
                    title TEXT NOT NULL CHECK (length(btrim(title)) > 0),
                    provider_ids JSONB NOT NULL DEFAULT '{}'::jsonb,
                    imported_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(season_id, episode_number)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_publication_sets (
                    id UUID PRIMARY KEY,
                    owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                    source_directory TEXT NOT NULL,
                    show_title TEXT NOT NULL,
                    season_number INTEGER NOT NULL CHECK (season_number > 0),
                    audience TEXT NOT NULL CHECK (audience IN ('private', 'vault-wide')),
                    state TEXT NOT NULL CHECK (state IN ('reviewed', 'publishing', 'published', 'failed')),
                    expected_episode_count INTEGER NOT NULL CHECK (expected_episode_count > 0),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, source_directory)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_tv_publication_set_members (
                    publication_set_id UUID NOT NULL REFERENCES vault_tv_publication_sets(id) ON DELETE RESTRICT,
                    arrival_item_id UUID NOT NULL UNIQUE,
                    asset_id UUID UNIQUE REFERENCES vault_assets(id) ON DELETE RESTRICT,
                    episode_number INTEGER NOT NULL CHECK (episode_number > 0),
                    canonical_destination TEXT NOT NULL,
                    published_at TIMESTAMPTZ,
                    PRIMARY KEY (publication_set_id, arrival_item_id),
                    UNIQUE(publication_set_id, episode_number)
                )
            """)

    def publish_complete_set(
        self,
        *,
        owner_user_id: UUID,
        source_directory: str,
        show_title: str,
        season_number: int,
        audience: str,
        episodes: list[tuple[UUID, UUID, int, str]],
    ) -> UUID:
        """Publish a fully received reviewed set atomically.

        The caller must not invoke this until every member receipt has been
        checksum-verified.  Upserts intentionally preserve a previously
        published show's audience and episode identity on retry.
        """
        if audience not in {"private", "vault-wide"} or not episodes:
            raise ValueError("TV publication set is invalid")
        if len({episode for _, _, episode, _ in episodes}) != len(episodes):
            raise ValueError("TV publication episode numbers are not unique")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, state, show_title FROM vault_tv_publication_sets
                   WHERE owner_user_id=%s AND source_directory=%s FOR UPDATE""",
                (owner_user_id, source_directory),
            )
            set_row = cursor.fetchone()
            set_id = set_row["id"] if set_row else uuid4()
            if set_row and set_row["state"] == "published":
                cursor.execute(
                    "SELECT id FROM vault_tv_shows WHERE owner_user_id=%s AND title=%s",
                    (owner_user_id, set_row["show_title"]),
                )
                show = cursor.fetchone()
                if show is None:
                    raise RuntimeError("Published TV set has no canonical Show")
                return show["id"]
            cursor.execute(
                """INSERT INTO vault_tv_publication_sets
                   (id, owner_user_id, source_directory, show_title, season_number, audience, state, expected_episode_count)
                   VALUES (%s,%s,%s,%s,%s,%s,'publishing',%s)
                   ON CONFLICT (owner_user_id, source_directory) DO UPDATE
                   SET state='publishing', updated_at=CURRENT_TIMESTAMP""",
                (set_id, owner_user_id, source_directory, show_title, season_number, audience, len(episodes)),
            )
            for arrival_item_id, asset_id, episode_number, destination in episodes:
                cursor.execute(
                    """INSERT INTO vault_tv_publication_set_members
                       (publication_set_id, arrival_item_id, asset_id, episode_number, canonical_destination, published_at)
                       VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                       ON CONFLICT (arrival_item_id) DO UPDATE
                       SET asset_id=EXCLUDED.asset_id, published_at=EXCLUDED.published_at""",
                    (set_id, arrival_item_id, asset_id, episode_number, destination),
                )
            cursor.execute(
                """SELECT id FROM vault_tv_shows WHERE owner_user_id=%s AND title=%s FOR UPDATE""",
                (owner_user_id, show_title),
            )
            show_row = cursor.fetchone()
            show_id = show_row["id"] if show_row else uuid4()
            cursor.execute(
                """INSERT INTO vault_tv_shows (id, owner_user_id, title, visibility)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (owner_user_id, title) DO UPDATE SET updated_at=CURRENT_TIMESTAMP""",
                (show_id, owner_user_id, show_title, audience),
            )
            cursor.execute(
                """SELECT id FROM vault_tv_seasons WHERE show_id=%s AND season_number=%s FOR UPDATE""",
                (show_id, season_number),
            )
            season_row = cursor.fetchone()
            season_id = season_row["id"] if season_row else uuid4()
            cursor.execute(
                """INSERT INTO vault_tv_seasons (id, show_id, season_number)
                   VALUES (%s,%s,%s) ON CONFLICT (show_id, season_number) DO NOTHING""",
                (season_id, show_id, season_number),
            )
            for _, asset_id, episode_number, _ in episodes:
                cursor.execute(
                    """INSERT INTO vault_tv_episodes (id, show_id, season_id, asset_id, episode_number, title)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (asset_id) DO NOTHING""",
                    (uuid4(), show_id, season_id, asset_id, episode_number, f"Episode {episode_number}"),
                )
            cursor.execute(
                "UPDATE vault_tv_publication_sets SET state='published', updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                (set_id,),
            )
        self.write_hierarchy_sidecars(show_id)
        return show_id

    def hierarchy_sidecar_documents(self, show_id: UUID) -> tuple[dict[str, object], list[dict[str, object]]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, owner_user_id, title, visibility, provider_ids, imported_metadata, metadata_provenance FROM vault_tv_shows WHERE id=%s", (show_id,))
            show = cursor.fetchone()
            if show is None:
                raise LookupError("TV Show not found")
            cursor.execute("SELECT id, season_number, provider_ids, imported_metadata, metadata_provenance FROM vault_tv_seasons WHERE show_id=%s ORDER BY season_number", (show_id,))
            seasons = cursor.fetchall()
        show_document = {
            "schema": "personal-vault.tv-show-sidecar.v1", "show": {"id": str(show["id"]), "title": show["title"], "owner_user_id": str(show["owner_user_id"]), "visibility": show["visibility"]},
            "provider_ids": show["provider_ids"], "imported_metadata": show["imported_metadata"], "provenance": show["metadata_provenance"], "seasons": [str(season["id"]) for season in seasons],
        }
        season_documents = [
            {"schema": "personal-vault.tv-season-sidecar.v1", "season": {"id": str(season["id"]), "show_id": str(show["id"]), "season_number": season["season_number"], "owner_user_id": str(show["owner_user_id"]), "visibility": show["visibility"], "audience_inherited": True}, "provider_ids": season["provider_ids"], "imported_metadata": season["imported_metadata"], "provenance": season["metadata_provenance"]}
            for season in seasons
        ]
        return show_document, season_documents

    def write_hierarchy_sidecars(self, show_id: UUID, storage_root: Path | None = None) -> None:
        root = (storage_root or get_metadata_storage_root()) / "tv-hierarchy-sidecars"
        root.mkdir(parents=True, exist_ok=True)
        show, seasons = self.hierarchy_sidecar_documents(show_id)
        for document in [show, *seasons]:
            identifier = document.get("show", document.get("season", {})).get("id")
            if not isinstance(identifier, str):
                raise ValueError("TV hierarchy sidecar lacks identity")
            destination = root / f"{identifier}.json"
            temporary = root / f".{identifier}.{uuid4().hex}.part"
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, destination)

    def visible_hierarchy_artwork(self, show_id: UUID, season_id: UUID | None, user_id: UUID, kind: str, storage_root: Path) -> tuple[Path, str] | None:
        if kind not in {"poster", "backdrop"}:
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT visibility, owner_user_id, owned_artwork FROM vault_tv_shows WHERE id=%s AND (owner_user_id=%s OR visibility='vault-wide')", (show_id, user_id))
            show = cursor.fetchone()
            if show is None:
                return None
            if season_id is None:
                record = (show["owned_artwork"] or {}).get(kind)
            else:
                cursor.execute("SELECT owned_artwork FROM vault_tv_seasons WHERE id=%s AND show_id=%s", (season_id, show_id))
                season = cursor.fetchone()
                record = (season["owned_artwork"] or {}).get(kind) if season else None
        if not isinstance(record, dict) or not isinstance(record.get("storage_key"), str) or not isinstance(record.get("mime_type"), str):
            return None
        path = (storage_root / record["storage_key"]).resolve(strict=False)
        if not path.is_relative_to(storage_root.resolve(strict=False)) or not path.is_file():
            return None
        return path, record["mime_type"]

    def retain_hierarchy_artwork(self, show_id: UUID, season_id: UUID | None, kind: str, content: bytes, mime_type: str, storage_root: Path | None = None) -> None:
        if kind not in {"poster", "backdrop"} or not content or not mime_type.startswith("image/"):
            raise ValueError("TV artwork is invalid")
        root = storage_root or get_metadata_storage_root()
        identity = season_id or show_id
        relative = Path("tv-artwork") / str(identity) / kind
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{kind}.{uuid4().hex}.part")
        with temporary.open("xb") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, destination)
        with self._connect() as connection, connection.cursor() as cursor:
            if season_id is None:
                cursor.execute("UPDATE vault_tv_shows SET owned_artwork=owned_artwork || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (psycopg.types.json.Jsonb({kind: {"storage_key": relative.as_posix(), "mime_type": mime_type}}), show_id))
            else:
                cursor.execute("UPDATE vault_tv_seasons SET owned_artwork=owned_artwork || %s, updated_at=CURRENT_TIMESTAMP WHERE id=%s AND show_id=%s", (psycopg.types.json.Jsonb({kind: {"storage_key": relative.as_posix(), "mime_type": mime_type}}), season_id, show_id))
        self.write_hierarchy_sidecars(show_id, root)

    def retain_episode_artwork(
        self,
        episode_id: UUID,
        kind: str,
        content: bytes,
        mime_type: str,
        storage_root: Path | None = None,
    ) -> None:
        """Retain an Episode still at its immutable PV identity.

        The descriptor is canonical catalogue metadata; it is deliberately a
        local storage key rather than a Jellyfin URL or provider credential.
        Re-importing the same still replaces one stable owned file.
        """
        if kind != "primary" or not content or not mime_type.startswith("image/"):
            raise ValueError("TV episode artwork is invalid")
        root = storage_root or get_metadata_storage_root()
        relative = Path("tv-artwork") / "episodes" / str(episode_id) / kind
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{kind}.{uuid4().hex}.part")
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        descriptor = {"storage_key": relative.as_posix(), "mime_type": mime_type}
        metadata = {"tv_artwork": {kind: descriptor}}
        provenance = {"tv_artwork": "jellyfin"}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_tv_episodes
                   SET imported_metadata=imported_metadata || %s,
                       metadata_provenance=metadata_provenance || %s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s""",
                (psycopg.types.json.Jsonb(metadata), psycopg.types.json.Jsonb(provenance), episode_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("TV Episode not found")
            cursor.execute(
                """UPDATE vault_assets
                   SET imported_metadata=imported_metadata || %s,
                       effective_metadata=effective_metadata || %s,
                       metadata_provenance=metadata_provenance || %s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=(SELECT asset_id FROM vault_tv_episodes WHERE id=%s)""",
                (
                    psycopg.types.json.Jsonb(metadata),
                    psycopg.types.json.Jsonb(metadata),
                    psycopg.types.json.Jsonb(provenance),
                    episode_id,
                ),
            )

    def visible_episode_artwork(
        self, episode_id: UUID, user_id: UUID, kind: str, storage_root: Path
    ) -> tuple[Path, str] | None:
        if kind != "primary":
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT asset.effective_metadata
                   FROM vault_tv_episodes AS episode
                   JOIN vault_tv_shows AS show ON show.id=episode.show_id
                   JOIN vault_assets AS asset ON asset.id=episode.asset_id
                   WHERE episode.id=%s
                     AND (show.owner_user_id=%s OR show.visibility='vault-wide')""",
                (episode_id, user_id),
            )
            row = cursor.fetchone()
        metadata = row["effective_metadata"] if row else {}
        record = (metadata.get("tv_artwork") or {}).get(kind) if isinstance(metadata, dict) else None
        expected_key = (Path("tv-artwork") / "episodes" / str(episode_id) / kind).as_posix()
        if (
            not isinstance(record, dict)
            or record.get("storage_key") != expected_key
            or not isinstance(record.get("mime_type"), str)
        ):
            return None
        path = (storage_root / expected_key).resolve(strict=False)
        if not path.is_relative_to(storage_root.resolve(strict=False)) or not path.is_file():
            return None
        return path, record["mime_type"]

    def episode_is_visible(self, episode_id: UUID, user_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM vault_tv_episodes episode
                   JOIN vault_tv_shows show ON show.id=episode.show_id
                   WHERE episode.id=%s AND (show.owner_user_id=%s OR show.visibility='vault-wide')""",
                (episode_id, user_id),
            )
            return cursor.fetchone() is not None

    def visible_episode_source(self, episode_id: UUID, user_id: UUID) -> TvEpisodeSource | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT episode.id, episode.asset_id, file.vault_path, show.owner_user_id, show.visibility
                   FROM vault_tv_episodes episode
                   JOIN vault_tv_shows show ON show.id=episode.show_id
                   JOIN vault_files file ON file.asset_id=episode.asset_id
                   WHERE episode.id=%s AND (show.owner_user_id=%s OR show.visibility='vault-wide')""",
                (episode_id, user_id),
            )
            row = cursor.fetchone()
        return TvEpisodeSource(**row) if row else None

    def import_episode_metadata(
        self, episode_id: UUID, *, title: str, episode_number: int,
        imported_metadata: dict[str, object], provider_ids: dict[str, str],
    ) -> None:
        """Persist imported metadata as Vault-owned evidence, never as access state."""
        provenance = {key: "jellyfin" for key in imported_metadata}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_tv_episodes SET title=%s, episode_number=%s, provider_ids=%s,
                   imported_metadata=%s, metadata_provenance=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                (title, episode_number, psycopg.types.json.Jsonb(provider_ids), psycopg.types.json.Jsonb(imported_metadata), psycopg.types.json.Jsonb(provenance), episode_id),
            )
            cursor.execute(
                """UPDATE vault_assets SET imported_metadata=imported_metadata || %s,
                   effective_metadata=effective_metadata || %s,
                   metadata_provenance=metadata_provenance || %s, updated_at=CURRENT_TIMESTAMP
                   WHERE id=(SELECT asset_id FROM vault_tv_episodes WHERE id=%s)""",
                (psycopg.types.json.Jsonb(imported_metadata), psycopg.types.json.Jsonb(imported_metadata), psycopg.types.json.Jsonb(provenance), episode_id),
            )

    def pending_metadata_episodes(self) -> list[PendingTvEpisode]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT episode.id, episode.asset_id, file.vault_path, episode.episode_number
                   FROM vault_tv_episodes episode JOIN vault_files file ON file.asset_id=episode.asset_id
                   JOIN vault_tv_publication_set_members member ON member.asset_id=episode.asset_id
                   JOIN vault_tv_publication_sets publication ON publication.id=member.publication_set_id
                   WHERE episode.provider_ids = '{}'::jsonb AND publication.state='published' ORDER BY episode.id"""
            )
            return [PendingTvEpisode(**row) for row in cursor.fetchall()]

    def pending_episode_artwork(self) -> list[PendingTvEpisode]:
        """Published Episodes missing only their PV-owned primary still.

        This supports additive historical backfill without revisiting
        publication, placement, identity, provider IDs, or audience.
        """
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT episode.id, episode.asset_id, file.vault_path, episode.episode_number
                   FROM vault_tv_episodes AS episode
                   JOIN vault_assets AS asset ON asset.id=episode.asset_id
                   JOIN vault_files AS file ON file.asset_id=episode.asset_id
                   JOIN vault_tv_publication_set_members AS member ON member.asset_id=episode.asset_id
                   JOIN vault_tv_publication_sets AS publication ON publication.id=member.publication_set_id
                   WHERE publication.state='published'
                     AND NOT COALESCE(asset.effective_metadata -> 'tv_artwork', '{}'::jsonb) ? 'primary'
                   ORDER BY episode.id"""
            )
            return [PendingTvEpisode(**row) for row in cursor.fetchall()]

    def mark_episode_artwork_unavailable(self, episode_id: UUID) -> None:
        """Persist a clean no-still result so the worker does not poll forever."""
        metadata = {"tv_artwork": {"primary": {"state": "unavailable"}}}
        provenance = {"tv_artwork": "jellyfin"}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_tv_episodes
                   SET imported_metadata=imported_metadata || %s,
                       metadata_provenance=metadata_provenance || %s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s""",
                (psycopg.types.json.Jsonb(metadata), psycopg.types.json.Jsonb(provenance), episode_id),
            )
            cursor.execute(
                """UPDATE vault_assets
                   SET imported_metadata=imported_metadata || %s,
                       effective_metadata=effective_metadata || %s,
                       metadata_provenance=metadata_provenance || %s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=(SELECT asset_id FROM vault_tv_episodes WHERE id=%s)""",
                (
                    psycopg.types.json.Jsonb(metadata),
                    psycopg.types.json.Jsonb(metadata),
                    psycopg.types.json.Jsonb(provenance),
                    episode_id,
                ),
            )

    def mark_metadata_import_failed(self, episode_ids: list[UUID]) -> None:
        if not episode_ids:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_tv_publication_sets SET state='failed', updated_at=CURRENT_TIMESTAMP
                   WHERE id IN (SELECT member.publication_set_id FROM vault_tv_publication_set_members member
                                JOIN vault_tv_episodes episode ON episode.asset_id=member.asset_id
                                WHERE episode.id = ANY(%s))""",
                (episode_ids,),
            )

    def import_hierarchy_provider_ids(self, episode_id: UUID, series_id: str | None, season_id: str | None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            if series_id:
                cursor.execute(
                    """UPDATE vault_tv_shows SET provider_ids=provider_ids || %s,
                       metadata_provenance=metadata_provenance || %s, updated_at=CURRENT_TIMESTAMP
                       WHERE id=(SELECT show_id FROM vault_tv_episodes WHERE id=%s)""",
                    (psycopg.types.json.Jsonb({"jellyfin_series_id": series_id}), psycopg.types.json.Jsonb({"jellyfin_series_id": "jellyfin"}), episode_id),
                )
            if season_id:
                cursor.execute(
                    """UPDATE vault_tv_seasons SET provider_ids=provider_ids || %s,
                       metadata_provenance=metadata_provenance || %s, updated_at=CURRENT_TIMESTAMP
                       WHERE id=(SELECT season_id FROM vault_tv_episodes WHERE id=%s)""",
                    (psycopg.types.json.Jsonb({"jellyfin_season_id": season_id}), psycopg.types.json.Jsonb({"jellyfin_season_id": "jellyfin"}), episode_id),
                )

    def list_visible(self, user_id: UUID) -> list[TvShowSummary]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT show.id, show.title, show.owned_artwork, count(season.id) AS season_count
                FROM vault_tv_shows AS show
                LEFT JOIN vault_tv_seasons AS season ON season.show_id = show.id
                WHERE show.owner_user_id = %s OR show.visibility = 'vault-wide'
                GROUP BY show.id, show.title, show.owned_artwork ORDER BY lower(show.title)
            """, (user_id,))
            return [TvShowSummary(id=row['id'], title=row['title'], season_count=int(row['season_count']), poster_url=f"/api/tv-shows/{row['id']}/artwork/poster" if isinstance((row['owned_artwork'] or {}).get('poster'), dict) else None) for row in cursor.fetchall()]

    def get_visible(self, show_id: UUID, user_id: UUID) -> TvShowDetails | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT id, title, owned_artwork FROM vault_tv_shows WHERE id=%s AND (owner_user_id=%s OR visibility='vault-wide')""", (show_id, user_id))
            show = cursor.fetchone()
            if not show:
                return None
            cursor.execute("""
                SELECT season.id AS season_id, season.season_number, season.owned_artwork, episode.id AS episode_id,
                       episode.asset_id, episode.episode_number, episode.title,
                       asset.effective_metadata
                FROM vault_tv_seasons AS season
                LEFT JOIN vault_tv_episodes AS episode ON episode.season_id=season.id
                LEFT JOIN vault_assets AS asset ON asset.id=episode.asset_id
                WHERE season.show_id=%s ORDER BY season.season_number, episode.episode_number
            """, (show_id,))
            seasons: dict[UUID, TvSeason] = {}
            for row in cursor.fetchall():
                season_id = row['season_id']
                season = seasons.setdefault(season_id, TvSeason(id=season_id, season_number=int(row['season_number']), episodes=[], poster_url=f"/api/tv-shows/{show_id}/seasons/{season_id}/artwork/poster" if isinstance((row['owned_artwork'] or {}).get('poster'), dict) else None))
                if row['episode_id'] is None:
                    continue
                metadata = row['effective_metadata'] or {}
                runtime_minutes = metadata.get('runtime_minutes') if isinstance(metadata, dict) else None
                runtime_ticks = metadata.get('runtime_ticks') if isinstance(metadata, dict) else None
                if not isinstance(runtime_minutes, int) and isinstance(runtime_ticks, int):
                    runtime_minutes = runtime_ticks // 600_000_000
                artwork = (metadata.get('tv_artwork') or {}).get('primary') if isinstance(metadata, dict) else None
                has_owned_artwork = isinstance(artwork, dict) and isinstance(artwork.get('storage_key'), str) and isinstance(artwork.get('mime_type'), str)
                season.episodes.append(TvEpisode(id=row['episode_id'], asset_id=row['asset_id'], episode_number=int(row['episode_number']), title=row['title'], runtime_minutes=runtime_minutes if isinstance(runtime_minutes, int) else None, artwork_url=f'/api/tv-shows/episodes/{row["episode_id"]}/artwork/primary' if has_owned_artwork else None, playback_url=f'/api/tv-shows/episodes/{row["episode_id"]}/playback'))
            return TvShowDetails(id=show['id'], title=show['title'], poster_url=f"/api/tv-shows/{show_id}/artwork/poster" if isinstance((show['owned_artwork'] or {}).get('poster'), dict) else None, seasons=list(seasons.values()))


def get_tv_store() -> PostgresTvShowStore:
    return PostgresTvShowStore(get_database_conninfo())


TvStore = Annotated[PostgresTvShowStore, Depends(get_tv_store)]
router = APIRouter(prefix='/api/tv-shows', tags=['tv shows'])


@router.get('', response_model=list[TvShowSummary])
def list_tv_shows(user: AuthenticatedUsername, store: TvStore) -> list[TvShowSummary]:
    return store.list_visible(authenticated_user_id(user))


@router.get('/episodes/{episode_id}/artwork/{kind}', response_class=FileResponse)
def episode_artwork(episode_id: UUID, kind: str, user: AuthenticatedUsername, store: TvStore) -> FileResponse:
    record = store.visible_episode_artwork(episode_id, authenticated_user_id(user), kind, get_metadata_storage_root())
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    path, mime_type = record
    return FileResponse(path, media_type=mime_type, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@router.get('/{show_id}', response_model=TvShowDetails)
def get_tv_show(show_id: UUID, user: AuthenticatedUsername, store: TvStore) -> TvShowDetails:
    result = store.get_visible(show_id, authenticated_user_id(user))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='TV Show not found')
    return result


@router.get('/{show_id}/artwork/{kind}', response_class=FileResponse)
def show_artwork(show_id: UUID, kind: str, user: AuthenticatedUsername, store: TvStore) -> FileResponse:
    record = store.visible_hierarchy_artwork(show_id, None, authenticated_user_id(user), kind, get_metadata_storage_root())
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    path, mime_type = record
    return FileResponse(path, media_type=mime_type, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@router.get('/{show_id}/seasons/{season_id}/artwork/{kind}', response_class=FileResponse)
def season_artwork(show_id: UUID, season_id: UUID, kind: str, user: AuthenticatedUsername, store: TvStore) -> FileResponse:
    record = store.visible_hierarchy_artwork(show_id, season_id, authenticated_user_id(user), kind, get_metadata_storage_root())
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    path, mime_type = record
    return FileResponse(path, media_type=mime_type, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
