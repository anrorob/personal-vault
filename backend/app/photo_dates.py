from datetime import date, datetime, timezone
from pathlib import Path
import re
from typing import Iterable


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


def select_oldest_photo_date(
    filename: str,
    modified_at: datetime,
    embedded_values: Iterable[object],
) -> tuple[date, str]:
    today = datetime.now(timezone.utc).date()
    candidates: list[tuple[date, int, str]] = []

    for value in embedded_values:
        parsed = parse_exif_date(value)
        if parsed is not None:
            candidates.append((parsed, 0, "embedded"))

    filename_date = parse_filename_date(filename)
    if filename_date is not None:
        candidates.append((filename_date, 1, "filename"))

    candidates.append((modified_at.date(), 2, "file_modified"))
    plausible = [
        candidate
        for candidate in candidates
        if MINIMUM_PHOTO_DATE <= candidate[0] <= today
    ]
    if not plausible:
        return modified_at.date(), "file_modified"

    selected_date, _, source = min(
        plausible,
        key=lambda candidate: (candidate[0], candidate[1]),
    )
    return selected_date, source
