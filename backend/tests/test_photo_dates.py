from datetime import datetime, timezone

from app.photo_dates import (
    parse_exif_date,
    parse_filename_date,
    select_oldest_photo_date,
)


def test_filename_date_parser_recovers_timestamp_style_names() -> None:
    assert parse_filename_date(
        "2012-07-15T21-52-31_0.jpg"
    ).isoformat() == "2012-07-15"
    assert parse_filename_date(
        "PXL_20250613_182754557.jpg"
    ).isoformat() == "2025-06-13"


def test_oldest_plausible_date_wins_across_all_sources() -> None:
    selected, source = select_oldest_photo_date(
        "2012-07-15T21-52-31_0.jpg",
        datetime(2026, 7, 27, tzinfo=timezone.utc),
        ["2015:10:20 12:00:00"],
    )

    assert selected.isoformat() == "2012-07-15"
    assert source == "filename"


def test_invalid_and_future_dates_are_ignored() -> None:
    selected, source = select_oldest_photo_date(
        "photo-2099-01-01.jpg",
        datetime(2018, 4, 3, tzinfo=timezone.utc),
        ["not-a-date"],
    )

    assert selected.isoformat() == "2018-04-03"
    assert source == "file_modified"


def test_embedded_date_parser_accepts_xmp_and_iptc_dates() -> None:
    assert parse_exif_date("1995-09-03T14:30:00").isoformat() == (
        "1995-09-03"
    )
    assert parse_exif_date("19950903").isoformat() == "1995-09-03"
