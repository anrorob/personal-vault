"""Deterministic, review-only grouping of Reading Room scans in Arrival Hall."""

from dataclasses import dataclass
from pathlib import Path
import unicodedata
from uuid import UUID

from app.vault_master import INCOMING_SOURCE, ImportItem


SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
CORRECTION_AUTHOR_KEY = "reading_room_author"
CORRECTION_TITLE_KEY = "reading_room_title"


def _normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


@dataclass(frozen=True)
class ParsedPublicationFilename:
    author: str
    title: str
    role: str


@dataclass(frozen=True)
class PublicationBundle:
    key: str
    author: str
    title: str
    source_item_ids: tuple[UUID, ...]
    front_cover_item_ids: tuple[UUID, ...]
    back_cover_item_ids: tuple[UUID, ...]
    issues: tuple[str, ...]
    review_status: str = "review_required"


def parse_publication_filename(filename: str) -> ParsedPublicationFilename | None:
    path = Path(filename)
    extension = path.suffix.casefold()
    if extension not in SUPPORTED_EXTENSIONS:
        return None
    stem = path.stem.strip()
    parts = stem.split(" - ")
    role = "source" if extension == ".pdf" else ""
    if extension in {".jpg", ".jpeg", ".png"}:
        lowered = stem.casefold()
        cover_role = next(
            (name for name in ("front", "back") if lowered.endswith((f" - {name}", f"-{name}"))),
            None,
        )
        if cover_role is None:
            return None
        role = f"{cover_role}_cover"
        stem = stem[: -len(cover_role)].rstrip(" -")
        parts = stem.split(" - ")
    if len(parts) < 2:
        parts = [part.strip() for part in stem.split("-", 1)]
    if len(parts) < 2:
        return None
    if "-" in parts[0]:
        compact_author, compact_title = parts[0].split("-", 1)
        parts = [compact_author.strip(), compact_title.strip(), *parts[1:]]
    author = unicodedata.normalize("NFC", parts[0].strip())
    title = unicodedata.normalize("NFC", " - ".join(parts[1:]).strip())
    if not author or not title:
        return None
    return ParsedPublicationFilename(author=author, title=title, role=role)


def _identity_tokens(value: str) -> set[str]:
    folded = "".join(
        character if character.isalnum() else " "
        for character in _normalise(value)
    )
    return {token for token in folded.split() if len(token) > 1}


def _likely_companion(
    source: ParsedPublicationFilename,
    cover: ParsedPublicationFilename,
) -> bool:
    if _normalise(source.author) != _normalise(cover.author):
        return False
    source_tokens = _identity_tokens(source.title)
    cover_tokens = _identity_tokens(cover.title)
    return bool(
        source_tokens
        and cover_tokens
        and (source_tokens <= cover_tokens or cover_tokens <= source_tokens)
    )


def build_publication_bundles(items: list[ImportItem]) -> list[PublicationBundle]:
    raw_groups: dict[
        tuple[UUID, str, str],
        list[tuple[ImportItem, ParsedPublicationFilename]],
    ] = {}
    for item in items:
        if item.source_kind != INCOMING_SOURCE or item.state not in {
            "inventoried",
            "needs_review",
            "approved",
        }:
            continue
        parsed = parse_publication_filename(item.filename)
        if parsed is None:
            continue
        raw_key = (
            item.batch_id,
            _normalise(parsed.author),
            _normalise(parsed.title),
        )
        raw_groups.setdefault(raw_key, []).append((item, parsed))

    inferred_companion_ids: set[UUID] = set()
    source_groups = [
        (key, members)
        for key, members in raw_groups.items()
        if any(parsed.role == "source" for _, parsed in members)
    ]
    for key, members in list(raw_groups.items()):
        if any(parsed.role == "source" for _, parsed in members):
            continue
        candidates = [
            source_key
            for source_key, source_members in source_groups
            if any(
                _likely_companion(source_parsed, cover_parsed)
                for _, source_parsed in source_members
                if source_parsed.role == "source"
                for _, cover_parsed in members
            )
        ]
        if len(candidates) == 1:
            raw_groups[candidates[0]].extend(members)
            inferred_companion_ids.update(item.id for item, _ in members)
            del raw_groups[key]

    raw_groups = {
        key: members
        for key, members in raw_groups.items()
        if (
            any(parsed.role == "source" for _, parsed in members)
            and any(parsed.role.endswith("_cover") for _, parsed in members)
        )
        or all(parsed.role.endswith("_cover") for _, parsed in members)
        or any(item.proposed_category == "Library" for item, _ in members)
        or any(
            CORRECTION_AUTHOR_KEY in item.metadata_overrides
            or CORRECTION_TITLE_KEY in item.metadata_overrides
            for item, _ in members
        )
    }

    grouped: dict[
        tuple[str, str],
        list[tuple[ImportItem, ParsedPublicationFilename]],
    ] = {}
    identities: dict[tuple[str, str], tuple[str, str]] = {}
    originals: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for members in raw_groups.values():
        anchor = next(
            ((item, parsed) for item, parsed in members if parsed.role == "source"),
            members[0],
        )
        anchor_item, anchor_parsed = anchor
        author = str(
            anchor_item.metadata_overrides.get(
                CORRECTION_AUTHOR_KEY,
                anchor_parsed.author,
            )
        ).strip()
        title = str(
            anchor_item.metadata_overrides.get(
                CORRECTION_TITLE_KEY,
                anchor_parsed.title,
            )
        ).strip()
        key = (_normalise(author), _normalise(title))
        grouped.setdefault(key, []).extend(members)
        identities[key] = (author, title)
        originals.setdefault(key, set()).update(
            (parsed.author, parsed.title) for _, parsed in members
        )

    bundles: list[PublicationBundle] = []
    checksum_counts: dict[str, int] = {}
    for members in grouped.values():
        for item, _ in members:
            checksum_counts[item.sha256] = checksum_counts.get(item.sha256, 0) + 1

    for normalised_key, members in grouped.items():
        sources = tuple(item.id for item, parsed in members if parsed.role == "source")
        fronts = tuple(
            item.id for item, parsed in members if parsed.role == "front_cover"
        )
        backs = tuple(
            item.id for item, parsed in members if parsed.role == "back_cover"
        )
        author, title = identities[normalised_key]
        issues: list[str] = []
        if not sources:
            issues.append("missing_source_pdf")
        if len(sources) > 1:
            issues.append("multiple_source_pdfs")
        if len(fronts) > 1:
            issues.append("ambiguous_front_cover")
        if len(backs) > 1:
            issues.append("ambiguous_back_cover")
        if len(originals[normalised_key]) > 1 and not any(
            item.id in inferred_companion_ids for item, _ in members
        ):
            issues.append("normalised_identity_collision")
        if any(checksum_counts[item.sha256] > 1 for item, _ in members):
            issues.append("duplicate_checksum")
        bundles.append(
            PublicationBundle(
                key=f"{normalised_key[0]}::{normalised_key[1]}",
                author=author,
                title=title,
                source_item_ids=sources,
                front_cover_item_ids=fronts,
                back_cover_item_ids=backs,
                issues=tuple(issues),
            )
        )
    return sorted(
        bundles,
        key=lambda bundle: (
            _normalise(bundle.author),
            _normalise(bundle.title),
        ),
    )
