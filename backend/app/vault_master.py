from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from typing import Literal, Protocol
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5
from xml.etree import ElementTree
from zipfile import BadZipFile, LargeZipFile, ZipFile

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from PIL import Image, IptcImagePlugin, UnidentifiedImageError
from mutagen import File as MutagenFile, MutagenError
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError
import reverse_geocode

from app.config import (
    get_admin_username,
    get_database_conninfo,
    get_metadata_storage_root,
)
from app.photo_dates import select_oldest_photo_date
from app.share_grants import (
    active_user_id,
    initialize_share_grants,
    sync_stage2c_local_share_grants,
    visible_asset_ids,
)
from app.federation import initialize_federation
from app.vault_master_sidecars import (
    canonical_sidecar_is_current,
    write_canonical_sidecar,
)


CHECKSUM_CHUNK_BYTES = 1024 * 1024
EMBEDDED_TEXT_MAX_CHARS = 16 * 1024
EMBEDDED_KEYWORD_LIMIT = 256
EMBEDDED_XML_MAX_BYTES = 1024 * 1024
ARCHIVE_ENTRY_LIMIT = 256
ARCHIVE_ENTRY_NAME_MAX_CHARS = 1024
AUDIO_TAG_VALUE_LIMIT = 64
MIME_OVERRIDES = {
    ".wma": "audio/x-ms-wma",
    ".mkv": "video/x-matroska",
}
VIDEO_PROBE_MAX_BYTES = 2 * 1024 * 1024
VIDEO_PROBE_STREAM_LIMIT = 32
VIDEO_PROBE_TIMEOUT_SECONDS = 30
INCOMING_SOURCE = "incoming"
INVENTORY_SOURCE = "inventory"
PRIVATE_ASSET_VISIBILITY = "private"
SHARED_ASSET_VISIBILITY = "shared"
VAULT_WIDE_ASSET_VISIBILITY = "vault-wide"
THEATRE_CATEGORIES = frozenset({"Movies", "TV Shows"})
THEATRE_ASSET_TYPES = frozenset({"Movie", "Movies", "TV Show", "TV Shows"})
SCREENSHOT_ARCHIVE_SUBFOLDER = "Screenshots"
SYSTEM_SCREENSHOT_PROPOSAL_REASON = (
    "Hard-coded screenshot routing requires Archives/Screenshots."
)
logger = logging.getLogger("pv.vault-master")


def detected_mime_type(path: Path) -> str:
    return (
        MIME_OVERRIDES.get(path.suffix.casefold())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def proposed_destination_relative_path(
    category: str,
    relative_path: str,
    filename: str,
) -> PurePosixPath:
    if category != "Music":
        return PurePosixPath(filename)

    candidate = PurePosixPath(relative_path.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or candidate.name != filename
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return PurePosixPath(filename)
    return candidate


def proposed_destination_path(
    category: str,
    relative_path: str,
    filename: str,
    destination_subfolder: str | None = None,
) -> str:
    root = (
        PurePosixPath("/vault/Theatre/Movies")
        if category == "Movies"
        else PurePosixPath("/vault/Theatre/TV Shows")
        if category == "TV Shows"
        else PurePosixPath("/vault") / category
    )
    if destination_subfolder:
        subfolder = PurePosixPath(destination_subfolder)
        if subfolder.is_absolute() or any(
            part in {"", ".", ".."} for part in subfolder.parts
        ):
            raise ValueError("The proposed destination subfolder is invalid")
        root /= subfolder
    return str(
        root
        / proposed_destination_relative_path(category, relative_path, filename)
    )


def is_theatre_category(category: str | None) -> bool:
    return category in THEATRE_CATEGORIES


def is_theatre_asset_type(asset_type: str | None) -> bool:
    return asset_type in THEATRE_ASSET_TYPES


MAKEMKV_GENERIC_MOVIE_PATTERN = re.compile(
    r"^(?:g\d+|title)_t\d+(?:_[a-z0-9]+)*$", re.IGNORECASE
)
MAKEMKV_TRACK_PATTERN = re.compile(
    r"^(?P<hint>.+?)_t(?P<track>\d+)(?:_[a-z0-9]+)*$", re.IGNORECASE
)
UNUSABLE_MOVIE_HINTS = frozenset(
    {"arrival hall", "incoming", "movie", "movies", "original", "title", "video"}
)
MOVIE_PUBLICATION_SET_SCHEMA = "personal-vault.movie-publication-set.v1"
TV_PUBLICATION_SET_SCHEMA = "personal-vault.tv-publication-set.v1"


def tv_publication_set_destination(
    item: "ImportItem", items: list["ImportItem"]
) -> tuple[str, dict[str, object]]:
    """Derive one immutable Show/Season group from explicit episode evidence."""
    from app.tv_shows import parse_reviewed_episode

    parsed = parse_reviewed_episode(item.relative_path, item.filename)
    if item.proposed_category != "TV Shows" or parsed is None:
        raise ValueError("TV Show publication requires an explicit SnnEnn episode filename")
    parent = PurePosixPath(item.relative_path.replace("\\", "/")).parent
    members = []
    for candidate in items:
        candidate_parsed = parse_reviewed_episode(candidate.relative_path, candidate.filename)
        if (
            candidate.source_kind == INCOMING_SOURCE
            and candidate.owner_user_id == item.owner_user_id
            and candidate.proposed_category == "TV Shows"
            and PurePosixPath(candidate.relative_path.replace("\\", "/")).parent == parent
            and candidate_parsed is not None
            and candidate_parsed.show_title.casefold() == parsed.show_title.casefold()
            and candidate_parsed.season_number == parsed.season_number
            and candidate.state not in {"arrival_removed", "duplicate_removed", "rejected"}
        ):
            members.append((candidate, candidate_parsed))
    if not members or item.id not in {candidate.id for candidate, _ in members}:
        raise ValueError("TV Show publication set membership is invalid")
    if len({parsed_episode.episode_number for _, parsed_episode in members}) != len(members):
        raise ValueError("TV Show publication set has duplicate episode numbers")
    safe_title = re.sub(r"[\\/:]+", " - ", parsed.show_title)
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" .-")
    if not safe_title:
        raise ValueError("TV Show title is invalid")
    destination = str(
        PurePosixPath("/vault/Theatre/TV Shows") / safe_title /
        f"Season {parsed.season_number:02d}" /
        f"{safe_title} - S{parsed.season_number:02d}E{parsed.episode_number:02d}{Path(item.filename).suffix.casefold()}"
    )
    evidence: dict[str, object] = {
        "schema": TV_PUBLICATION_SET_SCHEMA,
        "source_directory": parent.as_posix(),
        "show_title": safe_title,
        "season_number": parsed.season_number,
        "members": [
            {"item_id": str(candidate.id), "episode_number": member.episode_number}
            for candidate, member in sorted(members, key=lambda value: value[1].episode_number)
        ],
    }
    return destination, evidence


def tv_publication_set_is_ready(item: "ImportItem", items: list["ImportItem"]) -> bool:
    marker = item.metadata.get("tv_publication_set")
    if not isinstance(marker, dict) or marker.get("schema") != TV_PUBLICATION_SET_SCHEMA:
        return False
    raw_members = marker.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        return False
    try:
        member_ids = {UUID(str(member["item_id"])) for member in raw_members if isinstance(member, dict)}
    except (KeyError, TypeError, ValueError):
        return False
    if len(member_ids) != len(raw_members):
        return False
    allowed = {"approved", "move_failed", "move_queued", "moving", "theatre_promotion_pending", "moved"}
    members = [candidate for candidate in items if candidate.id in member_ids]
    return len(members) == len(member_ids) and all(
        candidate.proposed_category == "TV Shows"
        and candidate.state in allowed
        and candidate.metadata.get("tv_publication_set") == marker
        for candidate in members
    )


def tv_publication_set_has_consistent_audience(item: "ImportItem", items: list["ImportItem"]) -> bool:
    marker = item.metadata.get("tv_publication_set")
    if not isinstance(marker, dict):
        return False
    raw_members = marker.get("members")
    if not isinstance(raw_members, list):
        return False
    member_ids = {str(member.get("item_id")) for member in raw_members if isinstance(member, dict)}
    audiences = {candidate.publication_audience or VAULT_WIDE_ASSET_VISIBILITY for candidate in items if str(candidate.id) in member_ids}
    return len(audiences) == 1


def is_identity_insufficient_movie_filename(filename: str) -> bool:
    return bool(MAKEMKV_GENERIC_MOVIE_PATTERN.fullmatch(Path(filename).stem))


def provisional_movie_destination(item: "ImportItem") -> tuple[str, str] | None:
    """Build a collision-safe Jellyfin hint path without asserting identity."""
    if item.proposed_category != "Movies":
        return None
    match = MAKEMKV_TRACK_PATTERN.fullmatch(Path(item.filename).stem)
    if match is None:
        return None

    relative = PurePosixPath(item.relative_path.replace("\\", "/"))
    candidates = (
        ([relative.parent.name] if relative.parent != PurePosixPath(".") else [])
        + [match.group("hint")]
    )
    hint = None
    for candidate in candidates:
        cleaned = re.sub(r"[_-]+", " ", candidate).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
        if (
            cleaned
            and cleaned.casefold() not in UNUSABLE_MOVIE_HINTS
            and not re.fullmatch(r"g\d+", cleaned, re.IGNORECASE)
            and all(ord(character) >= 32 for character in cleaned)
        ):
            hint = cleaned
            break
    if hint is None:
        return None

    track = int(match.group("track"))
    track_suffix = "" if track == 0 else f"_t{track:02d}"
    filename = f"{hint}{track_suffix}{Path(item.filename).suffix.casefold()}"
    destination = str(
        PurePosixPath("/vault/Theatre/Movies") / hint / filename
    )
    return destination, hint


def movie_publication_destination(
    item: "ImportItem",
) -> tuple[str, dict[str, object] | None]:
    """Choose reviewed identity first, then a provenance-only hint."""
    title = item.metadata_overrides.get("display_title")
    year = item.metadata_overrides.get("release_year")
    if isinstance(title, str) and title.strip() and isinstance(year, int):
        return canonical_reviewed_movie_destination(item), None
    provisional = provisional_movie_destination(item)
    if provisional is None:
        return canonical_reviewed_movie_destination(item), None
    destination, hint = provisional
    return destination, {
        "state": "provisional",
        "hint": hint,
        "source_relative_path": PurePosixPath(
            item.relative_path.replace("\\", "/")
        ).as_posix(),
        "source_filename": item.filename,
    }


def _movie_publication_set_members(
    item: "ImportItem",
    items: list["ImportItem"],
) -> list["ImportItem"]:
    """Return direct MakeMKV siblings from one owner-controlled source folder."""
    relative = PurePosixPath(item.relative_path.replace("\\", "/"))
    if relative.parent == PurePosixPath("."):
        return [item]
    members = [
        candidate
        for candidate in items
        if candidate.source_kind == INCOMING_SOURCE
        and candidate.owner_user_id == item.owner_user_id
        and PurePosixPath(
            candidate.relative_path.replace("\\", "/")
        ).parent
        == relative.parent
        and MAKEMKV_TRACK_PATTERN.fullmatch(Path(candidate.filename).stem)
        and candidate.state
        not in {
            "arrival_removed",
            "duplicate_removed",
            "rejected",
        }
    ]
    return sorted(members, key=lambda candidate: candidate.relative_path.casefold())


def _positive_movie_number(
    value: object,
    field_name: str,
) -> float:
    numeric = float(value) if isinstance(value, (int, float)) else 0.0
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(numeric)
        or numeric <= 0
    ):
        raise ValueError(
            f"Movie publication set has invalid {field_name} evidence"
        )
    return numeric


def _positive_movie_integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(
            f"Movie publication set has invalid {field_name} evidence"
        )
    return value


def movie_publication_set_destination(
    item: "ImportItem",
    items: list["ImportItem"],
    *,
    allow_progressed: bool = False,
) -> tuple[
    str,
    dict[str, object] | None,
    dict[str, object] | None,
]:
    """Choose one evidenced main feature and route its siblings as extras."""
    destination, provisional = movie_publication_destination(item)
    members = _movie_publication_set_members(item, items)
    if len(members) <= 1:
        return destination, provisional, None
    if any(candidate.proposed_category != "Movies" for candidate in members):
        raise ValueError(
            "Every file in the Movie publication set must be reviewed as Movies"
        )
    if not allow_progressed and any(
        candidate.state not in {"needs_review", "approved"}
        for candidate in members
    ):
        raise ValueError(
            "Movie publication set membership has already progressed; "
            "this item cannot be approved independently"
        )

    evidence: list[dict[str, object]] = []
    for candidate in members:
        duration = _positive_movie_number(
            candidate.metadata.get("duration_seconds"), "duration"
        )
        width = _positive_movie_integer(candidate.metadata.get("width"), "width")
        height = _positive_movie_integer(
            candidate.metadata.get("height"), "height"
        )
        size_bytes = _positive_movie_integer(candidate.size_bytes, "size")
        evidence.append(
            {
                "item_id": str(candidate.id),
                "source_relative_path": PurePosixPath(
                    candidate.relative_path.replace("\\", "/")
                ).as_posix(),
                "duration_seconds": duration,
                "size_bytes": size_bytes,
                "width": width,
                "height": height,
                "resolution_pixels": width * height,
            }
        )

    maximum_duration = max(entry["duration_seconds"] for entry in evidence)
    maximum_size = max(entry["size_bytes"] for entry in evidence)
    maximum_resolution = max(entry["resolution_pixels"] for entry in evidence)
    main_matches = [
        entry
        for entry in evidence
        if entry["duration_seconds"] == maximum_duration
        and entry["size_bytes"] == maximum_size
        and entry["resolution_pixels"] == maximum_resolution
    ]
    if len(main_matches) != 1:
        raise ValueError(
            "Movie publication set has no unambiguous main feature"
        )
    main_evidence = main_matches[0]
    main_item = next(
        candidate
        for candidate in members
        if str(candidate.id) == main_evidence["item_id"]
    )
    main_destination, _ = movie_publication_destination(main_item)
    movie_root = PurePosixPath(main_destination).parent
    is_main = item.id == main_item.id
    selected_destination = (
        main_destination
        if is_main
        else str(movie_root / "extras" / Path(item.filename).name)
    )
    if len(
        {
            main_destination,
            *(
                str(movie_root / "extras" / Path(candidate.filename).name)
                for candidate in members
                if candidate.id != main_item.id
            ),
        }
    ) != len(members):
        raise ValueError("Movie publication set destinations are ambiguous")

    relative_parent = PurePosixPath(
        item.relative_path.replace("\\", "/")
    ).parent
    set_evidence: dict[str, object] = {
        "schema": MOVIE_PUBLICATION_SET_SCHEMA,
        "source_directory": relative_parent.as_posix(),
        "main_item_id": str(main_item.id),
        "main_source_relative_path": main_evidence["source_relative_path"],
        "role": "main" if is_main else "extra",
        "companion_count": len(members) - 1,
        "members": evidence,
    }
    return selected_destination, provisional, set_evidence


def movie_publication_set_continuation_plan(
    item: "ImportItem",
    items: list["ImportItem"],
) -> dict[UUID, tuple[str, dict[str, object] | None, dict[str, object]]]:
    """Rebuild one progressed set without changing any authoritative record."""
    members = _movie_publication_set_members(item, items)
    if len(members) <= 1:
        raise ValueError("Movie publication set has no companions")
    plan: dict[
        UUID, tuple[str, dict[str, object] | None, dict[str, object]]
    ] = {}
    for member in members:
        destination, provisional, evidence = movie_publication_set_destination(
            member,
            items,
            allow_progressed=True,
        )
        if evidence is None:
            raise ValueError("Movie publication set evidence is incomplete")
        plan[member.id] = (destination, provisional, evidence)
    return plan


def reconcile_memory_movie_publication_set(
    store: "MemoryVaultMasterStore",
    item: "ImportItem",
    username: str,
) -> "ImportItem":
    """Test-double equivalent of the transactional PostgreSQL continuation."""
    if item.state != "needs_review":
        raise ValueError("Only one reviewed Movie companion may join the set")
    all_items = []
    for candidate in store.items.values():
        publication = store.arrival_managed_publications.get(candidate.id)
        asset = (
            store.catalogued_assets.get(
                str(publication.get("logical_destination"))
            )
            if publication is not None
            else None
        )
        all_items.append(
            replace(
                candidate,
                metadata={**asset.detected_metadata, **candidate.metadata},
            )
            if asset is not None
            else candidate
        )
    item = next(candidate for candidate in all_items if candidate.id == item.id)
    plan = movie_publication_set_continuation_plan(item, all_items)
    members = {
        candidate.id: candidate
        for candidate in all_items
        if candidate.id in plan
    }
    if set(members) != set(plan):
        raise ValueError("Movie publication set evidence is incomplete")
    accepted_unpublished = {
        "approved", "move_failed", "move_queued", "moving",
        "theatre_promotion_pending",
    }
    reconciled: dict[UUID, ImportItem] = {}
    published_assets: dict[UUID, CataloguedAsset] = {}
    for member_id, member in members.items():
        destination, provisional, evidence = plan[member_id]
        publication = store.arrival_managed_publications.get(member_id)
        if publication is not None:
            asset = store.catalogued_assets.get(destination)
            if (
                asset is None
                or asset.owner_user_id != member.owner_user_id
                or asset.sha256 != member.sha256
                or asset.size_bytes != member.size_bytes
                or publication.get("item_id") != str(member.id)
                or publication.get("owner_user_id") != str(member.owner_user_id)
                or publication.get("logical_destination") != destination
                or publication.get("logical_area") != "Theatre / Movies"
                or publication.get("relative_path")
                != destination.removeprefix("/vault/")
                or publication.get("expected_sha256") != member.sha256
                or publication.get("expected_size_bytes") != member.size_bytes
            ):
                raise ValueError(
                    "Published Movie set evidence does not match the reviewed set"
                )
            published_assets[member_id] = asset
            next_state = "moved"
        else:
            if member_id != item.id and member.state not in accepted_unpublished:
                raise ValueError(
                    "Exactly one reviewed Movie companion may join the progressed set"
                )
            if member_id != item.id and member.proposed_destination != destination:
                raise ValueError(
                    "Progressed Movie set destination does not match the reviewed set"
                )
            next_state = "approved" if member_id == item.id else member.state
        metadata = dict(member.metadata)
        if provisional is not None:
            metadata["movie_identity_provisional"] = provisional
        metadata["movie_publication_set"] = evidence
        reconciled[member_id] = replace(
            member,
            state=next_state,
            proposed_destination=destination,
            metadata=metadata,
        )
    for member_id, updated in reconciled.items():
        previous = members[member_id]
        store.items[updated.source_path] = updated
        asset = published_assets.get(member_id)
        if asset is not None:
            marker = updated.metadata["movie_publication_set"]
            refreshed = replace(
                asset,
                detected_metadata={
                    **asset.detected_metadata,
                    "movie_publication_set": marker,
                },
                effective_metadata={
                    **asset.effective_metadata,
                    "movie_publication_set": marker,
                },
                metadata_provenance={
                    **asset.metadata_provenance,
                    "movie_publication_set": "detected",
                },
            )
            store.catalogued_assets[asset.vault_path] = refreshed
            store._export_sidecar(refreshed)
            if previous.state != "moved":
                store._record_activity(
                    "publication_state_reconciled",
                    batch_id=updated.batch_id,
                    item=updated,
                    username="Arrival Hall managed publisher",
                    detail=(
                        "Restored moved state from exact managed publication evidence"
                    ),
                )
    approved = reconciled[item.id]
    store._record_activity(
        "proposal_approved",
        batch_id=approved.batch_id,
        item=approved,
        username=username,
    )
    return approved


def movie_publication_set_is_ready(
    item: "ImportItem",
    items: list["ImportItem"],
) -> bool:
    """Require one coherent, fully approved set before any member is queued."""
    marker = item.metadata.get("movie_publication_set")
    if marker is None:
        return True
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != MOVIE_PUBLICATION_SET_SCHEMA
        or not isinstance(marker.get("members"), list)
    ):
        return False
    try:
        member_ids = {
            UUID(str(entry["item_id"]))
            for entry in marker["members"]
            if isinstance(entry, dict)
        }
    except (KeyError, TypeError, ValueError):
        return False
    if len(member_ids) != len(marker["members"]) or item.id not in member_ids:
        return False
    members = {
        candidate.id: candidate
        for candidate in items
        if candidate.id in member_ids
    }
    if set(members) != member_ids:
        return False
    accepted_states = {
        "approved",
        "move_failed",
        "move_queued",
        "moving",
        "theatre_promotion_pending",
        "moved",
    }
    roles: list[str] = []
    for candidate in members.values():
        candidate_marker = candidate.metadata.get("movie_publication_set")
        if (
            candidate.state not in accepted_states
            or candidate.proposed_category != "Movies"
            or not isinstance(candidate_marker, dict)
            or candidate_marker.get("schema") != MOVIE_PUBLICATION_SET_SCHEMA
            or candidate_marker.get("main_item_id") != marker.get("main_item_id")
            or candidate_marker.get("source_directory")
            != marker.get("source_directory")
            or candidate_marker.get("members") != marker.get("members")
            or candidate_marker.get("companion_count")
            != marker.get("companion_count")
        ):
            return False
        role = candidate_marker.get("role")
        if role not in {"main", "extra"}:
            return False
        roles.append(role)
    return roles.count("main") == 1 and roles.count("extra") == len(roles) - 1


def movie_publication_set_has_consistent_audience(
    item: "ImportItem", items: list["ImportItem"]
) -> bool:
    """Movie extras inherit their bundle's one selected publication audience."""
    marker = item.metadata.get("movie_publication_set")
    if not isinstance(marker, dict):
        return True
    member_ids = marker.get("members")
    if not isinstance(member_ids, list):
        return False
    members = {str(member_id) for member_id in member_ids}
    audiences = {
        candidate.publication_audience or VAULT_WIDE_ASSET_VISIBILITY
        for candidate in items
        if str(candidate.id) in members
    }
    return len(audiences) == 1


def canonical_reviewed_movie_destination(item: "ImportItem") -> str:
    """Derive a Jellyfin-compatible path only from reviewed movie identity."""
    if item.proposed_category != "Movies":
        raise ValueError("Only Movies use reviewed canonical movie destinations")
    title = item.metadata_overrides.get("display_title")
    year = item.metadata_overrides.get("release_year")
    if not isinstance(title, str) or not title.strip() or not isinstance(year, int):
        raise ValueError(
            "Generic MakeMKV movies require a reviewed title and release year"
        )
    if year < 1000 or year > 9999:
        raise ValueError("The reviewed movie release year is invalid")
    return canonical_movie_destination(title, year, Path(item.filename).suffix)


def canonical_movie_destination(title: str, year: int, suffix: str) -> str:
    if not isinstance(year, int) or year < 1000 or year > 9999:
        raise ValueError("The reviewed movie release year is invalid")
    if not suffix.startswith(".") or len(suffix) < 2:
        raise ValueError("The reviewed movie filename extension is invalid")
    safe_title = re.sub(r"[\\/:]+", " - ", title.strip())
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" .-")
    if not safe_title or any(ord(character) < 32 for character in safe_title):
        raise ValueError("The reviewed movie title is invalid")
    canonical_name = f"{safe_title} ({year})"
    return str(
        PurePosixPath("/vault/Theatre/Movies")
        / canonical_name
        / f"{canonical_name}{suffix.casefold()}"
    )


def matches_reliable_imported_movie_identity(
    detected: dict[str, object],
    imported: dict[str, object],
    title: str,
    year: int,
) -> bool:
    marker = detected.get("movie_identity_provisional")
    provider_ids = imported.get("provider_ids")
    return bool(
        isinstance(marker, dict)
        and marker.get("state") == "provisional"
        and imported.get("display_title") == title
        and imported.get("release_year") == year
        and isinstance(provider_ids, dict)
        and any(
            isinstance(key, str)
            and key.strip()
            and isinstance(value, str)
            and value.strip()
            for key, value in provider_ids.items()
        )
    )


def has_hard_coded_screenshot_marker(
    filename: str,
    metadata: dict[str, object],
) -> bool:
    """Return true when trusted file facts explicitly say this is a screenshot."""
    values = [filename]
    for key in (
        "description",
        "image_description",
        "display_title",
        "title",
        "software",
    ):
        value = metadata.get(key)
        if isinstance(value, str):
            values.append(value)
    return any("screenshot" in value.casefold() for value in values)


@dataclass(frozen=True)
class ScannedFile:
    source_path: str
    relative_path: str
    filename: str
    size_bytes: int
    mime_type: str
    modified_at: datetime
    sha256: str
    metadata: dict[str, object]
    owner_username: str | None = None
    owner_user_id: UUID | None = None


@dataclass(frozen=True)
class ImportItem:
    id: UUID
    batch_id: UUID
    source_kind: str
    source_path: str
    relative_path: str
    filename: str
    size_bytes: int
    mime_type: str
    modified_at: datetime
    sha256: str
    state: str
    duplicate_of_id: UUID | None
    proposed_category: str | None
    proposed_destination: str | None
    proposal_reason: str | None
    proposal_confidence: str | None
    metadata: dict[str, object]
    metadata_overrides: dict[str, object]
    publication_audience: str | None = None
    owner_username: str = "owner"
    owner_user_id: UUID | None = None


@dataclass(frozen=True)
class CataloguedFile:
    """One immutable physical file belonging to a canonical Vault asset."""

    vault_path: str
    filename: str
    size_bytes: int
    mime_type: str
    sha256: str
    file_role: str = "primary"


@dataclass(frozen=True)
class CataloguedAsset:
    id: UUID
    asset_type: str
    display_title: str
    captured_on: date | None
    location: str | None
    vault_path: str
    filename: str
    size_bytes: int
    mime_type: str
    sha256: str
    metadata: dict[str, object]
    metadata_provenance: dict[str, str]
    detected_metadata: dict[str, object] = field(default_factory=dict)
    imported_metadata: dict[str, object] = field(default_factory=dict)
    user_overrides: dict[str, object] = field(default_factory=dict)
    effective_metadata: dict[str, object] = field(default_factory=dict)
    owner_username: str = "owner"
    owner_user_id: UUID | None = None
    origin_vault_id: UUID | None = None
    visibility: str = PRIVATE_ASSET_VISIBILITY
    shared_with: tuple[str, ...] = ()
    shared_with_user_ids: tuple[UUID, ...] = ()
    lifecycle_state: str = "active"


@dataclass(frozen=True)
class AssetRelationshipAnalysis:
    """Deterministic, non-mutating evidence about two canonical assets."""

    classification: str
    confidence: str
    asset_ids: tuple[UUID, UUID]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class CataloguedAssetRelationship:
    first_asset_id: UUID
    second_asset_id: UUID
    relationship_type: str
    confidence: str
    evidence: tuple[str, ...]
    created_by: str
    created_at: datetime


_IDENTITY_NOISE_TOKENS = {
    "copy",
    "duplicate",
    "final",
    "hd",
    "uhd",
    "1080p",
    "2160p",
    "4k",
}
_EDITION_TOKENS = {
    "alternate",
    "cut",
    "director",
    "extended",
    "remaster",
    "remastered",
    "theatrical",
    "v2",
    "version",
}


def _asset_name_signals(asset: CataloguedAsset) -> tuple[str, frozenset[str]]:
    tokens = re.findall(r"[a-z0-9]+", Path(asset.filename).stem.lower())
    edition_tokens = frozenset(token for token in tokens if token in _EDITION_TOKENS)
    identity = " ".join(
        token
        for token in tokens
        if token not in _IDENTITY_NOISE_TOKENS and token not in _EDITION_TOKENS
    )
    return identity, edition_tokens


def _asset_number(asset: CataloguedAsset, field_name: str) -> float | None:
    value = asset.effective_metadata.get(field_name, asset.metadata.get(field_name))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def analyse_asset_relationship(
    first: CataloguedAsset,
    second: CataloguedAsset,
) -> AssetRelationshipAnalysis:
    """Compare two assets without persisting a relationship or proposal."""
    if first.id == second.id:
        raise ValueError("Relationship analysis requires two different assets")
    asset_ids = tuple(sorted((first.id, second.id), key=str))
    if first.sha256 == second.sha256:
        return AssetRelationshipAnalysis(
            classification="exact_duplicate",
            confidence="certain",
            asset_ids=asset_ids,
            evidence=("SHA-256 checksums are identical",),
        )

    evidence: list[str] = []
    first_identity, first_editions = _asset_name_signals(first)
    second_identity, second_editions = _asset_name_signals(second)
    same_identity = bool(first_identity) and first_identity == second_identity
    if same_identity:
        evidence.append(f"Normalised filename identity matches: {first_identity}")

    duration_first = _asset_number(first, "duration_seconds")
    duration_second = _asset_number(second, "duration_seconds")
    if duration_first is not None and duration_second is not None:
        difference = abs(duration_first - duration_second)
        if difference <= 2:
            evidence.append(f"Durations differ by only {difference:.3f} seconds")

    dimensions_first = (
        _asset_number(first, "width"),
        _asset_number(first, "height"),
    )
    dimensions_second = (
        _asset_number(second, "width"),
        _asset_number(second, "height"),
    )
    if None not in dimensions_first and dimensions_first == dimensions_second:
        evidence.append(
            f"Pixel dimensions match: {int(dimensions_first[0])} × {int(dimensions_first[1])}"
        )

    pages_first = _asset_number(first, "page_count")
    pages_second = _asset_number(second, "page_count")
    if pages_first is not None and pages_first == pages_second:
        evidence.append(f"Page counts match: {int(pages_first)}")

    larger_size = max(first.size_bytes, second.size_bytes)
    if larger_size and abs(first.size_bytes - second.size_bytes) / larger_size <= 0.02:
        evidence.append("File sizes differ by no more than 2 percent")

    technical_match = len(evidence) >= 2 if same_identity else False
    if same_identity and first_editions != second_editions and (
        first_editions or second_editions
    ):
        edition_evidence = ", ".join(sorted(first_editions | second_editions))
        return AssetRelationshipAnalysis(
            classification="alternate_version",
            confidence="high" if technical_match else "medium",
            asset_ids=asset_ids,
            evidence=tuple(evidence + [f"Edition markers differ: {edition_evidence}"]),
        )
    if technical_match:
        return AssetRelationshipAnalysis(
            classification="probable_duplicate",
            confidence="high",
            asset_ids=asset_ids,
            evidence=tuple(evidence),
        )
    if same_identity:
        return AssetRelationshipAnalysis(
            classification="related_file",
            confidence="low",
            asset_ids=asset_ids,
            evidence=tuple(evidence),
        )
    return AssetRelationshipAnalysis(
        classification="none",
        confidence="none",
        asset_ids=asset_ids,
        evidence=(),
    )


def canonical_relationship_type(classification: str) -> str:
    relationship_types = {
        "exact_duplicate": "duplicate",
        "probable_duplicate": "duplicate",
        "alternate_version": "alternate_version",
        "related_file": "related_file",
    }
    try:
        return relationship_types[classification]
    except KeyError as error:
        raise ValueError("Classification cannot become a canonical relationship") from error


def asset_is_visible_to(asset: CataloguedAsset, user: object) -> bool:
    """Return whether a user may discover or open a canonical asset.

    Private assets belong only to their owner.  Shared assets are visible only
    to explicitly listed family members; there is deliberately no implicit
    "all authenticated users" access path.
    """
    user_id = user if isinstance(user, UUID) else getattr(user, "user_id", None)
    if not isinstance(user_id, UUID):
        return False
    return asset.owner_user_id == user_id or asset.visibility == VAULT_WIDE_ASSET_VISIBILITY or (
        asset.visibility == SHARED_ASSET_VISIBILITY
        and user_id in asset.shared_with_user_ids
    )


def asset_is_owned_by(asset: CataloguedAsset, user: object) -> bool:
    """Return whether the immutable authenticated identity owns an asset.

    ``owner_username`` remains a compatibility/display field only.  It must
    never authorize a metadata, lifecycle, or sharing operation.
    """
    user_id = getattr(user, "user_id", None)
    return isinstance(user_id, UUID) and asset.owner_user_id is not None and asset.owner_user_id == user_id


def asset_is_editable_by(asset: CataloguedAsset, user: object) -> bool:
    """Only the immutable owner may change the canonical metadata record."""
    return asset_is_owned_by(asset, user)


@dataclass(frozen=True)
class VaultMasterActivity:
    id: UUID
    batch_id: UUID | None
    item_id: UUID | None
    source_kind: str | None
    filename: str | None
    action: str
    username: str | None
    detail: str
    succeeded: bool
    created_at: datetime


@dataclass(frozen=True)
class SidecarReconciliation:
    checked: int
    current: int
    repaired: int
    failed: int


class VaultMasterStore(Protocol):
    def get_local_vault_id(self) -> UUID: ...

    def migrate_source_root(
        self,
        source_kind: str,
        previous_root: str,
        current_root: str,
    ) -> int: ...

    def create_batch(self, source_kind: str, source_root: str) -> UUID: ...

    def record_file(
        self,
        batch_id: UUID,
        source_kind: str,
        scanned_file: ScannedFile,
    ) -> ImportItem: ...

    def complete_batch(self, batch_id: UUID, item_count: int) -> None: ...

    def fail_batch(self, batch_id: UUID, detail: str) -> None: ...

    def list_items(self) -> list[ImportItem]: ...

    def claim_next_batch(self) -> tuple[UUID, str, str] | None: ...

    def list_batches(self) -> list[dict[str, object]]: ...

    def find_active_batch(
        self,
        source_kind: str,
        source_root: str,
    ) -> UUID | None: ...

    def list_activity(
        self,
        limit: int = 100,
        *,
        include_file_inventory: bool = True,
        include_file_analysis: bool = True,
        include_empty_scans: bool = True,
    ) -> list[VaultMasterActivity]: ...

    def update_proposal(
        self,
        item_id: UUID,
        category: str,
        username: str,
        destination_subfolder: str | None = None,
        publication_audience: str | None = None,
    ) -> ImportItem | None: ...

    def apply_ai_proposal(
        self,
        item_id: UUID,
        category: str,
        reason: str,
        destination_subfolder: str | None = None,
        force: bool = False,
    ) -> ImportItem | None: ...

    def update_metadata_overrides(
        self,
        item_id: UUID,
        changes: dict[str, object | None],
        username: str,
    ) -> ImportItem | None: ...

    def record_decision(
        self, item_id: UUID, decision: str, username: str
    ) -> ImportItem | None: ...

    def get_item(self, item_id: UUID) -> ImportItem | None: ...

    def get_metadata_overrides(
        self, destination_paths: list[str]
    ) -> dict[str, dict[str, object]]: ...

    def get_catalogued_asset(
        self, vault_path: str
    ) -> CataloguedAsset | None: ...

    def get_catalogued_assets(
        self, vault_paths: list[str]
    ) -> dict[str, CataloguedAsset]: ...

    def get_visible_catalogued_assets(
        self, vault_paths: list[str], username: str
    ) -> dict[str, CataloguedAsset]: ...

    def get_catalogued_asset_by_id(
        self, asset_id: UUID
    ) -> CataloguedAsset | None: ...

    def list_owned_catalogued_assets(
        self, username: str
    ) -> list[CataloguedAsset]: ...

    def list_owned_catalogued_assets_by_user_id(
        self, owner_user_id: UUID
    ) -> list[CataloguedAsset]: ...

    def list_visible_catalogued_assets(
        self, username: str
    ) -> list[CataloguedAsset]: ...

    def search_catalogued_assets(
        self, query: str, limit: int = 50
    ) -> list[CataloguedAsset]: ...

    def get_visible_catalogued_asset_by_id(
        self, asset_id: UUID, username: str
    ) -> CataloguedAsset | None: ...

    def list_visible_movie_assets(
        self, username: str
    ) -> list[CataloguedAsset]: ...

    def list_catalogued_assets_by_vault_path_prefix(
        self, prefix: str
    ) -> list[CataloguedAsset]: ...

    def set_movie_exclusive_state(
        self, asset_id: UUID, username: str, is_exclusive: bool
    ) -> CataloguedAsset | None: ...

    def search_visible_catalogued_assets(
        self, query: str, username: str, limit: int = 50
    ) -> list[CataloguedAsset]: ...

    def update_catalogued_asset_metadata(
        self,
        asset_id: UUID,
        changes: dict[str, str | None],
        username: str,
    ) -> CataloguedAsset | None: ...

    def update_catalogued_asset_access(
        self,
        asset_id: UUID,
        visibility: str,
        shared_with: tuple[str, ...],
        username: str,
        *,
        local_all: bool = False,
        share_mode: Literal["quick", "standard"] = "quick",
        shared_with_user_ids: tuple[UUID, ...] = (),
    ) -> CataloguedAsset | None: ...

    def import_catalogued_asset_metadata(
        self,
        asset_id: UUID,
        metadata: dict[str, object],
        source: str,
    ) -> CataloguedAsset | None: ...

    def list_catalogued_asset_history(
        self, asset_id: UUID
    ) -> list[dict[str, object]]: ...

    def record_catalogued_asset_history(
        self,
        asset_id: UUID,
        username: str,
        action: str,
        current_values: dict[str, object],
    ) -> dict[str, object] | None: ...

    def request_asset_relationship_review(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        classification: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> dict[str, object] | None: ...

    def retain_separate_asset_relationship_review(
        self, first_asset_id: UUID, second_asset_id: UUID, username: str
    ) -> dict[str, object] | None: ...

    def create_catalogued_asset_relationship(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        relationship_type: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> CataloguedAssetRelationship | None: ...

    def list_catalogued_asset_relationships(
        self, asset_id: UUID
    ) -> list[CataloguedAssetRelationship]: ...

    def approve_asset_relationship_review(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        relationship_type: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> dict[str, object] | None: ...

    def request_catalogued_asset_quarantine_review(
        self,
        asset_id: UUID,
        username: str,
        reason: str | None,
    ) -> dict[str, object] | None: ...

    def cancel_catalogued_asset_quarantine_review(
        self,
        asset_id: UUID,
        username: str,
    ) -> dict[str, object] | None: ...

    def confirm_catalogued_asset_quarantine(
        self,
        asset_id: UUID,
        source_vault_path: str,
        quarantine_vault_path: str,
        username: str,
    ) -> CataloguedAsset | None: ...

    def update_catalogued_assets_access(
        self, asset_ids: list[UUID], visibility: str, shared_with: tuple[str, ...], username: str,
        *, local_all: bool = False, share_mode: Literal["quick", "standard"] = "quick",
    ) -> list[CataloguedAsset]: ...

    def relocate_catalogued_asset(
        self,
        asset_id: UUID,
        source_vault_path: str,
        destination_vault_path: str,
        username: str,
        action: str,
    ) -> CataloguedAsset | None: ...

    def request_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
        reason: str,
        eligible_at: datetime,
    ) -> dict[str, object] | None: ...

    def cancel_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
    ) -> dict[str, object] | None: ...

    def confirm_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
        checksum: str,
    ) -> dict[str, object] | None: ...

    def record_catalogued_asset_permanent_deletion(
        self,
        asset_id: UUID,
        vault_path: str,
        checksum: str,
        username: str,
    ) -> dict[str, object] | None: ...

    def set_catalogued_asset_lifecycle_state(
        self, asset_id: UUID, owner_user_id: UUID, username: str, state: str
    ) -> CataloguedAsset | None: ...

    def has_catalogued_asset_deletion(self, asset_id: UUID) -> bool: ...

    def reconcile_sidecars(self) -> SidecarReconciliation: ...

    def restore_catalogued_asset(
        self,
        asset: CataloguedAsset,
        username: str,
    ) -> CataloguedAsset: ...

    def record_sidecar_restore_failure(
        self,
        asset_id: UUID,
        username: str,
        detail: str,
    ) -> None: ...

    def record_move_result(
        self,
        item_id: UUID,
        state: str,
        username: str,
        detail: str,
        publish_catalogue: bool = True,
    ) -> ImportItem | None: ...

    def record_duplicate_result(
        self,
        item_id: UUID,
        state: str,
        username: str,
        detail: str,
    ) -> ImportItem | None: ...

    def queue_move(self, item_id: UUID, username: str) -> ImportItem | None: ...

    def claim_next_move(self) -> ImportItem | None: ...

    def mark_theatre_promotion_pending(self, item_id: UUID) -> ImportItem | None: ...

    def publish_arrival_managed_receipt(
        self, item_id: UUID, receipt: dict[str, object]
    ) -> CataloguedAsset | None: ...

    def theatre_movie_rename_snapshot(
        self, asset_id: UUID, owner_user_id: UUID
    ) -> dict[str, object] | None: ...

    def complete_theatre_movie_rename(
        self, receipt: dict[str, object]
    ) -> CataloguedAsset | None: ...


def sha256_file(
    path: Path,
    chunk_size: int = CHECKSUM_CHUNK_BYTES,
) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)

    return digest.hexdigest()


def require_file_within_root(path: Path, root: Path) -> Path:
    if path.is_symlink():
        raise ValueError("Symbolic links are not valid Vault Master inputs")

    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)

    if (
        resolved_path == resolved_root
        or resolved_root not in resolved_path.parents
        or not resolved_path.is_file()
    ):
        raise ValueError("File is outside the configured Vault root")

    return resolved_path


def safely_move_approved_file(
    item: ImportItem,
    incoming_root: Path,
    destination_root: Path,
) -> Path:
    if item.state not in {"approved", "move_failed"}:
        raise ValueError("Only an approved file can be moved")
    if Path(item.filename).name != item.filename:
        raise ValueError("The recorded filename is not safe")

    source = require_file_within_root(Path(item.source_path), incoming_root)
    resolved_destination_root = destination_root.resolve(strict=True)
    if not resolved_destination_root.is_dir():
        raise ValueError("The approved destination is not a directory")

    destination_relative = proposed_destination_relative_path(
        item.proposed_category or "",
        item.relative_path,
        item.filename,
    )
    destination_parent = resolved_destination_root
    for part in destination_relative.parent.parts:
        candidate_parent = destination_parent / part
        if candidate_parent.is_symlink():
            raise ValueError("The approved destination contains a symbolic link")
        if candidate_parent.exists() and not candidate_parent.is_dir():
            raise ValueError("The approved destination folder is not a directory")
        destination_parent = candidate_parent

    if sha256_file(source) != item.sha256:
        raise ValueError("The source checksum changed after approval")

    destination_parent.mkdir(parents=True, exist_ok=True)
    if not destination_parent.resolve(strict=True).is_relative_to(
        resolved_destination_root
    ):
        raise ValueError("The approved destination is outside its Vault root")
    destination = destination_parent / item.filename
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("The approved destination already exists")

    temporary = resolved_destination_root / (
        f".pv-vault-master-{item.id.hex}-{uuid4().hex}.part"
    )

    try:
        with source.open("rb") as source_handle:
            with temporary.open("xb") as destination_handle:
                shutil.copyfileobj(
                    source_handle,
                    destination_handle,
                    CHECKSUM_CHUNK_BYTES,
                )
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
        shutil.copystat(source, temporary, follow_symlinks=False)

        if sha256_file(temporary) != item.sha256:
            raise ValueError("The copied file failed checksum verification")
        if sha256_file(source) != item.sha256:
            raise ValueError("The source changed while it was being copied")

        # A hard link publishes the verified temporary file atomically and
        # fails rather than replacing a destination created concurrently.
        os.link(temporary, destination)
        temporary.unlink()
        source.unlink()
        return destination
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def safely_remove_exact_duplicate(
    item: ImportItem,
    duplicate: ImportItem,
    incoming_root: Path,
    inventory_roots: tuple[Path, ...],
) -> None:
    if item.state not in {"needs_review", "duplicate_remove_failed"}:
        raise ValueError("The duplicate is not awaiting review")
    if item.duplicate_of_id != duplicate.id:
        raise ValueError("The recorded duplicate does not match")
    if duplicate.source_kind != INVENTORY_SOURCE:
        raise ValueError("The matching file is not in the Vault inventory")
    if item.sha256 != duplicate.sha256:
        raise ValueError("The recorded checksums no longer match")

    source = require_file_within_root(Path(item.source_path), incoming_root)
    matching_file: Path | None = None
    for inventory_root in inventory_roots:
        try:
            matching_file = require_file_within_root(
                Path(duplicate.source_path),
                inventory_root,
            )
            break
        except (FileNotFoundError, ValueError):
            continue
    if matching_file is None:
        raise ValueError("The matching Vault file could not be validated")
    if sha256_file(matching_file) != item.sha256:
        raise ValueError("The matching Vault file checksum has changed")
    if sha256_file(source) != item.sha256:
        raise ValueError("The Arrival Hall file checksum has changed")

    source.unlink()


def safely_remove_rejected_arrival_item(
    item: ImportItem,
    incoming_root: Path,
) -> None:
    if item.source_kind != INCOMING_SOURCE or item.state != "rejected":
        raise ValueError("Only a rejected Arrival Hall file can be removed")
    source = require_file_within_root(Path(item.source_path), incoming_root)
    if source.stat().st_size != item.size_bytes:
        raise ValueError("The Arrival Hall file size has changed")
    if sha256_file(source) != item.sha256:
        raise ValueError("The Arrival Hall file checksum has changed")
    source.unlink()


def scan_file(
    path: Path,
    root: Path,
    owner_username: str | None = None,
    owner_user_id: UUID | None = None,
) -> ScannedFile:
    resolved_path = require_file_within_root(path, root)
    file_stat = resolved_path.stat()

    return ScannedFile(
        source_path=str(resolved_path),
        relative_path=str(resolved_path.relative_to(root.resolve(strict=True))),
        filename=resolved_path.name,
        size_bytes=file_stat.st_size,
        mime_type=detected_mime_type(resolved_path),
        modified_at=datetime.fromtimestamp(
            file_stat.st_mtime,
            tz=timezone.utc,
        ),
        sha256=sha256_file(resolved_path),
        metadata=extract_basic_metadata(resolved_path),
        owner_username=owner_username,
        owner_user_id=owner_user_id,
    )


def _gps_rational(value: object) -> float:
    if isinstance(value, tuple) and len(value) == 2:
        numerator, denominator = value
        return float(numerator) / float(denominator)
    return float(value)  # type: ignore[arg-type]


def _gps_coordinate(value: object, reference: object) -> float:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("GPS coordinate must contain degrees, minutes, seconds")
    degrees, minutes, seconds = (
        _gps_rational(component) for component in value
    )
    coordinate = degrees + (minutes / 60) + (seconds / 3600)
    direction = str(reference).strip().upper()
    if direction in {"S", "W"}:
        coordinate = -coordinate
    elif direction not in {"N", "E"}:
        raise ValueError("GPS coordinate has an invalid direction")
    return coordinate


def _extract_exif_gps(exif: Image.Exif) -> dict[str, object]:
    try:
        gps = exif.get_ifd(34853)
    except (AttributeError, KeyError, TypeError, ValueError):
        gps = exif.get(34853)
    if not isinstance(gps, dict):
        return {}

    try:
        latitude = _gps_coordinate(gps[2], gps[1])
        longitude = _gps_coordinate(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {}
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {}

    metadata: dict[str, object] = {
        "gps_latitude": round(latitude, 7),
        "gps_longitude": round(longitude, 7),
    }
    try:
        altitude = _gps_rational(gps[6])
        altitude_reference = int(gps.get(5, 0))
        metadata["gps_altitude_metres"] = round(
            -altitude if altitude_reference == 1 else altitude,
            3,
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    try:
        result = reverse_geocode.search([(latitude, longitude)])[0]
    except (IndexError, KeyError, OSError, TypeError, ValueError):
        result = {}
    city = str(result.get("city") or "").strip()
    country = str(
        result.get("country") or result.get("country_code") or ""
    ).strip()
    if city and country:
        metadata["location"] = f"{city}, {country}"
    elif city or country:
        metadata["location"] = city or country
    return metadata


def _exif_number(value: object) -> float | None:
    try:
        return _gps_rational(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _extract_exif_technical(exif: Image.Exif) -> dict[str, object]:
    metadata: dict[str, object] = {}
    text_tags = {
        270: "image_description",
        315: "artist",
        33432: "copyright",
        42033: "camera_serial_number",
        42035: "lens_make",
        42036: "lens_model",
        42037: "lens_serial_number",
    }
    for tag_id, name in text_tags.items():
        value = _embedded_text(exif.get(tag_id))
        if value:
            metadata[name] = value

    integer_tags = {
        274: "orientation",
        34850: "exposure_program",
        34855: "iso_speed",
        37383: "metering_mode",
        37385: "flash",
        40961: "color_space",
        40962: "pixel_width",
        40963: "pixel_height",
        41985: "custom_rendered",
        41986: "exposure_mode",
        41987: "white_balance",
        41990: "scene_capture_type",
        41991: "gain_control",
        41992: "contrast",
        41993: "saturation",
        41994: "sharpness",
        41996: "subject_distance_range",
    }
    for tag_id, name in integer_tags.items():
        value = exif.get(tag_id)
        try:
            if value is not None:
                metadata[name] = int(value)
        except (TypeError, ValueError):
            continue

    number_tags = {
        33434: "exposure_time_seconds",
        33437: "aperture_f_number",
        37386: "focal_length_mm",
        41486: "focal_plane_x_resolution",
        41487: "focal_plane_y_resolution",
        41988: "digital_zoom_ratio",
    }
    for tag_id, name in number_tags.items():
        value = _exif_number(exif.get(tag_id))
        if value is not None:
            metadata[name] = round(value, 6)

    focal_length_35mm = exif.get(41989)
    try:
        if focal_length_35mm is not None:
            metadata["focal_length_35mm"] = int(focal_length_35mm)
    except (TypeError, ValueError):
        pass
    return metadata


def _embedded_text(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("latin-1", errors="replace")
    if isinstance(value, str):
        return (
            value.strip().rstrip("\x00")[:EMBEDDED_TEXT_MAX_CHARS]
            or None
        )
    if isinstance(value, dict):
        preferred = value.get("x-default")
        if preferred is not None:
            return _embedded_text(preferred)
        for nested in value.values():
            text = _embedded_text(nested)
            if text:
                return text
    if isinstance(value, (tuple, list)):
        for nested in value:
            text = _embedded_text(nested)
            if text:
                return text
    return None


def _normalised_metadata_name(value: object) -> str:
    name = str(value).rsplit("}", 1)[-1].rsplit(":", 1)[-1]
    return "".join(
        character for character in name.casefold() if character.isalnum()
    )


def _embedded_texts(value: object) -> list[str]:
    if isinstance(value, dict):
        if "x-default" in value:
            text = _embedded_text(value["x-default"])
            return [text] if text else []
        return [
            text
            for nested in value.values()
            for text in _embedded_texts(nested)
        ]
    if isinstance(value, (tuple, list)):
        return [
            text
            for nested in value
            for text in _embedded_texts(nested)
        ]
    text = _embedded_text(value)
    return [text] if text else []


def _audio_tag_texts(value: object) -> list[str]:
    if isinstance(value, (tuple, list)):
        return [
            text
            for nested in value
            for text in _audio_tag_texts(nested)
        ]
    unwrapped = getattr(value, "value", value)
    if isinstance(unwrapped, (int, float)) and not isinstance(
        unwrapped, bool
    ):
        return [str(unwrapped)]
    return _embedded_texts(unwrapped)


def _collect_named_metadata(
    value: object,
    collected: dict[str, list[object]],
) -> None:
    if isinstance(value, dict):
        for name, nested in value.items():
            collected.setdefault(
                _normalised_metadata_name(name),
                [],
            ).append(nested)
            _collect_named_metadata(nested, collected)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _collect_named_metadata(nested, collected)


def _first_named_text(
    collected: dict[str, list[object]],
    *names: str,
) -> str | None:
    for name in names:
        for value in collected.get(name, []):
            text = _embedded_text(value)
            if text:
                return text
    return None


def _place_from_named_metadata(
    collected: dict[str, list[object]],
) -> str | None:
    parts: list[str] = []
    for names in (
        ("sublocation", "location"),
        ("city",),
        ("state", "province"),
        ("country", "countryname"),
    ):
        part = _first_named_text(collected, *names)
        if part and part.casefold() not in {
            existing.casefold() for existing in parts
        }:
            parts.append(part)
    return ", ".join(parts) or None


def _extract_xmp_metadata(xmp: object) -> dict[str, object]:
    collected: dict[str, list[object]] = {}
    _collect_named_metadata(xmp, collected)
    if not collected:
        return {}

    metadata: dict[str, object] = {}
    mappings = {
        "display_title": ("title", "headline"),
        "description": ("description", "caption"),
        "creator": ("creator", "artist", "byline"),
        "xmp_created_at": (
            "datetimeoriginal",
            "createdate",
            "datecreated",
        ),
    }
    for destination, names in mappings.items():
        value = _first_named_text(collected, *names)
        if value:
            metadata[destination] = value
    location = _place_from_named_metadata(collected)
    if location:
        metadata["location"] = location
    keywords = [
        keyword
        for name in ("subject", "keywords")
        for value in collected.get(name, [])
        for text in _embedded_texts(value)
        for keyword in text.replace(";", ",").split(",")
        if keyword.strip()
    ]
    if keywords:
        metadata["keywords"] = list(
            dict.fromkeys(keyword.strip() for keyword in keywords)
        )[:EMBEDDED_KEYWORD_LIMIT]
    return metadata


def _extract_iptc_metadata(iptc: object) -> dict[str, object]:
    if not isinstance(iptc, dict):
        return {}
    metadata: dict[str, object] = {}
    mappings = {
        "display_title": ((2, 5), (2, 105)),
        "description": ((2, 120),),
        "creator": ((2, 80),),
        "iptc_created_at": ((2, 55),),
    }
    for destination, tags in mappings.items():
        value = next(
            (
                text
                for tag in tags
                if (text := _embedded_text(iptc.get(tag)))
            ),
            None,
        )
        if value:
            metadata[destination] = value
    place_parts = [
        text
        for tag in ((2, 92), (2, 90), (2, 95), (2, 101))
        if (text := _embedded_text(iptc.get(tag)))
    ]
    if place_parts:
        metadata["location"] = ", ".join(dict.fromkeys(place_parts))
    keywords = iptc.get((2, 25))
    if keywords:
        values = keywords if isinstance(keywords, list) else [keywords]
        metadata["keywords"] = [
            text
            for value in values
            if (text := _embedded_text(value))
        ][:EMBEDDED_KEYWORD_LIMIT]
    return metadata


def _parse_embedded_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_ooxml_metadata(path: Path) -> dict[str, object]:
    suffix = path.suffix.casefold()
    if suffix not in {".docx", ".xlsx", ".pptx"}:
        return {}

    try:
        with ZipFile(path) as package:
            core_info = package.getinfo("docProps/core.xml")
            if core_info.file_size > EMBEDDED_XML_MAX_BYTES:
                return {}
            core_xml = package.read(core_info)
            if b"<!DOCTYPE" in core_xml.upper() or b"<!ENTITY" in core_xml.upper():
                return {}
            root = ElementTree.fromstring(core_xml)
    except (
        BadZipFile,
        KeyError,
        OSError,
        ElementTree.ParseError,
    ):
        return {}

    namespaces = {
        "cp": (
            "http://schemas.openxmlformats.org/package/2006/"
            "metadata/core-properties"
        ),
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
    }
    fields = {
        "dc:title": "display_title",
        "dc:subject": "subject",
        "dc:creator": "creator",
        "dc:description": "description",
        "cp:keywords": "keywords",
        "cp:lastModifiedBy": "last_modified_by",
        "cp:category": "category",
        "cp:revision": "revision",
    }
    metadata: dict[str, object] = {"document_format": suffix.removeprefix(".")}
    for query, destination in fields.items():
        node = root.find(query, namespaces)
        value = _embedded_text(node.text if node is not None else None)
        if value:
            metadata[destination] = value

    date_candidates: list[tuple[datetime, str]] = []
    for query, destination in (
        ("dcterms:created", "document_created_at"),
        ("dcterms:modified", "document_modified_at"),
    ):
        node = root.find(query, namespaces)
        value = _embedded_text(node.text if node is not None else None)
        if not value:
            continue
        parsed = _parse_embedded_datetime(value)
        if parsed is None:
            continue
        metadata[destination] = parsed.isoformat()
        date_candidates.append((parsed, destination))

    if date_candidates:
        captured_at, source = min(date_candidates, key=lambda candidate: candidate[0])
        metadata["captured_at"] = captured_at.isoformat()
        metadata["capture_date_source"] = source
    return metadata


def _normalise_document_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = _parse_embedded_datetime(value)
        if parsed is None:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_pdf_metadata(path: Path) -> dict[str, object]:
    if path.suffix.casefold() != ".pdf":
        return {}

    try:
        reader = PdfReader(path, strict=False)
        encrypted = reader.is_encrypted
    except (OSError, PdfReadError, ValueError):
        return {}

    metadata: dict[str, object] = {
        "document_format": "pdf",
        "pdf_encrypted": encrypted,
    }
    if encrypted:
        return metadata

    try:
        metadata["page_count"] = len(reader.pages)
        document_info = reader.metadata
    except (FileNotDecryptedError, OSError, PdfReadError, ValueError):
        return metadata
    if document_info is None:
        return metadata

    fields = {
        "title": "display_title",
        "author": "creator",
        "subject": "subject",
        "creator": "creating_application",
        "producer": "pdf_producer",
    }
    for source, destination in fields.items():
        value = _embedded_text(getattr(document_info, source, None))
        if value:
            metadata[destination] = value
    keywords = _embedded_text(document_info.get("/Keywords"))
    if keywords:
        metadata["keywords"] = keywords

    date_candidates: list[tuple[datetime, str]] = []
    for source, destination in (
        ("creation_date", "document_created_at"),
        ("modification_date", "document_modified_at"),
    ):
        try:
            value = getattr(document_info, source, None)
        except (IndexError, TypeError, ValueError):
            continue
        parsed = _normalise_document_datetime(value)
        if parsed is None:
            continue
        metadata[destination] = parsed.isoformat()
        date_candidates.append((parsed, destination))
    if date_candidates:
        captured_at, source = min(date_candidates, key=lambda candidate: candidate[0])
        metadata["captured_at"] = captured_at.isoformat()
        metadata["capture_date_source"] = source
    return metadata


def _zip_entry_datetime(date_time: tuple[int, int, int, int, int, int]) -> str | None:
    try:
        return datetime(*date_time, tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _extract_zip_metadata(path: Path) -> dict[str, object]:
    if path.suffix.casefold() != ".zip":
        return {}

    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
    except (BadZipFile, LargeZipFile, OSError, ValueError):
        return {}

    files = [info for info in infos if not info.is_dir()]
    directories = len(infos) - len(files)
    entry_dates = [
        parsed
        for info in infos
        if (parsed := _zip_entry_datetime(info.date_time)) is not None
    ]
    compression_names = {
        0: "stored",
        8: "deflate",
        12: "bzip2",
        14: "lzma",
        93: "zstandard",
    }
    entries = [
        {
            "path": info.filename[:ARCHIVE_ENTRY_NAME_MAX_CHARS],
            "size_bytes": info.file_size,
            "compressed_bytes": info.compress_size,
            "compression": compression_names.get(
                info.compress_type,
                f"method-{info.compress_type}",
            ),
            "crc32": f"{info.CRC:08x}",
            "encrypted": bool(info.flag_bits & 0x1),
            **(
                {"modified_at": modified_at}
                if (modified_at := _zip_entry_datetime(info.date_time))
                else {}
            ),
        }
        for info in files[:ARCHIVE_ENTRY_LIMIT]
    ]
    metadata: dict[str, object] = {
        "archive_format": "zip",
        "archive_entry_count": len(infos),
        "archive_file_count": len(files),
        "archive_directory_count": directories,
        "archive_uncompressed_bytes": sum(info.file_size for info in files),
        "archive_compressed_bytes": sum(info.compress_size for info in files),
        "archive_encrypted_file_count": sum(
            bool(info.flag_bits & 0x1) for info in files
        ),
        "archive_entries": entries,
        "archive_entries_truncated": len(files) > ARCHIVE_ENTRY_LIMIT,
    }
    if entry_dates:
        metadata["archive_earliest_entry_at"] = min(entry_dates)
        metadata["archive_latest_entry_at"] = max(entry_dates)
    if archive.comment:
        comment = _embedded_text(archive.comment)
        if comment:
            metadata["archive_comment"] = comment
    return metadata


def _tar_format(path: Path) -> str | None:
    name = path.name.casefold()
    formats = {
        ".tar": "tar",
        ".tar.gz": "tar-gzip",
        ".tgz": "tar-gzip",
        ".tar.bz2": "tar-bzip2",
        ".tbz2": "tar-bzip2",
        ".tar.xz": "tar-xz",
        ".txz": "tar-xz",
    }
    return next(
        (archive_format for suffix, archive_format in formats.items() if name.endswith(suffix)),
        None,
    )


def _tar_entry_datetime(timestamp: int | float) -> str | None:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _extract_tar_metadata(path: Path) -> dict[str, object]:
    archive_format = _tar_format(path)
    if archive_format is None:
        return {}

    entry_count = 0
    file_count = 0
    directory_count = 0
    symbolic_link_count = 0
    hard_link_count = 0
    other_count = 0
    uncompressed_bytes = 0
    entry_dates: list[str] = []
    entries: list[dict[str, object]] = []
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                entry_count += 1
                if member.isfile():
                    entry_type = "file"
                    file_count += 1
                    uncompressed_bytes += member.size
                elif member.isdir():
                    entry_type = "directory"
                    directory_count += 1
                elif member.issym():
                    entry_type = "symbolic-link"
                    symbolic_link_count += 1
                elif member.islnk():
                    entry_type = "hard-link"
                    hard_link_count += 1
                else:
                    entry_type = "other"
                    other_count += 1

                modified_at = _tar_entry_datetime(member.mtime)
                if modified_at:
                    entry_dates.append(modified_at)
                if len(entries) >= ARCHIVE_ENTRY_LIMIT:
                    continue
                entry: dict[str, object] = {
                    "path": member.name[:ARCHIVE_ENTRY_NAME_MAX_CHARS],
                    "type": entry_type,
                    "size_bytes": member.size,
                    "mode": f"{member.mode:04o}",
                    "uid": member.uid,
                    "gid": member.gid,
                }
                owner = _embedded_text(member.uname)
                group = _embedded_text(member.gname)
                if owner:
                    entry["owner"] = owner
                if group:
                    entry["group"] = group
                if modified_at:
                    entry["modified_at"] = modified_at
                if member.issym() or member.islnk():
                    entry["link_target"] = member.linkname[
                        :ARCHIVE_ENTRY_NAME_MAX_CHARS
                    ]
                entries.append(entry)
    except (OSError, tarfile.TarError, ValueError):
        return {}

    metadata: dict[str, object] = {
        "archive_format": archive_format,
        "archive_entry_count": entry_count,
        "archive_file_count": file_count,
        "archive_directory_count": directory_count,
        "archive_symbolic_link_count": symbolic_link_count,
        "archive_hard_link_count": hard_link_count,
        "archive_other_entry_count": other_count,
        "archive_uncompressed_bytes": uncompressed_bytes,
        "archive_entries": entries,
        "archive_entries_truncated": entry_count > ARCHIVE_ENTRY_LIMIT,
    }
    if entry_dates:
        metadata["archive_earliest_entry_at"] = min(entry_dates)
        metadata["archive_latest_entry_at"] = max(entry_dates)
    return metadata


def _normalise_audio_date(value: object) -> str | None:
    text = _embedded_text(value)
    if not text:
        return None
    match = re.match(
        r"^\s*(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?",
        text,
    )
    if not match:
        return None
    year, month, day = (
        int(match.group(1)),
        int(match.group(2) or 1),
        int(match.group(3) or 1),
    )
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _extract_audio_metadata(path: Path) -> dict[str, object]:
    mime_type = mimetypes.guess_type(path.name)[0] or ""
    suffix = path.suffix.casefold()
    if not mime_type.startswith("audio/") and suffix not in {
        ".aac",
        ".aif",
        ".aiff",
        ".alac",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }:
        return {}

    try:
        audio = MutagenFile(path, easy=True)
    except (MutagenError, OSError, ValueError):
        return {}
    if audio is None:
        return {}

    metadata: dict[str, object] = {
        "audio_format": suffix.removeprefix(".") or "unknown",
        "audio_codec": type(audio).__name__,
    }
    info = getattr(audio, "info", None)
    technical_fields = {
        "length": ("duration_seconds", float),
        "bitrate": ("bitrate_bps", int),
        "sample_rate": ("sample_rate_hz", int),
        "channels": ("channel_count", int),
        "bits_per_sample": ("bits_per_sample", int),
    }
    for source, (destination, coercion) in technical_fields.items():
        value = getattr(info, source, None)
        try:
            if value is not None:
                converted = coercion(value)
                metadata[destination] = (
                    round(converted, 3)
                    if destination == "duration_seconds"
                    else converted
                )
        except (TypeError, ValueError, OverflowError):
            continue

    tags = getattr(audio, "tags", None) or {}
    tag_lookup = {
        str(name).casefold(): value
        for name, value in tags.items()
    }
    tag_mappings = {
        ("title",): "display_title",
        ("album", "wm/albumtitle"): "album",
        ("artist", "author"): "artist",
        ("albumartist", "wm/albumartist"): "album_artist",
        ("composer", "wm/composer"): "composer",
        ("genre", "wm/genre"): "genre",
        ("tracknumber", "wm/tracknumber"): "track_number",
        ("discnumber", "wm/partofset"): "disc_number",
        ("comment", "description", "wm/subtitle"): "description",
        ("copyright",): "copyright",
        ("organization", "publisher", "wm/publisher"): "publisher",
        ("isrc", "wm/isrc"): "isrc",
    }
    for sources, destination in tag_mappings.items():
        values = [
            text
            for source in sources
            for text in _audio_tag_texts(tag_lookup.get(source))
        ][:AUDIO_TAG_VALUE_LIMIT]
        if values:
            metadata[destination] = values[0] if len(values) == 1 else values

    date_values = [
        value
        for name in ("originaldate", "date", "year", "wm/year")
        for value in _audio_tag_texts(tag_lookup.get(name))
    ]
    normalised_dates = [
        parsed
        for value in date_values
        if (parsed := _normalise_audio_date(value))
    ]
    if normalised_dates:
        earliest_date = min(normalised_dates)
        metadata["audio_recorded_at"] = earliest_date
        metadata["captured_at"] = earliest_date
        metadata["release_year"] = earliest_date[:4]
        metadata["capture_date_source"] = "audio_tag"
    return metadata


def _probe_number(value: object, coercion: type[int] | type[float]) -> object | None:
    try:
        converted = coercion(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(converted, 3) if coercion is float else converted


def _probe_frame_rate(value: object) -> float | None:
    text = _embedded_text(value)
    if not text:
        return None
    numerator, separator, denominator = text.partition("/")
    try:
        result = (
            float(numerator) / float(denominator)
            if separator
            else float(numerator)
        )
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return round(result, 3)


def _extract_video_metadata(path: Path) -> dict[str, object]:
    mime_type = mimetypes.guess_type(path.name)[0] or ""
    suffix = path.suffix.casefold()
    if not mime_type.startswith("video/") and suffix not in {
        ".3gp",
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }:
        return {}

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        "--",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=VIDEO_PROBE_TIMEOUT_SECONDS,
        )
        if len(result.stdout) > VIDEO_PROBE_MAX_BYTES:
            return {}
        probe = json.loads(result.stdout)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ):
        return {}
    if not isinstance(probe, dict):
        return {}

    format_data = probe.get("format")
    if not isinstance(format_data, dict):
        format_data = {}
    streams_data = probe.get("streams")
    streams = (
        [stream for stream in streams_data if isinstance(stream, dict)]
        if isinstance(streams_data, list)
        else []
    )

    metadata: dict[str, object] = {
        "video_format": suffix.removeprefix(".") or "unknown",
        "container_format": _embedded_text(
            format_data.get("format_name")
        ) or "unknown",
        "stream_count": len(streams),
        "video_stream_count": sum(
            stream.get("codec_type") == "video" for stream in streams
        ),
        "audio_stream_count": sum(
            stream.get("codec_type") == "audio" for stream in streams
        ),
        "subtitle_stream_count": sum(
            stream.get("codec_type") == "subtitle" for stream in streams
        ),
    }
    for source, destination, coercion in (
        ("duration", "duration_seconds", float),
        ("bit_rate", "bitrate_bps", int),
        ("size", "container_size_bytes", int),
    ):
        value = _probe_number(format_data.get(source), coercion)
        if value is not None:
            metadata[destination] = value

    retained_streams: list[dict[str, object]] = []
    creation_dates: list[datetime] = []
    format_tags = format_data.get("tags")
    if isinstance(format_tags, dict):
        created = _embedded_text(
            format_tags.get("creation_time")
            or format_tags.get("date")
        )
        if created and (parsed := _parse_embedded_datetime(created)):
            creation_dates.append(parsed)

    for stream in streams[:VIDEO_PROBE_STREAM_LIMIT]:
        retained: dict[str, object] = {}
        for source, destination in (
            ("index", "index"),
            ("codec_type", "type"),
            ("codec_name", "codec"),
            ("codec_long_name", "codec_description"),
            ("profile", "profile"),
            ("pix_fmt", "pixel_format"),
            ("color_space", "colour_space"),
            ("color_transfer", "colour_transfer"),
            ("color_primaries", "colour_primaries"),
            ("field_order", "field_order"),
        ):
            value = stream.get(source)
            text = _embedded_text(value)
            if text:
                retained[destination] = text
            elif source == "index" and isinstance(value, int):
                retained[destination] = value
        for source, destination in (
            ("width", "width"),
            ("height", "height"),
            ("sample_rate", "sample_rate_hz"),
            ("channels", "channel_count"),
            ("bit_rate", "bitrate_bps"),
            ("bits_per_sample", "bits_per_sample"),
        ):
            value = _probe_number(stream.get(source), int)
            if value is not None:
                retained[destination] = value
        frame_rate = _probe_frame_rate(
            stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        )
        if frame_rate is not None:
            retained["frame_rate_fps"] = frame_rate
        stream_tags = stream.get("tags")
        if isinstance(stream_tags, dict):
            for source, destination in (
                ("language", "language"),
                ("title", "title"),
                ("handler_name", "handler"),
            ):
                value = _embedded_text(stream_tags.get(source))
                if value:
                    retained[destination] = value
            created = _embedded_text(stream_tags.get("creation_time"))
            if created and (parsed := _parse_embedded_datetime(created)):
                creation_dates.append(parsed)
        disposition = stream.get("disposition")
        if isinstance(disposition, dict):
            flags = sorted(
                str(name)
                for name, enabled in disposition.items()
                if enabled in {1, True}
            )
            if flags:
                retained["disposition"] = flags
        retained_streams.append(retained)

    metadata["streams"] = retained_streams
    metadata["streams_truncated"] = len(streams) > VIDEO_PROBE_STREAM_LIMIT
    primary_video = next(
        (
            stream
            for stream in retained_streams
            if stream.get("type") == "video"
        ),
        None,
    )
    if primary_video:
        for source, destination in (
            ("codec", "video_codec"),
            ("width", "width"),
            ("height", "height"),
            ("frame_rate_fps", "frame_rate_fps"),
            ("pixel_format", "pixel_format"),
        ):
            if source in primary_video:
                metadata[destination] = primary_video[source]
    if creation_dates:
        captured_at = min(creation_dates).isoformat()
        metadata["video_created_at"] = captured_at
        metadata["captured_at"] = captured_at
        metadata["capture_date_source"] = "video_container"
    return metadata


def extract_basic_metadata(path: Path) -> dict[str, object]:
    document_metadata = _extract_ooxml_metadata(path)
    if document_metadata:
        return document_metadata

    document_metadata = _extract_pdf_metadata(path)
    if document_metadata:
        return document_metadata

    archive_metadata = _extract_zip_metadata(path)
    if archive_metadata:
        return archive_metadata

    archive_metadata = _extract_tar_metadata(path)
    if archive_metadata:
        return archive_metadata

    audio_metadata = _extract_audio_metadata(path)
    if audio_metadata:
        return audio_metadata

    video_metadata = _extract_video_metadata(path)
    if video_metadata:
        return video_metadata

    mime_type = mimetypes.guess_type(path.name)[0] or ""
    if not mime_type.startswith("image/"):
        return {}

    try:
        with Image.open(path) as image:
            metadata: dict[str, object] = {
                "width": image.width,
                "height": image.height,
                "format": image.format or "",
            }
            exif = image.getexif()
            try:
                xmp_metadata = _extract_xmp_metadata(image.getxmp())
            except (AttributeError, OSError, TypeError, ValueError):
                xmp_metadata = {}
            try:
                iptc_metadata = _extract_iptc_metadata(
                    IptcImagePlugin.getiptcinfo(image)
                )
            except (OSError, SyntaxError, TypeError, ValueError):
                iptc_metadata = {}
            for source_metadata in (xmp_metadata, iptc_metadata):
                for name, value in source_metadata.items():
                    metadata.setdefault(name, value)
            tags = {
                271: "camera_make",
                272: "camera_model",
                305: "software",
            }
            for tag_id, name in tags.items():
                value = _embedded_text(exif.get(tag_id))
                if value and name not in metadata:
                    metadata[name] = value
            metadata.update(_extract_exif_technical(exif))
            metadata.update(_extract_exif_gps(exif))
            captured_on, date_source = select_oldest_photo_date(
                path.name,
                datetime.fromtimestamp(
                    path.stat().st_mtime,
                    tz=timezone.utc,
                ),
                (
                    value
                    for value in (
                        exif.get(36867),
                        exif.get(36868),
                        exif.get(306),
                        metadata.get("xmp_created_at"),
                        metadata.get("iptc_created_at"),
                    )
                    if value
                ),
            )
            metadata["captured_at"] = captured_on.isoformat()
            metadata["capture_date_source"] = date_source
            return metadata
    except (OSError, UnidentifiedImageError, ValueError):
        return {}


def create_deterministic_proposal(
    scanned_file: ScannedFile,
) -> tuple[str, str, str, str]:
    suffix = Path(scanned_file.filename).suffix.casefold()
    mime_group = scanned_file.mime_type.partition("/")[0]

    if suffix in {
        ".pdf",
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".txt",
        ".xls",
        ".xlsx",
        ".ods",
        ".csv",
        ".ppt",
        ".pptx",
    }:
        category = "Documents"
        confidence = "medium"
        reason = "The file extension identifies a document-focused file."
    elif suffix in {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2"}:
        category = "Archives"
        confidence = "medium"
        reason = "The file extension identifies a packaged archive."
    elif mime_group == "image":
        if has_hard_coded_screenshot_marker(
            scanned_file.filename,
            scanned_file.metadata,
        ):
            category = "Archives"
            confidence = "low"
            reason = (
                "Screenshot capture context is known; semantic analysis will determine its content."
            )
        else:
            software = str(scanned_file.metadata.get("software", "")).casefold()
            scanner_signals = ("scan", "acrobat", "document")
            camera = scanned_file.metadata.get(
                "camera_model"
            ) or scanned_file.metadata.get("camera_make")
            if any(signal in software for signal in scanner_signals):
                category = "Documents"
                confidence = "medium"
                reason = (
                    "Embedded image software metadata indicates a scanned "
                    "document workflow."
                )
            elif camera:
                category = "Gallery"
                confidence = "medium"
                reason = (
                    "Embedded camera metadata indicates a photograph."
                )
            else:
                category = "Gallery"
                confidence = "low"
                reason = (
                    "The MIME type identifies an image, but no camera or scanner "
                    "metadata resolves whether it is a photograph or document."
                )
    elif mime_group == "audio":
        category = "Music"
        confidence = "medium" if scanned_file.metadata.get("artist") else "low"
        reason = (
            "Embedded audio tags identify a music track."
            if scanned_file.metadata.get("artist")
            else "The MIME type identifies audio; its identity requires review."
        )
    elif mime_group == "video":
        category = "Home Videos"
        confidence = "low"
        reason = (
            "The MIME type identifies video, but it cannot yet distinguish "
            "personal recordings from Theatre content."
        )
    else:
        category = "Archives"
        confidence = "low"
        reason = (
            "No specific deterministic rule matched; Archives is suggested "
            "for manual review."
        )

    destination = proposed_destination_path(
        category,
        scanned_file.relative_path,
        scanned_file.filename,
    )
    return category, destination, reason, confidence


def effective_asset_metadata(
    item: ImportItem,
) -> tuple[str, date | None, str | None, dict[str, str]]:
    overrides = item.metadata_overrides
    embedded_title = item.metadata.get("display_title")
    detected_title = (
        str(embedded_title)
        if embedded_title
        else Path(item.filename).stem.replace("_", " ")
    )
    display_title = overrides.get("display_title", detected_title)
    captured_value = overrides.get("captured_on")
    captured_source = "user_override"
    if not captured_value:
        detected_capture = item.metadata.get("captured_at")
        captured_value = (
            str(detected_capture) if detected_capture is not None else None
        )
        captured_source = str(
            item.metadata.get("capture_date_source", "detected")
        )
    try:
        captured_on = (
            date.fromisoformat(captured_value) if captured_value else None
        )
    except ValueError:
        captured_on = None
        captured_source = "unavailable"

    detected_location = item.metadata.get("location")
    location_value = overrides.get("location", detected_location)
    location = str(location_value) if location_value else None
    provenance = {
        "display_title": (
            "user_override"
            if "display_title" in overrides
            else ("embedded" if embedded_title else "filename")
        ),
        "captured_on": captured_source,
        "location": (
            "user_override"
            if "location" in overrides
            else ("embedded" if detected_location else "unavailable")
        ),
    }
    return display_title, captured_on, location, provenance


def canonical_asset_metadata_layers(
    item: ImportItem,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    display_title, captured_on, location, _ = effective_asset_metadata(item)
    detected = normalise_typed_metadata(item.metadata)
    detected.setdefault(
        "display_title",
        Path(item.filename).stem.replace("_", " "),
    )
    detected.setdefault(
        "captured_on",
        (
            str(item.metadata["captured_at"])[:10]
            if item.metadata.get("captured_at")
            else None
        ),
    )
    detected.setdefault("location", item.metadata.get("location"))
    imported: dict[str, object] = {}
    overrides: dict[str, object] = normalise_typed_metadata(item.metadata_overrides)
    effective = {
        **detected,
        **imported,
        **overrides,
        "display_title": display_title,
        "captured_on": captured_on.isoformat() if captured_on else None,
        "location": location,
    }
    return detected, imported, overrides, effective


NUMERIC_METADATA_FIELDS = {
    "track_number",
    "track_total",
    "disc_number",
    "disc_total",
    "release_year",
    "page_count",
    "quantity",
    "amount_minor_units",
}

MEASUREMENT_NUMERIC_FIELDS = {
    "measurement_value",
    "distance_m",
    "mass_kg",
    "volume_l",
    "temperature_celsius",
}


def _number_and_total(value: object) -> tuple[int | None, int | None]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value, None
    if isinstance(value, float) and value.is_integer():
        return int(value), None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)(?:\s*/\s*(\d+))?\s*", value)
        if match:
            return int(match.group(1)), int(match.group(2)) if match.group(2) else None
    return None, None


def normalise_typed_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Keep quantities numeric and dates ISO-8601 inside canonical storage."""
    normalised = dict(metadata)
    for field, total_field in (("track_number", "track_total"), ("disc_number", "disc_total")):
        if field not in normalised:
            continue
        number, total = _number_and_total(normalised[field])
        if number is not None:
            normalised[field] = number
        if total is not None:
            normalised[total_field] = total
    for field in NUMERIC_METADATA_FIELDS - {"track_number", "disc_number"}:
        value = normalised.get(field)
        if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
            normalised[field] = int(value)
    for field in MEASUREMENT_NUMERIC_FIELDS:
        value = normalised.get(field)
        if isinstance(value, str) and re.fullmatch(
            r"-?\d+(?:[.,]\d+)?", value.strip()
        ):
            normalised[field] = float(value.strip().replace(",", "."))
    return normalised


def portable_ingestion_evidence(row: dict[str, object] | None) -> dict[str, object] | None:
    """Return durable, typed AI evidence suitable for a permanent asset sidecar."""
    if not row:
        return None
    created_at = row.get("created_at")
    return {
        "schema": "personal-vault.ingestion-evidence",
        "version": 1,
        "content_type": row.get("content_type"),
        "caption": row.get("caption") or "",
        "ocr_text": row.get("ocr_text") or "",
        "confidence": float(row.get("confidence") or 0),
        "reasons": list(row.get("reasons") or []),
        "model_id": row.get("model_id"),
        "model_revision": row.get("model_revision"),
        "task_version": row.get("task_version"),
        "processing_ms": int(row.get("processing_ms") or 0),
        "recommended_destination": row.get("recommended_destination"),
        "decision_score": int(row.get("decision_score") or 0),
        "routing_band": row.get("routing_band"),
        "confidence_components": dict(row.get("confidence_components") or {}),
        "conflicts": list(row.get("conflicts") or []),
        "automatic_disqualifiers": list(row.get("automatic_disqualifiers") or []),
        "decision_model_version": row.get("decision_model_version"),
        "analysed_at": (
            created_at.isoformat() if hasattr(created_at, "isoformat") else created_at
        ),
    }


def refresh_catalogued_asset_detection(
    asset: CataloguedAsset,
    item: ImportItem,
) -> CataloguedAsset:
    detected, _, _, _ = canonical_asset_metadata_layers(item)
    if "ingestion_evidence" in asset.detected_metadata:
        detected["ingestion_evidence"] = asset.detected_metadata[
            "ingestion_evidence"
        ]
    imported = dict(asset.imported_metadata)
    overrides = dict(asset.user_overrides)

    # Preserve corrections made before user_overrides became a canonical
    # layer. Current records already carry these values explicitly.
    for field, value in (
        ("display_title", asset.display_title),
        (
            "captured_on",
            asset.captured_on.isoformat() if asset.captured_on else None,
        ),
        ("location", asset.location),
    ):
        if (
            asset.metadata_provenance.get(field) == "user_override"
            and field not in overrides
            and value
        ):
            overrides[field] = value

    display_title = str(
        overrides.get(
            "display_title",
            imported.get("display_title", detected["display_title"]),
        )
    )
    captured_value = overrides.get(
        "captured_on",
        imported.get("captured_on", detected.get("captured_on")),
    )
    try:
        captured_on = (
            date.fromisoformat(str(captured_value)[:10])
            if captured_value
            else None
        )
    except ValueError:
        captured_on = None
    location_value = overrides.get(
        "location",
        imported.get("location", detected.get("location")),
    )
    location = str(location_value) if location_value else None

    provenance: dict[str, str] = {}
    detected_sources = {
        "display_title": (
            "embedded" if item.metadata.get("display_title") else "filename"
        ),
        "captured_on": str(
            item.metadata.get("capture_date_source", "unavailable")
        ),
        "location": (
            "embedded" if item.metadata.get("location") else "unavailable"
        ),
    }
    for field in ("display_title", "captured_on", "location"):
        if field in overrides:
            provenance[field] = "user_override"
        elif imported.get(field):
            existing_source = asset.metadata_provenance.get(field, "")
            provenance[field] = (
                existing_source
                if existing_source.startswith("import:")
                else "imported"
            )
        else:
            provenance[field] = detected_sources[field]
    if "ingestion_evidence" in detected:
        provenance["ingestion_evidence"] = asset.metadata_provenance.get(
            "ingestion_evidence", "local_ai_reviewed"
        )

    effective = {
        **detected,
        **imported,
        **overrides,
        "display_title": display_title,
        "captured_on": captured_on.isoformat() if captured_on else None,
        "location": location,
    }
    return CataloguedAsset(
        **{
            **asset.__dict__,
            "display_title": display_title,
            "captured_on": captured_on,
            "location": location,
            "filename": item.filename,
            "size_bytes": item.size_bytes,
            "mime_type": item.mime_type,
            "sha256": item.sha256,
            "metadata": detected,
            "metadata_provenance": provenance,
            "detected_metadata": detected,
            "user_overrides": overrides,
            "effective_metadata": effective,
        }
    )


def _catalogued_asset_from_row(row: dict[str, object]) -> CataloguedAsset:
    return CataloguedAsset(
        id=UUID(str(row["id"])),
        asset_type=str(row["asset_type"]),
        display_title=str(row["display_title"]),
        captured_on=row["captured_on"],  # type: ignore[arg-type]
        location=(
            str(row["location"]) if row["location"] is not None else None
        ),
        vault_path=str(row["vault_path"]),
        filename=str(row["filename"]),
        size_bytes=int(str(row["size_bytes"])),
        mime_type=str(row["mime_type"]),
        sha256=str(row["sha256"]),
        metadata=dict(row["metadata"] or {}),  # type: ignore[arg-type]
        metadata_provenance={
            str(name): str(value)
            for name, value in dict(
                row["metadata_provenance"] or {}  # type: ignore[arg-type]
            ).items()
        },
        detected_metadata=dict(
            row.get("detected_metadata") or {}  # type: ignore[arg-type]
        ),
        imported_metadata=dict(
            row.get("imported_metadata") or {}  # type: ignore[arg-type]
        ),
        user_overrides=dict(
            row.get("user_overrides") or {}  # type: ignore[arg-type]
        ),
        effective_metadata=dict(
            row.get("effective_metadata") or {}  # type: ignore[arg-type]
        ),
        owner_username=str(row.get("owner_username") or "owner"),
        owner_user_id=(
            UUID(str(row["owner_user_id"]))
            if row.get("owner_user_id")
            else None
        ),
        origin_vault_id=(
            UUID(str(row["origin_vault_id"]))
            if row.get("origin_vault_id")
            else None
        ),
        visibility=str(
            row.get("visibility") or PRIVATE_ASSET_VISIBILITY
        ),
        shared_with=tuple(
            str(username)
            for username in (row.get("shared_with") or [])  # type: ignore[union-attr]
        ),
        lifecycle_state=str(row.get("lifecycle_state") or "active"),
    )


def apply_catalogue_metadata_changes(
    asset: CataloguedAsset,
    changes: dict[str, str | None],
) -> CataloguedAsset:
    changes = dict(changes)
    capture_timestamp = changes.get("captured_at")
    if capture_timestamp:
        try:
            capture_date = datetime.fromisoformat(capture_timestamp).date()
        except ValueError as error:
            raise ValueError("Capture timestamp must be ISO-8601") from error
        explicit_date = changes.get("captured_on")
        if explicit_date is not None and date.fromisoformat(explicit_date) != capture_date:
            raise ValueError("Capture date must match the capture timestamp")
        changes.setdefault("captured_on", capture_date.isoformat())
    values: dict[str, object] = {
        "display_title": asset.display_title,
        "captured_on": asset.captured_on,
        "location": asset.location,
    }
    provenance = dict(asset.metadata_provenance)
    user_overrides = dict(asset.user_overrides)
    detected_title = Path(asset.filename).stem.replace("_", " ")
    detected_capture = asset.metadata.get("captured_at")
    try:
        detected_date = (
            date.fromisoformat(str(detected_capture)[:10])
            if detected_capture
            else None
        )
    except ValueError:
        detected_date = None
    detected_location = asset.metadata.get("location")

    for field, value in changes.items():
        if value:
            user_overrides[field] = value
        else:
            user_overrides.pop(field, None)
        if field == "display_title":
            values[field] = value or detected_title
            provenance[field] = "user_override" if value else "filename"
        elif field == "captured_on":
            values[field] = date.fromisoformat(value) if value else detected_date
            provenance[field] = (
                "user_override"
                if value
                else str(
                    asset.metadata.get(
                        "capture_date_source",
                        "unavailable",
                    )
                )
            )
        elif field == "location":
            values[field] = value or (
                str(detected_location) if detected_location else None
            )
            provenance[field] = (
                "user_override"
                if value
                else ("embedded" if detected_location else "unavailable")
            )
        else:
            fallback = asset.imported_metadata.get(
                field,
                asset.detected_metadata.get(field),
            )
            provenance[field] = (
                "user_override"
                if value
                else (
                    "imported"
                    if field in asset.imported_metadata
                    else ("detected" if fallback is not None else "unavailable")
                )
            )

    effective_metadata = {
        **asset.effective_metadata,
        **asset.detected_metadata,
        **asset.imported_metadata,
        **user_overrides,
        "display_title": values["display_title"],
        "captured_on": (
            values["captured_on"].isoformat()
            if isinstance(values["captured_on"], date)
            else None
        ),
        "location": values["location"],
    }
    return CataloguedAsset(
        **{
            **asset.__dict__,
            **values,
            "metadata_provenance": provenance,
            "user_overrides": user_overrides,
            "effective_metadata": effective_metadata,
        }
    )


def catalogue_metadata_field_value(
    asset: CataloguedAsset,
    field: str,
) -> object:
    return getattr(asset, field, asset.effective_metadata.get(field))


def apply_catalogue_access_changes(
    asset: CataloguedAsset,
    visibility: str,
    shared_with: tuple[str, ...],
    *,
    shared_with_user_ids: tuple[UUID, ...] = (),
    require_shared_user_ids: bool = False,
    local_all: bool = False,
) -> CataloguedAsset:
    """Apply an owner-approved access policy to a canonical asset."""
    if visibility not in {
        PRIVATE_ASSET_VISIBILITY,
        SHARED_ASSET_VISIBILITY,
        VAULT_WIDE_ASSET_VISIBILITY,
    }:
        raise ValueError("Unsupported Vault asset visibility")
    if visibility == PRIVATE_ASSET_VISIBILITY:
        shared_with = ()
        shared_with_user_ids = ()
    elif visibility == VAULT_WIDE_ASSET_VISIBILITY:
        shared_with = ()
        shared_with_user_ids = ()
    else:
        if not shared_with and not local_all:
            raise ValueError("Shared Vault assets require at least one user")
        if require_shared_user_ids and len(shared_with) != len(shared_with_user_ids):
            raise ValueError("Shared Vault users require immutable identities")
        if any(not username.strip() for username in shared_with):
            raise ValueError("Shared Vault users cannot be blank")
        if len(set(shared_with)) != len(shared_with):
            raise ValueError("Shared Vault users must be unique")
        if len(set(shared_with_user_ids)) != len(shared_with_user_ids):
            raise ValueError("Shared Vault user identities must be unique")
        if asset.owner_username in shared_with:
            raise ValueError("The owner cannot be added as a shared user")
        if asset.owner_user_id in shared_with_user_ids:
            raise ValueError("The owner cannot be added as a shared user")
    return CataloguedAsset(
        **{
            **asset.__dict__,
            "visibility": visibility,
            "shared_with": shared_with,
            "shared_with_user_ids": shared_with_user_ids,
        }
    )


def apply_imported_asset_metadata(
    asset: CataloguedAsset,
    metadata: dict[str, object],
    source: str,
) -> CataloguedAsset:
    imported_metadata = normalise_typed_metadata(
        {**asset.imported_metadata, **metadata}
    )
    provenance = dict(asset.metadata_provenance)

    detected_title = asset.detected_metadata.get(
        "display_title",
        Path(asset.filename).stem.replace("_", " "),
    )
    detected_capture = asset.detected_metadata.get("captured_on")
    detected_location = asset.detected_metadata.get("location")

    display_title = str(
        asset.user_overrides.get(
            "display_title",
            imported_metadata.get("display_title", detected_title),
        )
    )
    captured_value = asset.user_overrides.get(
        "captured_on",
        imported_metadata.get("captured_on", detected_capture),
    )
    try:
        captured_on = (
            date.fromisoformat(str(captured_value)[:10])
            if captured_value
            else None
        )
    except ValueError:
        captured_on = None
    location_value = asset.user_overrides.get(
        "location",
        imported_metadata.get("location", detected_location),
    )
    location = str(location_value) if location_value else None

    for field, imported_value, detected_value in (
        (
            "display_title",
            imported_metadata.get("display_title"),
            detected_title,
        ),
        ("captured_on", imported_metadata.get("captured_on"), detected_capture),
        ("location", imported_metadata.get("location"), detected_location),
    ):
        if field in asset.user_overrides:
            provenance[field] = "user_override"
        elif imported_value:
            provenance[field] = f"import:{source}"
        elif detected_value:
            provenance[field] = str(
                asset.metadata_provenance.get(field, "detected")
            )
        else:
            provenance[field] = "unavailable"

    effective_metadata = {
        **asset.detected_metadata,
        **imported_metadata,
        **asset.user_overrides,
        "display_title": display_title,
        "captured_on": captured_on.isoformat() if captured_on else None,
        "location": location,
    }
    storage_placement = asset.effective_metadata.get("storage_placement")
    if isinstance(storage_placement, dict):
        effective_metadata["storage_placement"] = dict(storage_placement)
    return CataloguedAsset(
        **{
            **asset.__dict__,
            "display_title": display_title,
            "captured_on": captured_on,
            "location": location,
            "metadata_provenance": provenance,
            "imported_metadata": imported_metadata,
            "effective_metadata": effective_metadata,
        }
    )


def inventory_catalogue_location(
    source_path: str,
) -> tuple[str, str] | None:
    source = Path(source_path).resolve(strict=False)
    library_root = Path(
        os.getenv("PV_LIBRARY_PATH", "/media/library")
    ).resolve(strict=False)
    try:
        library_relative = source.relative_to(library_root)
    except ValueError:
        library_relative = None
    if library_relative is not None:
        parts = library_relative.parts
        if (
            len(parts) == 4
            and parts[2] == "source"
            and source.suffix.casefold() == ".pdf"
            and (library_root / parts[0] / parts[1] / ".publication-ready").is_file()
        ):
            return "Library", str(
                PurePosixPath("/vault/Library")
                / PurePosixPath(library_relative.as_posix())
            )
        return None
    configured_roots = (
        (
            "Movies",
            Path(os.getenv("PV_MOVIES_PATH", "/media/movies")),
            PurePosixPath("/vault/Theatre/Movies"),
        ),
        (
            "Gallery",
            Path(os.getenv("PV_GALLERY_PATH", "/media/gallery")),
            PurePosixPath("/vault/Gallery"),
        ),
        (
            "Home Videos",
            Path(
                os.getenv(
                    "PV_PERSONAL_VIDEOS_PATH",
                    "/media/personal-videos",
                )
            ),
            PurePosixPath("/vault/Home Videos"),
        ),
        (
            "Documents",
            Path(os.getenv("PV_DOCUMENTS_PATH", "/media/documents")),
            PurePosixPath("/vault/Documents"),
        ),
        (
            "Archives",
            Path(os.getenv("PV_ARCHIVES_PATH", "/media/archives")),
            PurePosixPath("/vault/Archives"),
        ),
        (
            "Music",
            Path(os.getenv("PV_MUSIC_PATH", "/media/music")),
            PurePosixPath("/vault/Music"),
        ),
    )
    for asset_type, configured_root, logical_root in configured_roots:
        try:
            relative_path = source.relative_to(
                configured_root.resolve(strict=False)
            )
        except ValueError:
            continue
        return asset_type, str(
            logical_root / PurePosixPath(relative_path.as_posix())
        )
    return None


def scan_root(
    store: VaultMasterStore,
    root: Path,
    source_kind: str,
    playback_publisher: Callable[[tuple[Path, ...]], object] | None = None,
    owner_lookup: Callable[[Path], str | UUID | None] | None = None,
    source_context_lookup: Callable[[Path], dict[str, object] | None] | None = None,
) -> UUID:
    resolved_root = root.resolve(strict=True)

    if not resolved_root.is_dir():
        raise ValueError("Vault Master scan root is not a directory")

    batch_id = store.create_batch(source_kind, str(resolved_root))
    item_count = 0
    inventory_paths: list[Path] = []

    try:
        for path in sorted(resolved_root.rglob("*")):
            if (
                path.name.startswith(".pv-")
                or any(part.startswith(".pv-") for part in path.relative_to(resolved_root).parts)
                or path.is_symlink()
                or not path.is_file()
            ):
                continue

            owner = (
                owner_lookup(path)
                if source_kind == INCOMING_SOURCE and owner_lookup
                else None
            )
            scanned = scan_file(
                path,
                resolved_root,
                owner_username=owner if isinstance(owner, str) else None,
                owner_user_id=owner if isinstance(owner, UUID) else None,
            )
            if source_kind == INCOMING_SOURCE and source_context_lookup:
                source_context = source_context_lookup(path)
                if source_context is not None:
                    scanned = ScannedFile(**{**scanned.__dict__, "metadata": {**scanned.metadata, "source_context": source_context}})
            store.record_file(
                batch_id,
                source_kind,
                scanned,
            )
            if source_kind == INVENTORY_SOURCE:
                inventory_paths.append(path)
            item_count += 1
        _publish_playback_updates(playback_publisher, tuple(inventory_paths))
    except Exception as error:
        store.fail_batch(batch_id, str(error))
        raise

    store.complete_batch(batch_id, item_count)
    return batch_id


def enqueue_root(
    store: VaultMasterStore,
    root: Path,
    source_kind: str,
) -> UUID:
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("Vault Master scan root is not a directory")
    return store.create_batch(source_kind, str(resolved_root))


def enqueue_catalogue_backfill(
    store: VaultMasterStore,
    roots: tuple[Path, ...],
) -> tuple[list[UUID], int]:
    batch_ids: list[UUID] = []
    reused_count = 0
    for root in roots:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("Vault Master scan root is not a directory")
        source_root = str(resolved_root)
        existing_batch_id = store.find_active_batch(
            INVENTORY_SOURCE,
            source_root,
        )
        if existing_batch_id:
            batch_ids.append(existing_batch_id)
            reused_count += 1
            continue
        batch_id = store.create_batch(INVENTORY_SOURCE, source_root)
        batch_ids.append(batch_id)

    return batch_ids, reused_count


def _publish_playback_updates(
    playback_publisher: Callable[[tuple[Path, ...]], object] | None,
    paths: tuple[Path, ...],
) -> None:
    if playback_publisher is None or not paths:
        return
    try:
        playback_publisher(paths)
    except Exception:
        logger.exception(
            "Playback library updates could not be published for %s; "
            "the Vault operation remains complete",
            paths,
        )


def process_next_batch(
    store: VaultMasterStore,
    playback_publisher: Callable[[tuple[Path, ...]], object] | None = None,
    owner_lookup: Callable[[Path], str | UUID | None] | None = None,
    source_context_lookup: Callable[[Path], dict[str, object] | None] | None = None,
) -> UUID | None:
    claimed = store.claim_next_batch()
    if claimed is None:
        return None

    batch_id, source_kind, source_root = claimed
    item_count = 0
    root = Path(source_root)
    inventory_paths: list[Path] = []

    try:
        for path in sorted(root.rglob("*")):
            if (
                path.name.startswith(".pv-")
                or any(part.startswith(".pv-") for part in path.relative_to(root).parts)
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            owner = (
                owner_lookup(path)
                if source_kind == INCOMING_SOURCE and owner_lookup
                else None
            )
            scanned = scan_file(
                path,
                root,
                owner_username=owner if isinstance(owner, str) else None,
                owner_user_id=owner if isinstance(owner, UUID) else None,
            )
            if source_kind == INCOMING_SOURCE and source_context_lookup:
                source_context = source_context_lookup(path)
                if source_context is not None:
                    scanned = ScannedFile(**{**scanned.__dict__, "metadata": {**scanned.metadata, "source_context": source_context}})
            store.record_file(
                batch_id,
                source_kind,
                scanned,
            )
            if source_kind == INVENTORY_SOURCE:
                inventory_paths.append(path)
            item_count += 1
        _publish_playback_updates(playback_publisher, tuple(inventory_paths))
    except Exception as error:
        store.fail_batch(batch_id, str(error))
        return batch_id

    store.complete_batch(batch_id, item_count)
    return batch_id


def process_next_move(
    store: VaultMasterStore,
    incoming_root: Path,
    destination_roots: dict[str, Path],
    playback_publisher: Callable[[tuple[Path, ...]], object] | None = None,
    theatre_queue: Callable[[ImportItem], None] | None = None,
) -> UUID | None:
    item = store.claim_next_move()
    if item is None:
        return None
    if item.proposed_category in {"Movies", "TV Shows"}:
        if theatre_queue is None:
            store.record_move_result(item.id, "move_failed", "Vault Master worker", "Theatre publisher is unavailable")
            return item.id
        try:
            theatre_queue(item)
            store.mark_theatre_promotion_pending(item.id)
        except (OSError, ValueError) as error:
            store.record_move_result(item.id, "move_failed", "Vault Master worker", str(error))
        return item.id
    destination_root = destination_roots.get(item.proposed_category or "")
    if destination_root is None:
        store.record_move_result(
            item.id,
            "move_failed",
            "Vault Master worker",
            "The approved destination is not available",
        )
        return item.id

    try:
        destination = safely_move_approved_file(
            ImportItem(**{**item.__dict__, "state": "approved"}),
            incoming_root,
            destination_root,
        )
    except (FileExistsError, OSError, ValueError) as error:
        store.record_move_result(
            item.id,
            "move_failed",
            "Vault Master worker",
            str(error),
        )
        return item.id

    store.record_move_result(
        item.id,
        "moved",
        "Vault Master worker",
        f"Moved to {destination}",
    )
    inventory_batch = store.create_batch(
        INVENTORY_SOURCE,
        str(destination_root.resolve(strict=True)),
    )
    try:
        store.record_file(
            inventory_batch,
            INVENTORY_SOURCE,
            scan_file(
                destination,
                destination_root,
                owner_username=item.owner_username,
                owner_user_id=item.owner_user_id,
            ),
        )
        store.complete_batch(inventory_batch, 1)
        _publish_playback_updates(playback_publisher, (destination,))
    except (OSError, ValueError) as error:
        store.fail_batch(inventory_batch, str(error))
    return item.id


def get_inventory_paths() -> tuple[Path, ...]:
    configured = os.getenv(
        "PV_VAULT_MASTER_INVENTORY_PATHS",
        (
            "/media/movies,/media/gallery,/media/personal-videos,"
            "/media/documents,/media/archives,/media/music,/media/tv"
            ",/media/library"
        ),
    )
    return tuple(
        Path(value.strip())
        for value in configured.split(",")
        if value.strip()
    )


class MemoryVaultMasterStore:
    def __init__(
        self,
        sidecar_root: Path | None = None,
        default_asset_owner: str = "owner",
    ) -> None:
        self.batches: dict[UUID, dict[str, object]] = {}
        self.items: dict[str, ImportItem] = {}
        self.moved_destinations: dict[str, UUID] = {}
        self.catalogued_assets: dict[str, CataloguedAsset] = {}
        self.catalogued_file_ids: dict[UUID, UUID] = {}
        self.arrival_managed_publications: dict[UUID, dict[str, object]] = {}
        self.asset_history: list[dict[str, object]] = []
        self.asset_relationships: dict[
            tuple[UUID, UUID], CataloguedAssetRelationship
        ] = {}
        self.deleted_assets: dict[UUID, dict[str, object]] = {}
        self.activity: list[VaultMasterActivity] = []
        self._sidecar_root = sidecar_root
        self._default_asset_owner = default_asset_owner
        self._local_vault_id = uuid4()

    def get_local_vault_id(self) -> UUID:
        return self._local_vault_id

    def _export_sidecar(self, asset: CataloguedAsset) -> bool:
        if self._sidecar_root is None:
            return False
        try:
            write_canonical_sidecar(asset, self._sidecar_root)
        except (OSError, TypeError, ValueError) as error:
            logger.exception(
                "Vault Master could not export sidecar for asset %s",
                asset.id,
            )
            self.activity.append(
                VaultMasterActivity(
                    id=uuid4(),
                    batch_id=None,
                    item_id=None,
                    source_kind=INVENTORY_SOURCE,
                    filename=asset.filename,
                    action="sidecar_export_failed",
                    username=None,
                    detail=f"Asset {asset.id}: {error}",
                    succeeded=False,
                    created_at=datetime.now(timezone.utc),
                )
            )
            return False
        return True

    def reconcile_sidecars(self) -> SidecarReconciliation:
        if self._sidecar_root is None:
            return SidecarReconciliation(0, 0, 0, 0)
        assets = sorted(
            self.catalogued_assets.values(),
            key=lambda asset: str(asset.id),
        )
        current = repaired = failed = 0
        for asset in assets:
            if canonical_sidecar_is_current(asset, self._sidecar_root):
                current += 1
            elif self._export_sidecar(asset):
                repaired += 1
            else:
                failed += 1
        if repaired or failed:
            self._record_activity(
                "sidecars_reconciled",
                detail=(
                    f"{len(assets)} checked; {repaired} repaired; "
                    f"{failed} failed"
                ),
                succeeded=failed == 0,
            )
        return SidecarReconciliation(
            checked=len(assets),
            current=current,
            repaired=repaired,
            failed=failed,
        )

    def restore_catalogued_asset(
        self,
        asset: CataloguedAsset,
        username: str,
    ) -> CataloguedAsset:
        if self.get_catalogued_asset_by_id(asset.id) is not None:
            raise ValueError("The asset already exists in the catalogue")
        if self.get_catalogued_asset(asset.vault_path) is not None:
            raise ValueError("The Vault path is already assigned")
        self.catalogued_assets[asset.vault_path] = asset
        self.catalogued_file_ids[asset.id] = uuid4()
        self._record_activity(
            "sidecar_restored",
            username=username,
            detail=f"Restored {asset.vault_path} from canonical sidecar",
        )
        self._export_sidecar(asset)
        return asset

    def record_sidecar_restore_failure(
        self,
        asset_id: UUID,
        username: str,
        detail: str,
    ) -> None:
        self._record_activity(
            "sidecar_restore_failed",
            username=username,
            detail=f"Recovery refused for {asset_id}: {detail}",
            succeeded=False,
        )

    def _record_activity(
        self,
        action: str,
        *,
        batch_id: UUID | None = None,
        item: ImportItem | None = None,
        username: str | None = None,
        detail: str = "",
        succeeded: bool = True,
    ) -> None:
        batch = self.batches.get(batch_id) if batch_id else None
        self.activity.append(
            VaultMasterActivity(
                id=uuid4(),
                batch_id=batch_id,
                item_id=item.id if item else None,
                source_kind=(
                    item.source_kind
                    if item
                    else str(batch["source_kind"]) if batch else None
                ),
                filename=item.filename if item else None,
                action=action,
                username=username,
                detail=detail,
                succeeded=succeeded,
                created_at=datetime.now(timezone.utc),
            )
        )

    def _publish_catalogued_asset(
        self,
        item: ImportItem,
        vault_path: str,
        *,
        preserve_existing_metadata: bool,
    ) -> CataloguedAsset:
        existing = self.catalogued_assets.get(vault_path)
        if existing and preserve_existing_metadata:
            published = refresh_catalogued_asset_detection(existing, item)
        else:
            display_title, captured_on, location, provenance = (
                effective_asset_metadata(item)
            )
            (
                detected_metadata,
                imported_metadata,
                user_overrides,
                effective_metadata,
            ) = canonical_asset_metadata_layers(item)
            published = CataloguedAsset(
                id=existing.id if existing else uuid4(),
                asset_type=item.proposed_category or (
                    inventory_catalogue_location(item.source_path) or (
                        "Uncategorised",
                        vault_path,
                    )
                )[0],
                display_title=display_title,
                captured_on=captured_on,
                location=location,
                vault_path=vault_path,
                filename=Path(vault_path).name,
                size_bytes=item.size_bytes,
                mime_type=item.mime_type,
                sha256=item.sha256,
                metadata=dict(item.metadata),
                metadata_provenance=provenance,
                detected_metadata=detected_metadata,
                imported_metadata=imported_metadata,
                user_overrides=user_overrides,
                effective_metadata=effective_metadata,
                owner_username=(
                    existing.owner_username
                    if existing
                    else item.owner_username
                ),
                owner_user_id=(
                    existing.owner_user_id
                    if existing
                    else item.owner_user_id
                ),
                origin_vault_id=(
                    existing.origin_vault_id
                    if existing
                    else self._local_vault_id
                ),
                visibility=(
                    existing.visibility
                    if existing
                    else (
                        VAULT_WIDE_ASSET_VISIBILITY
                        if item.proposed_category in {"Movies", "TV Shows"} and item.publication_audience != "private"
                        else PRIVATE_ASSET_VISIBILITY
                    )
                ),
                shared_with=existing.shared_with if existing else (),
                shared_with_user_ids=existing.shared_with_user_ids if existing else (),
            )
        self.catalogued_assets[vault_path] = published
        self._export_sidecar(published)
        return published

    def migrate_source_root(
        self,
        source_kind: str,
        previous_root: str,
        current_root: str,
    ) -> int:
        previous_prefix = f"{previous_root.rstrip('/')}/"
        current_prefix = f"{current_root.rstrip('/')}/"
        replacements: dict[str, ImportItem] = {}

        for source_path, item in self.items.items():
            if (
                item.source_kind == source_kind
                and source_path.startswith(previous_prefix)
            ):
                migrated_path = (
                    current_prefix + source_path[len(previous_prefix) :]
                )
                if migrated_path in self.items:
                    raise ValueError(
                        "Arrival Hall path migration would collide with "
                        "an existing item"
                    )
                replacements[source_path] = ImportItem(
                    **{**item.__dict__, "source_path": migrated_path}
                )

        for source_path, item in replacements.items():
            del self.items[source_path]
            self.items[item.source_path] = item

        for batch in self.batches.values():
            source_root = str(batch["source_root"])
            if batch["source_kind"] != source_kind:
                continue
            if source_root == previous_root:
                batch["source_root"] = current_root
            elif source_root.startswith(previous_prefix):
                batch["source_root"] = (
                    current_prefix + source_root[len(previous_prefix) :]
                )

        return len(replacements)

    def create_batch(self, source_kind: str, source_root: str) -> UUID:
        batch_id = uuid4()
        self.batches[batch_id] = {
            "source_kind": source_kind,
            "source_root": source_root,
            "status": "queued",
            "item_count": 0,
        }
        return batch_id

    def record_file(
        self,
        batch_id: UUID,
        source_kind: str,
        scanned_file: ScannedFile,
    ) -> ImportItem:
        existing = self.items.get(scanned_file.source_path)
        state = (
            "inventoried"
            if source_kind == INVENTORY_SOURCE
            else "needs_review"
        )
        proposal = (
            create_deterministic_proposal(scanned_file)
            if source_kind == INCOMING_SOURCE
            else (None, None, None, None)
        )
        proposed_category = (
            existing.proposed_category
            if existing
            and existing.sha256 == scanned_file.sha256
            and existing.proposal_reason
            in {
                "Category selected by the user.",
                "Local image evidence suggests this destination.",
            }
            else proposal[0]
        )
        owner_username = (
            scanned_file.owner_username
            or (existing.owner_username if existing else self._default_asset_owner)
        )
        owner_user_id = (
            scanned_file.owner_user_id
            or (existing.owner_user_id if existing else uuid5(
                NAMESPACE_URL,
                f"personal-vault-test:{owner_username}",
            ))
        )
        duplicate = self._arrival_duplicate_candidate(
            scanned_file.sha256,
            source_kind,
            proposed_category,
            owner_user_id,
        )
        item = ImportItem(
            id=existing.id if existing else uuid4(),
            batch_id=batch_id,
            source_kind=source_kind,
            source_path=scanned_file.source_path,
            relative_path=scanned_file.relative_path,
            filename=scanned_file.filename,
            size_bytes=scanned_file.size_bytes,
            mime_type=scanned_file.mime_type,
            modified_at=scanned_file.modified_at,
            sha256=scanned_file.sha256,
            state=(
                existing.state
                if existing
                and existing.state
                in {
                    "approved",
                    "rejected",
                    "move_failed",
                    "move_queued",
                    "moving",
                    "theatre_promotion_pending",
                    "duplicate_kept",
                    "duplicate_remove_failed",
                }
                else state
            ),
            duplicate_of_id=duplicate.id if duplicate else None,
            proposed_category=proposed_category,
            proposed_destination=(
                existing.proposed_destination
                if existing
                and existing.sha256 == scanned_file.sha256
                and existing.proposal_reason
                in {
                    "Category selected by the user.",
                    "Local image evidence suggests this destination.",
                }
                else proposal[1]
            ),
            proposal_reason=(
                existing.proposal_reason
                if existing
                and existing.sha256 == scanned_file.sha256
                and existing.proposal_reason
                in {
                    "Category selected by the user.",
                    "Local image evidence suggests this destination.",
                }
                else proposal[2]
            ),
            proposal_confidence=(
                existing.proposal_confidence
                if existing
                and existing.sha256 == scanned_file.sha256
                and existing.proposal_reason
                in {
                    "Category selected by the user.",
                    "Local image evidence suggests this destination.",
                }
                else proposal[3]
            ),
            metadata=scanned_file.metadata,
            metadata_overrides=(
                existing.metadata_overrides if existing else {}
            ),
            owner_username=owner_username,
            owner_user_id=owner_user_id,
        )
        self.items[item.source_path] = item
        if source_kind == INVENTORY_SOURCE:
            catalogue_location = inventory_catalogue_location(
                item.source_path
            )
            if catalogue_location:
                self._publish_catalogued_asset(
                    item,
                    catalogue_location[1],
                    preserve_existing_metadata=True,
                )
        self._record_activity(
            (
                "file_inventoried"
                if source_kind == INVENTORY_SOURCE
                else "file_analysed"
            ),
            batch_id=batch_id,
            item=item,
        )
        return item

    def complete_batch(self, batch_id: UUID, item_count: int) -> None:
        self.batches[batch_id]["status"] = "completed"
        self.batches[batch_id]["item_count"] = item_count
        self._record_activity(
            "scan_completed",
            batch_id=batch_id,
            detail=f"{item_count} file(s) analysed",
        )

    def fail_batch(self, batch_id: UUID, detail: str) -> None:
        self.batches[batch_id]["status"] = "failed"
        self.batches[batch_id]["error"] = detail
        self._record_activity(
            "scan_failed",
            batch_id=batch_id,
            detail=detail,
            succeeded=False,
        )

    def list_items(self) -> list[ImportItem]:
        return list(self.items.values())

    def claim_next_batch(self) -> tuple[UUID, str, str] | None:
        ordered_batches = sorted(
            self.batches.items(),
            key=lambda entry: (
                entry[1]["source_kind"] != INCOMING_SOURCE,
            ),
        )
        for batch_id, batch in ordered_batches:
            if batch["status"] == "queued":
                batch["status"] = "scanning"
                return (
                    batch_id,
                    str(batch["source_kind"]),
                    str(batch["source_root"]),
                )
        return None

    def list_batches(self) -> list[dict[str, object]]:
        return [
            {"id": batch_id, **batch}
            for batch_id, batch in reversed(self.batches.items())
        ]

    def find_active_batch(
        self,
        source_kind: str,
        source_root: str,
    ) -> UUID | None:
        for batch_id, batch in reversed(self.batches.items()):
            if (
                batch["source_kind"] == source_kind
                and batch["source_root"] == source_root
                and batch["status"] in {"queued", "scanning"}
            ):
                return batch_id
        return None

    def list_activity(
        self,
        limit: int = 100,
        *,
        include_file_inventory: bool = True,
        include_file_analysis: bool = True,
        include_empty_scans: bool = True,
    ) -> list[VaultMasterActivity]:
        if limit < 1:
            return []
        events = reversed(self.activity)
        if not include_file_inventory:
            events = (
                event for event in events if event.action != "file_inventoried"
            )
        if not include_file_analysis:
            events = (
                event for event in events if event.action != "file_analysed"
            )
        if not include_empty_scans:
            events = (
                event
                for event in events
                if not (
                    event.action == "scan_completed"
                    and event.source_kind == INCOMING_SOURCE
                    and self.batches.get(event.batch_id, {}).get(
                        "item_count"
                    )
                    == 0
                )
            )
        return list(events)[:limit]

    def _find_item(self, item_id: UUID) -> ImportItem | None:
        return next(
            (item for item in self.items.values() if item.id == item_id),
            None,
        )

    def _arrival_duplicate_candidate(
        self,
        sha256: str,
        source_kind: str,
        proposed_category: str | None,
        owner_user_id: UUID,
    ) -> ImportItem | None:
        """Return only an active canonical duplicate relevant to this intake.

        Arrival Hall history is deliberately not duplicate authority.  A
        personal candidate must belong to the immutable owner; Theatre keeps
        its shared-canonical scope without making private personal matches
        observable.
        """
        if source_kind != INCOMING_SOURCE:
            return None
        matching_assets = [
            asset
            for asset in self.catalogued_assets.values()
            if asset.sha256 == sha256
        ]
        if matching_assets:
            if is_theatre_category(proposed_category):
                relevant = any(
                    is_theatre_asset_type(asset.asset_type)
                    for asset in matching_assets
                )
            else:
                relevant = any(
                    not is_theatre_asset_type(asset.asset_type)
                    and asset.owner_user_id == owner_user_id
                    for asset in matching_assets
                )
            if not relevant:
                return None
        elif is_theatre_category(proposed_category):
            return None

        return next(
            (
                candidate
                for candidate in self.items.values()
                if candidate.source_kind == INVENTORY_SOURCE
                and candidate.sha256 == sha256
                and (
                    is_theatre_category(proposed_category)
                    or candidate.owner_user_id == owner_user_id
                )
            ),
            None,
        )

    def update_proposal(
        self,
        item_id: UUID,
        category: str,
        username: str,
        destination_subfolder: str | None = None,
        publication_audience: str | None = None,
    ) -> ImportItem | None:
        del username
        item = self._find_item(item_id)
        if (
            item is None
            or item.source_kind != INCOMING_SOURCE
            or item.state
            not in {"needs_review", "approved", "rejected", "move_failed"}
        ):
            return None
        updated = ImportItem(
            **{
                **item.__dict__,
                "proposed_category": category,
                "proposed_destination": proposed_destination_path(
                    category,
                    item.relative_path,
                    item.filename,
                    destination_subfolder,
                ),
                "proposal_reason": "Category selected by the user.",
                "proposal_confidence": "high",
                "publication_audience": publication_audience if category in {"Movies", "TV Shows"} else None,
                "state": "needs_review",
            }
        )
        duplicate = self._arrival_duplicate_candidate(
            updated.sha256,
            updated.source_kind,
            updated.proposed_category,
            updated.owner_user_id,
        ) if updated.owner_user_id is not None else None
        updated = ImportItem(
            **{
                **updated.__dict__,
                "duplicate_of_id": duplicate.id if duplicate is not None else None,
            }
        )
        self.items[item.source_path] = updated
        return updated

    def apply_ai_proposal(
        self,
        item_id: UUID,
        category: str,
        reason: str,
        destination_subfolder: str | None = None,
        force: bool = False,
    ) -> ImportItem | None:
        item = self._find_item(item_id)
        if (
            item is None
            or item.source_kind != INCOMING_SOURCE
            or item.state not in {"inventoried", "needs_review"}
            or item.proposal_reason == "Category selected by the user."
            or (
                not force
                and item.proposal_confidence not in {"low", "medium"}
                and item.proposal_reason != SYSTEM_SCREENSHOT_PROPOSAL_REASON
            )
        ):
            return None
        updated = ImportItem(
            **{
                **item.__dict__,
                "proposed_category": category,
                "proposed_destination": proposed_destination_path(
                    category,
                    item.relative_path,
                    item.filename,
                    destination_subfolder,
                ),
                "proposal_reason": reason,
                "state": "needs_review",
            }
        )
        duplicate = self._arrival_duplicate_candidate(
            updated.sha256,
            updated.source_kind,
            updated.proposed_category,
            updated.owner_user_id,
        ) if updated.owner_user_id is not None else None
        updated = ImportItem(
            **{
                **updated.__dict__,
                "duplicate_of_id": duplicate.id if duplicate is not None else None,
            }
        )
        self.items[item.source_path] = updated
        return updated

    def update_catalogued_asset_access(
        self,
        asset_id: UUID,
        visibility: str,
        shared_with: tuple[str, ...],
        username: str,
        *,
        local_all: bool = False,
        share_mode: Literal["quick", "standard"] = "quick",
        shared_with_user_ids: tuple[UUID, ...] = (),
    ) -> CataloguedAsset | None:
        entry = next(
            (
                (vault_path, asset)
                for vault_path, asset in self.catalogued_assets.items()
                if asset.id == asset_id
            ),
            None,
        )
        if entry is None:
            return None
        vault_path, asset = entry
        updated = apply_catalogue_access_changes(
            asset,
            visibility,
            shared_with,
            shared_with_user_ids=shared_with_user_ids,
            require_shared_user_ids=True,
            local_all=local_all,
        )
        self.catalogued_assets[vault_path] = updated
        self._export_sidecar(updated)
        self.asset_history.append(
            {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "access_policy_updated",
                "username": username,
                "previous_values": {
                    "visibility": asset.visibility,
                    "shared_with": ", ".join(asset.shared_with) or None,
                },
                "current_values": {
                    "visibility": updated.visibility,
                    "shared_with": ", ".join(updated.shared_with) or None,
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        return updated

    def update_metadata_overrides(
        self,
        item_id: UUID,
        changes: dict[str, object | None],
        username: str,
    ) -> ImportItem | None:
        del username
        item = self._find_item(item_id)
        if item is None or item.source_kind != INCOMING_SOURCE:
            return None
        overrides = dict(item.metadata_overrides)
        for name, value in changes.items():
            if value is None:
                overrides.pop(name, None)
            else:
                overrides[name] = value
        updated = ImportItem(
            **{
                **item.__dict__,
                "metadata_overrides": overrides,
                "state": "needs_review",
            }
        )
        self.items[item.source_path] = updated
        return updated

    def record_decision(
        self, item_id: UUID, decision: str, username: str
    ) -> ImportItem | None:
        item = self._find_item(item_id)
        if item is None or item.source_kind != INCOMING_SOURCE:
            return None
        values = {**item.__dict__, "state": decision}
        if (
            decision == "approved"
            and item.proposed_category == "Movies"
            and MAKEMKV_TRACK_PATTERN.fullmatch(Path(item.filename).stem)
        ):
            try:
                destination, provisional, set_evidence = (
                    movie_publication_set_destination(
                        item, list(self.items.values())
                    )
                )
            except ValueError as error:
                if str(error) != (
                    "Movie publication set membership has already progressed; "
                    "this item cannot be approved independently"
                ):
                    raise
                return reconcile_memory_movie_publication_set(
                    self, item, username
                )
            values["proposed_destination"] = destination
            values["metadata"] = dict(item.metadata)
            if provisional is not None:
                values["metadata"]["movie_identity_provisional"] = provisional
            if set_evidence is not None:
                values["metadata"]["movie_publication_set"] = set_evidence
            else:
                values["metadata"].pop("movie_publication_set", None)
        elif decision == "approved" and item.proposed_category == "TV Shows":
            destination, set_evidence = tv_publication_set_destination(
                item, list(self.items.values())
            )
            values["proposed_destination"] = destination
            values["metadata"] = dict(item.metadata)
            values["metadata"]["tv_publication_set"] = set_evidence
        updated = ImportItem(**values)
        self.items[item.source_path] = updated
        self._record_activity(
            f"proposal_{decision}",
            batch_id=item.batch_id,
            item=updated,
            username=username,
        )
        return updated

    def get_item(self, item_id: UUID) -> ImportItem | None:
        return self._find_item(item_id)

    def get_metadata_overrides(
        self, destination_paths: list[str]
    ) -> dict[str, dict[str, object]]:
        requested = set(destination_paths)
        result: dict[str, dict[str, object]] = {}
        for destination_path, item_id in self.moved_destinations.items():
            if destination_path not in requested:
                continue
            item = self._find_item(item_id)
            if item and item.metadata_overrides:
                result[destination_path] = dict(item.metadata_overrides)
        return result

    def get_catalogued_asset(
        self, vault_path: str
    ) -> CataloguedAsset | None:
        return self.catalogued_assets.get(vault_path)

    def get_catalogued_assets(
        self, vault_paths: list[str]
    ) -> dict[str, CataloguedAsset]:
        return {
            vault_path: self.catalogued_assets[vault_path]
            for vault_path in vault_paths
            if vault_path in self.catalogued_assets
        }

    def get_visible_catalogued_assets(
        self, vault_paths: list[str], username: str
    ) -> dict[str, CataloguedAsset]:
        return {
            vault_path: asset
            for vault_path, asset in self.get_catalogued_assets(
                vault_paths
            ).items()
            if asset_is_visible_to(asset, username)
        }

    def get_catalogued_asset_by_id(
        self, asset_id: UUID
    ) -> CataloguedAsset | None:
        return next(
            (
                asset
                for asset in self.catalogued_assets.values()
                if asset.id == asset_id
            ),
            None,
        )

    def list_owned_catalogued_assets(
        self, username: str
    ) -> list[CataloguedAsset]:
        return sorted(
            (
                asset
                for asset in self.catalogued_assets.values()
                if asset.owner_username == username
            ),
            key=lambda asset: (asset.filename.casefold(), str(asset.id)),
        )

    def search_catalogued_assets(
        self, query: str, limit: int = 50
    ) -> list[CataloguedAsset]:
        needle = query.strip().casefold()
        if not needle:
            return []
        matches = [
            asset
            for asset in self.catalogued_assets.values()
            if any(
                needle in value.casefold()
                for value in (
                    asset.display_title,
                    asset.filename,
                    asset.vault_path,
                    asset.location or "",
                )
            )
        ]
        return sorted(
            matches,
            key=lambda asset: (
                asset.display_title.casefold(),
                asset.vault_path.casefold(),
            ),
        )[:limit]

    def get_visible_catalogued_asset_by_id(
        self, asset_id: UUID, username: str
    ) -> CataloguedAsset | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None or not asset_is_visible_to(asset, username):
            return None
        return asset

    def list_visible_movie_assets(
        self, username: str
    ) -> list[CataloguedAsset]:
        return sorted(
            {
                asset.id: asset
                for asset in self.catalogued_assets.values()
                if asset.asset_type.casefold() in {"movie", "movies"}
                and asset_is_visible_to(asset, username)
            }.values(),
            key=lambda asset: (asset.display_title.casefold(), str(asset.id)),
        )

    def list_owned_catalogued_assets_by_user_id(
        self, owner_user_id: UUID
    ) -> list[CataloguedAsset]:
        return sorted(
            (asset for asset in self.catalogued_assets.values() if asset.owner_user_id == owner_user_id),
            key=lambda asset: (asset.filename.casefold(), str(asset.id)),
        )

    def list_visible_catalogued_assets(
        self, username: str
    ) -> list[CataloguedAsset]:
        return sorted(
            (
                asset
                for asset in self.catalogued_assets.values()
                if asset_is_visible_to(asset, username)
            ),
            key=lambda asset: (asset.filename.casefold(), str(asset.id)),
        )

    def set_movie_exclusive_state(
        self, asset_id: UUID, username: str, is_exclusive: bool
    ) -> CataloguedAsset | None:
        entry = next(
            (
                (path, asset)
                for path, asset in self.catalogued_assets.items()
                if asset.id == asset_id
                and asset.asset_type.casefold() in {"movie", "movies"}
                and asset_is_editable_by(asset, username)
            ),
            None,
        )
        if entry is None:
            return None
        vault_path, asset = entry
        overrides = dict(asset.user_overrides)
        overrides["exclusive_movie"] = is_exclusive
        effective = {**asset.effective_metadata, "exclusive_movie": is_exclusive}
        updated = replace(
            asset,
            user_overrides=overrides,
            effective_metadata=effective,
        )
        self.catalogued_assets[vault_path] = updated
        self.asset_history.append(
            {
                "asset_id": asset.id,
                "action": "exclusive_movie_state_changed",
                "username": username,
                "previous_values": {
                    "exclusive_movie": bool(
                        asset.effective_metadata.get("exclusive_movie", False)
                    )
                },
                "current_values": {"exclusive_movie": is_exclusive},
                "created_at": datetime.now(timezone.utc),
            }
        )
        self._export_sidecar(updated)
        return updated

    def list_catalogued_assets_by_vault_path_prefix(
        self, prefix: str
    ) -> list[CataloguedAsset]:
        return sorted(
            (asset for path, asset in self.catalogued_assets.items() if path.startswith(prefix)),
            key=lambda asset: str(asset.id),
        )

    def update_catalogued_assets_access(
        self, asset_ids: list[UUID], visibility: str, shared_with: tuple[str, ...], username: str,
        *, local_all: bool = False, share_mode: Literal["quick", "standard"] = "quick", shared_with_user_ids: tuple[UUID, ...] = (),
    ) -> list[CataloguedAsset]:
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("Selected assets must be unique")
        entries = [
            next(((path, asset) for path, asset in self.catalogued_assets.items() if asset.id == asset_id), None)
            for asset_id in asset_ids
        ]
        if any(entry is None for entry in entries):
            raise ValueError("Selected asset was not found")
        updates = [
            (path, asset, apply_catalogue_access_changes(asset, visibility, shared_with, local_all=local_all, shared_with_user_ids=shared_with_user_ids, require_shared_user_ids=True))
            for path, asset in entries if path is not None
        ]
        for path, asset, updated in updates:
            self.catalogued_assets[path] = updated
            self._export_sidecar(updated)
            self.asset_history.append({
                "id": uuid4(), "asset_id": asset.id, "action": "access_policy_updated", "username": username,
                "previous_values": {"visibility": asset.visibility, "shared_with": ", ".join(asset.shared_with) or None},
                "current_values": {"visibility": updated.visibility, "shared_with": ", ".join(updated.shared_with) or None},
                "created_at": datetime.now(timezone.utc),
            })
        return [updated for _, _, updated in updates]

    def search_visible_catalogued_assets(
        self, query: str, username: str, limit: int = 50
    ) -> list[CataloguedAsset]:
        return [
            asset
            for asset in self.search_catalogued_assets(
                query, len(self.catalogued_assets)
            )
            if asset_is_visible_to(asset, username)
        ][:limit]

    def update_catalogued_asset_metadata(
        self,
        asset_id: UUID,
        changes: dict[str, str | None],
        username: str,
    ) -> CataloguedAsset | None:
        entry = next(
            (
                (vault_path, asset)
                for vault_path, asset in self.catalogued_assets.items()
                if asset.id == asset_id
            ),
            None,
        )
        if entry is None:
            return None
        vault_path, asset = entry
        updated = apply_catalogue_metadata_changes(asset, changes)
        self.catalogued_assets[vault_path] = updated
        self._export_sidecar(updated)
        self.asset_history.append(
            {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "metadata_updated",
                "username": username,
                "previous_values": {
                    field: (
                        value.isoformat()
                        if isinstance(value, date)
                        else value
                    )
                    for field in changes
                    for value in (catalogue_metadata_field_value(asset, field),)
                },
                "current_values": {
                    field: (
                        value.isoformat()
                        if isinstance(value, date)
                        else value
                    )
                    for field in changes
                    for value in (catalogue_metadata_field_value(updated, field),)
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        return updated

    def import_catalogued_asset_metadata(
        self,
        asset_id: UUID,
        metadata: dict[str, object],
        source: str,
    ) -> CataloguedAsset | None:
        entry = next(
            (
                (vault_path, asset)
                for vault_path, asset in self.catalogued_assets.items()
                if asset.id == asset_id
            ),
            None,
        )
        if entry is None:
            return None
        vault_path, asset = entry
        updated = apply_imported_asset_metadata(asset, metadata, source)
        self.catalogued_assets[vault_path] = updated
        self._export_sidecar(updated)
        return updated

    def list_catalogued_asset_history(
        self, asset_id: UUID
    ) -> list[dict[str, object]]:
        return sorted(
            (
                dict(entry)
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == asset_id
            ),
            key=lambda entry: entry["created_at"],
            reverse=True,
        )

    def request_catalogued_asset_quarantine_review(
        self,
        asset_id: UUID,
        username: str,
        reason: str | None,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        previous_state = (
            "quarantined"
            if asset.vault_path.startswith("/vault/Quarantine/")
            else asset.lifecycle_state
        )
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": "quarantine_review_requested",
            "username": username,
            "previous_values": {},
            "current_values": {
                "reason": reason,
                "state": "pending_review",
            },
            "created_at": datetime.now(timezone.utc),
        }
        self.asset_history.append(entry)
        return dict(entry)

    def request_asset_relationship_review(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        classification: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> dict[str, object] | None:
        if any(
            self.get_catalogued_asset_by_id(asset_id) is None
            for asset_id in (first_asset_id, second_asset_id)
        ):
            return None
        existing = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == first_asset_id
                and entry["action"].startswith("relationship_review_")
                and entry["current_values"].get("candidate_asset_id")
                == str(second_asset_id)
            ),
            None,
        )
        if existing is not None:
            return None
        created_at = datetime.now(timezone.utc)
        entries = []
        for asset_id, candidate_id in (
            (first_asset_id, second_asset_id),
            (second_asset_id, first_asset_id),
        ):
            entry = {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "relationship_review_requested",
                "username": username,
                "previous_values": {},
                "current_values": {
                    "candidate_asset_id": str(candidate_id),
                    "classification": classification,
                    "confidence": confidence,
                    "evidence": json.dumps(evidence),
                    "state": "pending_review",
                },
                "created_at": created_at,
            }
            self.asset_history.append(entry)
            entries.append(entry)
        return dict(entries[0])

    def retain_separate_asset_relationship_review(
        self, first_asset_id: UUID, second_asset_id: UUID, username: str
    ) -> dict[str, object] | None:
        latest = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == first_asset_id
                and entry["action"]
                in {"relationship_review_requested", "relationship_review_retained"}
                and entry["current_values"].get("candidate_asset_id")
                == str(second_asset_id)
            ),
            None,
        )
        if latest is None or latest["action"] != "relationship_review_requested":
            return None
        created_at = datetime.now(timezone.utc)
        entries = []
        for asset_id, candidate_id in (
            (first_asset_id, second_asset_id),
            (second_asset_id, first_asset_id),
        ):
            entry = {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "relationship_review_retained",
                "username": username,
                "previous_values": {"state": "pending_review"},
                "current_values": {
                    "candidate_asset_id": str(candidate_id),
                    "decision": "retain_separately",
                    "state": "resolved",
                },
                "created_at": created_at,
            }
            self.asset_history.append(entry)
            entries.append(entry)
        return dict(entries[0])

    def create_catalogued_asset_relationship(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        relationship_type: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> CataloguedAssetRelationship | None:
        asset_ids = tuple(sorted((first_asset_id, second_asset_id), key=str))
        if first_asset_id == second_asset_id or any(
            self.get_catalogued_asset_by_id(asset_id) is None for asset_id in asset_ids
        ):
            return None
        if asset_ids in self.asset_relationships:
            return None
        relationship = CataloguedAssetRelationship(
            first_asset_id=asset_ids[0],
            second_asset_id=asset_ids[1],
            relationship_type=relationship_type,
            confidence=confidence,
            evidence=evidence,
            created_by=username,
            created_at=datetime.now(timezone.utc),
        )
        self.asset_relationships[asset_ids] = relationship
        return relationship

    def list_catalogued_asset_relationships(
        self, asset_id: UUID
    ) -> list[CataloguedAssetRelationship]:
        return sorted(
            (
                relationship
                for relationship in self.asset_relationships.values()
                if asset_id
                in (relationship.first_asset_id, relationship.second_asset_id)
            ),
            key=lambda relationship: (
                relationship.relationship_type,
                str(relationship.first_asset_id),
                str(relationship.second_asset_id),
            ),
        )

    def approve_asset_relationship_review(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        relationship_type: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> dict[str, object] | None:
        latest = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == first_asset_id
                and entry["action"]
                in {
                    "relationship_review_requested",
                    "relationship_review_retained",
                    "relationship_review_linked",
                }
                and entry["current_values"].get("candidate_asset_id")
                == str(second_asset_id)
            ),
            None,
        )
        if latest is None or latest["action"] != "relationship_review_requested":
            return None
        relationship = self.create_catalogued_asset_relationship(
            first_asset_id,
            second_asset_id,
            relationship_type,
            confidence,
            evidence,
            username,
        )
        if relationship is None:
            return None
        created_at = relationship.created_at
        entries = []
        for asset_id, candidate_id in (
            (first_asset_id, second_asset_id),
            (second_asset_id, first_asset_id),
        ):
            entry = {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "relationship_review_linked",
                "username": username,
                "previous_values": {"state": "pending_review"},
                "current_values": {
                    "candidate_asset_id": str(candidate_id),
                    "relationship_type": relationship_type,
                    "state": "resolved",
                },
                "created_at": created_at,
            }
            self.asset_history.append(entry)
            entries.append(entry)
        return dict(entries[0])

    def cancel_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        lifecycle_entry = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == asset_id
                and entry["action"]
                in {
                    "permanent_deletion_review_requested",
                    "permanent_deletion_review_cancelled",
                    "permanent_deletion_confirmed",
                }
            ),
            None,
        )
        if lifecycle_entry is None or lifecycle_entry["action"] != (
            "permanent_deletion_review_requested"
        ):
            return None
        previous_state = (
            "quarantined"
            if asset.vault_path.startswith("/vault/Quarantine/")
            else asset.lifecycle_state
        )
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": "permanent_deletion_review_cancelled",
            "username": username,
            "previous_values": {"state": "pending_permanent_deletion_review"},
            "current_values": {"state": previous_state},
            "created_at": datetime.now(timezone.utc),
        }
        self.asset_history.append(entry)
        return dict(entry)

    def confirm_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
        checksum: str,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        lifecycle_entry = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == asset_id
                and entry["action"]
                in {
                    "permanent_deletion_review_requested",
                    "permanent_deletion_review_cancelled",
                    "permanent_deletion_confirmed",
                }
            ),
            None,
        )
        if lifecycle_entry is None or lifecycle_entry["action"] != (
            "permanent_deletion_review_requested"
        ):
            return None
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": "permanent_deletion_confirmed",
            "username": username,
            "previous_values": {"state": "pending_permanent_deletion_review"},
            "current_values": {
                "state": "approved_for_permanent_deletion",
                "checksum": checksum,
            },
            "created_at": datetime.now(timezone.utc),
        }
        self.asset_history.append(entry)
        return dict(entry)

    def record_catalogued_asset_permanent_deletion(
        self,
        asset_id: UUID,
        vault_path: str,
        checksum: str,
        username: str,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if (
            asset is None
            or asset.vault_path != vault_path
            or asset.sha256 != checksum
        ):
            return None
        lifecycle_entry = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == asset_id
                and entry["action"] == "permanent_deletion_confirmed"
            ),
            None,
        )
        if (
            lifecycle_entry is None
            or lifecycle_entry["current_values"].get("checksum") != checksum
        ):
            return None
        deleted_at = datetime.now(timezone.utc)
        self.deleted_assets[asset_id] = {
            "asset_id": asset_id,
            "vault_path": vault_path,
            "filename": asset.filename,
            "size_bytes": asset.size_bytes,
            "mime_type": asset.mime_type,
            "sha256": checksum,
            "deleted_by": username,
            "deleted_at": deleted_at,
        }
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": "permanently_deleted",
            "username": username,
            "previous_values": {
                "state": "approved_for_permanent_deletion",
                "vault_path": vault_path,
                "checksum": checksum,
            },
            "current_values": {"state": "deleted"},
            "created_at": deleted_at,
        }
        self.asset_history.append(entry)
        del self.catalogued_assets[vault_path]
        return dict(entry)

    def set_catalogued_asset_lifecycle_state(
        self, asset_id: UUID, owner_user_id: UUID, username: str, state: str
    ) -> CataloguedAsset | None:
        if state not in {"active", "hidden"}:
            raise ValueError("Unsupported catalogue lifecycle state")
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None or asset.owner_user_id != owner_user_id:
            return None
        if asset.lifecycle_state == state:
            return asset
        updated = replace(asset, lifecycle_state=state)
        self.catalogued_assets[asset.vault_path] = updated
        self.asset_history.append(
            {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "asset_hidden" if state == "hidden" else "asset_unhidden",
                "username": username,
                "previous_values": {"lifecycle_state": asset.lifecycle_state},
                "current_values": {"lifecycle_state": state},
                "created_at": datetime.now(timezone.utc),
            }
        )
        return updated

    def has_catalogued_asset_deletion(self, asset_id: UUID) -> bool:
        return asset_id in self.deleted_assets

    def cancel_catalogued_asset_quarantine_review(
        self,
        asset_id: UUID,
        username: str,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        lifecycle_entry = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == asset_id
                and entry["action"]
                in {
                    "quarantine_review_requested",
                    "quarantine_review_cancelled",
                }
            ),
            None,
        )
        if lifecycle_entry is None or lifecycle_entry["action"] != (
            "quarantine_review_requested"
        ):
            return None
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": "quarantine_review_cancelled",
            "username": username,
            "previous_values": {"state": "pending_review"},
            "current_values": {"state": "cancelled"},
            "created_at": datetime.now(timezone.utc),
        }
        self.asset_history.append(entry)
        return dict(entry)

    def confirm_catalogued_asset_quarantine(
        self,
        asset_id: UUID,
        source_vault_path: str,
        quarantine_vault_path: str,
        username: str,
    ) -> CataloguedAsset | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if (
            asset is None
            or asset.vault_path != source_vault_path
            or quarantine_vault_path in self.catalogued_assets
        ):
            return None
        lifecycle_entry = next(
            (
                entry
                for entry in reversed(self.asset_history)
                if entry["asset_id"] == asset_id
                and entry["action"]
                in {
                    "quarantine_review_requested",
                    "quarantine_review_cancelled",
                    "quarantined",
                }
            ),
            None,
        )
        if lifecycle_entry is None or lifecycle_entry["action"] != (
            "quarantine_review_requested"
        ):
            return None
        updated = replace(
            asset,
            vault_path=quarantine_vault_path,
            filename=Path(quarantine_vault_path).name,
        )
        del self.catalogued_assets[source_vault_path]
        self.catalogued_assets[quarantine_vault_path] = updated
        self._export_sidecar(updated)
        self.asset_history.append(
            {
                "id": uuid4(),
                "asset_id": asset_id,
                "action": "quarantined",
                "username": username,
                "previous_values": {
                    "vault_path": source_vault_path,
                    "state": "pending_review",
                },
                "current_values": {
                    "vault_path": quarantine_vault_path,
                    "state": "quarantined",
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        return updated

    def relocate_catalogued_asset(
        self,
        asset_id: UUID,
        source_vault_path: str,
        destination_vault_path: str,
        username: str,
        action: str,
    ) -> CataloguedAsset | None:
        """Record an already verified, owner-confirmed filesystem relocation."""
        asset = self.get_catalogued_asset_by_id(asset_id)
        if (
            asset is None
            or asset.vault_path != source_vault_path
            or destination_vault_path in self.catalogued_assets
        ):
            return None
        updated = replace(asset, vault_path=destination_vault_path, filename=Path(destination_vault_path).name)
        del self.catalogued_assets[source_vault_path]
        self.catalogued_assets[destination_vault_path] = updated
        self._export_sidecar(updated)
        self.asset_history.append(
            {
                "id": uuid4(), "asset_id": asset_id, "action": action, "username": username,
                "previous_values": {"vault_path": source_vault_path, "checksum": asset.sha256},
                "current_values": {"vault_path": destination_vault_path, "checksum": asset.sha256, "state": "relocated"},
                "created_at": datetime.now(timezone.utc),
            }
        )
        return updated

    def record_catalogued_asset_history(
        self,
        asset_id: UUID,
        username: str,
        action: str,
        current_values: dict[str, object],
    ) -> dict[str, object] | None:
        if self.get_catalogued_asset_by_id(asset_id) is None:
            return None
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": action,
            "username": username,
            "previous_values": {},
            "current_values": dict(current_values),
            "created_at": datetime.now(timezone.utc),
        }
        self.asset_history.append(entry)
        return dict(entry)

    def request_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
        reason: str,
        eligible_at: datetime,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        previous_state = (
            "quarantined"
            if asset.vault_path.startswith("/vault/Quarantine/")
            else asset.lifecycle_state
        )
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": "permanent_deletion_review_requested",
            "username": username,
            "previous_values": {"state": previous_state},
            "current_values": {
                "reason": reason,
                "state": "pending_permanent_deletion_review",
                "eligible_at": eligible_at.isoformat(),
            },
            "created_at": datetime.now(timezone.utc),
        }
        self.asset_history.append(entry)
        return dict(entry)

    def record_move_result(
        self,
        item_id: UUID,
        state: str,
        username: str,
        detail: str,
        publish_catalogue: bool = True,
    ) -> ImportItem | None:
        item = self._find_item(item_id)
        if item is None:
            return None
        if state == "move_failed" and item.state == "moved":
            return item
        updated = ImportItem(**{**item.__dict__, "state": state})
        self.items[item.source_path] = updated
        if state == "moved" and detail.startswith("Moved to "):
            self.moved_destinations[detail.removeprefix("Moved to ")] = item_id
            if publish_catalogue and item.proposed_destination:
                self._publish_catalogued_asset(
                    item,
                    item.proposed_destination,
                    preserve_existing_metadata=False,
                )
        self._record_activity(
            "file_moved" if state == "moved" else "move_failed",
            batch_id=item.batch_id,
            item=updated,
            username=username,
            detail=detail,
            succeeded=state == "moved",
        )
        return updated

    def record_duplicate_result(
        self,
        item_id: UUID,
        state: str,
        username: str,
        detail: str,
    ) -> ImportItem | None:
        item = self._find_item(item_id)
        if item is None:
            return None
        if state == "duplicate_remove_failed" and item.state == "duplicate_removed":
            return item
        updated = ImportItem(**{**item.__dict__, "state": state})
        self.items[item.source_path] = updated
        self._record_activity(
            {
                "duplicate_kept": "duplicate_kept",
                "duplicate_removed": "duplicate_removed",
            }.get(state, "duplicate_remove_failed"),
            batch_id=item.batch_id,
            item=updated,
            username=username,
            detail=detail,
            succeeded=state != "duplicate_remove_failed",
        )
        return updated

    def queue_move(self, item_id: UUID, username: str) -> ImportItem | None:
        item = self._find_item(item_id)
        if item is None or item.state not in {"approved", "move_failed"}:
            return None
        if not movie_publication_set_is_ready(
            item, list(self.items.values())
        ):
            return None
        updated = ImportItem(**{**item.__dict__, "state": "move_queued"})
        self.items[item.source_path] = updated
        self._record_activity(
            "move_queued",
            batch_id=item.batch_id,
            item=updated,
            username=username,
        )
        return updated

    def claim_next_move(self) -> ImportItem | None:
        for item in self.items.values():
            if item.state == "moving":
                recovered = ImportItem(
                    **{**item.__dict__, "state": "move_queued"}
                )
                self.items[item.source_path] = recovered
        for item in self.items.values():
            if item.state == "move_queued":
                claimed = ImportItem(**{**item.__dict__, "state": "moving"})
                self.items[item.source_path] = claimed
                return claimed
        return None

    def mark_theatre_promotion_pending(self, item_id: UUID) -> ImportItem | None:
        item = self._find_item(item_id)
        if item is None or item.state != "moving":
            return None
        updated = ImportItem(**{**item.__dict__, "state": "theatre_promotion_pending"})
        self.items[item.source_path] = updated
        return updated

    def publish_arrival_managed_receipt(
        self, item_id: UUID, receipt: dict[str, object]
    ) -> CataloguedAsset | None:
        item = self._find_item(item_id)
        if item is None or item.proposed_category != "Movies" or item.owner_user_id is None:
            return None
        destination = item.proposed_destination or proposed_destination_path(
            "Movies", item.relative_path, item.filename
        )
        expected_relative = destination.removeprefix("/vault/")
        if (
            receipt.get("item_id") != str(item.id)
            or receipt.get("owner_user_id") != str(item.owner_user_id)
            or receipt.get("logical_destination") != destination
            or receipt.get("logical_area") != "Theatre / Movies"
            or receipt.get("relative_path") != expected_relative
            or receipt.get("expected_sha256") != item.sha256
            or receipt.get("expected_size_bytes") != item.size_bytes
            or not isinstance(receipt.get("slot_id"), str)
        ):
            return None
        existing = self.catalogued_assets.get(destination)
        if existing is not None:
            publication = self.arrival_managed_publications.get(item.id)
            if (
                publication is None
                or existing.sha256 != item.sha256
                or existing.size_bytes != item.size_bytes
                or any(
                    publication.get(field) != receipt.get(field)
                    for field in (
                        "request_id", "item_id", "owner_user_id",
                        "logical_destination", "logical_area", "slot_id",
                        "relative_path", "expected_sha256",
                        "expected_size_bytes",
                    )
                )
            ):
                return None
            self.items[item.source_path] = ImportItem(**{**item.__dict__, "state": "moved"})
            return None
        if item.state != "theatre_promotion_pending":
            return None
        self.items[item.source_path] = ImportItem(**{**item.__dict__, "state": "moved"})
        published = self._publish_catalogued_asset(item, destination, preserve_existing_metadata=False)
        placement = {"slot_id": receipt["slot_id"], "relative_path": expected_relative}
        published = replace(
            published,
            metadata={**published.metadata, "storage_placement": placement},
            metadata_provenance={**published.metadata_provenance, "storage_placement": "root_verified_receipt"},
            effective_metadata={**published.effective_metadata, "storage_placement": placement},
        )
        self.catalogued_assets[destination] = published
        self.arrival_managed_publications[item.id] = {
            **receipt,
            "asset_id": str(published.id),
            "file_id": str(self.catalogued_file_ids.setdefault(published.id, uuid4())),
        }
        self._record_activity("file_moved", batch_id=item.batch_id, item=item, username="Arrival Hall managed publisher", detail=f"Published root-verified managed receipt {receipt['request_id']}")
        return published

    def publish_arrival_theatre_receipt(self, item_id: UUID, receipt: dict[str, object]) -> CataloguedAsset | None:
        """Legacy receipt interface retained for historical Theatre receipts only."""
        return self.publish_arrival_managed_receipt(item_id, receipt)

    def theatre_movie_rename_snapshot(
        self, asset_id: UUID, owner_user_id: UUID
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if (
            asset is None
            or asset.owner_user_id != owner_user_id
            or asset.asset_type not in {"Movie", "Movies"}
            or "movie_publication_set" in asset.detected_metadata
        ):
            return None
        placement = asset.effective_metadata.get("storage_placement")
        if not isinstance(placement, dict):
            return None
        slot_id = placement.get("slot_id")
        relative_path = placement.get("relative_path")
        if not isinstance(slot_id, str) or not isinstance(relative_path, str):
            return None
        file_id = self.catalogued_file_ids.setdefault(asset.id, uuid4())
        return {
            "asset_id": asset.id,
            "file_id": file_id,
            "owner_user_id": asset.owner_user_id,
            "slot_id": slot_id,
            "vault_path": asset.vault_path,
            "filename": asset.filename,
            "relative_path": relative_path,
            "size_bytes": asset.size_bytes,
            "sha256": asset.sha256,
        }

    def complete_theatre_movie_rename(
        self, receipt: dict[str, object]
    ) -> CataloguedAsset | None:
        if receipt.get("schema") != "personal-vault.theatre-movie-rename.v1":
            return None
        try:
            asset_id = UUID(str(receipt["asset_id"]))
            file_id = UUID(str(receipt["file_id"]))
            owner_user_id = UUID(str(receipt["owner_user_id"]))
            title = str(receipt["title"])
            year = int(receipt["release_year"])
            source = str(receipt["source_logical_path"])
            destination = str(receipt["destination_logical_path"])
        except (KeyError, TypeError, ValueError):
            return None
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None or asset.vault_path == destination:
            return None
        placement = asset.effective_metadata.get("storage_placement")
        if not isinstance(placement, dict):
            return None
        source_relative = placement.get("relative_path")
        slot_id = placement.get("slot_id")
        expected = {
            "owner_user_id": str(asset.owner_user_id),
            "slot_id": slot_id,
            "source_logical_path": asset.vault_path,
            "source_relative_path": source_relative,
            "expected_sha256": asset.sha256,
            "expected_size_bytes": asset.size_bytes,
        }
        if (
            asset.owner_user_id != owner_user_id
            or self.catalogued_file_ids.get(asset.id) != file_id
            or any(receipt.get(key) != value for key, value in expected.items())
            or destination
            != canonical_movie_destination(title, year, Path(source).suffix)
            or destination in self.catalogued_assets
        ):
            return None
        destination_relative = destination.removeprefix("/vault/")
        if receipt.get("destination_relative_path") != destination_relative:
            return None
        updated_placement = {
            "slot_id": str(slot_id),
            "relative_path": destination_relative,
        }
        imported_identity = matches_reliable_imported_movie_identity(
            asset.detected_metadata, asset.imported_metadata, title, year
        )
        overrides = (
            dict(asset.user_overrides)
            if imported_identity
            else {
                **asset.user_overrides,
                "display_title": title,
                "release_year": year,
            }
        )
        provenance = dict(asset.metadata_provenance)
        effective = dict(asset.effective_metadata)
        if not imported_identity:
            provenance.update(
                {
                    "display_title": "user_override",
                    "release_year": "user_override",
                }
            )
            effective.update(overrides)
        updated = replace(
            asset,
            vault_path=destination,
            filename=Path(destination).name,
            display_title=title,
            metadata={**asset.metadata, "storage_placement": updated_placement},
            metadata_provenance=provenance,
            user_overrides=overrides,
            effective_metadata={
                **effective,
                "storage_placement": updated_placement,
            },
        )
        del self.catalogued_assets[source]
        self.catalogued_assets[destination] = updated
        self.asset_history.append(
            {
                "id": uuid4(),
                "asset_id": asset.id,
                "action": "theatre_movie_renamed",
                "username": "Theatre managed rename",
                "previous_values": {
                    "vault_path": source,
                    "relative_path": source_relative,
                    "sha256": asset.sha256,
                },
                "current_values": {
                    "vault_path": destination,
                    "relative_path": destination_relative,
                    "sha256": asset.sha256,
                    "request_id": str(receipt["request_id"]),
                },
                "created_at": datetime.now(timezone.utc),
            }
        )
        self._export_sidecar(updated)
        return updated


class PostgresVaultMasterStore:
    def __init__(
        self,
        conninfo: str,
        sidecar_root: Path | None = None,
        default_asset_owner: str = "owner",
    ) -> None:
        self._conninfo = conninfo
        self._sidecar_root = sidecar_root
        self._default_asset_owner = default_asset_owner

    def _export_sidecar(self, asset: CataloguedAsset) -> bool:
        if self._sidecar_root is None:
            return False
        try:
            write_canonical_sidecar(asset, self._sidecar_root)
        except (OSError, TypeError, ValueError) as error:
            logger.exception(
                "Vault Master could not export sidecar for asset %s",
                asset.id,
            )
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, action, detail, succeeded
                        )
                        VALUES (
                            %s, 'sidecar_export_failed', %s, FALSE
                        )
                        """,
                        (uuid4(), f"Asset {asset.id}: {error}"),
                    )
            return False
        return True

    def reconcile_sidecars(self) -> SidecarReconciliation:
        if self._sidecar_root is None:
            return SidecarReconciliation(0, 0, 0, 0)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id, asset.asset_type, asset.display_title,
                        asset.captured_on, asset.location, asset.metadata,
                        asset.metadata_provenance, asset.detected_metadata,
                        asset.imported_metadata, asset.user_overrides,
                        asset.effective_metadata, asset.owner_username, asset.owner_user_id,
                        asset.origin_vault_id, asset.visibility, asset.shared_with, file.vault_path,
                        file.filename, file.size_bytes, file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE file.file_role = 'primary'
                    ORDER BY asset.id
                    """
                )
                assets = [
                    _catalogued_asset_from_row(row)
                    for row in cursor.fetchall()
                ]
        current = repaired = failed = 0
        for asset in assets:
            if canonical_sidecar_is_current(asset, self._sidecar_root):
                current += 1
            elif self._export_sidecar(asset):
                repaired += 1
            else:
                failed += 1
        if repaired or failed:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, action, detail, succeeded
                        )
                        VALUES (
                            %s, 'sidecars_reconciled', %s, %s
                        )
                        """,
                        (
                            uuid4(),
                            (
                                f"{len(assets)} checked; {repaired} "
                                f"repaired; {failed} failed"
                            ),
                            failed == 0,
                        ),
                    )
        return SidecarReconciliation(
            checked=len(assets),
            current=current,
            repaired=repaired,
            failed=failed,
        )

    def restore_catalogued_asset(
        self,
        asset: CataloguedAsset,
        username: str,
    ) -> CataloguedAsset:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    owner_user_id = asset.owner_user_id or self._resolve_owner_user_id(
                        cursor, asset.owner_username
                    )
                    asset = replace(
                        asset,
                        owner_user_id=owner_user_id,
                        origin_vault_id=(
                            asset.origin_vault_id or self._local_vault_id(cursor)
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT 1
                        FROM vault_assets
                        WHERE id = %s
                        UNION ALL
                        SELECT 1
                        FROM vault_files
                        WHERE vault_path = %s
                        LIMIT 1
                        """,
                        (asset.id, asset.vault_path),
                    )
                    if cursor.fetchone() is not None:
                        raise ValueError(
                            "The asset identity or Vault path is already assigned"
                        )
                    cursor.execute(
                        """
                        INSERT INTO vault_assets (
                            id, asset_type, display_title, captured_on,
                            location, metadata, metadata_provenance,
                            detected_metadata, imported_metadata,
                            user_overrides, effective_metadata,
                            owner_username, owner_user_id, origin_vault_id,
                            visibility, shared_with
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            asset.id,
                            asset.asset_type,
                            asset.display_title,
                            asset.captured_on,
                            asset.location,
                            Jsonb(asset.metadata),
                            Jsonb(asset.metadata_provenance),
                            Jsonb(asset.detected_metadata),
                            Jsonb(asset.imported_metadata),
                            Jsonb(asset.user_overrides),
                            Jsonb(asset.effective_metadata),
                            asset.owner_username,
                            owner_user_id,
                            asset.origin_vault_id,
                            asset.visibility,
                            Jsonb(list(asset.shared_with)),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO vault_files (
                            id, asset_id, vault_path, filename,
                            size_bytes, mime_type, sha256
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            asset.id,
                            asset.vault_path,
                            asset.filename,
                            asset.size_bytes,
                            asset.mime_type,
                            asset.sha256,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, action, username, detail, succeeded
                        )
                        VALUES (
                            %s, 'sidecar_restored', %s, %s, TRUE
                        )
                        """,
                        (
                            uuid4(),
                            username,
                            (
                                f"Restored {asset.vault_path} from "
                                "canonical sidecar"
                            ),
                        ),
                    )
        except psycopg.errors.UniqueViolation as error:
            raise ValueError(
                "The asset identity or Vault path is already assigned"
            ) from error
        self._export_sidecar(asset)
        return asset

    def record_sidecar_restore_failure(
        self,
        asset_id: UUID,
        username: str,
        detail: str,
    ) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO vault_master_activity (
                        id, action, username, detail, succeeded
                    )
                    VALUES (
                        %s, 'sidecar_restore_failed', %s, %s, FALSE
                    )
                    """,
                    (
                        uuid4(),
                        username,
                        f"Recovery refused for {asset_id}: {detail}",
                    ),
                )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    @staticmethod
    def _local_vault_id(cursor: psycopg.Cursor) -> UUID:
        cursor.execute("SELECT vault_id FROM vaults WHERE is_local = TRUE")
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                "Vault identity initialization requires exactly one local Vault"
            )
        return UUID(str(rows[0]["vault_id"]))

    def get_local_vault_id(self) -> UUID:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                return self._local_vault_id(cursor)

    @staticmethod
    def _resolve_owner_user_id(
        cursor: psycopg.Cursor,
        owner_username: str,
    ) -> UUID:
        cursor.execute(
            "SELECT user_id FROM auth_accounts WHERE username = %s",
            (owner_username,),
        )
        row = cursor.fetchone()
        if row is None or row.get("user_id") is None:
            raise RuntimeError(
                "Asset ownership migration could not resolve account "
                f"{owner_username!r}"
            )
        return UUID(str(row["user_id"]))

    @staticmethod
    def _visible_asset_ids_for_username(
        cursor: psycopg.Cursor,
        username: str,
        asset_ids: list[UUID],
    ) -> set[UUID]:
        user_id = active_user_id(cursor, username)
        if user_id is None:
            return set()
        return visible_asset_ids(cursor, user_id, asset_ids)

    def _publish_catalogued_asset(
        self,
        cursor: psycopg.Cursor,
        item: ImportItem,
        vault_path: str,
        *,
        preserve_existing_metadata: bool,
    ) -> None:
        if item.owner_user_id is None:
            raise RuntimeError(
                "Arrival Hall publication requires a resolved asset owner identity"
            )
        local_vault_id = self._local_vault_id(cursor)
        cursor.execute(
            """
            SELECT
                asset.*,
                file.asset_id,
                file.vault_path,
                file.filename,
                file.size_bytes,
                file.mime_type,
                file.sha256
            FROM vault_files AS file
            JOIN vault_assets AS asset ON asset.id = file.asset_id
            WHERE file.vault_path = %s
            """,
            (vault_path,),
        )
        existing_file = cursor.fetchone()
        previous_vault_path: str | None = None
        if existing_file is None and preserve_existing_metadata:
            cursor.execute(
                """
                SELECT
                    asset.*,
                    file.id AS file_id,
                    file.asset_id,
                    file.vault_path,
                    file.filename,
                    file.size_bytes,
                    file.mime_type,
                    file.sha256
                FROM vault_files AS file
                JOIN vault_assets AS asset ON asset.id = file.asset_id
                WHERE file.sha256 = %s
                ORDER BY file.updated_at DESC
                LIMIT 2
                """,
                (item.sha256,),
            )
            checksum_matches = cursor.fetchall()
            if len(checksum_matches) == 1:
                existing_file = checksum_matches[0]
                previous_vault_path = str(existing_file["vault_path"])
        asset_id = (
            existing_file["asset_id"] if existing_file else uuid4()
        )
        if not (existing_file and preserve_existing_metadata):
            display_title, captured_on, location, provenance = (
                effective_asset_metadata(item)
            )
            (
                detected_metadata,
                imported_metadata,
                user_overrides,
                effective_metadata,
            ) = canonical_asset_metadata_layers(item)
            cursor.execute(
                """
                SELECT to_regclass('public.vault_ingestion_ai_evidence') AS relation
                """
            )
            evidence_relation = cursor.fetchone()
            if evidence_relation and evidence_relation["relation"]:
                cursor.execute(
                    """
                    SELECT evidence.*
                    FROM vault_ingestion_ai_evidence AS evidence
                    WHERE evidence.item_id = %s
                    ORDER BY evidence.created_at DESC
                    LIMIT 1
                    """,
                    (item.id,),
                )
                evidence = portable_ingestion_evidence(cursor.fetchone())
                if evidence:
                    detected_metadata["ingestion_evidence"] = evidence
                    effective_metadata["ingestion_evidence"] = evidence
                    provenance["ingestion_evidence"] = "local_ai_reviewed"
            asset_type = item.proposed_category
            if not asset_type:
                location_result = inventory_catalogue_location(
                    item.source_path
                )
                asset_type = (
                    location_result[0]
                    if location_result
                    else "Uncategorised"
                )
            cursor.execute(
                """
                INSERT INTO vault_assets (
                    id, asset_type, display_title, captured_on,
                    location, metadata, metadata_provenance,
                    detected_metadata, imported_metadata,
                    user_overrides, effective_metadata,
                    owner_username, owner_user_id, origin_vault_id,
                    visibility, shared_with
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    asset_type = EXCLUDED.asset_type,
                    display_title = EXCLUDED.display_title,
                    captured_on = EXCLUDED.captured_on,
                    location = EXCLUDED.location,
                    metadata = EXCLUDED.metadata,
                    metadata_provenance = EXCLUDED.metadata_provenance,
                    detected_metadata = EXCLUDED.detected_metadata,
                    imported_metadata = EXCLUDED.imported_metadata,
                    user_overrides = EXCLUDED.user_overrides,
                    effective_metadata = EXCLUDED.effective_metadata,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    asset_id,
                    asset_type,
                    display_title,
                    captured_on,
                    location,
                    Jsonb(detected_metadata),
                    Jsonb(provenance),
                    Jsonb(detected_metadata),
                    Jsonb(imported_metadata),
                    Jsonb(user_overrides),
                    Jsonb(effective_metadata),
                    (
                        str(existing_file["owner_username"])
                        if existing_file
                        else item.owner_username
                    ),
                    (
                        existing_file["owner_user_id"]
                        if existing_file
                        else item.owner_user_id
                    ),
                    (
                        existing_file["origin_vault_id"]
                        if existing_file
                        else local_vault_id
                    ),
                    (
                        str(existing_file["visibility"])
                        if existing_file
                        else (
                            VAULT_WIDE_ASSET_VISIBILITY
                            if item.proposed_category in {"Movies", "TV Shows"} and item.publication_audience != "private"
                            else PRIVATE_ASSET_VISIBILITY
                        )
                    ),
                    Jsonb(
                        list(existing_file["shared_with"])
                        if existing_file
                        else []
                    ),
                ),
            )
        else:
            refreshed = refresh_catalogued_asset_detection(
                _catalogued_asset_from_row(existing_file),
                item,
            )
            cursor.execute(
                """
                UPDATE vault_assets
                SET asset_type = %s,
                    display_title = %s,
                    captured_on = %s,
                    location = %s,
                    metadata = %s,
                    metadata_provenance = %s,
                    detected_metadata = %s,
                    user_overrides = %s,
                    effective_metadata = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    item.proposed_category or refreshed.asset_type,
                    refreshed.display_title,
                    refreshed.captured_on,
                    refreshed.location,
                    Jsonb(refreshed.metadata),
                    Jsonb(refreshed.metadata_provenance),
                    Jsonb(refreshed.detected_metadata),
                    Jsonb(refreshed.user_overrides),
                    Jsonb(refreshed.effective_metadata),
                    asset_id,
                ),
            )
        if previous_vault_path:
            cursor.execute(
                """
                UPDATE vault_files
                SET vault_path = %s,
                    filename = %s,
                    size_bytes = %s,
                    mime_type = %s,
                    sha256 = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE asset_id = %s AND vault_path = %s
                """,
                (
                    vault_path,
                    Path(vault_path).name,
                    item.size_bytes,
                    item.mime_type,
                    item.sha256,
                    asset_id,
                    previous_vault_path,
                ),
            )
            cursor.execute(
                """
                INSERT INTO vault_asset_history (
                    id, asset_id, action, username,
                    previous_values, current_values
                )
                VALUES (%s, %s, 'section_moved', %s, %s, %s)
                """,
                (
                    uuid4(),
                    asset_id,
                    self._default_asset_owner,
                    Jsonb({"vault_path": previous_vault_path}),
                    Jsonb({"vault_path": vault_path}),
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO vault_files (
                    id, asset_id, vault_path, filename,
                    size_bytes, mime_type, sha256
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (vault_path) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    size_bytes = EXCLUDED.size_bytes,
                    mime_type = EXCLUDED.mime_type,
                    sha256 = EXCLUDED.sha256,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    uuid4(),
                    asset_id,
                    vault_path,
                    Path(vault_path).name,
                    item.size_bytes,
                    item.mime_type,
                    item.sha256,
                ),
            )

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vaults (
                        vault_id UUID PRIMARY KEY,
                        is_local BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    "SELECT vault_id FROM vaults WHERE is_local = TRUE"
                )
                local_vaults = cursor.fetchall()
                if len(local_vaults) > 1:
                    raise RuntimeError(
                        "Vault identity initialization found multiple local Vaults"
                    )
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS vaults_one_local_idx
                    ON vaults (is_local) WHERE is_local = TRUE
                    """
                )
                if not local_vaults:
                    cursor.execute(
                        "INSERT INTO vaults (vault_id, is_local) "
                        "VALUES (%s, TRUE) ON CONFLICT DO NOTHING",
                        (uuid4(),),
                    )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_master_batches (
                        id UUID PRIMARY KEY,
                        source_kind TEXT NOT NULL
                            CHECK (source_kind IN ('incoming', 'inventory')),
                        source_root TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK (status IN (
                                'queued', 'scanning', 'completed', 'failed'
                            )),
                        item_count INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMPTZ
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_batches
                    DROP CONSTRAINT IF EXISTS
                        vault_master_batches_status_check
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_batches
                    ADD CONSTRAINT vault_master_batches_status_check
                    CHECK (status IN (
                        'queued', 'scanning', 'completed', 'failed'
                    ))
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_master_items (
                        id UUID PRIMARY KEY,
                        batch_id UUID NOT NULL
                            REFERENCES vault_master_batches(id),
                        source_kind TEXT NOT NULL
                            CHECK (source_kind IN ('incoming', 'inventory')),
                        source_path TEXT NOT NULL UNIQUE,
                        relative_path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
                        mime_type TEXT NOT NULL,
                        modified_at TIMESTAMPTZ NOT NULL,
                        sha256 CHAR(64) NOT NULL,
                        state TEXT NOT NULL
                            CHECK (state IN (
                                'inventoried', 'needs_review',
                                'approved', 'rejected', 'moved', 'move_failed',
                                'move_queued', 'moving', 'theatre_promotion_pending',
                                'duplicate_kept', 'duplicate_removed',
                                'duplicate_remove_failed', 'arrival_removed'
                            )),
                        duplicate_of_id UUID
                            REFERENCES vault_master_items(id),
                        proposed_category TEXT,
                        proposed_destination TEXT,
                        proposal_reason TEXT,
                        proposal_confidence TEXT
                            CHECK (
                                proposal_confidence IS NULL
                                OR proposal_confidence IN (
                                'low', 'medium', 'high'
                            )
                        ),
                        publication_audience TEXT
                            CHECK (publication_audience IN ('vault-wide', 'private')),
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        metadata_overrides JSONB NOT NULL
                            DEFAULT '{}'::jsonb,
                        owner_username TEXT NOT NULL,
                        owner_user_id UUID,
                        discovered_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_items
                    ADD COLUMN IF NOT EXISTS proposed_category TEXT,
                    ADD COLUMN IF NOT EXISTS proposed_destination TEXT,
                    ADD COLUMN IF NOT EXISTS proposal_reason TEXT,
                    ADD COLUMN IF NOT EXISTS proposal_confidence TEXT,
                    ADD COLUMN IF NOT EXISTS publication_audience TEXT
                        CHECK (publication_audience IN ('vault-wide', 'private')),
                    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL
                        DEFAULT '{}'::jsonb,
                    ADD COLUMN IF NOT EXISTS metadata_overrides JSONB NOT NULL
                        DEFAULT '{}'::jsonb,
                    ADD COLUMN IF NOT EXISTS owner_username TEXT,
                    ADD COLUMN IF NOT EXISTS owner_user_id UUID
                    """
                )
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM vault_master_items
                    WHERE owner_username IS NULL OR btrim(owner_username) = ''
                    """,
                )
                if int(cursor.fetchone()["count"]):
                    raise RuntimeError("Arrival Hall ownership migration found a missing owner username")
                cursor.execute(
                    """
                    UPDATE vault_master_items AS item
                    SET owner_user_id = account.user_id
                    FROM auth_accounts AS account
                    WHERE item.owner_username = account.username
                      AND item.owner_user_id IS NULL
                    """
                )
                cursor.execute("SELECT COUNT(*) FROM vault_master_items WHERE owner_user_id IS NULL")
                if int(cursor.fetchone()["count"]):
                    raise RuntimeError("Arrival Hall ownership migration found an unknown account")
                cursor.execute(
                    """
                    ALTER TABLE vault_master_items
                    ALTER COLUMN owner_username SET NOT NULL
                    """
                )
                cursor.execute("ALTER TABLE vault_master_items ALTER COLUMN owner_user_id SET NOT NULL")
                cursor.execute("ALTER TABLE vault_master_items DROP CONSTRAINT IF EXISTS vault_master_items_owner_user_id_fkey")
                cursor.execute("ALTER TABLE vault_master_items ADD CONSTRAINT vault_master_items_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES auth_accounts(user_id)")
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS vault_master_items_owner_user_id_idx "
                    "ON vault_master_items(owner_user_id)"
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_items
                    DROP CONSTRAINT IF EXISTS vault_master_items_state_check
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_items
                    ADD CONSTRAINT vault_master_items_state_check
                    CHECK (state IN (
                        'inventoried', 'needs_review',
                        'approved', 'rejected', 'moved', 'move_failed',
                        'move_queued', 'moving', 'theatre_promotion_pending',
                        'duplicate_kept', 'duplicate_removed',
                        'duplicate_remove_failed', 'arrival_removed'
                    ))
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        vault_master_items_sha256_idx
                    ON vault_master_items (sha256)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_assets (
                        id UUID PRIMARY KEY,
                        asset_type TEXT NOT NULL,
                        display_title TEXT NOT NULL,
                        captured_on DATE,
                        location TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        metadata_provenance JSONB NOT NULL
                            DEFAULT '{}'::jsonb,
                        detected_metadata JSONB NOT NULL
                            DEFAULT '{}'::jsonb,
                        imported_metadata JSONB NOT NULL
                            DEFAULT '{}'::jsonb,
                        user_overrides JSONB NOT NULL
                            DEFAULT '{}'::jsonb,
                        effective_metadata JSONB NOT NULL
                            DEFAULT '{}'::jsonb,
                        owner_username TEXT NOT NULL,
                        owner_user_id UUID,
                        origin_vault_id UUID,
                        visibility TEXT NOT NULL DEFAULT 'private',
                        shared_with JSONB NOT NULL DEFAULT '[]'::jsonb,
                        lifecycle_state TEXT NOT NULL DEFAULT 'active',
                        CONSTRAINT vault_assets_visibility_check
                            CHECK (visibility IN ('private', 'shared', 'vault-wide')),
                        CONSTRAINT vault_assets_lifecycle_state_check
                            CHECK (lifecycle_state IN ('active', 'hidden')),
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_assets
                    ADD COLUMN IF NOT EXISTS detected_metadata JSONB NOT NULL
                        DEFAULT '{}'::jsonb,
                    ADD COLUMN IF NOT EXISTS imported_metadata JSONB NOT NULL
                        DEFAULT '{}'::jsonb,
                    ADD COLUMN IF NOT EXISTS user_overrides JSONB NOT NULL
                        DEFAULT '{}'::jsonb,
                    ADD COLUMN IF NOT EXISTS effective_metadata JSONB NOT NULL
                        DEFAULT '{}'::jsonb
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_assets
                    ADD COLUMN IF NOT EXISTS owner_username TEXT,
                    ADD COLUMN IF NOT EXISTS owner_user_id UUID,
                    ADD COLUMN IF NOT EXISTS origin_vault_id UUID,
                    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL
                        DEFAULT 'private',
                    ADD COLUMN IF NOT EXISTS shared_with JSONB NOT NULL
                        DEFAULT '[]'::jsonb,
                    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL
                        DEFAULT 'active'
                    """
                )
                cursor.execute(
                    "ALTER TABLE vault_assets DROP CONSTRAINT IF EXISTS vault_assets_lifecycle_state_check"
                )
                cursor.execute(
                    "ALTER TABLE vault_assets ADD CONSTRAINT vault_assets_lifecycle_state_check "
                    "CHECK (lifecycle_state IN ('active', 'hidden'))"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS vault_assets_lifecycle_state_idx "
                    "ON vault_assets(lifecycle_state)"
                )
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM vault_assets
                    WHERE owner_username IS NULL OR btrim(owner_username) = ''
                    """,
                )
                if int(cursor.fetchone()["count"]):
                    raise RuntimeError("Asset ownership migration found a missing owner username")
                cursor.execute(
                    """
                    UPDATE vault_assets AS asset
                    SET owner_user_id = account.user_id
                    FROM auth_accounts AS account
                    WHERE asset.owner_username = account.username
                      AND asset.owner_user_id IS NULL
                    """
                )
                cursor.execute("SELECT COUNT(*) FROM vault_assets WHERE owner_user_id IS NULL")
                if int(cursor.fetchone()["count"]):
                    raise RuntimeError("Asset ownership migration found an unknown account")
                cursor.execute(
                    """
                    ALTER TABLE vault_assets
                    ALTER COLUMN owner_username SET NOT NULL
                    """
                )
                cursor.execute("ALTER TABLE vault_assets ALTER COLUMN owner_user_id SET NOT NULL")
                cursor.execute("ALTER TABLE vault_assets DROP CONSTRAINT IF EXISTS vault_assets_owner_user_id_fkey")
                cursor.execute("ALTER TABLE vault_assets ADD CONSTRAINT vault_assets_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES auth_accounts(user_id)")
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS vault_assets_owner_user_id_idx "
                    "ON vault_assets(owner_user_id)"
                )
                local_vault_id = self._local_vault_id(cursor)
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET origin_vault_id = %s
                    WHERE origin_vault_id IS NULL
                    """,
                    (local_vault_id,),
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM vault_assets WHERE origin_vault_id IS NULL"
                )
                if int(cursor.fetchone()["count"]):
                    raise RuntimeError(
                        "Asset provenance migration found a missing origin Vault"
                    )
                cursor.execute(
                    "ALTER TABLE vault_assets ALTER COLUMN origin_vault_id SET NOT NULL"
                )
                cursor.execute(
                    "ALTER TABLE vault_assets DROP CONSTRAINT IF EXISTS "
                    "vault_assets_origin_vault_id_fkey"
                )
                cursor.execute(
                    "ALTER TABLE vault_assets ADD CONSTRAINT "
                    "vault_assets_origin_vault_id_fkey FOREIGN KEY "
                    "(origin_vault_id) REFERENCES vaults(vault_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS vault_assets_origin_vault_id_idx "
                    "ON vault_assets(origin_vault_id)"
                )
                initialize_share_grants(cursor)
                # Federation schema is established only by this controlled
                # bootstrap; request-time authorization performs no DDL.
                initialize_federation(cursor)
                cursor.execute(
                    """
                    ALTER TABLE vault_assets
                    DROP CONSTRAINT IF EXISTS vault_assets_visibility_check,
                    ADD CONSTRAINT vault_assets_visibility_check
                        CHECK (visibility IN ('private', 'shared', 'vault-wide'))
                    """
                )
                # Legacy Movies were published before audience was an explicit
                # contract.  Private was then the schema default, not evidence
                # of an intentional restriction; only explicit shared policies
                # remain unchanged.
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET visibility = 'vault-wide', updated_at = CURRENT_TIMESTAMP
                    WHERE asset_type IN ('Movie', 'Movies')
                      AND visibility = 'private'
                    """
                )
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET detected_metadata = metadata
                    WHERE detected_metadata = '{}'::jsonb
                      AND metadata <> '{}'::jsonb
                    """
                )
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET user_overrides =
                        CASE
                            WHEN metadata_provenance->>'display_title'
                                = 'user_override'
                            THEN jsonb_build_object(
                                'display_title', display_title
                            )
                            ELSE '{}'::jsonb
                        END
                        || CASE
                            WHEN metadata_provenance->>'captured_on'
                                = 'user_override'
                                 AND captured_on IS NOT NULL
                            THEN jsonb_build_object(
                                'captured_on', captured_on
                            )
                            ELSE '{}'::jsonb
                        END
                        || CASE
                            WHEN metadata_provenance->>'location'
                                = 'user_override'
                                 AND location IS NOT NULL
                            THEN jsonb_build_object('location', location)
                            ELSE '{}'::jsonb
                        END
                    WHERE user_overrides = '{}'::jsonb
                    """
                )
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET effective_metadata =
                        detected_metadata
                        || imported_metadata
                        || user_overrides
                        || jsonb_strip_nulls(jsonb_build_object(
                            'display_title', display_title,
                            'captured_on', captured_on,
                            'location', location
                        ))
                    WHERE effective_metadata = '{}'::jsonb
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_files (
                        id UUID PRIMARY KEY,
                        asset_id UUID NOT NULL
                            REFERENCES vault_assets(id),
                        vault_path TEXT NOT NULL UNIQUE,
                        filename TEXT NOT NULL,
                        size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
                        mime_type TEXT NOT NULL,
                        sha256 CHAR(64) NOT NULL,
                        file_role TEXT NOT NULL DEFAULT 'primary',
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS vault_files_asset_id_idx
                    ON vault_files (asset_id)
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS vault_files_sha256_idx
                    ON vault_files (sha256)
                    """
                )
                # Storage placement is deliberately separate from the stable
                # logical Vault path.  A file remains independently readable
                # on its slot; this table is the authoritative routing record.
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_storage_slots (
                        slot_id TEXT PRIMARY KEY
                            CHECK (slot_id ~ '^PV-DISK-[0-9]{3,}$'),
                        state TEXT NOT NULL CHECK (state IN ('active', 'missing', 'retired')),
                        commissioned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        retired_at TIMESTAMPTZ,
                        hardware JSONB NOT NULL DEFAULT '{}'::jsonb,
                        assigned_areas JSONB NOT NULL DEFAULT '[]'::jsonb,
                        CHECK ((state = 'retired') = (retired_at IS NOT NULL))
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_file_storage_placements (
                        file_id UUID PRIMARY KEY REFERENCES vault_files(id) ON DELETE RESTRICT,
                        slot_id TEXT NOT NULL REFERENCES vault_storage_slots(slot_id),
                        relative_path TEXT NOT NULL
                            CHECK (relative_path <> '' AND relative_path !~ '(^/|(^|/)\\.\\.?(/|$))'),
                        assigned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        assigned_by TEXT NOT NULL,
                        placement_reason TEXT NOT NULL,
                        UNIQUE (slot_id, relative_path)
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS vault_file_storage_placements_slot_idx "
                    "ON vault_file_storage_placements(slot_id)"
                )
                # A commissioned slot is a durable logical identity.  Hardware
                # replacements append immutable evidence here rather than
                # allocating a new slot or rewriting canonical placements.
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_storage_slot_hardware_history (
                        id UUID PRIMARY KEY,
                        slot_id TEXT NOT NULL REFERENCES vault_storage_slots(slot_id),
                        operation_id UUID NOT NULL UNIQUE,
                        previous_hardware JSONB NOT NULL,
                        replacement_hardware JSONB NOT NULL,
                        receipt_sha256 CHAR(64) NOT NULL,
                        recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS vault_storage_slot_hardware_history_slot_idx "
                    "ON vault_storage_slot_hardware_history(slot_id, recorded_at DESC)"
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_arrival_theatre_promotions (
                        item_id UUID PRIMARY KEY REFERENCES vault_master_items(id) ON DELETE RESTRICT,
                        request_id UUID NOT NULL UNIQUE,
                        asset_id UUID NOT NULL UNIQUE REFERENCES vault_assets(id) ON DELETE RESTRICT,
                        file_id UUID NOT NULL UNIQUE REFERENCES vault_files(id) ON DELETE RESTRICT,
                        slot_id TEXT NOT NULL REFERENCES vault_storage_slots(slot_id),
                        relative_path TEXT NOT NULL,
                        published_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_arrival_managed_publications (
                        item_id UUID PRIMARY KEY REFERENCES vault_master_items(id) ON DELETE RESTRICT,
                        request_id UUID NOT NULL UNIQUE,
                        owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id) ON DELETE RESTRICT,
                        logical_destination TEXT NOT NULL UNIQUE,
                        logical_area TEXT NOT NULL,
                        asset_id UUID NOT NULL UNIQUE REFERENCES vault_assets(id) ON DELETE RESTRICT,
                        file_id UUID NOT NULL UNIQUE REFERENCES vault_files(id) ON DELETE RESTRICT,
                        slot_id TEXT NOT NULL REFERENCES vault_storage_slots(slot_id),
                        relative_path TEXT NOT NULL,
                        published_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_asset_relationships (
                        first_asset_id UUID NOT NULL REFERENCES vault_assets(id),
                        second_asset_id UUID NOT NULL REFERENCES vault_assets(id),
                        relationship_type TEXT NOT NULL CHECK (
                            relationship_type IN (
                                'duplicate', 'alternate_version', 'related_file'
                            )
                        ),
                        confidence TEXT NOT NULL CHECK (
                            confidence IN ('certain', 'high', 'medium', 'low')
                        ),
                        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_by TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (first_asset_id, second_asset_id),
                        CHECK (first_asset_id <> second_asset_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        vault_asset_relationships_second_asset_idx
                    ON vault_asset_relationships (second_asset_id)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_asset_history (
                        id UUID PRIMARY KEY,
                        asset_id UUID NOT NULL
                            REFERENCES vault_assets(id),
                        action TEXT NOT NULL,
                        username TEXT NOT NULL,
                        previous_values JSONB NOT NULL,
                        current_values JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        vault_asset_history_asset_id_idx
                    ON vault_asset_history (asset_id, created_at DESC)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_asset_deletions (
                        asset_id UUID PRIMARY KEY
                            REFERENCES vault_assets(id),
                        vault_path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
                        mime_type TEXT NOT NULL,
                        sha256 CHAR(64) NOT NULL,
                        deleted_by TEXT NOT NULL,
                        deleted_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_master_activity (
                        id UUID PRIMARY KEY,
                        activity_sequence BIGSERIAL NOT NULL,
                        batch_id UUID REFERENCES vault_master_batches(id),
                        item_id UUID REFERENCES vault_master_items(id),
                        action TEXT NOT NULL,
                        username TEXT,
                        detail TEXT NOT NULL DEFAULT '',
                        succeeded BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_activity
                    ADD COLUMN IF NOT EXISTS username TEXT
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_activity
                    ADD COLUMN IF NOT EXISTS activity_sequence BIGSERIAL
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_master_decisions (
                        id UUID PRIMARY KEY,
                        item_id UUID NOT NULL
                            REFERENCES vault_master_items(id),
                        decision TEXT NOT NULL
                            CHECK (decision IN (
                                'proposal_edited', 'metadata_edited',
                                'approved', 'rejected'
                            )),
                        username TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_decisions
                    DROP CONSTRAINT IF EXISTS
                        vault_master_decisions_decision_check
                    """
                )
                cursor.execute(
                    """
                    SELECT id, metadata_overrides
                    FROM vault_master_items
                    WHERE metadata_overrides <> '{}'::jsonb
                    """
                )
                for row in cursor.fetchall():
                    previous = dict(row["metadata_overrides"] or {})
                    typed = normalise_typed_metadata(previous)
                    if typed != previous:
                        cursor.execute(
                            """
                            UPDATE vault_master_items
                            SET metadata_overrides = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """,
                            (Jsonb(typed), row["id"]),
                        )
                cursor.execute(
                    """
                    SELECT id, detected_metadata, imported_metadata,
                           user_overrides, effective_metadata
                    FROM vault_assets
                    """
                )
                for row in cursor.fetchall():
                    layers = {
                        field: normalise_typed_metadata(dict(row[field] or {}))
                        for field in (
                            "detected_metadata",
                            "imported_metadata",
                            "user_overrides",
                            "effective_metadata",
                        )
                    }
                    if any(layers[field] != dict(row[field] or {}) for field in layers):
                        cursor.execute(
                            """
                            UPDATE vault_assets
                            SET detected_metadata = %s,
                                imported_metadata = %s,
                                user_overrides = %s,
                                effective_metadata = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """,
                            (
                                Jsonb(layers["detected_metadata"]),
                                Jsonb(layers["imported_metadata"]),
                                Jsonb(layers["user_overrides"]),
                                Jsonb(layers["effective_metadata"]),
                                row["id"],
                            ),
                        )
                cursor.execute(
                    """
                    ALTER TABLE vault_master_decisions
                    ADD CONSTRAINT vault_master_decisions_decision_check
                    CHECK (decision IN (
                        'proposal_edited', 'metadata_edited',
                        'approved', 'rejected'
                    ))
                    """
                )

    def migrate_source_root(
        self,
        source_kind: str,
        previous_root: str,
        current_root: str,
    ) -> int:
        previous_root = previous_root.rstrip("/")
        current_root = current_root.rstrip("/")
        if previous_root == current_root:
            return 0

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source.id
                    FROM vault_master_items AS source
                    JOIN vault_master_items AS existing
                      ON existing.source_path = (
                          %s || substring(
                              source.source_path
                              FROM length(%s) + 1
                          )
                      )
                     AND existing.id <> source.id
                    WHERE source.source_kind = %s
                      AND left(
                          source.source_path,
                          length(%s) + 1
                      ) = %s || '/'
                    LIMIT 1
                    """,
                    (
                        current_root,
                        previous_root,
                        source_kind,
                        previous_root,
                        previous_root,
                    ),
                )
                if cursor.fetchone():
                    raise ValueError(
                        "Arrival Hall path migration would collide with "
                        "an existing item"
                    )

                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET source_path = (
                            %s || substring(
                                source_path FROM length(%s) + 1
                            )
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE source_kind = %s
                      AND left(
                          source_path,
                          length(%s) + 1
                      ) = %s || '/'
                    """,
                    (
                        current_root,
                        previous_root,
                        source_kind,
                        previous_root,
                        previous_root,
                    ),
                )
                migrated_items = cursor.rowcount
                cursor.execute(
                    """
                    UPDATE vault_master_batches
                    SET source_root = CASE
                        WHEN source_root = %s THEN %s
                        ELSE %s || substring(
                            source_root FROM length(%s) + 1
                        )
                    END
                    WHERE source_kind = %s
                      AND (
                          source_root = %s
                          OR left(
                              source_root,
                              length(%s) + 1
                          ) = %s || '/'
                      )
                    """,
                    (
                        previous_root,
                        current_root,
                        current_root,
                        previous_root,
                        source_kind,
                        previous_root,
                        previous_root,
                        previous_root,
                    ),
                )

        return migrated_items

    def create_batch(self, source_kind: str, source_root: str) -> UUID:
        batch_id = uuid4()

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO vault_master_batches (
                        id, source_kind, source_root, status
                    )
                    VALUES (%s, %s, %s, 'queued')
                    """,
                    (batch_id, source_kind, source_root),
                )

        return batch_id

    @staticmethod
    def _to_item(row: dict[str, object]) -> ImportItem:
        return ImportItem(
            id=UUID(str(row["id"])),
            batch_id=UUID(str(row["batch_id"])),
            source_kind=str(row["source_kind"]),
            source_path=str(row["source_path"]),
            relative_path=str(row["relative_path"]),
            filename=str(row["filename"]),
            size_bytes=int(row["size_bytes"]),
            mime_type=str(row["mime_type"]),
            modified_at=row["modified_at"],
            sha256=str(row["sha256"]),
            state=str(row["state"]),
            duplicate_of_id=(
                UUID(str(row["duplicate_of_id"]))
                if row["duplicate_of_id"]
                else None
            ),
            proposed_category=(
                str(row["proposed_category"])
                if row.get("proposed_category")
                else None
            ),
            proposed_destination=(
                str(row["proposed_destination"])
                if row.get("proposed_destination")
                else None
            ),
            proposal_reason=(
                str(row["proposal_reason"])
                if row.get("proposal_reason")
                else None
            ),
            proposal_confidence=(
                str(row["proposal_confidence"])
                if row.get("proposal_confidence")
                else None
            ),
            metadata=dict(row.get("metadata") or {}),
            metadata_overrides=normalise_typed_metadata(
                dict(row.get("metadata_overrides") or {})
            ),
            publication_audience=(
                str(row["publication_audience"])
                if row.get("publication_audience")
                else None
            ),
            owner_username=str(
                row.get("owner_username") or self._default_asset_owner
            ),
            owner_user_id=(
                UUID(str(row["owner_user_id"]))
                if row.get("owner_user_id")
                else None
            ),
        )

    @staticmethod
    def _arrival_duplicate_candidate(
        cursor: object,
        sha256: str,
        source_kind: str,
        proposed_category: str | None,
        owner_user_id: UUID,
    ) -> UUID | None:
        """Find canonical duplicate authority without consulting Arrival history."""
        if source_kind != INCOMING_SOURCE:
            return None
        theatre = is_theatre_category(proposed_category)
        cursor.execute(
            """
            SELECT candidate.id
            FROM vault_master_items AS candidate
            WHERE candidate.source_kind = 'inventory'
              AND candidate.sha256 = %s
              AND (
                  (
                      %s
                      AND EXISTS (
                          SELECT 1
                          FROM vault_files AS file
                          JOIN vault_assets AS asset ON asset.id = file.asset_id
                          WHERE file.sha256 = candidate.sha256
                            AND asset.asset_type IN ('Movie', 'Movies', 'TV Show', 'TV Shows')
                      )
                  )
                  OR (
                      NOT %s
                      AND EXISTS (
                          SELECT 1
                          FROM vault_files AS file
                          JOIN vault_assets AS asset ON asset.id = file.asset_id
                          WHERE file.sha256 = candidate.sha256
                            AND asset.owner_user_id = %s
                            AND asset.asset_type NOT IN ('Movie', 'Movies', 'TV Show', 'TV Shows')
                      )
                      AND candidate.owner_user_id = %s
                  )
                  OR (
                      NOT %s
                      AND NOT EXISTS (
                          SELECT 1 FROM vault_files AS file
                          WHERE file.sha256 = candidate.sha256
                      )
                      AND candidate.owner_user_id = %s
                  )
              )
            ORDER BY candidate.discovered_at, candidate.id
            LIMIT 1
            """,
            (
                sha256,
                theatre,
                theatre,
                owner_user_id,
                owner_user_id,
                theatre,
                owner_user_id,
            ),
        )
        row = cursor.fetchone()
        return UUID(str(row["id"])) if row is not None else None

    def record_file(
        self,
        batch_id: UUID,
        source_kind: str,
        scanned_file: ScannedFile,
    ) -> ImportItem:
        sidecar_vault_path: str | None = None
        state = (
            "inventoried"
            if source_kind == INVENTORY_SOURCE
            else "needs_review"
        )
        proposal = (
            create_deterministic_proposal(scanned_file)
            if source_kind == INCOMING_SOURCE
            else (None, None, None, None)
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if scanned_file.owner_user_id is not None:
                    cursor.execute(
                        "SELECT username FROM auth_accounts WHERE user_id = %s",
                        (scanned_file.owner_user_id,),
                    )
                    owner_row = cursor.fetchone()
                    if owner_row is None or not owner_row.get("username"):
                        raise RuntimeError(
                            "Arrival Hall ownership could not resolve account UUID "
                            f"{scanned_file.owner_user_id!s}"
                        )
                    owner_username = str(owner_row["username"])
                    owner_user_id = scanned_file.owner_user_id
                else:
                    if source_kind == INCOMING_SOURCE and not scanned_file.owner_username:
                        raise RuntimeError(
                            "Arrival Hall scan refused a file without immutable owner identity"
                        )
                    owner_username = scanned_file.owner_username or self._default_asset_owner
                    owner_user_id = self._resolve_owner_user_id(cursor, owner_username)
                cursor.execute(
                    """
                    SELECT sha256, proposed_category, proposal_reason
                    FROM vault_master_items
                    WHERE source_path = %s
                    """,
                    (scanned_file.source_path,),
                )
                existing = cursor.fetchone()
                effective_category = proposal[0]
                if (
                    existing is not None
                    and str(existing["sha256"]) == scanned_file.sha256
                    and existing.get("proposal_reason") in {
                        "Category selected by the user.",
                        "Local image evidence suggests this destination.",
                    }
                ):
                    effective_category = existing.get("proposed_category")
                duplicate_id = self._arrival_duplicate_candidate(
                    cursor,
                    scanned_file.sha256,
                    source_kind,
                    effective_category,
                    owner_user_id,
                )
                cursor.execute(
                    """
                    INSERT INTO vault_master_items (
                        id, batch_id, source_kind, source_path,
                        relative_path, filename, size_bytes, mime_type,
                        modified_at, sha256, state, duplicate_of_id,
                        proposed_category, proposed_destination,
                        proposal_reason, proposal_confidence, metadata,
                        owner_username, owner_user_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (source_path) DO UPDATE SET
                        batch_id = EXCLUDED.batch_id,
                        source_kind = EXCLUDED.source_kind,
                        relative_path = EXCLUDED.relative_path,
                        filename = EXCLUDED.filename,
                        size_bytes = EXCLUDED.size_bytes,
                        mime_type = EXCLUDED.mime_type,
                        modified_at = EXCLUDED.modified_at,
                        sha256 = EXCLUDED.sha256,
                        state = CASE
                            WHEN vault_master_items.state IN (
                                'approved', 'rejected', 'move_failed',
                                'move_queued', 'moving', 'theatre_promotion_pending',
                                'duplicate_kept',
                                'duplicate_remove_failed'
                            )
                            THEN vault_master_items.state
                            ELSE EXCLUDED.state
                        END,
                        duplicate_of_id = EXCLUDED.duplicate_of_id,
                        proposed_category = CASE
                            WHEN vault_master_items.sha256 = EXCLUDED.sha256
                             AND vault_master_items.proposal_reason IN (
                                'Category selected by the user.',
                                'Local image evidence suggests this destination.'
                             )
                            THEN vault_master_items.proposed_category
                            ELSE EXCLUDED.proposed_category
                        END,
                        proposed_destination = CASE
                            WHEN vault_master_items.sha256 = EXCLUDED.sha256
                             AND vault_master_items.proposal_reason IN (
                                'Category selected by the user.',
                                'Local image evidence suggests this destination.'
                             )
                            THEN vault_master_items.proposed_destination
                            ELSE EXCLUDED.proposed_destination
                        END,
                        proposal_reason = CASE
                            WHEN vault_master_items.sha256 = EXCLUDED.sha256
                             AND vault_master_items.proposal_reason IN (
                                'Category selected by the user.',
                                'Local image evidence suggests this destination.'
                             )
                            THEN vault_master_items.proposal_reason
                            ELSE EXCLUDED.proposal_reason
                        END,
                        proposal_confidence = CASE
                            WHEN vault_master_items.sha256 = EXCLUDED.sha256
                             AND vault_master_items.proposal_reason IN (
                                'Category selected by the user.',
                                'Local image evidence suggests this destination.'
                             )
                            THEN vault_master_items.proposal_confidence
                            ELSE EXCLUDED.proposal_confidence
                        END,
                        metadata = EXCLUDED.metadata,
                        owner_username = EXCLUDED.owner_username,
                        owner_user_id = EXCLUDED.owner_user_id,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                    """,
                    (
                        uuid4(),
                        batch_id,
                        source_kind,
                        scanned_file.source_path,
                        scanned_file.relative_path,
                        scanned_file.filename,
                        scanned_file.size_bytes,
                        scanned_file.mime_type,
                        scanned_file.modified_at,
                        scanned_file.sha256,
                        state,
                        duplicate_id,
                        proposal[0],
                        proposal[1],
                        proposal[2],
                        proposal[3],
                        Jsonb(scanned_file.metadata),
                        owner_username,
                        owner_user_id,
                    ),
                )
                row = cursor.fetchone()
                if source_kind == INVENTORY_SOURCE:
                    item = self._to_item(row)
                    catalogue_location = inventory_catalogue_location(
                        item.source_path
                    )
                    if catalogue_location:
                        sidecar_vault_path = catalogue_location[1]
                        self._publish_catalogued_asset(
                            cursor,
                            item,
                            catalogue_location[1],
                            preserve_existing_metadata=True,
                        )
                cursor.execute(
                    """
                    INSERT INTO vault_master_activity (
                        id, batch_id, item_id, action
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        batch_id,
                        row["id"],
                        (
                            "file_inventoried"
                            if source_kind == INVENTORY_SOURCE
                            else "file_analysed"
                        ),
                    ),
                )

        item = self._to_item(row)
        if sidecar_vault_path:
            asset = self.get_catalogued_asset(sidecar_vault_path)
            if asset:
                self._export_sidecar(asset)
        return item

    def complete_batch(self, batch_id: UUID, item_count: int) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_master_batches
                    SET status = 'completed',
                        item_count = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (item_count, batch_id),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_master_activity (
                        id, batch_id, action, detail
                    )
                    VALUES (%s, %s, 'scan_completed', %s)
                    """,
                    (uuid4(), batch_id, f"{item_count} file(s) analysed"),
                )

    def fail_batch(self, batch_id: UUID, detail: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_master_batches
                    SET status = 'failed',
                        error = %s,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (detail, batch_id),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_master_activity (
                        id, batch_id, action, detail, succeeded
                    )
                    VALUES (%s, %s, 'scan_failed', %s, FALSE)
                    """,
                    (uuid4(), batch_id, detail),
                )

    def list_items(self) -> list[ImportItem]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM vault_master_items
                    ORDER BY discovered_at DESC
                    """
                )
                rows = cursor.fetchall()

        return [self._to_item(row) for row in rows]

    def claim_next_batch(self) -> tuple[UUID, str, str] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_master_batches
                    SET status = 'queued'
                    WHERE status = 'scanning'
                    """
                )
                cursor.execute(
                    """
                    SELECT id, source_kind, source_root
                    FROM vault_master_batches
                    WHERE status = 'queued'
                    ORDER BY
                        CASE WHEN source_kind = 'incoming' THEN 0 ELSE 1 END,
                        created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cursor.execute(
                    """
                    UPDATE vault_master_batches
                    SET status = 'scanning'
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                return (
                    UUID(str(row["id"])),
                    str(row["source_kind"]),
                    str(row["source_root"]),
                )

    def list_batches(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, source_kind, source_root, status,
                           item_count, error, created_at, completed_at
                    FROM vault_master_batches
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
                return list(cursor.fetchall())

    def find_active_batch(
        self,
        source_kind: str,
        source_root: str,
    ) -> UUID | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM vault_master_batches
                    WHERE source_kind = %s
                      AND source_root = %s
                      AND status IN ('queued', 'scanning')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (source_kind, source_root),
                )
                row = cursor.fetchone()
                return UUID(str(row["id"])) if row else None

    def list_activity(
        self,
        limit: int = 100,
        *,
        include_file_inventory: bool = True,
        include_file_analysis: bool = True,
        include_empty_scans: bool = True,
    ) -> list[VaultMasterActivity]:
        if limit < 1:
            return []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        activity.id,
                        activity.batch_id,
                        activity.item_id,
                        COALESCE(item.source_kind, batch.source_kind)
                            AS source_kind,
                        item.filename,
                        activity.action,
                        activity.username,
                        activity.detail,
                        activity.succeeded,
                        activity.created_at
                    FROM vault_master_activity AS activity
                    LEFT JOIN vault_master_batches AS batch
                      ON batch.id = activity.batch_id
                    LEFT JOIN vault_master_items AS item
                      ON item.id = activity.item_id
                    WHERE (%s OR activity.action <> 'file_inventoried')
                      AND (%s OR activity.action <> 'file_analysed')
                      AND (
                          %s
                          OR activity.action <> 'scan_completed'
                          OR batch.source_kind <> 'incoming'
                          OR batch.item_count <> 0
                      )
                    ORDER BY activity.activity_sequence DESC
                    LIMIT %s
                    """,
                    (
                        include_file_inventory,
                        include_file_analysis,
                        include_empty_scans,
                        limit,
                    ),
                )
                rows = cursor.fetchall()
        return [VaultMasterActivity(**row) for row in rows]

    def update_proposal(
        self,
        item_id: UUID,
        category: str,
        username: str,
        destination_subfolder: str | None = None,
        publication_audience: str | None = None,
    ) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT relative_path, filename
                    FROM vault_master_items
                    WHERE id = %s AND source_kind = 'incoming'
                    """,
                    (item_id,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    return None
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET proposed_category = %s,
                        proposed_destination = %s,
                        proposal_reason = 'Category selected by the user.',
                        proposal_confidence = 'high',
                        publication_audience = %s,
                        state = 'needs_review',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND source_kind = 'incoming'
                    RETURNING *
                    """,
                    (
                        category,
                        proposed_destination_path(
                            category,
                            str(existing["relative_path"]),
                            str(existing["filename"]),
                            destination_subfolder,
                        ),
                        publication_audience if category in {"Movies", "TV Shows"} else None,
                        item_id,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    updated = self._to_item(row)
                    duplicate_id = self._arrival_duplicate_candidate(
                        cursor,
                        updated.sha256,
                        updated.source_kind,
                        updated.proposed_category,
                        updated.owner_user_id,
                    ) if updated.owner_user_id is not None else None
                    cursor.execute(
                        "UPDATE vault_master_items SET duplicate_of_id = %s, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING *",
                        (duplicate_id, item_id),
                    )
                    row = cursor.fetchone()
                    cursor.execute(
                        """
                        INSERT INTO vault_master_decisions (
                            id, item_id, decision, username, detail
                        )
                        VALUES (
                            %s, %s, 'proposal_edited', %s, %s
                        )
                        """,
                        (uuid4(), item_id, username, category),
                    )
        return self._to_item(row) if row else None

    def apply_ai_proposal(
        self,
        item_id: UUID,
        category: str,
        reason: str,
        destination_subfolder: str | None = None,
        force: bool = False,
    ) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT relative_path, filename
                    FROM vault_master_items
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND state IN ('inventoried', 'needs_review')
                      AND COALESCE(proposal_reason, '') <>
                          'Category selected by the user.'
                      AND (
                          %s
                          OR proposal_confidence IN ('low', 'medium')
                          OR COALESCE(proposal_reason, '') =
                              'Hard-coded screenshot routing requires Archives/Screenshots.'
                      )
                    """,
                    (item_id, force),
                )
                existing = cursor.fetchone()
                if existing is None:
                    return None
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET proposed_category = %s,
                        proposed_destination = %s,
                        proposal_reason = %s,
                        state = 'needs_review',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND state IN ('inventoried', 'needs_review')
                      AND COALESCE(proposal_reason, '') <>
                          'Category selected by the user.'
                      AND (
                          %s
                          OR proposal_confidence IN ('low', 'medium')
                          OR COALESCE(proposal_reason, '') =
                              'Hard-coded screenshot routing requires Archives/Screenshots.'
                      )
                    RETURNING *
                    """,
                    (
                        category,
                        proposed_destination_path(
                            category,
                            str(existing["relative_path"]),
                            str(existing["filename"]),
                            destination_subfolder,
                        ),
                        reason,
                        item_id,
                        force,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    updated = self._to_item(row)
                    duplicate_id = self._arrival_duplicate_candidate(
                        cursor,
                        updated.sha256,
                        updated.source_kind,
                        updated.proposed_category,
                        updated.owner_user_id,
                    ) if updated.owner_user_id is not None else None
                    cursor.execute(
                        "UPDATE vault_master_items SET duplicate_of_id = %s, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING *",
                        (duplicate_id, item_id),
                    )
                    row = cursor.fetchone()
        return self._to_item(row) if row else None

    def _approve_progressed_movie_publication_set(
        self,
        cursor: object,
        item: ImportItem,
        all_items: list[ImportItem],
        username: str,
    ) -> tuple[ImportItem, tuple[UUID, ...]]:
        if item.state != "needs_review":
            raise ValueError("Only one reviewed Movie companion may join the set")
        raw_members = _movie_publication_set_members(item, all_items)
        if len(raw_members) <= 1:
            raise ValueError("Movie publication set has no companions")
        cursor.execute(
            """
            SELECT publication.*, file.vault_path, file.sha256,
                   file.size_bytes, asset.owner_user_id AS asset_owner_user_id,
                   asset.detected_metadata AS asset_detected_metadata
            FROM vault_arrival_managed_publications AS publication
            JOIN vault_files AS file ON file.id = publication.file_id
            JOIN vault_assets AS asset ON asset.id = publication.asset_id
            WHERE publication.item_id = ANY(%s)
            FOR UPDATE OF publication, file, asset
            """,
            ([candidate.id for candidate in raw_members],),
        )
        publications = {
            UUID(str(row["item_id"])): row for row in cursor.fetchall()
        }
        hydrated_items = [
            replace(
                candidate,
                metadata={
                    **dict(
                        publications[candidate.id].get(
                            "asset_detected_metadata"
                        )
                        or {}
                    ),
                    **candidate.metadata,
                },
            )
            if candidate.id in publications
            else candidate
            for candidate in all_items
        ]
        item = next(
            candidate for candidate in hydrated_items if candidate.id == item.id
        )
        plan = movie_publication_set_continuation_plan(item, hydrated_items)
        members = {
            candidate.id: candidate
            for candidate in hydrated_items
            if candidate.id in plan
        }
        if set(members) != set(plan):
            raise ValueError("Movie publication set evidence is incomplete")
        accepted_unpublished = {
            "approved",
            "move_failed",
            "move_queued",
            "moving",
            "theatre_promotion_pending",
        }
        reconciled: dict[UUID, ImportItem] = {}
        published_assets: dict[UUID, dict[str, object]] = {}
        for member_id, member in members.items():
            destination, provisional, evidence = plan[member_id]
            publication = publications.get(member_id)
            if publication is not None:
                if (
                    str(publication["asset_owner_user_id"])
                    != str(member.owner_user_id)
                    or str(publication["sha256"]) != member.sha256
                    or int(publication["size_bytes"]) != member.size_bytes
                    or str(publication["item_id"]) != str(member.id)
                    or str(publication["owner_user_id"])
                    != str(member.owner_user_id)
                    or str(publication["logical_destination"]) != destination
                    or str(publication["logical_area"]) != "Theatre / Movies"
                    or str(publication["relative_path"])
                    != destination.removeprefix("/vault/")
                    or str(publication["vault_path"]) != destination
                ):
                    raise ValueError(
                        "Published Movie set evidence does not match the reviewed set"
                    )
                published_assets[member_id] = dict(publication)
                next_state = "moved"
            else:
                if member_id != item.id and member.state not in accepted_unpublished:
                    raise ValueError(
                        "Exactly one reviewed Movie companion may join the progressed set"
                    )
                if member_id != item.id and member.proposed_destination != destination:
                    raise ValueError(
                        "Progressed Movie set destination does not match the reviewed set"
                    )
                next_state = "approved" if member_id == item.id else member.state
            metadata = dict(member.metadata)
            if provisional is not None:
                metadata["movie_identity_provisional"] = provisional
            metadata["movie_publication_set"] = evidence
            reconciled[member_id] = replace(
                member,
                state=next_state,
                proposed_destination=destination,
                metadata=metadata,
            )
        for member_id, updated in reconciled.items():
            previous = members[member_id]
            cursor.execute(
                """
                UPDATE vault_master_items
                SET state = %s, proposed_destination = %s,
                    metadata = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING *
                """,
                (
                    updated.state,
                    updated.proposed_destination,
                    Jsonb(updated.metadata),
                    member_id,
                ),
            )
            updated_row = cursor.fetchone()
            if updated_row is None:
                raise RuntimeError("Movie publication set changed during reconciliation")
            reconciled[member_id] = self._to_item(updated_row)
            publication = published_assets.get(member_id)
            if publication is not None:
                marker = updated.metadata["movie_publication_set"]
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET detected_metadata = detected_metadata || %s,
                        effective_metadata = effective_metadata || %s,
                        metadata_provenance = metadata_provenance || %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        Jsonb({"movie_publication_set": marker}),
                        Jsonb({"movie_publication_set": marker}),
                        Jsonb({"movie_publication_set": "detected"}),
                        publication["asset_id"],
                    ),
                )
                if previous.state != "moved":
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, batch_id, item_id, action, username,
                            detail, succeeded
                        ) VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                        """,
                        (
                            uuid4(),
                            updated.batch_id,
                            member_id,
                            "publication_state_reconciled",
                            "Arrival Hall managed publisher",
                            "Restored moved state from exact managed publication evidence",
                        ),
                    )
        approved = reconciled[item.id]
        cursor.execute(
            """
            INSERT INTO vault_master_decisions (id, item_id, decision, username)
            VALUES (%s, %s, 'approved', %s)
            """,
            (uuid4(), item.id, username),
        )
        cursor.execute(
            """
            INSERT INTO vault_master_activity (
                id, batch_id, item_id, action, username
            ) VALUES (%s, %s, %s, 'proposal_approved', %s)
            """,
            (uuid4(), approved.batch_id, approved.id, username),
        )
        return approved, tuple(
            UUID(str(publication["asset_id"]))
            for publication in published_assets.values()
        )

    def record_decision(
        self, item_id: UUID, decision: str, username: str
    ) -> ImportItem | None:
        result: ImportItem | None = None
        sidecar_asset_ids: tuple[UUID, ...] = ()
        continued_set = False
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM vault_master_items WHERE id = %s AND source_kind = 'incoming' FOR UPDATE",
                    (item_id,),
                )
                current_row = cursor.fetchone()
                if current_row is None:
                    return None
                current = self._to_item(current_row)
                destination = current.proposed_destination
                tv_set_evidence: dict[str, object] | None = None
                if (
                    decision == "approved"
                    and current.proposed_category == "Movies"
                    and MAKEMKV_TRACK_PATTERN.fullmatch(Path(current.filename).stem)
                ):
                    cursor.execute(
                        """
                        SELECT *
                        FROM vault_master_items
                        WHERE source_kind = 'incoming'
                          AND owner_user_id IS NOT DISTINCT FROM %s
                        FOR UPDATE
                        """,
                        (current.owner_user_id,),
                    )
                    owner_items = [
                        self._to_item(candidate)
                        for candidate in cursor.fetchall()
                    ]
                    try:
                        destination, provisional, set_evidence = (
                            movie_publication_set_destination(
                                current, owner_items
                            )
                        )
                    except ValueError as error:
                        if str(error) != (
                            "Movie publication set membership has already "
                            "progressed; this item cannot be approved independently"
                        ):
                            raise
                        result, sidecar_asset_ids = (
                            self._approve_progressed_movie_publication_set(
                                cursor, current, owner_items, username
                            )
                        )
                        continued_set = True
                elif decision == "approved" and current.proposed_category == "TV Shows":
                    cursor.execute(
                        """SELECT * FROM vault_master_items WHERE source_kind = 'incoming'
                           AND owner_user_id IS NOT DISTINCT FROM %s FOR UPDATE""",
                        (current.owner_user_id,),
                    )
                    owner_items = [self._to_item(candidate) for candidate in cursor.fetchall()]
                    destination, tv_set_evidence = tv_publication_set_destination(current, owner_items)
                    provisional = None
                    set_evidence = None
                else:
                    provisional = None
                    set_evidence = None
                if not continued_set:
                    metadata = dict(current.metadata)
                    if provisional is not None:
                        metadata["movie_identity_provisional"] = provisional
                    if decision == "approved" and set_evidence is not None:
                        metadata["movie_publication_set"] = set_evidence
                    elif decision == "approved":
                        metadata.pop("movie_publication_set", None)
                    if decision == "approved" and tv_set_evidence is not None:
                        metadata["tv_publication_set"] = tv_set_evidence
                    elif decision == "approved":
                        metadata.pop("tv_publication_set", None)
                    cursor.execute(
                        """
                        UPDATE vault_master_items
                        SET state = %s,
                            proposed_destination = %s,
                            metadata = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND source_kind = 'incoming'
                        RETURNING *
                        """,
                        (decision, destination, Jsonb(metadata), item_id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    cursor.execute(
                        """
                        INSERT INTO vault_master_decisions (
                            id, item_id, decision, username
                        )
                        VALUES (%s, %s, %s, %s)
                        """,
                        (uuid4(), item_id, decision, username),
                    )
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, batch_id, item_id, action, username
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            row["batch_id"],
                            item_id,
                            f"proposal_{decision}",
                            username,
                        ),
                    )
                    result = self._to_item(row)
        for asset_id in sidecar_asset_ids:
            asset = self.get_catalogued_asset_by_id(asset_id)
            if asset is not None:
                self._export_sidecar(asset)
        return result

    def update_metadata_overrides(
        self,
        item_id: UUID,
        changes: dict[str, object | None],
        username: str,
    ) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT metadata_overrides
                    FROM vault_master_items
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND state IN (
                          'needs_review', 'approved',
                          'rejected', 'move_failed'
                      )
                    FOR UPDATE
                    """,
                    (item_id,),
                )
                current = cursor.fetchone()
                if current is None:
                    return None
                previous = normalise_typed_metadata(
                    dict(current.get("metadata_overrides") or {})
                )
                updated = dict(previous)
                for name, value in changes.items():
                    if value is None:
                        updated.pop(name, None)
                    else:
                        updated[name] = value
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET metadata_overrides = %s,
                        state = 'needs_review',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND state IN (
                          'needs_review', 'approved',
                          'rejected', 'move_failed'
                      )
                    RETURNING *
                    """,
                    (Jsonb(updated), item_id),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO vault_master_decisions (
                        id, item_id, decision, username, detail
                    )
                    VALUES (%s, %s, 'metadata_edited', %s, %s)
                    """,
                    (
                        uuid4(),
                        item_id,
                        username,
                        json.dumps(
                            {
                                "previous": previous,
                                "updated": updated,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
        return self._to_item(row)

    def get_item(self, item_id: UUID) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM vault_master_items WHERE id = %s",
                    (item_id,),
                )
                row = cursor.fetchone()
        return self._to_item(row) if row else None

    def get_metadata_overrides(
        self, destination_paths: list[str]
    ) -> dict[str, dict[str, object]]:
        if not destination_paths:
            return {}
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (requested.path)
                        requested.path,
                        item.metadata_overrides
                    FROM unnest(%s::text[]) AS requested(path)
                    JOIN vault_master_activity AS activity
                      ON activity.action = 'file_moved'
                     AND activity.succeeded = TRUE
                     AND right(activity.detail, length(requested.path))
                         = requested.path
                    JOIN vault_master_items AS item
                      ON item.id = activity.item_id
                    WHERE item.metadata_overrides <> '{}'::jsonb
                    ORDER BY requested.path, activity.created_at DESC
                    """,
                    (destination_paths,),
                )
                rows = cursor.fetchall()
        return {
            str(row["path"]): normalise_typed_metadata(
                dict(row["metadata_overrides"] or {})
            )
            for row in rows
        }

    def get_catalogued_asset(
        self, vault_path: str
    ) -> CataloguedAsset | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        asset.lifecycle_state,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE file.vault_path = %s
                    """,
                    (vault_path,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        return _catalogued_asset_from_row(row)

    def get_catalogued_assets(
        self, vault_paths: list[str]
    ) -> dict[str, CataloguedAsset]:
        if not vault_paths:
            return {}
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        asset.lifecycle_state,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE file.vault_path = ANY(%s)
                    """,
                    (vault_paths,),
                )
                rows = cursor.fetchall()
        return {
            str(row["vault_path"]): _catalogued_asset_from_row(row)
            for row in rows
        }

    def get_visible_catalogued_assets(
        self, vault_paths: list[str], username: str
    ) -> dict[str, CataloguedAsset]:
        if not vault_paths:
            return {}
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        asset.lifecycle_state,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE file.vault_path = ANY(%s)
                    """,
                    (vault_paths,),
                )
                rows = cursor.fetchall()
                allowed = self._visible_asset_ids_for_username(
                    cursor, username, [UUID(str(row["id"])) for row in rows]
                )
        return {
            str(row["vault_path"]): _catalogued_asset_from_row(row)
            for row in rows
            if UUID(str(row["id"])) in allowed
        }

    def get_catalogued_asset_by_id(
        self, asset_id: UUID
    ) -> CataloguedAsset | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT file.vault_path
                    FROM vault_files AS file
                    WHERE file.asset_id = %s
                    ORDER BY
                        (file.file_role = 'primary') DESC,
                        file.created_at
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        return self.get_catalogued_asset(str(row["vault_path"]))

    def list_owned_catalogued_assets(
        self, username: str
    ) -> list[CataloguedAsset]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE asset.owner_username = %s
                    ORDER BY lower(file.filename), asset.id
                    """,
                    (username,),
                )
                rows = cursor.fetchall()
        return [_catalogued_asset_from_row(row) for row in rows]

    def list_owned_catalogued_assets_by_user_id(
        self, owner_user_id: UUID
    ) -> list[CataloguedAsset]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT asset.id, asset.asset_type, asset.display_title, asset.captured_on,
                      asset.location, asset.metadata, asset.metadata_provenance,
                      asset.detected_metadata, asset.imported_metadata, asset.user_overrides,
                      asset.effective_metadata, asset.owner_username, asset.owner_user_id,
                      asset.origin_vault_id, asset.visibility, asset.shared_with, file.vault_path,
                      file.filename, file.size_bytes, file.mime_type, file.sha256
                    FROM vault_files AS file JOIN vault_assets AS asset ON asset.id=file.asset_id
                    WHERE asset.owner_user_id=%s ORDER BY lower(file.filename), asset.id
                    """,
                    (owner_user_id,),
                )
                rows = cursor.fetchall()
        return [_catalogued_asset_from_row(row) for row in rows]

    def list_visible_catalogued_assets(
        self, username: str
    ) -> list[CataloguedAsset]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (asset.id)
                        asset.id, asset.asset_type, asset.display_title,
                        asset.captured_on, asset.location, asset.metadata,
                        asset.metadata_provenance, asset.detected_metadata,
                        asset.imported_metadata, asset.user_overrides,
                        asset.effective_metadata, asset.owner_username,
                        asset.owner_user_id, asset.origin_vault_id,
                        asset.visibility, asset.shared_with, file.vault_path,
                        file.filename, file.size_bytes, file.mime_type, file.sha256
                    FROM vault_assets AS asset
                    JOIN vault_files AS file ON file.asset_id = asset.id
                    ORDER BY asset.id, (file.file_role = 'primary') DESC, file.created_at
                    """
                )
                rows = cursor.fetchall()
                allowed = self._visible_asset_ids_for_username(
                    cursor, username, [UUID(str(row["id"])) for row in rows]
                )
        return [
            _catalogued_asset_from_row(row)
            for row in rows
            if UUID(str(row["id"])) in allowed
        ]

    def search_catalogued_assets(
        self, query: str, limit: int = 50
    ) -> list[CataloguedAsset]:
        cleaned_query = query.strip()
        if not cleaned_query:
            return []
        pattern = f"%{cleaned_query}%"
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE asset.display_title ILIKE %s
                       OR file.filename ILIKE %s
                       OR file.vault_path ILIKE %s
                       OR COALESCE(asset.location, '') ILIKE %s
                    ORDER BY
                        lower(asset.display_title),
                        lower(file.vault_path)
                    LIMIT %s
                    """,
                    (pattern, pattern, pattern, pattern, limit),
                )
                rows = cursor.fetchall()
        return [
            _catalogued_asset_from_row(row)
            for row in rows
        ]

    def get_visible_catalogued_asset_by_id(
        self, asset_id: UUID, username: str
    ) -> CataloguedAsset | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                allowed = self._visible_asset_ids_for_username(
                    cursor, username, [asset_id]
                )
        if asset_id not in allowed:
            return None
        return self.get_catalogued_asset_by_id(asset_id)

    def list_visible_movie_assets(
        self, username: str
    ) -> list[CataloguedAsset]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT asset.id, asset.asset_type, asset.display_title,
                           asset.captured_on, asset.location, asset.metadata,
                           asset.metadata_provenance, asset.detected_metadata,
                           asset.imported_metadata, asset.user_overrides,
                           asset.effective_metadata, asset.owner_username, asset.owner_user_id,
                           asset.origin_vault_id, asset.visibility, asset.shared_with, file.vault_path,
                           file.filename, file.size_bytes, file.mime_type, file.sha256
                    FROM vault_assets AS asset
                    JOIN LATERAL (
                        SELECT * FROM vault_files
                        WHERE asset_id = asset.id
                        ORDER BY (file_role = 'primary') DESC, created_at
                        LIMIT 1
                    ) AS file ON TRUE
                    WHERE lower(asset.asset_type) IN ('movie', 'movies')
                    ORDER BY lower(asset.display_title), asset.id
                    """,
                )
                rows = cursor.fetchall()
                allowed = self._visible_asset_ids_for_username(
                    cursor, username, [UUID(str(row["id"])) for row in rows]
                )
        return [
            _catalogued_asset_from_row(row)
            for row in rows
            if UUID(str(row["id"])) in allowed
        ]

    def set_movie_exclusive_state(
        self, asset_id: UUID, username: str, is_exclusive: bool
    ) -> CataloguedAsset | None:
        asset = self.get_visible_catalogued_asset_by_id(asset_id, username)
        if (
            asset is None
            or asset.asset_type.casefold() not in {"movie", "movies"}
            or not asset_is_editable_by(asset, username)
        ):
            return None
        previous = bool(asset.effective_metadata.get("exclusive_movie", False))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET user_overrides = jsonb_set(user_overrides, '{exclusive_movie}', to_jsonb(%s::boolean), TRUE),
                        effective_metadata = jsonb_set(effective_metadata, '{exclusive_movie}', to_jsonb(%s::boolean), TRUE),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (is_exclusive, is_exclusive, asset_id),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (id, asset_id, action, username, previous_values, current_values)
                    VALUES (%s, %s, 'exclusive_movie_state_changed', %s, %s, %s)
                    """,
                    (uuid4(), asset_id, username, Jsonb({"exclusive_movie": previous}), Jsonb({"exclusive_movie": is_exclusive})),
                )
        updated = self.get_catalogued_asset_by_id(asset_id)
        if updated is not None:
            self._export_sidecar(updated)
        return updated

    def list_catalogued_assets_by_vault_path_prefix(
        self, prefix: str
    ) -> list[CataloguedAsset]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT asset_id FROM vault_files WHERE vault_path LIKE %s ORDER BY asset_id",
                    (f"{prefix}%",),
                )
                asset_ids = [UUID(str(row["asset_id"])) for row in cursor.fetchall()]
        return [
            asset
            for asset_id in asset_ids
            if (asset := self.get_catalogued_asset_by_id(asset_id)) is not None
        ]

    def search_visible_catalogued_assets(
        self, query: str, username: str, limit: int = 50
    ) -> list[CataloguedAsset]:
        cleaned_query = query.strip()
        if not cleaned_query:
            return []
        pattern = f"%{cleaned_query}%"
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_files AS file
                    JOIN vault_assets AS asset ON asset.id = file.asset_id
                    WHERE (
                        asset.display_title ILIKE %s
                        OR file.filename ILIKE %s
                        OR file.vault_path ILIKE %s
                        OR COALESCE(asset.location, '') ILIKE %s
                    )
                    ORDER BY
                        lower(asset.display_title),
                        lower(file.vault_path)
                    LIMIT %s
                    """,
                    (
                        pattern,
                        pattern,
                        pattern,
                        pattern,
                        limit,
                    ),
                )
                rows = cursor.fetchall()
                allowed = self._visible_asset_ids_for_username(
                    cursor, username, [UUID(str(row["id"])) for row in rows]
                )
        return [
            _catalogued_asset_from_row(row)
            for row in rows
            if UUID(str(row["id"])) in allowed
        ]

    def update_catalogued_asset_metadata(
        self,
        asset_id: UUID,
        changes: dict[str, str | None],
        username: str,
    ) -> CataloguedAsset | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id,
                        asset.asset_type,
                        asset.display_title,
                        asset.captured_on,
                        asset.location,
                        asset.metadata,
                        asset.metadata_provenance,
                        asset.detected_metadata,
                        asset.imported_metadata,
                        asset.user_overrides,
                        asset.effective_metadata,
                        asset.owner_username,
                        asset.owner_user_id,
                        asset.origin_vault_id,
                        asset.visibility,
                        asset.shared_with,
                        file.vault_path,
                        file.filename,
                        file.size_bytes,
                        file.mime_type,
                        file.sha256
                    FROM vault_assets AS asset
                    JOIN vault_files AS file ON file.asset_id = asset.id
                    WHERE asset.id = %s
                    ORDER BY file.file_role = 'primary' DESC,
                             file.created_at
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                asset = _catalogued_asset_from_row(row)
                updated = apply_catalogue_metadata_changes(asset, changes)
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET display_title = %s,
                        captured_on = %s,
                        location = %s,
                        metadata_provenance = %s,
                        user_overrides = %s,
                        effective_metadata = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        updated.display_title,
                        updated.captured_on,
                        updated.location,
                        Jsonb(updated.metadata_provenance),
                        Jsonb(updated.user_overrides),
                        Jsonb(updated.effective_metadata),
                        asset_id,
                    ),
                )
                previous_values = {
                    field: (
                        value.isoformat() if isinstance(value, date) else value
                    )
                    for field in changes
                    for value in (catalogue_metadata_field_value(asset, field),)
                }
                current_values = {
                    field: (
                        value.isoformat() if isinstance(value, date) else value
                    )
                    for field in changes
                    for value in (catalogue_metadata_field_value(updated, field),)
                }
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (%s, %s, 'metadata_updated', %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        asset_id,
                        username,
                        Jsonb(previous_values),
                        Jsonb(current_values),
                    ),
                )
        self._export_sidecar(updated)
        return updated

    def import_catalogued_asset_metadata(
        self,
        asset_id: UUID,
        metadata: dict[str, object],
        source: str,
    ) -> CataloguedAsset | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id, asset.asset_type, asset.display_title,
                        asset.captured_on, asset.location, asset.metadata,
                        asset.metadata_provenance, asset.detected_metadata,
                        asset.imported_metadata, asset.user_overrides,
                        asset.effective_metadata, asset.owner_username, asset.owner_user_id,
                        asset.origin_vault_id, asset.visibility, asset.shared_with, file.vault_path,
                        file.filename, file.size_bytes, file.mime_type,
                        file.sha256
                    FROM vault_assets AS asset
                    JOIN vault_files AS file ON file.asset_id = asset.id
                    WHERE asset.id = %s
                    ORDER BY file.file_role = 'primary' DESC,
                             file.created_at
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                updated = apply_imported_asset_metadata(
                    _catalogued_asset_from_row(row),
                    metadata,
                    source,
                )
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET display_title = %s,
                        captured_on = %s,
                        location = %s,
                        metadata_provenance = %s,
                        imported_metadata = %s,
                        effective_metadata = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        updated.display_title,
                        updated.captured_on,
                        updated.location,
                        Jsonb(updated.metadata_provenance),
                        Jsonb(updated.imported_metadata),
                        Jsonb(updated.effective_metadata),
                        asset_id,
                    ),
                )
        self._export_sidecar(updated)
        return updated

    def update_catalogued_asset_access(
        self,
        asset_id: UUID,
        visibility: str,
        shared_with: tuple[str, ...],
        username: str,
        *,
        local_all: bool = False,
        share_mode: Literal["quick", "standard"] = "quick",
    ) -> CataloguedAsset | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.id, asset.asset_type, asset.display_title,
                        asset.captured_on, asset.location, asset.metadata,
                        asset.metadata_provenance, asset.detected_metadata,
                        asset.imported_metadata, asset.user_overrides,
                        asset.effective_metadata, asset.owner_username, asset.owner_user_id,
                        asset.origin_vault_id, asset.visibility, asset.shared_with, file.vault_path,
                        file.filename, file.size_bytes, file.mime_type,
                        file.sha256
                    FROM vault_assets AS asset
                    JOIN vault_files AS file ON file.asset_id = asset.id
                    WHERE asset.id = %s
                    ORDER BY file.file_role = 'primary' DESC,
                             file.created_at
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                asset = _catalogued_asset_from_row(row)
                caller_user_id = active_user_id(cursor, username)
                if (
                    caller_user_id is None
                    or asset.owner_user_id != caller_user_id
                ):
                    return None
                updated = apply_catalogue_access_changes(
                    asset,
                    visibility,
                    shared_with,
                    local_all=local_all,
                )
                cursor.execute(
                    """
                    UPDATE vault_assets
                    SET visibility = %s,
                        shared_with = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        updated.visibility,
                        Jsonb(list(updated.shared_with)),
                        asset_id,
                    ),
                )
                if asset.owner_user_id is None or asset.origin_vault_id is None:
                    raise RuntimeError(
                        "Asset sharing requires immutable ownership and origin"
                    )
                sync_stage2c_local_share_grants(
                    cursor,
                    asset_id,
                    asset.owner_user_id,
                    asset.origin_vault_id,
                    updated.visibility,
                    updated.shared_with,
                    local_all=local_all,
                    share_mode=share_mode,
                )
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (%s, %s, 'access_policy_updated', %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        asset_id,
                        username,
                        Jsonb(
                            {
                                "visibility": asset.visibility,
                                "shared_with": ", ".join(asset.shared_with)
                                or None,
                            }
                        ),
                        Jsonb(
                            {
                                "visibility": updated.visibility,
                                "shared_with": ", ".join(
                                    updated.shared_with
                                )
                                or None,
                            }
                        ),
                    ),
                )
        self._export_sidecar(updated)
        return updated

    def update_catalogued_assets_access(
        self, asset_ids: list[UUID], visibility: str, shared_with: tuple[str, ...], username: str,
        *, local_all: bool = False, share_mode: Literal["quick", "standard"] = "quick",
    ) -> list[CataloguedAsset]:
        """Commit all owner-validated sharing changes in one database transaction."""
        if not asset_ids or len(set(asset_ids)) != len(asset_ids):
            raise ValueError("Selected assets must be unique and non-empty")
        updated_assets: list[CataloguedAsset] = []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                caller = active_user_id(cursor, username)
                if caller is None:
                    raise ValueError("Authenticated owner is unavailable")
                cursor.execute("""SELECT DISTINCT ON (asset.id) asset.id, asset.asset_type, asset.display_title, asset.captured_on, asset.location, asset.metadata, asset.metadata_provenance, asset.detected_metadata, asset.imported_metadata, asset.user_overrides, asset.effective_metadata, asset.owner_username, asset.owner_user_id, asset.origin_vault_id, asset.visibility, asset.shared_with, file.vault_path, file.filename, file.size_bytes, file.mime_type, file.sha256 FROM vault_assets asset JOIN vault_files file ON file.asset_id=asset.id WHERE asset.id=ANY(%s) ORDER BY asset.id, (file.file_role='primary') DESC, file.created_at""", (asset_ids,))
                assets = {}
                for row in cursor.fetchall():
                    asset = _catalogued_asset_from_row(row)
                    assets[asset.id] = asset
                for asset_id in asset_ids:
                    asset = assets.get(asset_id)
                    if asset is None:
                        raise ValueError(f"Selected asset {asset_id} was not found")
                    if asset.owner_user_id != caller or asset.origin_vault_id is None:
                        raise ValueError(f"Selected asset {asset.display_title or asset.filename} is not owned by the authenticated user")
                for asset_id in asset_ids:
                    asset = assets[asset_id]
                    updated = apply_catalogue_access_changes(asset, visibility, shared_with, local_all=local_all)
                    cursor.execute("UPDATE vault_assets SET visibility=%s, shared_with=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (updated.visibility, Jsonb(list(updated.shared_with)), asset_id))
                    sync_stage2c_local_share_grants(cursor, asset_id, caller, asset.origin_vault_id, updated.visibility, updated.shared_with, local_all=local_all, share_mode=share_mode)
                    cursor.execute("INSERT INTO vault_asset_history (id, asset_id, action, username, previous_values, current_values) VALUES (%s,%s,'access_policy_updated',%s,%s,%s)", (uuid4(), asset_id, username, Jsonb({"visibility": asset.visibility, "shared_with": ", ".join(asset.shared_with) or None}), Jsonb({"visibility": updated.visibility, "shared_with": ", ".join(updated.shared_with) or None})))
                    updated_assets.append(updated)
        for asset in updated_assets:
            self._export_sidecar(asset)
        return updated_assets

    def list_catalogued_asset_history(
        self, asset_id: UUID
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id,
                        asset_id,
                        action,
                        username,
                        previous_values,
                        current_values,
                        created_at
                    FROM vault_asset_history
                    WHERE asset_id = %s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (asset_id,),
                )
                rows = cursor.fetchall()
        return [
            {
                "id": UUID(str(row["id"])),
                "asset_id": UUID(str(row["asset_id"])),
                "action": str(row["action"]),
                "username": str(row["username"]),
                "previous_values": dict(row["previous_values"] or {}),
                "current_values": dict(row["current_values"] or {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_catalogued_asset_history(
        self,
        asset_id: UUID,
        username: str,
        action: str,
        current_values: dict[str, object],
    ) -> dict[str, object] | None:
        if self.get_catalogued_asset_by_id(asset_id) is None:
            return None
        created_at = datetime.now(timezone.utc)
        entry = {
            "id": uuid4(),
            "asset_id": asset_id,
            "action": action,
            "username": username,
            "previous_values": {},
            "current_values": dict(current_values),
            "created_at": created_at,
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username, previous_values, current_values, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        entry["id"],
                        asset_id,
                        action,
                        username,
                        Jsonb({}),
                        Jsonb(current_values),
                        created_at,
                    ),
                )
        return entry

    def request_asset_relationship_review(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        classification: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> dict[str, object] | None:
        created_at = datetime.now(timezone.utc)
        values = {
            "classification": classification,
            "confidence": confidence,
            "evidence": json.dumps(evidence),
            "state": "pending_review",
        }
        first_entry_id = uuid4()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM vault_assets WHERE id IN (%s, %s)",
                    (first_asset_id, second_asset_id),
                )
                if len(cursor.fetchall()) != 2:
                    return None
                cursor.execute(
                    """
                    SELECT 1 FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action LIKE 'relationship_review_%%'
                      AND current_values ->> 'candidate_asset_id' = %s
                    LIMIT 1
                    """,
                    (first_asset_id, str(second_asset_id)),
                )
                if cursor.fetchone() is not None:
                    return None
                for entry_id, asset_id, candidate_id in (
                    (first_entry_id, first_asset_id, second_asset_id),
                    (uuid4(), second_asset_id, first_asset_id),
                ):
                    cursor.execute(
                        """
                        INSERT INTO vault_asset_history (
                            id, asset_id, action, username,
                            previous_values, current_values, created_at
                        ) VALUES (
                            %s, %s, 'relationship_review_requested', %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            entry_id,
                            asset_id,
                            username,
                            Jsonb({}),
                            Jsonb({**values, "candidate_asset_id": str(candidate_id)}),
                            created_at,
                        ),
                    )
        return {
            "id": first_entry_id,
            "asset_id": first_asset_id,
            "action": "relationship_review_requested",
            "username": username,
            "previous_values": {},
            "current_values": {
                **values,
                "candidate_asset_id": str(second_asset_id),
            },
            "created_at": created_at,
        }

    def retain_separate_asset_relationship_review(
        self, first_asset_id: UUID, second_asset_id: UUID, username: str
    ) -> dict[str, object] | None:
        created_at = datetime.now(timezone.utc)
        first_entry_id = uuid4()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action
                    FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'relationship_review_requested',
                          'relationship_review_retained'
                      )
                      AND current_values ->> 'candidate_asset_id' = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (first_asset_id, str(second_asset_id)),
                )
                latest = cursor.fetchone()
                if latest is None or latest["action"] != "relationship_review_requested":
                    return None
                for entry_id, asset_id, candidate_id in (
                    (first_entry_id, first_asset_id, second_asset_id),
                    (uuid4(), second_asset_id, first_asset_id),
                ):
                    cursor.execute(
                        """
                        INSERT INTO vault_asset_history (
                            id, asset_id, action, username,
                            previous_values, current_values, created_at
                        ) VALUES (
                            %s, %s, 'relationship_review_retained', %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            entry_id,
                            asset_id,
                            username,
                            Jsonb({"state": "pending_review"}),
                            Jsonb(
                                {
                                    "candidate_asset_id": str(candidate_id),
                                    "decision": "retain_separately",
                                    "state": "resolved",
                                }
                            ),
                            created_at,
                        ),
                    )
        return {
            "id": first_entry_id,
            "asset_id": first_asset_id,
            "action": "relationship_review_retained",
            "username": username,
            "previous_values": {"state": "pending_review"},
            "current_values": {
                "candidate_asset_id": str(second_asset_id),
                "decision": "retain_separately",
                "state": "resolved",
            },
            "created_at": created_at,
        }

    def create_catalogued_asset_relationship(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        relationship_type: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> CataloguedAssetRelationship | None:
        asset_ids = tuple(sorted((first_asset_id, second_asset_id), key=str))
        if first_asset_id == second_asset_id:
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO vault_asset_relationships (
                        first_asset_id, second_asset_id, relationship_type,
                        confidence, evidence, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (first_asset_id, second_asset_id) DO NOTHING
                    RETURNING created_at
                    """,
                    (
                        asset_ids[0],
                        asset_ids[1],
                        relationship_type,
                        confidence,
                        Jsonb(list(evidence)),
                        username,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return CataloguedAssetRelationship(
            first_asset_id=asset_ids[0],
            second_asset_id=asset_ids[1],
            relationship_type=relationship_type,
            confidence=confidence,
            evidence=evidence,
            created_by=username,
            created_at=row["created_at"],
        )

    def list_catalogued_asset_relationships(
        self, asset_id: UUID
    ) -> list[CataloguedAssetRelationship]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT first_asset_id, second_asset_id, relationship_type,
                           confidence, evidence, created_by, created_at
                    FROM vault_asset_relationships
                    WHERE first_asset_id = %s OR second_asset_id = %s
                    ORDER BY relationship_type, first_asset_id, second_asset_id
                    """,
                    (asset_id, asset_id),
                )
                rows = cursor.fetchall()
        return [
            CataloguedAssetRelationship(
                first_asset_id=UUID(str(row["first_asset_id"])),
                second_asset_id=UUID(str(row["second_asset_id"])),
                relationship_type=str(row["relationship_type"]),
                confidence=str(row["confidence"]),
                evidence=tuple(str(item) for item in row["evidence"]),
                created_by=str(row["created_by"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def approve_asset_relationship_review(
        self,
        first_asset_id: UUID,
        second_asset_id: UUID,
        relationship_type: str,
        confidence: str,
        evidence: tuple[str, ...],
        username: str,
    ) -> dict[str, object] | None:
        asset_ids = tuple(sorted((first_asset_id, second_asset_id), key=str))
        first_entry_id = uuid4()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'relationship_review_requested',
                          'relationship_review_retained',
                          'relationship_review_linked'
                      )
                      AND current_values ->> 'candidate_asset_id' = %s
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (first_asset_id, str(second_asset_id)),
                )
                latest = cursor.fetchone()
                if latest is None or latest["action"] != "relationship_review_requested":
                    return None
                cursor.execute(
                    """
                    INSERT INTO vault_asset_relationships (
                        first_asset_id, second_asset_id, relationship_type,
                        confidence, evidence, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (first_asset_id, second_asset_id) DO NOTHING
                    RETURNING created_at
                    """,
                    (
                        asset_ids[0], asset_ids[1], relationship_type,
                        confidence, Jsonb(list(evidence)), username,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                created_at = row["created_at"]
                for entry_id, asset_id, candidate_id in (
                    (first_entry_id, first_asset_id, second_asset_id),
                    (uuid4(), second_asset_id, first_asset_id),
                ):
                    cursor.execute(
                        """
                        INSERT INTO vault_asset_history (
                            id, asset_id, action, username,
                            previous_values, current_values, created_at
                        ) VALUES (
                            %s, %s, 'relationship_review_linked', %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            entry_id, asset_id, username,
                            Jsonb({"state": "pending_review"}),
                            Jsonb({
                                "candidate_asset_id": str(candidate_id),
                                "relationship_type": relationship_type,
                                "state": "resolved",
                            }),
                            created_at,
                        ),
                    )
        return {
            "id": first_entry_id,
            "asset_id": first_asset_id,
            "action": "relationship_review_linked",
            "username": username,
            "previous_values": {"state": "pending_review"},
            "current_values": {
                "candidate_asset_id": str(second_asset_id),
                "relationship_type": relationship_type,
                "state": "resolved",
            },
            "created_at": created_at,
        }

    def request_catalogued_asset_quarantine_review(
        self,
        asset_id: UUID,
        username: str,
        reason: str | None,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM vault_assets WHERE id = %s",
                    (asset_id,),
                )
                if cursor.fetchone() is None:
                    return None
                entry_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (
                        %s, %s, 'quarantine_review_requested', %s,
                        %s, %s
                    )
                    RETURNING created_at
                    """,
                    (
                        entry_id,
                        asset_id,
                        username,
                        Jsonb({}),
                        Jsonb(
                            {
                                "reason": reason,
                                "state": "pending_review",
                            }
                        ),
                    ),
                )
                created_at = cursor.fetchone()["created_at"]
        return {
            "id": entry_id,
            "asset_id": asset_id,
            "action": "quarantine_review_requested",
            "username": username,
            "previous_values": {},
            "current_values": {
                "reason": reason,
                "state": "pending_review",
            },
            "created_at": created_at,
        }

    def cancel_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        previous_state = (
            "quarantined"
            if asset.vault_path.startswith("/vault/Quarantine/")
            else asset.lifecycle_state
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action
                    FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'permanent_deletion_review_requested',
                          'permanent_deletion_review_cancelled',
                          'permanent_deletion_confirmed'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                latest_entry = cursor.fetchone()
                if latest_entry is None or latest_entry["action"] != (
                    "permanent_deletion_review_requested"
                ):
                    return None
                entry_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (
                        %s, %s, 'permanent_deletion_review_cancelled', %s,
                        %s, %s
                    )
                    RETURNING created_at
                    """,
                    (
                        entry_id,
                        asset_id,
                        username,
                        Jsonb({"state": "pending_permanent_deletion_review"}),
                        Jsonb({"state": previous_state}),
                    ),
                )
                created_at = cursor.fetchone()["created_at"]
        return {
            "id": entry_id,
            "asset_id": asset_id,
            "action": "permanent_deletion_review_cancelled",
            "username": username,
            "previous_values": {"state": "pending_permanent_deletion_review"},
            "current_values": {"state": previous_state},
            "created_at": created_at,
        }

    def confirm_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
        checksum: str,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action
                    FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'permanent_deletion_review_requested',
                          'permanent_deletion_review_cancelled',
                          'permanent_deletion_confirmed'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                latest_entry = cursor.fetchone()
                if latest_entry is None or latest_entry["action"] != (
                    "permanent_deletion_review_requested"
                ):
                    return None
                entry_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (
                        %s, %s, 'permanent_deletion_confirmed', %s,
                        %s, %s
                    )
                    RETURNING created_at
                    """,
                    (
                        entry_id,
                        asset_id,
                        username,
                        Jsonb({"state": "pending_permanent_deletion_review"}),
                        Jsonb(
                            {
                                "state": "approved_for_permanent_deletion",
                                "checksum": checksum,
                            }
                        ),
                    ),
                )
                created_at = cursor.fetchone()["created_at"]
        return {
            "id": entry_id,
            "asset_id": asset_id,
            "action": "permanent_deletion_confirmed",
            "username": username,
            "previous_values": {"state": "pending_permanent_deletion_review"},
            "current_values": {
                "state": "approved_for_permanent_deletion",
                "checksum": checksum,
            },
            "created_at": created_at,
        }

    def record_catalogued_asset_permanent_deletion(
        self,
        asset_id: UUID,
        vault_path: str,
        checksum: str,
        username: str,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT filename, size_bytes, mime_type, sha256
                    FROM vault_files
                    WHERE asset_id = %s AND vault_path = %s
                    FOR UPDATE
                    """,
                    (asset_id, vault_path),
                )
                file_row = cursor.fetchone()
                if file_row is None or str(file_row["sha256"]) != checksum:
                    return None
                cursor.execute(
                    """
                    SELECT action, current_values
                    FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'permanent_deletion_review_requested',
                          'permanent_deletion_review_cancelled',
                          'permanent_deletion_confirmed',
                          'permanently_deleted'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                lifecycle_entry = cursor.fetchone()
                if (
                    lifecycle_entry is None
                    or lifecycle_entry["action"] != "permanent_deletion_confirmed"
                    or dict(lifecycle_entry["current_values"] or {}).get("checksum")
                    != checksum
                ):
                    return None
                deleted_at = datetime.now(timezone.utc)
                cursor.execute(
                    """
                    INSERT INTO vault_asset_deletions (
                        asset_id, vault_path, filename, size_bytes,
                        mime_type, sha256, deleted_by, deleted_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        asset_id,
                        vault_path,
                        file_row["filename"],
                        file_row["size_bytes"],
                        file_row["mime_type"],
                        checksum,
                        username,
                        deleted_at,
                    ),
                )
                entry_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values, created_at
                    )
                    VALUES (%s, %s, 'permanently_deleted', %s, %s, %s, %s)
                    """,
                    (
                        entry_id,
                        asset_id,
                        username,
                        Jsonb(
                            {
                                "state": "approved_for_permanent_deletion",
                                "vault_path": vault_path,
                                "checksum": checksum,
                            }
                        ),
                        Jsonb({"state": "deleted"}),
                        deleted_at,
                    ),
                )
                cursor.execute(
                    "DELETE FROM vault_files WHERE asset_id = %s AND vault_path = %s",
                    (asset_id, vault_path),
                )
        return {
            "id": entry_id,
            "asset_id": asset_id,
            "action": "permanently_deleted",
            "username": username,
            "previous_values": {
                "state": "approved_for_permanent_deletion",
                "vault_path": vault_path,
                "checksum": checksum,
            },
            "current_values": {"state": "deleted"},
            "created_at": deleted_at,
        }

    def cancel_catalogued_asset_quarantine_review(
        self,
        asset_id: UUID,
        username: str,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM vault_assets WHERE id = %s",
                    (asset_id,),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    SELECT action
                    FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'quarantine_review_requested',
                          'quarantine_review_cancelled'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                latest_entry = cursor.fetchone()
                if latest_entry is None or latest_entry["action"] != (
                    "quarantine_review_requested"
                ):
                    return None
                entry_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (
                        %s, %s, 'quarantine_review_cancelled', %s,
                        %s, %s
                    )
                    RETURNING created_at
                    """,
                    (
                        entry_id,
                        asset_id,
                        username,
                        Jsonb({"state": "pending_review"}),
                        Jsonb({"state": "cancelled"}),
                    ),
                )
                created_at = cursor.fetchone()["created_at"]
        return {
            "id": entry_id,
            "asset_id": asset_id,
            "action": "quarantine_review_cancelled",
            "username": username,
            "previous_values": {"state": "pending_review"},
            "current_values": {"state": "cancelled"},
            "created_at": created_at,
        }

    def confirm_catalogued_asset_quarantine(
        self,
        asset_id: UUID,
        source_vault_path: str,
        quarantine_vault_path: str,
        username: str,
    ) -> CataloguedAsset | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None or asset.vault_path != source_vault_path:
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM vault_files
                    WHERE asset_id = %s
                      AND vault_path = %s
                    FOR UPDATE
                    """,
                    (asset_id, source_vault_path),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    SELECT action
                    FROM vault_asset_history
                    WHERE asset_id = %s
                      AND action IN (
                          'quarantine_review_requested',
                          'quarantine_review_cancelled',
                          'quarantined'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (asset_id,),
                )
                latest_entry = cursor.fetchone()
                if latest_entry is None or latest_entry["action"] != (
                    "quarantine_review_requested"
                ):
                    return None
                cursor.execute(
                    "SELECT id FROM vault_files WHERE vault_path = %s",
                    (quarantine_vault_path,),
                )
                if cursor.fetchone() is not None:
                    return None
                cursor.execute(
                    """
                    UPDATE vault_files
                    SET vault_path = %s,
                        filename = %s
                    WHERE asset_id = %s
                      AND vault_path = %s
                    """,
                    (
                        quarantine_vault_path,
                        Path(quarantine_vault_path).name,
                        asset_id,
                        source_vault_path,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (%s, %s, 'quarantined', %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        asset_id,
                        username,
                        Jsonb(
                            {
                                "vault_path": source_vault_path,
                                "state": "pending_review",
                            }
                        ),
                        Jsonb(
                            {
                                "vault_path": quarantine_vault_path,
                                "state": "quarantined",
                            }
                        ),
                    ),
                )
        updated = replace(
            asset,
            vault_path=quarantine_vault_path,
            filename=Path(quarantine_vault_path).name,
        )
        self._export_sidecar(updated)
        return updated

    def relocate_catalogued_asset(
        self,
        asset_id: UUID,
        source_vault_path: str,
        destination_vault_path: str,
        username: str,
        action: str,
    ) -> CataloguedAsset | None:
        if action not in {
            "moved_to_folder",
            "restored_from_bin",
            "historical_exclusive_movie_path_reconciled",
        }:
            raise ValueError("The lifecycle action is not supported")
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None or asset.vault_path != source_vault_path:
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM vault_files WHERE asset_id = %s AND vault_path = %s FOR UPDATE",
                    (asset_id, source_vault_path),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute("SELECT id FROM vault_files WHERE vault_path = %s", (destination_vault_path,))
                if cursor.fetchone() is not None:
                    return None
                cursor.execute(
                    "UPDATE vault_files SET vault_path = %s, filename = %s WHERE asset_id = %s AND vault_path = %s",
                    (destination_vault_path, Path(destination_vault_path).name, asset_id, source_vault_path),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (id, asset_id, action, username, previous_values, current_values)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(), asset_id, action, username,
                        Jsonb({"vault_path": source_vault_path, "checksum": asset.sha256}),
                        Jsonb({"vault_path": destination_vault_path, "checksum": asset.sha256, "state": "relocated"}),
                    ),
                )
        updated = replace(asset, vault_path=destination_vault_path, filename=Path(destination_vault_path).name)
        self._export_sidecar(updated)
        return updated

    def request_catalogued_asset_permanent_deletion_review(
        self,
        asset_id: UUID,
        username: str,
        reason: str,
        eligible_at: datetime,
    ) -> dict[str, object] | None:
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            return None
        previous_state = (
            "quarantined"
            if asset.vault_path.startswith("/vault/Quarantine/")
            else asset.lifecycle_state
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                entry_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username,
                        previous_values, current_values
                    )
                    VALUES (
                        %s, %s, 'permanent_deletion_review_requested', %s,
                        %s, %s
                    )
                    RETURNING created_at
                    """,
                    (
                        entry_id,
                        asset_id,
                        username,
                        Jsonb({"state": previous_state}),
                        Jsonb(
                            {
                                "reason": reason,
                                "state": "pending_permanent_deletion_review",
                                "eligible_at": eligible_at.isoformat(),
                            }
                        ),
                    ),
                )
                created_at = cursor.fetchone()["created_at"]
        return {
            "id": entry_id,
            "asset_id": asset_id,
            "action": "permanent_deletion_review_requested",
            "username": username,
            "previous_values": {"state": previous_state},
            "current_values": {
                "reason": reason,
                "state": "pending_permanent_deletion_review",
                "eligible_at": eligible_at.isoformat(),
            },
            "created_at": created_at,
        }

    def record_move_result(
        self,
        item_id: UUID,
        state: str,
        username: str,
        detail: str,
        publish_catalogue: bool = True,
    ) -> ImportItem | None:
        sidecar_vault_path: str | None = None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET state = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND (%s <> 'move_failed' OR state <> 'moved')
                    RETURNING *
                    """,
                    (state, item_id, state),
                )
                row = cursor.fetchone()
                if row:
                    if publish_catalogue and state == "moved" and row["proposed_destination"]:
                        item = self._to_item(row)
                        sidecar_vault_path = item.proposed_destination
                        self._publish_catalogued_asset(
                            cursor,
                            item,
                            item.proposed_destination,
                            preserve_existing_metadata=False,
                        )
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, batch_id, item_id, action, username,
                            detail, succeeded
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            row["batch_id"],
                            item_id,
                            "file_moved" if state == "moved" else "move_failed",
                            username,
                            detail,
                            state == "moved",
                        ),
                    )
        if sidecar_vault_path:
            asset = self.get_catalogued_asset(sidecar_vault_path)
            if asset:
                self._export_sidecar(asset)
        return self._to_item(row) if row else None

    def record_duplicate_result(
        self,
        item_id: UUID,
        state: str,
        username: str,
        detail: str,
    ) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET state = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND (
                        %s <> 'duplicate_remove_failed'
                        OR state <> 'duplicate_removed'
                      )
                    RETURNING *
                    """,
                    (state, item_id, state),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, batch_id, item_id, action, username,
                            detail, succeeded
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            row["batch_id"],
                            item_id,
                            {
                                "duplicate_kept": "duplicate_kept",
                                "duplicate_removed": "duplicate_removed",
                            }.get(state, "duplicate_remove_failed"),
                            username,
                            detail,
                            state != "duplicate_remove_failed",
                        ),
                    )
        return self._to_item(row) if row else None

    def queue_move(self, item_id: UUID, username: str) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM vault_master_items
                    WHERE id = %s
                      AND source_kind = 'incoming'
                      AND state IN ('approved', 'move_failed')
                    FOR UPDATE
                    """,
                    (item_id,),
                )
                current_row = cursor.fetchone()
                row = None
                if current_row:
                    current = self._to_item(current_row)
                    cursor.execute(
                        """
                        SELECT * FROM vault_master_items
                        WHERE source_kind = 'incoming'
                          AND owner_user_id IS NOT DISTINCT FROM %s
                        FOR UPDATE
                        """,
                        (current.owner_user_id,),
                    )
                    owner_items = [
                        self._to_item(candidate)
                        for candidate in cursor.fetchall()
                    ]
                    if movie_publication_set_is_ready(current, owner_items):
                        cursor.execute(
                            """
                            UPDATE vault_master_items
                            SET state = 'move_queued',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            RETURNING *
                            """,
                            (item_id,),
                        )
                        row = cursor.fetchone()
                if row:
                    cursor.execute(
                        """
                        INSERT INTO vault_master_activity (
                            id, batch_id, item_id, action, username
                        )
                        VALUES (%s, %s, %s, 'move_queued', %s)
                        """,
                        (uuid4(), row["batch_id"], item_id, username),
                    )
        return self._to_item(row) if row else None

    def claim_next_move(self) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET state = 'move_queued'
                    WHERE state = 'moving'
                    """
                )
                cursor.execute(
                    """
                    SELECT id
                    FROM vault_master_items
                    WHERE state = 'move_queued'
                    ORDER BY updated_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cursor.execute(
                    """
                    UPDATE vault_master_items
                    SET state = 'moving', updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING *
                    """,
                    (row["id"],),
                )
                claimed = cursor.fetchone()
        return self._to_item(claimed)

    def mark_theatre_promotion_pending(self, item_id: UUID) -> ImportItem | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE vault_master_items SET state='theatre_promotion_pending', updated_at=CURRENT_TIMESTAMP WHERE id=%s AND state='moving' RETURNING *", (item_id,))
                row = cursor.fetchone()
        return self._to_item(row) if row else None

    def theatre_movie_rename_snapshot(
        self, asset_id: UUID, owner_user_id: UUID
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT asset.id AS asset_id, asset.owner_user_id,
                              file.id AS file_id, file.vault_path, file.filename,
                              file.size_bytes, file.sha256, placement.slot_id,
                              placement.relative_path
                       FROM vault_assets asset
                       JOIN vault_files file ON file.asset_id=asset.id
                       JOIN vault_file_storage_placements placement ON placement.file_id=file.id
                       WHERE asset.id=%s AND asset.owner_user_id=%s
                         AND asset.asset_type IN ('Movie','Movies')
                         AND NOT (asset.detected_metadata ? 'movie_publication_set')
                         AND file.file_role='primary'""",
                    (asset_id, owner_user_id),
                )
                row = cursor.fetchone()
        return dict(row) if row else None

    def complete_theatre_movie_rename(
        self, receipt: dict[str, object]
    ) -> CataloguedAsset | None:
        if receipt.get("schema") != "personal-vault.theatre-movie-rename.v1":
            return None
        asset_id, file_id = UUID(str(receipt["asset_id"])), UUID(str(receipt["file_id"]))
        source, destination = str(receipt["source_logical_path"]), str(receipt["destination_logical_path"])
        if not source.startswith("/vault/Theatre/Movies/") or not destination.startswith("/vault/Theatre/Movies/"):
            return None
        title, year = str(receipt["title"]), int(receipt["release_year"])
        if destination != canonical_movie_destination(title, year, Path(source).suffix):
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT asset.*, file.id AS file_id, file.vault_path,
                              file.filename, file.size_bytes, file.mime_type,
                              file.sha256, placement.slot_id, placement.relative_path
                       FROM vault_assets asset
                       JOIN vault_files file ON file.asset_id=asset.id
                       JOIN vault_file_storage_placements placement ON placement.file_id=file.id
                       WHERE asset.id=%s AND file.id=%s
                       FOR UPDATE OF asset,file,placement""",
                    (asset_id, file_id),
                )
                row = cursor.fetchone()
                if row is None or str(row["vault_path"]) == destination:
                    return None
                expected = {
                    "owner_user_id": str(row["owner_user_id"]),
                    "slot_id": str(row["slot_id"]),
                    "source_logical_path": str(row["vault_path"]),
                    "source_relative_path": str(row["relative_path"]),
                    "expected_sha256": str(row["sha256"]),
                    "expected_size_bytes": int(row["size_bytes"]),
                }
                if any(receipt.get(key) != value for key, value in expected.items()):
                    return None
                destination_relative = destination.removeprefix("/vault/")
                if receipt.get("destination_relative_path") != destination_relative:
                    return None
                cursor.execute("SELECT 1 FROM vault_files WHERE vault_path=%s", (destination,))
                if cursor.fetchone() is not None:
                    return None
                placement = {"slot_id": str(row["slot_id"]), "relative_path": destination_relative}
                metadata = {**dict(row.get("metadata") or {}), "storage_placement": placement}
                imported_identity = matches_reliable_imported_movie_identity(
                    dict(row.get("detected_metadata") or {}),
                    dict(row.get("imported_metadata") or {}),
                    title,
                    year,
                )
                overrides = dict(row.get("user_overrides") or {})
                effective = dict(row.get("effective_metadata") or {})
                provenance = dict(row.get("metadata_provenance") or {})
                if not imported_identity:
                    overrides.update({"display_title": title, "release_year": year})
                    effective.update(overrides)
                    provenance.update({"display_title": "user_override", "release_year": "user_override"})
                effective["storage_placement"] = placement
                cursor.execute("UPDATE vault_files SET vault_path=%s,filename=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND vault_path=%s", (destination, Path(destination).name, file_id, source))
                if cursor.rowcount != 1:
                    raise RuntimeError("The authoritative Theatre file changed during rename reconciliation")
                cursor.execute("UPDATE vault_file_storage_placements SET relative_path=%s WHERE file_id=%s AND slot_id=%s AND relative_path=%s", (destination_relative, file_id, row["slot_id"], row["relative_path"]))
                if cursor.rowcount != 1:
                    raise RuntimeError("The authoritative Theatre placement changed during rename reconciliation")
                cursor.execute("UPDATE vault_assets SET display_title=%s,metadata=%s,metadata_provenance=%s,user_overrides=%s,effective_metadata=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (title, Jsonb(metadata), Jsonb(provenance), Jsonb(overrides), Jsonb(effective), asset_id))
                cursor.execute(
                    "INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values) VALUES (%s,%s,'theatre_movie_renamed',%s,%s,%s)",
                    (uuid4(), asset_id, "Theatre managed rename", Jsonb({"vault_path": source, "relative_path": row["relative_path"], "sha256": row["sha256"]}), Jsonb({"vault_path": destination, "relative_path": destination_relative, "sha256": row["sha256"], "request_id": str(receipt["request_id"])})),
                )
        updated = self.get_catalogued_asset_by_id(asset_id)
        if updated is not None:
            self._export_sidecar(updated)
        return updated

    def publish_arrival_managed_receipt(
        self, item_id: UUID, receipt: dict[str, object]
    ) -> CataloguedAsset | None:
        """Atomically consume one signed root receipt exactly once.

        The unique ``vault_files.vault_path`` index is the database-wide
        logical-path guard.  The placement row and Arrival Hall transition are
        committed with it, so a crash can leave either no catalogue evidence or
        one complete publication, never a half-published copy.
        """
        sidecar_vault_path: str | None = None
        tv_marker: dict[str, object] | None = None
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT * FROM vault_master_items WHERE id = %s FOR UPDATE", (item_id,))
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    item = self._to_item(row)
                    marker = item.metadata.get("tv_publication_set")
                    tv_marker = marker if isinstance(marker, dict) else None
                    category = item.proposed_category
                    if category not in {"Movies", "TV Shows"}:
                        return None
                    # A TV episode is never a standalone Theatre publication.
                    # The root executor has already moved its bytes by the time
                    # this receipt is reconciled, so reject malformed review
                    # state before creating (or marking) any catalogue record.
                    if category == "TV Shows" and not self._valid_tv_receipt_group(
                        cursor, item, tv_marker
                    ):
                        return None
                    logical_area = f"Theatre / {category}"
                    destination = item.proposed_destination or proposed_destination_path(
                        category, item.relative_path, item.filename
                    )
                    expected_relative = destination.removeprefix("/vault/")
                    if (
                        category not in {"Movies", "TV Shows"}
                        or item.proposed_destination != destination
                        or receipt.get("item_id") != str(item.id)
                        or item.owner_user_id is None
                        or receipt.get("owner_user_id") != str(item.owner_user_id)
                        or receipt.get("logical_destination") != destination
                        or receipt.get("logical_area") != logical_area
                        or receipt.get("relative_path") != expected_relative
                        or receipt.get("expected_sha256") != item.sha256
                        or receipt.get("expected_size_bytes") != item.size_bytes
                        or not isinstance(receipt.get("slot_id"), str)
                    ):
                        return None
                    cursor.execute("SELECT asset.id, file.id AS file_id, file.sha256, file.size_bytes FROM vault_files AS file JOIN vault_assets AS asset ON asset.id = file.asset_id WHERE file.vault_path = %s FOR UPDATE", (destination,))
                    existing = cursor.fetchone()
                    if existing is not None:
                        cursor.execute("SELECT request_id, owner_user_id, logical_destination, logical_area, asset_id, file_id, slot_id, relative_path FROM vault_arrival_managed_publications WHERE item_id = %s FOR UPDATE", (item.id,))
                        promotion = cursor.fetchone()
                        if (
                            promotion is None
                            or str(existing["sha256"]) != item.sha256
                            or int(existing["size_bytes"]) != item.size_bytes
                            or str(promotion["request_id"]) != str(receipt["request_id"])
                            or str(promotion["owner_user_id"]) != str(item.owner_user_id)
                            or str(promotion["logical_destination"]) != destination
                            or str(promotion["logical_area"]) != logical_area
                            or str(promotion["asset_id"]) != str(existing["id"])
                            or str(promotion["file_id"]) != str(existing["file_id"])
                            or str(promotion["slot_id"]) != str(receipt["slot_id"])
                            or str(promotion["relative_path"]) != expected_relative
                        ):
                            return None
                        cursor.execute("UPDATE vault_master_items SET state = 'moved' WHERE id = %s AND state <> 'moved'", (item.id,))
                        return None
                    else:
                        if item.state != "theatre_promotion_pending":
                            return None
                        # The slot row is an auditable receipt of the root-owned
                        # final manifest, not a backend filesystem authority.
                        cursor.execute("INSERT INTO vault_storage_slots (slot_id, state, assigned_areas) VALUES (%s, 'active', %s) ON CONFLICT (slot_id) DO UPDATE SET state = 'active', assigned_areas = EXCLUDED.assigned_areas", (receipt["slot_id"], Jsonb([logical_area])))
                        self._publish_catalogued_asset(cursor, item, destination, preserve_existing_metadata=False)
                        cursor.execute("SELECT id, asset_id FROM vault_files WHERE vault_path = %s FOR UPDATE", (destination,))
                        file_row = cursor.fetchone()
                        if file_row is None:
                            raise RuntimeError("Theatre catalogue publication did not create its file")
                        cursor.execute("INSERT INTO vault_file_storage_placements (file_id, slot_id, relative_path, assigned_by, placement_reason) VALUES (%s, %s, %s, %s, %s)", (file_row["id"], receipt["slot_id"], expected_relative, "Arrival Hall managed publisher", "root-verified Arrival Hall managed publication"))
                        cursor.execute("INSERT INTO vault_arrival_managed_publications (item_id, request_id, owner_user_id, logical_destination, logical_area, asset_id, file_id, slot_id, relative_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (item.id, receipt["request_id"], item.owner_user_id, destination, logical_area, file_row["asset_id"], file_row["id"], receipt["slot_id"], expected_relative))
                        placement = {"slot_id": receipt["slot_id"], "relative_path": expected_relative}
                        cursor.execute("UPDATE vault_assets SET metadata = metadata || %s, metadata_provenance = metadata_provenance || %s, effective_metadata = effective_metadata || %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (Jsonb({"storage_placement": placement}), Jsonb({"storage_placement": "root_verified_receipt"}), Jsonb({"storage_placement": placement}), file_row["asset_id"]))
                        cursor.execute("UPDATE vault_master_items SET state = 'moved', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (item.id,))
                        asset_id = UUID(str(file_row["asset_id"]))
                    cursor.execute("INSERT INTO vault_master_activity (id, batch_id, item_id, action, username, detail, succeeded) VALUES (%s, %s, %s, 'file_moved', %s, %s, TRUE)", (uuid4(), item.batch_id, item.id, "Arrival Hall managed publisher", f"Published root-verified managed receipt {receipt['request_id']}"))
                    sidecar_vault_path = destination
        except psycopg.errors.UniqueViolation:
            # A concurrent logical-path reservation must never overwrite the
            # other asset.  A later reconciliation can only accept an exact
            # already-published copy under the locked path above.
            return None
        asset = self.get_catalogued_asset_by_id(asset_id)
        if asset is not None and sidecar_vault_path:
            self._export_sidecar(asset)
        if asset is not None and tv_marker is not None:
            self._publish_completed_tv_set(tv_marker, item.owner_user_id)
        return asset

    @staticmethod
    def _valid_tv_receipt_group(
        cursor: psycopg.Cursor,
        item: ImportItem,
        marker: dict[str, object] | None,
    ) -> bool:
        """Require durable, complete, explicitly-audienced TV review state."""
        if (
            marker is None
            or marker.get("schema") != TV_PUBLICATION_SET_SCHEMA
            or not isinstance(marker.get("source_directory"), str)
            or not isinstance(marker.get("show_title"), str)
            or not isinstance(marker.get("season_number"), int)
            or int(marker["season_number"]) <= 0
        ):
            return False
        raw_members = marker.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            return False
        try:
            member_numbers = {
                UUID(str(member["item_id"])): int(member["episode_number"])
                for member in raw_members
                if isinstance(member, dict)
            }
        except (KeyError, TypeError, ValueError):
            return False
        if (
            len(member_numbers) != len(raw_members)
            or item.id not in member_numbers
            or any(number <= 0 for number in member_numbers.values())
            or len(set(member_numbers.values())) != len(member_numbers)
        ):
            return False
        cursor.execute(
            """SELECT * FROM vault_master_items WHERE id = ANY(%s) FOR UPDATE""",
            (list(member_numbers),),
        )
        members = [PostgresVaultMasterStore._to_item(row) for row in cursor.fetchall()]
        if len(members) != len(member_numbers):
            return False
        audiences = {member.publication_audience for member in members}
        from app.tv_shows import parse_reviewed_episode
        expected_source = marker["source_directory"]
        expected_title = marker["show_title"]
        expected_season = marker["season_number"]
        return (
            len(audiences) == 1
            and audiences <= {PRIVATE_ASSET_VISIBILITY, VAULT_WIDE_ASSET_VISIBILITY}
            and all(
                member.owner_user_id == item.owner_user_id
                and member.proposed_category == "TV Shows"
                and member.state
                in {
                    "approved", "move_failed", "move_queued", "moving",
                    "theatre_promotion_pending", "moved",
                }
                and member.metadata.get("tv_publication_set") == marker
                and PurePosixPath(member.relative_path.replace("\\", "/")).parent.as_posix()
                == expected_source
                and (parsed := parse_reviewed_episode(member.relative_path, member.filename))
                is not None
                and re.sub(r"\s+", " ", re.sub(r"[\\/:]+", " - ", parsed.show_title)).strip(" .-")
                == expected_title
                and parsed.season_number == expected_season
                and parsed.episode_number == member_numbers[member.id]
                for member in members
            )
        )

    def _publish_completed_tv_set(
        self, marker: dict[str, object], owner_user_id: UUID | None
    ) -> None:
        """Expose a Show only after every root-verified group receipt exists."""
        if owner_user_id is None or marker.get("schema") != TV_PUBLICATION_SET_SCHEMA:
            return
        raw_members = marker.get("members")
        if not isinstance(raw_members, list):
            return
        members = [candidate for candidate in self.list_items() if candidate.metadata.get("tv_publication_set") == marker]
        if len(members) != len(raw_members) or any(candidate.state != "moved" for candidate in members):
            return
        episodes: list[tuple[UUID, UUID, int, str]] = []
        for candidate in members:
            asset = self.get_catalogued_asset(candidate.proposed_destination or "")
            parsed = next((entry for entry in raw_members if isinstance(entry, dict) and entry.get("item_id") == str(candidate.id)), None)
            if asset is None or not isinstance(parsed, dict) or not isinstance(parsed.get("episode_number"), int):
                return
            episodes.append((candidate.id, asset.id, parsed["episode_number"], candidate.proposed_destination or ""))
        from app.tv_shows import PostgresTvShowStore
        show_title = marker.get("show_title")
        season_number = marker.get("season_number")
        if not isinstance(show_title, str) or not isinstance(season_number, int):
            return
        audience = members[0].publication_audience or VAULT_WIDE_ASSET_VISIBILITY
        PostgresTvShowStore(self._conninfo).publish_complete_set(
            owner_user_id=owner_user_id, source_directory=str(marker.get("source_directory", "")),
            show_title=show_title, season_number=season_number, audience=audience, episodes=episodes,
        )

    def recover_moved_tv_publication_set(
        self,
        *,
        owner_user_id: UUID,
        source_directory: str,
        audience: str = VAULT_WIDE_ASSET_VISIBILITY,
    ) -> UUID:
        """Rebuild one missing TV hierarchy from immutable moved-receipt evidence.

        This deliberately has no filesystem operation.  It accepts only a
        complete, unmarked set whose item, receipt, file, asset and placement
        evidence agrees exactly, then records the recovered review marker and
        invokes the existing canonical TV publisher.
        """
        if audience not in {PRIVATE_ASSET_VISIBILITY, VAULT_WIDE_ASSET_VISIBILITY}:
            raise ValueError("TV recovery audience is invalid")
        source_parent = PurePosixPath(source_directory)
        if source_parent.is_absolute() or any(part in {"", ".", ".."} for part in source_parent.parts):
            raise ValueError("TV recovery source directory is invalid")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM vault_master_items
                       WHERE source_kind='incoming' AND owner_user_id=%s
                         AND proposed_category='TV Shows'
                       FOR UPDATE""",
                    (owner_user_id,),
                )
                candidates = [self._to_item(row) for row in cursor.fetchall()]
                members = [
                    candidate for candidate in candidates
                    if PurePosixPath(candidate.relative_path.replace("\\", "/")).parent == source_parent
                ]
                if not members or any(member.state != "moved" for member in members):
                    raise ValueError("TV recovery requires one complete moved publication set")
                parsed_members = []
                from app.tv_shows import parse_reviewed_episode
                for member in members:
                    parsed = parse_reviewed_episode(member.relative_path, member.filename)
                    if parsed is None:
                        raise ValueError("TV recovery requires explicit SnnEnn episode filenames")
                    parsed_members.append((member, parsed))
                titles = {parsed.show_title for _, parsed in parsed_members}
                seasons = {parsed.season_number for _, parsed in parsed_members}
                numbers = [parsed.episode_number for _, parsed in parsed_members]
                if len(titles) != 1 or len(seasons) != 1 or len(numbers) != len(set(numbers)):
                    raise ValueError("TV recovery evidence is not one unambiguous Show and Season")
                safe_title = re.sub(r"[\\/:]+", " - ", next(iter(titles)))
                safe_title = re.sub(r"\s+", " ", safe_title).strip(" .-")
                marker: dict[str, object] = {
                    "schema": TV_PUBLICATION_SET_SCHEMA,
                    "source_directory": source_parent.as_posix(),
                    "show_title": safe_title,
                    "season_number": next(iter(seasons)),
                    "members": [
                        {"item_id": str(member.id), "episode_number": parsed.episode_number}
                        for member, parsed in sorted(parsed_members, key=lambda pair: pair[1].episode_number)
                    ],
                }
                existing_markers = [member.metadata.get("tv_publication_set") for member in members]
                if any(marker is not None for marker in existing_markers):
                    if (
                        not all(existing_marker == marker for existing_marker in existing_markers)
                        or any(member.publication_audience != audience for member in members)
                    ):
                        raise ValueError("TV recovery found divergent durable publication markers")
                    cursor.execute(
                        """SELECT show.id FROM vault_tv_publication_sets publication
                           JOIN vault_tv_shows show ON show.owner_user_id=publication.owner_user_id
                             AND show.title=publication.show_title
                           WHERE publication.owner_user_id=%s AND publication.source_directory=%s
                             AND publication.show_title=%s AND publication.season_number=%s
                             AND publication.audience=%s AND publication.state='published'""",
                        (owner_user_id, source_parent.as_posix(), safe_title, next(iter(seasons)), audience),
                    )
                    existing_show = cursor.fetchone()
                    if existing_show is None:
                        raise ValueError("TV recovery found marker state without its canonical published hierarchy")
                    return UUID(str(existing_show["id"]))
                member_ids = [member.id for member, _ in parsed_members]
                cursor.execute(
                    """SELECT publication.item_id, publication.asset_id, publication.file_id,
                              publication.owner_user_id, publication.logical_destination,
                              publication.logical_area, publication.slot_id, publication.relative_path,
                              asset.owner_user_id AS asset_owner_user_id, asset.lifecycle_state,
                              file.vault_path, file.sha256, file.size_bytes,
                              placement.slot_id AS placement_slot_id,
                              placement.relative_path AS placement_relative_path
                       FROM vault_arrival_managed_publications publication
                       JOIN vault_assets asset ON asset.id=publication.asset_id
                       JOIN vault_files file ON file.id=publication.file_id AND file.asset_id=asset.id
                       JOIN vault_file_storage_placements placement
                         ON placement.file_id=file.id AND placement.slot_id=publication.slot_id
                       WHERE publication.item_id=ANY(%s) FOR UPDATE""",
                    (member_ids,),
                )
                evidence = {UUID(str(row["item_id"])): row for row in cursor.fetchall()}
                if len(evidence) != len(members):
                    raise ValueError("TV recovery requires one complete managed receipt per episode")
                episodes: list[tuple[UUID, UUID, int, str]] = []
                for member, parsed in parsed_members:
                    row = evidence.get(member.id)
                    destination = member.proposed_destination
                    if (
                        row is None or destination is None
                        or row["owner_user_id"] != owner_user_id
                        or row["asset_owner_user_id"] != owner_user_id
                        or row["logical_area"] != "Theatre / TV Shows"
                        or row["logical_destination"] != destination
                        or row["vault_path"] != destination
                        or row["relative_path"] != destination.removeprefix("/vault/")
                        or row["placement_relative_path"] != row["relative_path"]
                        or row["placement_slot_id"] != row["slot_id"]
                        or row["sha256"] != member.sha256
                        or int(row["size_bytes"]) != member.size_bytes
                        or row["lifecycle_state"] != "active"
                    ):
                        raise ValueError("TV recovery receipt, asset, file or placement evidence disagrees")
                    episodes.append((member.id, UUID(str(row["asset_id"])), parsed.episode_number, destination))
        from app.tv_shows import PostgresTvShowStore
        show_id = PostgresTvShowStore(self._conninfo).publish_complete_set(
            owner_user_id=owner_user_id,
            source_directory=source_parent.as_posix(),
            show_title=safe_title,
            season_number=next(iter(seasons)),
            audience=audience,
            episodes=episodes,
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE vault_master_items
                       SET publication_audience=%s, metadata=metadata || %s,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=ANY(%s) AND state='moved'
                         AND proposed_category='TV Shows'
                         AND (publication_audience IS NULL OR publication_audience=%s)
                         AND NOT (metadata ? 'tv_publication_set')""",
                    (audience, Jsonb({"tv_publication_set": marker}), member_ids, audience),
                )
                if cursor.rowcount != len(member_ids):
                    raise RuntimeError("TV recovery state changed while canonical hierarchy was being recorded")
                for member_id in member_ids:
                    cursor.execute(
                        """INSERT INTO vault_master_activity
                           (id, item_id, action, username, detail, succeeded)
                           SELECT %s, %s, 'tv_publication_recovered', %s, %s, TRUE
                           WHERE NOT EXISTS (
                               SELECT 1 FROM vault_master_activity
                               WHERE item_id=%s AND action='tv_publication_recovered'
                           )""",
                        (uuid4(), member_id, "VM-079 recovery", "Reconstructed canonical TV hierarchy from existing managed receipt evidence", member_id),
                    )
        return show_id

    def publish_arrival_theatre_receipt(self, item_id: UUID, receipt: dict[str, object]) -> CataloguedAsset | None:
        """Legacy method name retained while Theatre is the first consumer."""
        return self.publish_arrival_managed_receipt(item_id, receipt)

    def set_catalogued_asset_lifecycle_state(
        self, asset_id: UUID, owner_user_id: UUID, username: str, state: str
    ) -> CataloguedAsset | None:
        if state not in {"active", "hidden"}:
            raise ValueError("Unsupported catalogue lifecycle state")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT lifecycle_state FROM vault_assets
                    WHERE id = %s AND owner_user_id = %s FOR UPDATE
                    """,
                    (asset_id, owner_user_id),
                )
                existing = cursor.fetchone()
                if existing is None:
                    return None
                previous_state = str(existing["lifecycle_state"])
                if previous_state == state:
                    return self.get_catalogued_asset_by_id(asset_id)
                cursor.execute(
                    "UPDATE vault_assets SET lifecycle_state = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (state, asset_id),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_asset_history (
                        id, asset_id, action, username, previous_values, current_values
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        asset_id,
                        "asset_hidden" if state == "hidden" else "asset_unhidden",
                        username,
                        Jsonb({"lifecycle_state": previous_state}),
                        Jsonb({"lifecycle_state": state}),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO vault_master_activity (id, action, username, detail, succeeded)
                    VALUES (%s, %s, %s, %s, TRUE)
                    """,
                    (
                        uuid4(),
                        "asset_hidden" if state == "hidden" else "asset_unhidden",
                        username,
                        f"{state.title()} {asset_id}",
                    ),
                )
        return self.get_catalogued_asset_by_id(asset_id)

    def has_catalogued_asset_deletion(self, asset_id: UUID) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM vault_asset_deletions WHERE asset_id = %s",
                    (asset_id,),
                )
                return cursor.fetchone() is not None

    def reset(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                # Stage 6 remote state is not an owned asset, but outgoing
                # records reference canonical assets.  Clear it first in the
                # controlled test/reset path; ordinary request paths never
                # perform destructive federation cleanup.
                cursor.execute("DELETE FROM vault_federation_download_provenance")
                cursor.execute("DELETE FROM vault_federation_download_operations")
                cursor.execute("DELETE FROM vault_federation_collection_distribution")
                cursor.execute("DELETE FROM vault_federation_distribution")
                cursor.execute("DELETE FROM vault_federation_viewer_progress")
                cursor.execute("DELETE FROM vault_federation_cache_entries")
                cursor.execute("DELETE FROM vault_federation_local_annotations")
                cursor.execute("DELETE FROM vault_federation_origin_metadata")
                cursor.execute("DELETE FROM vault_federation_receipts")
                cursor.execute("DELETE FROM vault_federation_audit")
                cursor.execute("DELETE FROM vault_federation_collection_deliveries")
                cursor.execute("DELETE FROM vault_federation_deliveries")
                cursor.execute("DELETE FROM vault_federation_collection_memberships")
                cursor.execute("DELETE FROM vault_federation_incoming_collections")
                cursor.execute("DELETE FROM vault_federation_incoming_shares")
                cursor.execute("DELETE FROM vault_federation_outgoing_collection_shares")
                cursor.execute("DELETE FROM vault_federation_outgoing_shares")
                cursor.execute("DELETE FROM user_gallery_collection_preferences")
                cursor.execute("DELETE FROM user_gallery_shared_preferences")
                cursor.execute("DELETE FROM vault_share_grants")
                cursor.execute("DELETE FROM vault_collection_share_grants")
                cursor.execute("DELETE FROM vault_shared_collection_members")
                cursor.execute("DELETE FROM vault_shared_collections")
                cursor.execute("DELETE FROM vault_asset_history")
                cursor.execute("DELETE FROM vault_asset_deletions")
                cursor.execute("DELETE FROM vault_asset_relationships")
                cursor.execute("DELETE FROM vault_arrival_managed_publications")
                cursor.execute("DELETE FROM vault_arrival_theatre_promotions")
                cursor.execute("""
                    DO $$ BEGIN
                        IF to_regclass('vault_tv_publication_set_members') IS NOT NULL THEN
                            DELETE FROM vault_tv_publication_set_members;
                            DELETE FROM vault_tv_publication_sets;
                            DELETE FROM vault_tv_episodes;
                            DELETE FROM vault_tv_seasons;
                            DELETE FROM vault_tv_shows;
                        END IF;
                    END $$
                """)
                cursor.execute("DELETE FROM vault_file_storage_placements")
                cursor.execute("DELETE FROM vault_files")
                cursor.execute("DELETE FROM vault_assets")
                cursor.execute("DELETE FROM vault_storage_slot_hardware_history")
                cursor.execute("DELETE FROM vault_storage_slots")
                cursor.execute("DELETE FROM vault_master_activity")
                cursor.execute("DELETE FROM vault_master_decisions")
                cursor.execute("""
                    DO $$ BEGIN
                        IF to_regclass('vault_routing_memory_examples') IS NOT NULL THEN
                            DELETE FROM vault_routing_memory_examples;
                        END IF;
                        IF to_regclass('vault_routing_memory_rules') IS NOT NULL THEN
                            DELETE FROM vault_routing_memory_rules;
                        END IF;
                    END $$
                """)
                cursor.execute("DELETE FROM vault_master_items")
                cursor.execute("DELETE FROM vault_master_batches")


@lru_cache
def get_vault_master_store() -> VaultMasterStore:
    return PostgresVaultMasterStore(
        get_database_conninfo(),
        sidecar_root=get_metadata_storage_root(),
        default_asset_owner=get_admin_username(),
    )
