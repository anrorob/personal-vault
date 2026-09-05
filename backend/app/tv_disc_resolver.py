"""Deterministic, review-only resolver for MakeMKV-style TV disc tracks.

This module deliberately has no publication side effects.  It turns staged
Arrival Hall facts into a proposal which must still be accepted through the
existing TV publication workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from statistics import median
from typing import Iterable
from uuid import UUID

from app.vault_master import INCOMING_SOURCE, ImportItem


RESOLVER_VERSION = "pv-tv-disc-resolver.v1"
_DISC_TRACK = re.compile(r"\bdisc\s*(?P<disc>\d+)\s*[_ -]*t(?P<track>\d+)\b", re.I)
_SEASON = re.compile(r"\bseason\s*(?P<season>\d{1,3})\b", re.I)
_SHOW_FROM_NAME = re.compile(r"^(?P<show>.+?)\s*-?\s*season\s*\d{1,3}\b", re.I)


@dataclass(frozen=True)
class TvTrackProposal:
    item_id: UUID
    filename: str
    season_number: int | None
    disc_number: int | None
    track_number: int | None
    duration_seconds: float | None
    classification: str
    episode_number: int | None
    confidence: str
    evidence: tuple[str, ...]
    destination: str | None


@dataclass(frozen=True)
class TvBatchProposal:
    batch_key: str
    show_title: str | None
    confidence: str
    needs_review: bool
    evidence: tuple[str, ...]
    tracks: tuple[TvTrackProposal, ...]


def parse_disc_track(filename: str) -> tuple[int, int] | None:
    """Parse a MakeMKV disc/track hint; t00 remains meaningful evidence."""
    match = _DISC_TRACK.search(Path(filename).stem)
    return (int(match["disc"]), int(match["track"])) if match else None


def _path_context(item: ImportItem) -> tuple[str | None, int | None]:
    # `source_context` is advisory-only and may be introduced by Supplier
    # deployments.  The Arrival Hall relative path remains the fallback.
    context = item.metadata.get("source_context")
    relative = context.get("relative_path") if isinstance(context, dict) else item.relative_path
    if not isinstance(relative, str):
        relative = item.relative_path
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized) or "//" in normalized:
        return None, None
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"."})
    if not parts or any(part == ".." for part in parts):
        return None, None
    season = next((int(match["season"]) for part in reversed(parts[:-1]) if (match := _SEASON.search(part))), None)
    show = None
    if season is not None:
        season_index = next((index for index, part in enumerate(parts) if _SEASON.search(part)), None)
        if season_index and parts[season_index - 1] not in {".", ".."}:
            show = parts[season_index - 1]
    source_label = context.get("source_label") if isinstance(context, dict) else None
    label = _clean_show(source_label) if isinstance(source_label, str) else None
    return _clean_show(show) or label, season


def _clean_show(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"[_-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or None


def _filename_context(item: ImportItem) -> tuple[str | None, int | None]:
    stem = Path(item.filename).stem
    season_match = _SEASON.search(stem)
    title_match = _SHOW_FROM_NAME.search(stem)
    return _clean_show(title_match["show"] if title_match else None), (int(season_match["season"]) if season_match else None)


def _duration(item: ImportItem) -> float | None:
    value = item.metadata.get("duration_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _duration_cluster(durations: list[float]) -> tuple[float, float] | None:
    """Return a dominant long-form centre and tolerance without a global cutoff.

    The highest dense runtime band is selected; requiring three members keeps
    sparse and ambiguous batches review-only.  A longer finale is accepted by
    the asymmetric upper tolerance.
    """
    if len(durations) < 3:
        return None
    ordered = sorted(durations)
    best: list[float] = []
    for candidate in ordered:
        band = [duration for duration in ordered if candidate * .72 <= duration <= candidate * 1.45]
        if len(band) > len(best) or (len(band) == len(best) and sum(band) > sum(best)):
            best = band
    if len(best) < 3:
        return None
    centre = float(median(best))
    return centre, centre * .32


def resolve_tv_disc_batch(items: Iterable[ImportItem]) -> TvBatchProposal:
    candidates = [item for item in items if item.source_kind == INCOMING_SOURCE and item.state not in {"moved", "arrival_removed", "rejected"}]
    parsed = []
    for item in candidates:
        path_show, path_season = _path_context(item)
        name_show, name_season = _filename_context(item)
        disc_track = parse_disc_track(item.filename)
        parsed.append((item, path_show, path_season, name_show, name_season, disc_track, _duration(item)))
    shows = {show.casefold(): show for _, path_show, _, name_show, _, _, _ in parsed for show in (path_show, name_show) if show}
    show_title = next(iter(shows.values()), None) if len(shows) == 1 else None
    durations = [duration for *_, duration in parsed if duration is not None]
    cluster = _duration_cluster(durations)
    duplicate_item_ids: set[UUID] = set()
    seen_hashes: set[str] = set()
    # Preserve the first staged instance as the candidate.  Exact later
    # instances are never assigned an independent episode number.
    for item, *_ in parsed:
        if item.sha256 in seen_hashes:
            duplicate_item_ids.add(item.id)
        seen_hashes.add(item.sha256)

    evidence: list[str] = [f"resolver={RESOLVER_VERSION}"]
    if show_title:
        evidence.append("show name agrees across available context")
    else:
        evidence.append("show name is missing or conflicting")
    if cluster:
        evidence.append(f"runtime cluster centre={round(cluster[0])}s")
    else:
        evidence.append("no reliable runtime cluster")

    preliminary: list[TvTrackProposal] = []
    for item, path_show, path_season, name_show, name_season, disc_track, duration in parsed:
        track_evidence: list[str] = []
        conflict = path_season is not None and name_season is not None and path_season != name_season
        season = path_season or name_season
        if path_season is not None and path_season == name_season:
            track_evidence.append("folder and filename season agree")
        elif conflict:
            track_evidence.append("folder and filename season conflict")
        if disc_track:
            track_evidence.append("MakeMKV disc-track parsed")
        if item.id in duplicate_item_ids or item.duplicate_of_id is not None:
            classification = "duplicate"
        elif conflict or season is None or not disc_track:
            classification = "unresolved"
        elif cluster and duration is not None and cluster[0] - cluster[1] <= duration <= cluster[0] * 1.75:
            classification = "likely_episode"
            track_evidence.append("duration belongs to dominant long-form cluster")
        elif cluster and duration is not None and duration < cluster[0] - cluster[1]:
            classification = "likely_extra"
            track_evidence.append("duration is below dominant long-form cluster")
        else:
            classification = "unresolved"
        preliminary.append(TvTrackProposal(item.id, item.filename, season, disc_track[0] if disc_track else None, disc_track[1] if disc_track else None, duration, classification, None, "low", tuple(track_evidence), None))

    numbered: list[TvTrackProposal] = []
    per_season: dict[int, int] = {}
    for track in sorted(preliminary, key=lambda entry: (entry.season_number or 9999, entry.disc_number or 9999, entry.track_number or 9999, entry.filename.casefold())):
        high = track.classification == "likely_episode" and show_title is not None
        episode = None
        confidence = "low"
        destination = None
        if high and track.season_number is not None:
            per_season[track.season_number] = per_season.get(track.season_number, 0) + 1
            episode = per_season[track.season_number]
            confidence = "high"
            destination = f"/vault/Theatre/TV Shows/{show_title}/Season {track.season_number:02d}/{show_title} - S{track.season_number:02d}E{episode:02d}{Path(track.filename).suffix.casefold()}"
        numbered.append(TvTrackProposal(**{**track.__dict__, "episode_number": episode, "confidence": confidence, "destination": destination}))
    ordered_by_item = {track.item_id: track for track in numbered}
    tracks = tuple(ordered_by_item[item.id] for item, *_ in parsed)
    high_count = sum(track.confidence == "high" for track in tracks)
    batch_confidence = "high" if high_count >= 3 and all(track.classification != "unresolved" for track in tracks if track.classification != "likely_extra") else "medium" if high_count else "low"
    return TvBatchProposal("|".join(sorted(str(item.id) for item in candidates)), show_title, batch_confidence, batch_confidence != "high", tuple(evidence), tracks)


def discover_tv_disc_batches(items: Iterable[ImportItem]) -> tuple[tuple[ImportItem, ...], ...]:
    """Conservatively group only items sharing a parsed show identity.

    A MakeMKV track name and a season signal are both required.  This prevents
    unrelated video files that happened to arrive together from becoming one
    review decision.
    """
    groups: dict[tuple[UUID | None, str], list[ImportItem]] = {}
    for item in items:
        if item.source_kind != INCOMING_SOURCE or parse_disc_track(item.filename) is None:
            continue
        path_show, path_season = _path_context(item)
        name_show, name_season = _filename_context(item)
        show = path_show or name_show
        if show is None or (path_season is None and name_season is None):
            continue
        context = item.metadata.get("source_context")
        source_id = context.get("source_id") if isinstance(context, dict) else None
        source_label = context.get("source_label") if isinstance(context, dict) else None
        source_key = source_id if isinstance(source_id, str) and source_id else source_label
        grouping_key = f"supplier:{source_key.casefold()}" if isinstance(source_key, str) and source_key else f"show:{show.casefold()}"
        groups.setdefault((item.owner_user_id, grouping_key), []).append(item)
    return tuple(tuple(group) for _, group in sorted(groups.items(), key=lambda pair: pair[0][1]))
