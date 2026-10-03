from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Iterable


# Original-file evidence only. Never recursively collect arbitrary *_at fields.
GALLERY_DATE_FIELDS = (
    "source_created_at", "source_modified_at", "exif_original_at",
    "exif_created_at", "exif_modified_at", "xmp_created_at", "xmp_original_at", "iptc_created_at",
    "captured_at", "captured_on", "capture_date_source",
    "document_created_at", "document_modified_at",
)


def _valid_evidence_date(value: object, *, source: bool = False) -> date | None:
    if not isinstance(value, str):
        return None
    if source and (timestamp := parse_source_timestamp(value)) is not None:
        return timestamp.date()
    parsed = parse_exif_date(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
        except ValueError:
            return None
    if parsed < MINIMUM_PHOTO_DATE or parsed in {date(1900, 1, 1), date(1970, 1, 1)}:
        return None
    if parsed > (datetime.now(timezone.utc) + timedelta(days=2)).date():
        return None
    return parsed


def gallery_date_evidence(metadata: dict[str, object]) -> dict[str, object]:
    """Copy only known source date fields, retaining their original precision."""
    result = {key: metadata[key] for key in GALLERY_DATE_FIELDS if key in metadata}
    context = metadata.get("source_context")
    if isinstance(context, dict):
        result["source_context"] = {
            key: context[key] for key in ("source_created_at", "source_modified_at")
            if key in context
        }
    return result


def normalize_source_date(value: object) -> str | None:
    if _valid_evidence_date(value, source=True) is None:
        return None
    parsed = parse_source_timestamp(value)
    return parsed.isoformat() if parsed else str(value).strip()


def retain_oldest_source_dates(previous: dict[str, object], incoming: dict[str, object]) -> dict[str, object]:
    result = dict(incoming)
    for key in ("source_created_at", "source_modified_at"):
        old = _valid_evidence_date(previous.get(key), source=True)
        new = _valid_evidence_date(result.get(key), source=True)
        if old is not None and (new is None or old < new):
            result[key] = previous[key]
    return result


def resolve_gallery_effective_date(
    layers: Iterable[dict[str, object]],
    overrides: dict[str, object] | None = None,
    *, legacy_date: date | None = None, legacy_source: str = "unavailable",
) -> tuple[date | None, str]:
    """One Gallery authority: owner correction, exact capture, oldest source date."""
    manual = (overrides or {}).get("captured_on")
    if manual:
        # Owner corrections are preserved exactly; source sentinel rules do not apply.
        return date.fromisoformat(str(manual)[:10]), "user_override"
    if legacy_source == "user_override" and legacy_date is not None:
        return legacy_date, "user_override"
    exact: list[date] = []
    historical_capture: list[date] = []
    fallback: list[tuple[date, str]] = []
    for layer in layers:
        for field in ("exif_original_at", "xmp_original_at", "iptc_created_at"):
            if (parsed := _valid_evidence_date(layer.get(field))) is not None:
                exact.append(parsed)
        # Historical extraction stored capture evidence under these existing keys.
        if layer.get("capture_date_source") in {"embedded", "video_container"}:
            if (parsed := _valid_evidence_date(layer.get("captured_at") or layer.get("captured_on"))) is not None:
                historical_capture.append(parsed)
        for field in ("exif_created_at", "exif_modified_at", "xmp_created_at", "document_created_at", "document_modified_at"):
            if (parsed := _valid_evidence_date(layer.get(field))) is not None:
                fallback.append((parsed, "embedded_file_date"))
        context = layer.get("source_context")
        for evidence in (layer, context if isinstance(context, dict) else {}):
            for field, source_name in (("source_created_at", "source_file_created"), ("source_modified_at", "source_file_modified")):
                if (parsed := _valid_evidence_date(evidence.get(field), source=True)) is not None:
                    fallback.append((parsed, source_name))
    if exact:
        return min(exact), "embedded"
    if historical_capture:
        return min(historical_capture), "embedded"
    if legacy_date is not None and legacy_source == "embedded":
        return legacy_date, "embedded"
    if fallback:
        return min(fallback, key=lambda value: (value[0], value[1]))
    return None, "unavailable"


MINIMUM_PHOTO_DATE = date(1900, 1, 1)
FILENAME_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})"
    r"[-_]?(?P<month>0[1-9]|1[0-2])"
    r"[-_]?(?P<day>0[1-9]|[12]\d|3[01])(?!\d)"
)


def parse_exif_date(value: object) -> date | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return None

    text = value.strip().rstrip("\x00")
    for date_format in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def parse_filename_date(filename: str) -> date | None:
    match = FILENAME_DATE_PATTERN.search(Path(filename).stem)
    if match is None:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def parse_source_timestamp(value: object, *, now: datetime | None = None) -> datetime | None:
    """Parse a trusted-source timestamp without ever accepting a PV timestamp."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    # Windows epoch placeholders and future clock errors are not evidence.
    if parsed.date() < date(1980, 1, 1) or parsed > current + timedelta(days=2):
        return None
    return parsed


def resolve_photo_date(
    embedded_values: Iterable[object],
    source_created_at: object = None,
    source_modified_at: object = None,
) -> tuple[date | None, str]:
    """Embedded capture evidence wins; otherwise use oldest trusted source time."""
    return resolve_gallery_effective_date((
        *({"exif_original_at": value} for value in embedded_values),
        {"source_created_at": source_created_at, "source_modified_at": source_modified_at},
    ))


def select_oldest_photo_date(
    filename: str,
    modified_at: datetime,
    embedded_values: Iterable[object],
) -> tuple[date | None, str]:
    # Kept for callers while deliberately excluding filename and destination
    # filesystem mtime: neither is original capture evidence.
    del filename, modified_at
    return resolve_photo_date(embedded_values)
