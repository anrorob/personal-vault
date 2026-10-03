from datetime import datetime, timezone

from app.photo_dates import resolve_photo_date
from app.vault_master import apply_source_timestamp_provenance


def test_source_modified_is_used_when_no_embedded_capture_exists() -> None:
    metadata = apply_source_timestamp_provenance(
        {"capture_date_source": "unavailable"},
        {"source_modified_at": "2025-11-21T10:30:00Z"},
    )

    assert metadata["captured_at"] == "2025-11-21"
    assert metadata["capture_date_source"] == "source_file_modified"


def test_embedded_capture_always_wins_over_source_filesystem_time() -> None:
    captured, provenance = resolve_photo_date(
        ("2024:05:01 10:00:00",),
        "2025-11-22T10:30:00Z",
        "2025-11-21T10:30:00Z",
    )

    assert captured and captured.isoformat() == "2024-05-01"
    assert provenance == "embedded"


def test_oldest_valid_source_time_and_invalid_values() -> None:
    captured, provenance = resolve_photo_date(
        (), "2025-11-22T10:30:00Z", "2025-11-21T10:30:00Z"
    )
    assert captured and captured.isoformat() == "2025-11-21"
    assert provenance == "source_file_modified"

    captured, provenance = resolve_photo_date(
        (), "1970-01-01T00:00:00Z", "not-a-timestamp"
    )
    assert captured is None
    assert provenance == "unavailable"
