from datetime import datetime, timezone

from app.photo_dates import (
    parse_exif_date,
    parse_filename_date,
    select_oldest_photo_date,
)


def test_filename_date_parser_recovers_timestamp_style_names() -> None:
    assert parse_filename_date(
        "2001-02-03T04-05-06_0.jpg"
    ).isoformat() == "2001-02-03"
    assert parse_filename_date(
        "PXL_20020304_050607008.jpg"
    ).isoformat() == "2002-03-04"


def test_embedded_capture_wins_over_filename_and_pv_modified_time() -> None:
    selected, source = select_oldest_photo_date(
        "2001-02-03T04-05-06_0.jpg",
        datetime(2026, 7, 27, tzinfo=timezone.utc),
        ["2015:10:20 12:00:00"],
    )

    assert selected and selected.isoformat() == "2015-10-20"
    assert source == "embedded"


def test_no_embedded_or_original_source_date_is_unknown() -> None:
    selected, source = select_oldest_photo_date(
        "photo-2099-01-01.jpg",
        datetime(2018, 4, 3, tzinfo=timezone.utc),
        ["not-a-date"],
    )

    assert selected is None
    assert source == "unavailable"


def test_embedded_date_parser_accepts_xmp_and_iptc_dates() -> None:
    assert parse_exif_date("1995-09-03T14:30:00").isoformat() == (
        "1995-09-03"
    )
    assert parse_exif_date("19950903").isoformat() == "1995-09-03"
