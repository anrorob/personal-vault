from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zipfile import ZipFile

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.vault_master import (
    INCOMING_SOURCE,
    INVENTORY_SOURCE,
    CataloguedAsset,
    MemoryVaultMasterStore,
    ScannedFile,
    _extract_audio_metadata,
    _extract_exif_gps,
    _extract_exif_technical,
    _extract_iptc_metadata,
    _extract_ooxml_metadata,
    _extract_pdf_metadata,
    _extract_tar_metadata,
    _extract_video_metadata,
    _extract_zip_metadata,
    analyse_asset_relationship,
    asset_is_editable_by,
    asset_is_visible_to,
    _extract_xmp_metadata,
    create_deterministic_proposal,
    effective_asset_metadata,
    enqueue_root,
    enqueue_catalogue_backfill,
    inventory_catalogue_location,
    normalise_typed_metadata,
    portable_ingestion_evidence,
    process_next_batch,
    process_next_move,
    require_file_within_root,
    safely_move_approved_file,
    safely_remove_exact_duplicate,
    scan_file,
    scan_root,
    sha256_file,
)


def test_owner_editability_requires_exact_immutable_user_id() -> None:
    owner_id = uuid4()
    asset = CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="photo", captured_on=None,
        location=None, vault_path="/vault/Gallery/photo.jpg", filename="photo.jpg",
        size_bytes=1, mime_type="image/jpeg", sha256="a" * 64,
        metadata={}, metadata_provenance={},
        owner_username="owner", owner_user_id=owner_id,
    )
    owner = SimpleNamespace(user_id=owner_id, username="renamed-login")
    same_name_other_user = SimpleNamespace(user_id=uuid4(), username="owner")

    assert asset_is_editable_by(asset, owner)
    assert not asset_is_editable_by(asset, same_name_other_user)
    assert not asset_is_editable_by(replace(asset, owner_user_id=None), owner)
    assert not asset_is_editable_by(asset, "owner")


def test_memory_catalogue_visibility_requires_immutable_user_ids() -> None:
    owner_id = uuid4()
    recipient_id = uuid4()
    same_name_other_user_id = uuid4()
    asset = CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="photo", captured_on=None,
        location=None, vault_path="/vault/Gallery/photo.jpg", filename="photo.jpg",
        size_bytes=1, mime_type="image/jpeg", sha256="a" * 64,
        metadata={}, metadata_provenance={}, owner_username="owner", owner_user_id=owner_id,
        visibility="shared", shared_with=("recipient",), shared_with_user_ids=(recipient_id,),
    )

    assert asset_is_visible_to(asset, SimpleNamespace(user_id=owner_id, username="renamed-owner"))
    assert asset_is_visible_to(asset, SimpleNamespace(user_id=recipient_id, username="recipient"))
    assert not asset_is_visible_to(asset, SimpleNamespace(user_id=same_name_other_user_id, username="recipient"))
    assert not asset_is_visible_to(asset, "recipient")
    assert not asset_is_visible_to(asset, SimpleNamespace(username="recipient"))


def test_hidden_lifecycle_preserves_canonical_asset_identity() -> None:
    owner_id = uuid4()
    asset = CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="photo", captured_on=None,
        location=None, vault_path="/vault/Gallery/photo.jpg", filename="photo.jpg",
        size_bytes=1, mime_type="image/jpeg", sha256="a" * 64,
        metadata={}, metadata_provenance={}, owner_username="owner", owner_user_id=owner_id,
    )
    store = MemoryVaultMasterStore()
    store.restore_catalogued_asset(asset, "owner")

    hidden = store.set_catalogued_asset_lifecycle_state(asset.id, owner_id, "owner", "hidden")
    restored = store.set_catalogued_asset_lifecycle_state(asset.id, owner_id, "owner", "active")

    assert hidden is not None and hidden.lifecycle_state == "hidden"
    assert hidden.id == asset.id and hidden.vault_path == asset.vault_path
    assert restored is not None and restored.lifecycle_state == "active"
    assert restored.id == asset.id and restored.owner_user_id == owner_id
    assert [entry["action"] for entry in store.list_catalogued_asset_history(asset.id)[:2]] == [
        "asset_unhidden",
        "asset_hidden",
    ]


def test_canonical_numeric_metadata_is_not_stored_as_text() -> None:
    assert normalise_typed_metadata(
        {
            "track_number": "03/13",
            "disc_number": "1/2",
            "release_year": "2021",
            "quantity": "12",
            "mass_kg": "2,5",
        }
    ) == {
        "track_number": 3,
        "track_total": 13,
        "disc_number": 1,
        "disc_total": 2,
        "release_year": 2021,
        "quantity": 12,
        "mass_kg": 2.5,
    }


def test_portable_ingestion_evidence_preserves_typed_values_and_iso_date() -> None:
    evidence = portable_ingestion_evidence(
        {
            "content_type": "financial_document",
            "caption": "Bank statement",
            "ocr_text": "Balance 123.45",
            "confidence": 0.91,
            "reasons": ["OCR contains financial terms"],
            "model_id": "florence-2-large",
            "model_revision": "pinned",
            "task_version": "v1",
            "processing_ms": 250,
            "recommended_destination": "Ledger",
            "decision_score": 91,
            "routing_band": "automatic_eligible",
            "confidence_components": {"ocr": 91},
            "conflicts": [],
            "automatic_disqualifiers": [],
            "decision_model_version": "intelligent-routing-v1",
            "created_at": datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc),
        }
    )
    assert evidence is not None
    assert evidence["decision_score"] == 91
    assert isinstance(evidence["decision_score"], int)
    assert evidence["confidence"] == 0.91
    assert evidence["analysed_at"] == "2026-08-02T12:30:00+00:00"


def relationship_asset(
    asset_number: int,
    filename: str,
    sha256: str,
    *,
    size_bytes: int = 1_000,
    metadata: dict[str, object] | None = None,
) -> CataloguedAsset:
    return CataloguedAsset(
        id=UUID(int=asset_number),
        asset_type="Movies",
        display_title=Path(filename).stem,
        captured_on=None,
        location=None,
        vault_path=f"/vault/Theatre/Movies/{filename}",
        filename=filename,
        size_bytes=size_bytes,
        mime_type="video/x-matroska",
        sha256=sha256,
        metadata=metadata or {},
        metadata_provenance={},
        effective_metadata=metadata or {},
    )


def test_asset_relationship_analysis_distinguishes_exact_and_probable_duplicates() -> None:
    exact = analyse_asset_relationship(
        relationship_asset(1, "Family Film.mkv", "a" * 64),
        relationship_asset(2, "Renamed Film.mkv", "a" * 64),
    )
    probable = analyse_asset_relationship(
        relationship_asset(
            3,
            "Family Film 1080p.mkv",
            "b" * 64,
            metadata={"duration_seconds": 120.0, "width": 1920, "height": 1080},
        ),
        relationship_asset(
            4,
            "Family Film copy.mkv",
            "c" * 64,
            size_bytes=1_010,
            metadata={"duration_seconds": 121.2, "width": 1920, "height": 1080},
        ),
    )

    assert exact.classification == "exact_duplicate"
    assert exact.confidence == "certain"
    assert exact.evidence == ("SHA-256 checksums are identical",)
    assert probable.classification == "probable_duplicate"
    assert probable.confidence == "high"
    assert any("Durations differ" in evidence for evidence in probable.evidence)
    assert any("Pixel dimensions match" in evidence for evidence in probable.evidence)


def test_asset_relationship_analysis_labels_editions_and_weak_relationships() -> None:
    alternate = analyse_asset_relationship(
        relationship_asset(5, "Family Film theatrical.mkv", "d" * 64),
        relationship_asset(6, "Family Film extended.mkv", "e" * 64, size_bytes=2_000),
    )
    related = analyse_asset_relationship(
        relationship_asset(7, "Family Film.mkv", "f" * 64),
        relationship_asset(8, "Family Film copy.mkv", "1" * 64, size_bytes=4_000),
    )
    unrelated = analyse_asset_relationship(
        relationship_asset(9, "Holiday.mkv", "2" * 64),
        relationship_asset(10, "Birthday.mkv", "3" * 64),
    )

    assert alternate.classification == "alternate_version"
    assert alternate.confidence == "medium"
    assert alternate.evidence[-1] == "Edition markers differ: extended, theatrical"
    assert related.classification == "related_file"
    assert related.confidence == "low"
    assert unrelated.classification == "none"
    assert unrelated.evidence == ()


def test_canonical_asset_relationship_normalises_pair_without_mutating_assets() -> None:
    store = MemoryVaultMasterStore()
    first = relationship_asset(20, "Family Film.mkv", "a" * 64)
    second = relationship_asset(19, "Family Film copy.mkv", "b" * 64)
    store.restore_catalogued_asset(first, first.owner_username)
    store.restore_catalogued_asset(second, second.owner_username)

    relationship = store.create_catalogued_asset_relationship(
        first.id,
        second.id,
        "duplicate",
        "high",
        ("Normalised filename identity matches: family film",),
        first.owner_username,
    )

    assert relationship is not None
    assert relationship.first_asset_id == second.id
    assert relationship.second_asset_id == first.id
    assert relationship.relationship_type == "duplicate"
    assert relationship.evidence == (
        "Normalised filename identity matches: family film",
    )
    assert store.create_catalogued_asset_relationship(
        second.id,
        first.id,
        "related_file",
        "low",
        ("Different evidence",),
        first.owner_username,
    ) is None
    assert store.get_catalogued_asset_by_id(first.id) == first
    assert store.get_catalogued_asset_by_id(second.id) == second


class FakeExif(dict[int, object]):
    def get_ifd(self, tag_id: int) -> object:
        return self[tag_id]


class FakeAudioInfo:
    length = 183.4567
    bitrate = 921600
    sample_rate = 48000
    channels = 2
    bits_per_sample = 24


class FakeAudio:
    info = FakeAudioInfo()
    tags = {
        "title": ["Family recording"],
        "album": ["Home tapes"],
        "artist": ["Owner"],
        "albumartist": ["Family"],
        "composer": ["Alice"],
        "genre": ["Spoken Word"],
        "tracknumber": ["2/8"],
        "discnumber": ["1/1"],
        "comment": ["Digitised from cassette"],
        "copyright": ["Private"],
        "publisher": ["Personal Vault"],
        "isrc": ["GBABC9500001"],
        "originaldate": ["1995-09-03"],
        "date": ["2026"],
    }


class FakeAsfAttribute:
    def __init__(self, value: object) -> None:
        self.value = value


class FakeAsfAudio(FakeAudio):
    tags = {
        "Title": [FakeAsfAttribute("Track01")],
        "Author": [FakeAsfAttribute("Unknown Artist")],
        "WM/AlbumArtist": [FakeAsfAttribute("Unknown Artist")],
        "WM/AlbumTitle": [FakeAsfAttribute("Unknown Title")],
        "WM/TrackNumber": [FakeAsfAttribute(1)],
        "WM/PartOfSet": [FakeAsfAttribute("1/1")],
        "WM/Year": [FakeAsfAttribute("-1")],
    }


def test_audio_tags_and_technical_facts_are_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "family-recording.flac"
    recording.write_bytes(b"test audio")
    monkeypatch.setattr(
        "app.vault_master.MutagenFile",
        lambda path, easy: FakeAudio(),
    )

    assert _extract_audio_metadata(recording) == {
        "audio_format": "flac",
        "audio_codec": "FakeAudio",
        "duration_seconds": 183.457,
        "bitrate_bps": 921600,
        "sample_rate_hz": 48000,
        "channel_count": 2,
        "bits_per_sample": 24,
        "display_title": "Family recording",
        "album": "Home tapes",
        "artist": "Owner",
        "album_artist": "Family",
        "composer": "Alice",
        "genre": "Spoken Word",
        "track_number": "2/8",
        "disc_number": "1/1",
        "description": "Digitised from cassette",
        "copyright": "Private",
        "publisher": "Personal Vault",
        "isrc": "GBABC9500001",
        "audio_recorded_at": "1995-09-03T00:00:00+00:00",
        "captured_at": "1995-09-03T00:00:00+00:00",
        "release_year": "1995",
        "capture_date_source": "audio_tag",
    }


def test_wma_asf_tags_and_mime_type_are_normalised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "Track01.wma"
    recording.write_bytes(b"test wma audio")
    monkeypatch.setattr(
        "app.vault_master.MutagenFile",
        lambda path, easy: FakeAsfAudio(),
    )

    scanned = scan_file(recording, tmp_path)

    assert scanned.mime_type == "audio/x-ms-wma"
    assert scanned.metadata["display_title"] == "Track01"
    assert scanned.metadata["artist"] == "Unknown Artist"
    assert scanned.metadata["album_artist"] == "Unknown Artist"
    assert scanned.metadata["album"] == "Unknown Title"
    assert scanned.metadata["track_number"] == "1"
    assert scanned.metadata["disc_number"] == "1/1"
    assert "audio_recorded_at" not in scanned.metadata


def test_audio_extraction_ignores_non_audio_and_invalid_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _extract_audio_metadata(tmp_path / "notes.txt") == {}
    recording = tmp_path / "invalid.mp3"
    recording.write_bytes(b"not audio")
    monkeypatch.setattr(
        "app.vault_master.MutagenFile",
        lambda path, easy: None,
    )
    assert _extract_audio_metadata(recording) == {}


def test_video_container_and_stream_facts_are_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "family-recording.mp4"
    recording.write_bytes(b"test video")
    probe = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "62.4567",
            "size": "12500000",
            "bit_rate": "1600000",
            "tags": {"creation_time": "2026-07-29T12:00:00Z"},
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "codec_long_name": "H.265 / HEVC",
                "profile": "Main 10",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "color_space": "bt2020nc",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "field_order": "progressive",
                "avg_frame_rate": "30000/1001",
                "bit_rate": "1400000",
                "tags": {
                    "language": "und",
                    "handler_name": "VideoHandler",
                    "creation_time": "1995-09-03T10:30:00Z",
                },
                "disposition": {"default": 1, "forced": 0},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "bit_rate": "192000",
                "tags": {"language": "eng", "title": "Original audio"},
                "disposition": {"default": 1},
            },
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "mov_text",
                "tags": {"language": "eng"},
            },
        ],
    }
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(stdout=json.dumps(probe).encode())

    monkeypatch.setattr("app.vault_master.subprocess.run", fake_run)

    metadata = _extract_video_metadata(recording)

    assert observed["command"] == [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        "--",
        str(recording),
    ]
    assert observed["check"] is True
    assert observed["capture_output"] is True
    assert "shell" not in observed
    assert metadata["container_format"] == "mov,mp4,m4a,3gp,3g2,mj2"
    assert metadata["duration_seconds"] == 62.457
    assert metadata["bitrate_bps"] == 1600000
    assert metadata["container_size_bytes"] == 12500000
    assert metadata["stream_count"] == 3
    assert metadata["video_stream_count"] == 1
    assert metadata["audio_stream_count"] == 1
    assert metadata["subtitle_stream_count"] == 1
    assert metadata["video_codec"] == "hevc"
    assert metadata["width"] == 3840
    assert metadata["height"] == 2160
    assert metadata["frame_rate_fps"] == 29.97
    assert metadata["captured_at"] == "1995-09-03T10:30:00+00:00"
    assert metadata["capture_date_source"] == "video_container"
    streams = metadata["streams"]
    assert isinstance(streams, list)
    assert streams[0]["disposition"] == ["default"]
    assert streams[1]["language"] == "eng"


def test_video_extraction_tolerates_missing_or_invalid_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _extract_video_metadata(tmp_path / "notes.txt") == {}
    recording = tmp_path / "invalid.mkv"
    recording.write_bytes(b"not video")
    monkeypatch.setattr(
        "app.vault_master.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"not json"),
    )
    assert _extract_video_metadata(recording) == {}


def test_ooxml_core_properties_are_retained(tmp_path: Path) -> None:
    document = tmp_path / "family-record.docx"
    core_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/">
  <dc:title>Family record</dc:title>
  <dc:subject>Correspondence</dc:subject>
  <dc:creator>Owner</dc:creator>
  <dc:description>Preserved letter</dc:description>
  <cp:keywords>family, letter</cp:keywords>
  <cp:lastModifiedBy>Alice</cp:lastModifiedBy>
  <cp:category>Personal</cp:category>
  <cp:revision>7</cp:revision>
  <dcterms:created>1995-09-03T10:30:00Z</dcterms:created>
  <dcterms:modified>2026-07-29T09:00:00Z</dcterms:modified>
</cp:coreProperties>
"""
    with ZipFile(document, "w") as package:
        package.writestr("docProps/core.xml", core_xml)

    assert _extract_ooxml_metadata(document) == {
        "document_format": "docx",
        "display_title": "Family record",
        "subject": "Correspondence",
        "creator": "Owner",
        "description": "Preserved letter",
        "keywords": "family, letter",
        "last_modified_by": "Alice",
        "category": "Personal",
        "revision": "7",
        "document_created_at": "1995-09-03T10:30:00+00:00",
        "document_modified_at": "2026-07-29T09:00:00+00:00",
        "captured_at": "1995-09-03T10:30:00+00:00",
        "capture_date_source": "document_created_at",
    }


def test_ooxml_extraction_rejects_invalid_or_oversized_core_xml(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.docx"
    invalid.write_bytes(b"not an office package")
    assert _extract_ooxml_metadata(invalid) == {}

    oversized = tmp_path / "oversized.xlsx"
    with ZipFile(oversized, "w") as package:
        package.writestr("docProps/core.xml", b"x" * (1024 * 1024 + 1))
    assert _extract_ooxml_metadata(oversized) == {}


def test_pdf_document_properties_and_page_count_are_retained(
    tmp_path: Path,
) -> None:
    document = tmp_path / "family-record.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": "Family record",
            "/Author": "Owner",
            "/Subject": "Correspondence",
            "/Keywords": "family, letter",
            "/Creator": "Personal Vault",
            "/Producer": "Vault PDF",
            "/CreationDate": "D:19950903103000+00'00'",
            "/ModDate": "D:20260729090000+00'00'",
        }
    )
    with document.open("wb") as output:
        writer.write(output)

    assert _extract_pdf_metadata(document) == {
        "document_format": "pdf",
        "pdf_encrypted": False,
        "page_count": 2,
        "display_title": "Family record",
        "creator": "Owner",
        "subject": "Correspondence",
        "creating_application": "Personal Vault",
        "pdf_producer": "Vault PDF",
        "keywords": "family, letter",
        "document_created_at": "1995-09-03T10:30:00+00:00",
        "document_modified_at": "2026-07-29T09:00:00+00:00",
        "captured_at": "1995-09-03T10:30:00+00:00",
        "capture_date_source": "document_created_at",
    }


def test_pdf_extraction_identifies_encryption_without_unlocking(
    tmp_path: Path,
) -> None:
    document = tmp_path / "private.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "Must not be read"})
    writer.encrypt("secret")
    with document.open("wb") as output:
        writer.write(output)

    assert _extract_pdf_metadata(document) == {
        "document_format": "pdf",
        "pdf_encrypted": True,
    }


def test_pdf_extraction_ignores_an_empty_optional_date(
    tmp_path: Path,
) -> None:
    document = tmp_path / "empty-modification-date.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": "Family record",
            "/ModDate": "",
        }
    )
    with document.open("wb") as output:
        writer.write(output)

    assert _extract_pdf_metadata(document) == {
        "document_format": "pdf",
        "pdf_encrypted": False,
        "page_count": 1,
        "display_title": "Family record",
        "pdf_producer": "pypdf",
    }


def test_pdf_extraction_tolerates_invalid_files(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"not a PDF")
    assert _extract_pdf_metadata(invalid) == {}


def test_zip_structure_and_bounded_entry_facts_are_retained(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "family-archive.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.comment = b"Family records"
        archive.writestr("letters/", b"")
        archive.writestr("letters/one.txt", b"first record")
        archive.writestr("photo.jpg", b"photo")

    metadata = _extract_zip_metadata(archive_path)

    assert metadata["archive_format"] == "zip"
    assert metadata["archive_entry_count"] == 3
    assert metadata["archive_file_count"] == 2
    assert metadata["archive_directory_count"] == 1
    assert metadata["archive_uncompressed_bytes"] == 17
    assert metadata["archive_encrypted_file_count"] == 0
    assert metadata["archive_comment"] == "Family records"
    assert metadata["archive_entries_truncated"] is False
    assert [
        entry["path"] for entry in metadata["archive_entries"]  # type: ignore[union-attr]
    ] == ["letters/one.txt", "photo.jpg"]
    assert metadata["archive_earliest_entry_at"]
    assert metadata["archive_latest_entry_at"]


def test_zip_extraction_is_inert_bounded_and_tolerates_invalid_files(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not an archive")
    assert _extract_zip_metadata(invalid) == {}

    archive_path = tmp_path / "large-directory.zip"
    with ZipFile(archive_path, "w") as archive:
        for index in range(260):
            archive.writestr(f"entry-{index:03}.txt", b"x")

    metadata = _extract_zip_metadata(archive_path)
    assert metadata["archive_file_count"] == 260
    assert len(metadata["archive_entries"]) == 256  # type: ignore[arg-type]
    assert metadata["archive_entries_truncated"] is True


def test_tar_structure_and_link_facts_are_retained(tmp_path: Path) -> None:
    archive_path = tmp_path / "family-records.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        directory = tarfile.TarInfo("letters/")
        directory.type = tarfile.DIRTYPE
        directory.mtime = 788956200
        archive.addfile(directory)

        payload = b"first record"
        document = tarfile.TarInfo("letters/one.txt")
        document.size = len(payload)
        document.mtime = 788956800
        document.uname = "owner"
        document.gname = "family"
        archive.addfile(document, BytesIO(payload))

        link = tarfile.TarInfo("latest.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "letters/one.txt"
        link.mtime = 788957400
        archive.addfile(link)

    metadata = _extract_tar_metadata(archive_path)

    assert metadata["archive_format"] == "tar-gzip"
    assert metadata["archive_entry_count"] == 3
    assert metadata["archive_file_count"] == 1
    assert metadata["archive_directory_count"] == 1
    assert metadata["archive_symbolic_link_count"] == 1
    assert metadata["archive_hard_link_count"] == 0
    assert metadata["archive_uncompressed_bytes"] == 12
    assert metadata["archive_entries_truncated"] is False
    entries = metadata["archive_entries"]
    assert entries[1]["owner"] == "owner"  # type: ignore[index]
    assert entries[1]["group"] == "family"  # type: ignore[index]
    assert entries[2]["link_target"] == "letters/one.txt"  # type: ignore[index]
    assert metadata["archive_earliest_entry_at"]
    assert metadata["archive_latest_entry_at"]


def test_tar_extraction_is_bounded_and_tolerates_invalid_files(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.tar"
    invalid.write_bytes(b"not a tar archive")
    assert _extract_tar_metadata(invalid) == {}

    archive_path = tmp_path / "many-files.tar"
    with tarfile.open(archive_path, "w") as archive:
        for index in range(260):
            entry = tarfile.TarInfo(f"entry-{index:03}.txt")
            entry.size = 1
            archive.addfile(entry, BytesIO(b"x"))

    metadata = _extract_tar_metadata(archive_path)
    assert metadata["archive_file_count"] == 260
    assert len(metadata["archive_entries"]) == 256  # type: ignore[arg-type]
    assert metadata["archive_entries_truncated"] is True


def test_exif_gps_is_retained_and_resolved_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.vault_master.reverse_geocode.search",
        lambda coordinates: [
            {"city": "Gdansk", "country": "Poland"}
        ],
    )
    exif = FakeExif(
        {
            34853: {
                1: "N",
                2: ((54, 1), (21, 1), (72, 10)),
                3: "E",
                4: ((18, 1), (38, 1), (4776, 100)),
                5: 0,
                6: (12, 1),
            }
        }
    )

    metadata = _extract_exif_gps(exif)  # type: ignore[arg-type]

    assert metadata == {
        "gps_latitude": 54.352,
        "gps_longitude": 18.6466,
        "gps_altitude_metres": 12.0,
        "location": "Gdansk, Poland",
    }


def test_exif_gps_applies_south_and_west_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.vault_master.reverse_geocode.search",
        lambda coordinates: [],
    )
    exif = FakeExif(
        {
            34853: {
                1: "S",
                2: (33, 52, 8.4),
                3: "W",
                4: (151, 12, 32.4),
            }
        }
    )

    metadata = _extract_exif_gps(exif)  # type: ignore[arg-type]

    assert metadata["gps_latitude"] == -33.869
    assert metadata["gps_longitude"] == -151.209
    assert "location" not in metadata


def test_malformed_exif_gps_is_ignored() -> None:
    exif = FakeExif({34853: {1: "N", 2: (54, 21)}})

    assert _extract_exif_gps(exif) == {}  # type: ignore[arg-type]


def test_exif_technical_camera_facts_are_normalised() -> None:
    exif = FakeExif(
        {
            270: "Wedding portrait",
            274: 6,
            315: "Owner",
            33434: (1, 125),
            33437: (28, 10),
            33432: "Copyright owner",
            34855: 200,
            37383: 5,
            37385: 16,
            37386: (50, 1),
            40961: 1,
            41987: 0,
            41989: 75,
            42033: "BODY-123",
            42035: "Example Lens Company",
            42036: "Example 50mm",
            42037: "LENS-456",
        }
    )

    metadata = _extract_exif_technical(exif)  # type: ignore[arg-type]

    assert metadata == {
        "image_description": "Wedding portrait",
        "artist": "Owner",
        "copyright": "Copyright owner",
        "camera_serial_number": "BODY-123",
        "lens_make": "Example Lens Company",
        "lens_model": "Example 50mm",
        "lens_serial_number": "LENS-456",
        "orientation": 6,
        "iso_speed": 200,
        "metering_mode": 5,
        "flash": 16,
        "color_space": 1,
        "white_balance": 0,
        "exposure_time_seconds": 0.008,
        "aperture_f_number": 2.8,
        "focal_length_mm": 50.0,
        "focal_length_35mm": 75,
    }


def test_invalid_exif_technical_values_are_ignored() -> None:
    exif = FakeExif(
        {
            274: "sideways",
            33434: (1, 0),
            33437: object(),
            34855: None,
        }
    )

    assert _extract_exif_technical(exif) == {}  # type: ignore[arg-type]


def test_xmp_descriptive_metadata_is_normalised() -> None:
    metadata = _extract_xmp_metadata(
        {
            "dc": {
                "title": {"x-default": "Family wedding"},
                "description": {"x-default": "Outside the town hall"},
                "creator": ["Owner"],
                "subject": {"Bag": {"li": ["family", "wedding"]}},
            },
            "photoshop": {
                "DateCreated": "1995-09-03T14:30:00",
                "City": "Starogard Gdanski",
                "State": "Pomorskie",
                "Country": "Poland",
            },
        }
    )

    assert metadata == {
        "display_title": "Family wedding",
        "description": "Outside the town hall",
        "creator": "Owner",
        "xmp_created_at": "1995-09-03T14:30:00",
        "location": "Starogard Gdanski, Pomorskie, Poland",
        "keywords": ["family", "wedding"],
    }


def test_iptc_descriptive_metadata_is_normalised() -> None:
    metadata = _extract_iptc_metadata(
        {
            (2, 5): b"Family wedding",
            (2, 25): [b"family", b"wedding"],
            (2, 55): b"19950903",
            (2, 80): b"Owner",
            (2, 90): b"Starogard Gdanski",
            (2, 95): b"Pomorskie",
            (2, 101): b"Poland",
            (2, 120): b"Outside the town hall",
        }
    )

    assert metadata == {
        "display_title": "Family wedding",
        "description": "Outside the town hall",
        "creator": "Owner",
        "iptc_created_at": "19950903",
        "location": "Starogard Gdanski, Pomorskie, Poland",
        "keywords": ["family", "wedding"],
    }


def test_path_validation_rejects_files_outside_root(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    safe_file = incoming / "safe.txt"
    safe_file.write_text("safe", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")

    assert require_file_within_root(safe_file, incoming) == (
        safe_file.resolve()
    )

    with pytest.raises(ValueError):
        require_file_within_root(outside_file, incoming)


def test_sha256_handles_chunk_boundaries(tmp_path: Path) -> None:
    file_path = tmp_path / "content.bin"
    file_path.write_bytes(b"vault-master-checksum")

    assert sha256_file(file_path, chunk_size=3) == (
        "5de3563a95d1abf7729699ad1aeb9003933485d916295c051291"
        "d654dc8653a7"
    )


def test_image_facts_extract_dimensions_and_camera_metadata(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    image_path = incoming / "photo.jpeg"
    image = Image.new("RGB", (640, 480), color="navy")
    exif = Image.Exif()
    exif[271] = "Example Camera Company"
    exif[272] = "Example Camera"
    exif[36867] = "2019:02:03 04:05:06"
    image.save(image_path, exif=exif)

    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]

    assert item.metadata["width"] == 640
    assert item.metadata["height"] == 480
    assert item.metadata["camera_model"] == "Example Camera"
    assert item.metadata["captured_at"] == "2019-02-03"
    assert item.metadata["capture_date_source"] == "embedded"
    assert item.proposed_category == "Gallery"
    assert item.proposal_confidence == "medium"


def test_image_facts_extract_xmp_into_detected_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    image_path = incoming / "photo.jpeg"
    Image.new("RGB", (640, 480), color="navy").save(image_path)
    monkeypatch.setattr(
        Image.Image,
        "getxmp",
        lambda image: {
            "dc": {"title": {"x-default": "Family wedding"}},
            "photoshop": {
                "DateCreated": "1995-09-03T14:30:00",
                "City": "Starogard Gdanski",
                "Country": "Poland",
            },
        },
    )

    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]

    assert item.metadata["display_title"] == "Family wedding"
    assert item.metadata["captured_at"] == "1995-09-03"
    assert item.metadata["capture_date_source"] == "embedded"
    assert item.metadata["location"] == "Starogard Gdanski, Poland"


def test_scanner_software_metadata_proposes_documents(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    image_path = incoming / "scan.jpeg"
    image = Image.new("RGB", (800, 1200), color="white")
    exif = Image.Exif()
    exif[305] = "Example Document Scanner"
    image.save(image_path, exif=exif)

    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]

    assert item.proposed_category == "Documents"
    assert item.proposal_confidence == "medium"


def test_detected_location_becomes_effective_catalogue_metadata(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    image_path = incoming / "photo.jpeg"
    Image.new("RGB", (10, 10), color="navy").save(image_path)
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    item.metadata["location"] = "Gdansk, Poland"

    _, _, location, provenance = effective_asset_metadata(item)

    assert location == "Gdansk, Poland"
    assert provenance["location"] == "embedded"


def test_embedded_title_becomes_effective_catalogue_metadata(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    image_path = incoming / "photo.jpeg"
    Image.new("RGB", (10, 10), color="navy").save(image_path)
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    item.metadata["display_title"] = "Family wedding"

    title, _, _, provenance = effective_asset_metadata(item)

    assert title == "Family wedding"
    assert provenance["display_title"] == "embedded"


def test_existing_vault_inventory_is_read_only_and_catalogued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "Documents"
    library.mkdir()
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(library))
    existing = library / "record.txt"
    existing.write_text("original", encoding="utf-8")
    store = MemoryVaultMasterStore()

    scan_root(store, library, INVENTORY_SOURCE)

    item = store.list_items()[0]
    asset = store.get_catalogued_asset("/vault/Documents/record.txt")
    assert item.state == "inventoried"
    assert asset is not None
    assert asset.asset_type == "Documents"
    assert asset.display_title == "record"
    assert asset.sha256 == item.sha256
    assert existing.read_text(encoding="utf-8") == "original"


def test_inventory_rescan_preserves_catalogue_identity_and_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gallery = tmp_path / "Gallery"
    gallery.mkdir()
    monkeypatch.setenv("PV_GALLERY_PATH", str(gallery))
    photo = gallery / "photo.jpg"
    photo.write_bytes(b"first")
    store = MemoryVaultMasterStore()
    scan_root(store, gallery, INVENTORY_SOURCE)
    original = store.get_catalogued_asset("/vault/Gallery/photo.jpg")
    assert original is not None
    store.catalogued_assets[original.vault_path] = replace(
        original,
        display_title="User title",
        metadata_provenance={
            **original.metadata_provenance,
            "display_title": "user_override",
        },
    )

    photo.write_bytes(b"updated technical content")
    monkeypatch.setattr(
        "app.vault_master.extract_basic_metadata",
        lambda _: {
            "captured_at": "1995-09-03",
            "capture_date_source": "embedded",
            "location": "Gdansk, Poland",
        },
    )
    scan_root(store, gallery, INVENTORY_SOURCE)
    rescanned = store.get_catalogued_asset("/vault/Gallery/photo.jpg")

    assert rescanned is not None
    assert rescanned.id == original.id
    assert rescanned.display_title == "User title"
    assert rescanned.metadata_provenance["display_title"] == "user_override"
    assert rescanned.captured_on == date(1995, 9, 3)
    assert rescanned.metadata_provenance["captured_on"] == "embedded"
    assert rescanned.location == "Gdansk, Poland"
    assert rescanned.metadata_provenance["location"] == "embedded"
    assert rescanned.effective_metadata["location"] == "Gdansk, Poland"
    assert rescanned.sha256 != original.sha256


def test_incoming_duplicate_matches_existing_vault_file(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    documents = tmp_path / "Documents"
    incoming.mkdir()
    documents.mkdir()
    (documents / "original.txt").write_text("same", encoding="utf-8")
    (incoming / "copy.txt").write_text("same", encoding="utf-8")
    store = MemoryVaultMasterStore()

    scan_root(store, documents, INVENTORY_SOURCE)
    scan_root(store, incoming, INCOMING_SOURCE)

    items = {item.filename: item for item in store.list_items()}
    assert items["original.txt"].duplicate_of_id is None
    assert items["copy.txt"].duplicate_of_id == items["original.txt"].id
    assert items["copy.txt"].state == "needs_review"


def test_personal_duplicate_matching_uses_owner_uuid_and_ignores_rejected_arrival_history(
    tmp_path: Path,
) -> None:
    inventory, incoming = tmp_path / "Documents", tmp_path / "Incoming"
    inventory.mkdir(); incoming.mkdir()
    (inventory / "canonical.txt").write_text("same", encoding="utf-8")
    (incoming / "rejected.txt").write_text("same", encoding="utf-8")
    (incoming / "other-owner.txt").write_text("same", encoding="utf-8")
    owner_a, owner_b = uuid4(), uuid4()
    store = MemoryVaultMasterStore()
    inventory_item = store.record_file(
        store.create_batch(INVENTORY_SOURCE, str(inventory)), INVENTORY_SOURCE,
        scan_file(inventory / "canonical.txt", inventory, owner_user_id=owner_a),
    )
    first = store.record_file(
        store.create_batch(INCOMING_SOURCE, str(incoming)), INCOMING_SOURCE,
        scan_file(incoming / "rejected.txt", incoming, owner_user_id=owner_a),
    )
    assert first.duplicate_of_id == inventory_item.id
    assert store.record_decision(first.id, "rejected", "owner-a") is not None
    second = store.record_file(
        store.create_batch(INCOMING_SOURCE, str(incoming)), INCOMING_SOURCE,
        scan_file(incoming / "other-owner.txt", incoming, owner_user_id=owner_b),
    )

    assert second.duplicate_of_id is None
    assert second.state == "needs_review"


def test_rejected_arrival_history_does_not_block_same_owner_reupload(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    first_file, second_file = incoming / "rejected.txt", incoming / "reupload.txt"
    first_file.write_text("same", encoding="utf-8")
    second_file.write_text("same", encoding="utf-8")
    owner = uuid4()
    store = MemoryVaultMasterStore()
    rejected = store.record_file(
        store.create_batch(INCOMING_SOURCE, str(incoming)), INCOMING_SOURCE,
        scan_file(first_file, incoming, owner_user_id=owner),
    )
    assert rejected.duplicate_of_id is None
    assert store.record_decision(rejected.id, "rejected", "owner") is not None

    reupload = store.record_file(
        store.create_batch(INCOMING_SOURCE, str(incoming)), INCOMING_SOURCE,
        scan_file(second_file, incoming, owner_user_id=owner),
    )

    assert reupload.duplicate_of_id is None
    assert reupload.state == "needs_review"


@pytest.mark.parametrize(
    ("asset_type", "category"),
    [("Movies", "Movies"), ("TV Shows", "TV Shows")],
)
def test_theatre_duplicate_is_vault_wide_only_after_theatre_classification(
    tmp_path: Path,
    asset_type: str,
    category: str,
) -> None:
    inventory, incoming = tmp_path / "Movies", tmp_path / "Incoming"
    inventory.mkdir(); incoming.mkdir()
    (inventory / "film.mkv").write_bytes(b"same-film")
    (incoming / "film.mkv").write_bytes(b"same-film")
    owner_a, owner_b = uuid4(), uuid4()
    store = MemoryVaultMasterStore()
    inventory_item = store.record_file(
        store.create_batch(INVENTORY_SOURCE, str(inventory)), INVENTORY_SOURCE,
        scan_file(inventory / "film.mkv", inventory, owner_user_id=owner_a),
    )
    store.restore_catalogued_asset(CataloguedAsset(
        id=uuid4(), asset_type=asset_type, display_title="Film", captured_on=None,
        location=None, vault_path="/vault/Theatre/Movies/film.mkv", filename="film.mkv",
        size_bytes=9, mime_type="video/x-matroska", sha256=inventory_item.sha256,
        metadata={}, metadata_provenance={}, owner_username="owner-a", owner_user_id=owner_a,
    ), "owner-a")
    arrival = store.record_file(
        store.create_batch(INCOMING_SOURCE, str(incoming)), INCOMING_SOURCE,
        scan_file(incoming / "film.mkv", incoming, owner_user_id=owner_b),
    )

    assert arrival.duplicate_of_id is None
    theatre = store.update_proposal(arrival.id, category, "owner-b")
    assert theatre is not None and theatre.duplicate_of_id == inventory_item.id
    personal = store.update_proposal(arrival.id, "Home Videos", "owner-b")
    assert personal is not None and personal.duplicate_of_id is None


def test_repeated_scan_updates_existing_item_without_duplication(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    file_path = incoming / "one.txt"
    file_path.write_text("first", encoding="utf-8")
    store = MemoryVaultMasterStore()

    scan_root(store, incoming, INCOMING_SOURCE)
    original_id = store.list_items()[0].id
    file_path.write_text("changed", encoding="utf-8")
    scan_root(store, incoming, INCOMING_SOURCE)

    assert len(store.list_items()) == 1
    assert store.list_items()[0].id == original_id


def test_reappearing_moved_file_returns_to_review(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    file_path = incoming / "returned.txt"
    file_path.write_bytes(b"same")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    store.record_move_result(item.id, "moved", "owner", "test")

    scan_root(store, incoming, INCOMING_SOURCE)

    assert store.list_items()[0].state == "needs_review"


def test_queued_scan_is_restart_safe_until_worker_claims_it(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    (incoming / "queued.txt").write_text("queued", encoding="utf-8")
    store = MemoryVaultMasterStore()

    batch_id = enqueue_root(store, incoming, INCOMING_SOURCE)

    assert store.batches[batch_id]["status"] == "queued"
    assert store.list_items() == []
    assert process_next_batch(store) == batch_id
    assert store.batches[batch_id]["status"] == "completed"
    assert store.list_items()[0].filename == "queued.txt"


def test_arrival_hall_owner_survives_scan_move_and_catalogue(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    gallery = tmp_path / "Gallery"
    incoming.mkdir()
    gallery.mkdir()
    uploaded = incoming / "recipient-photo.jpg"
    uploaded.write_bytes(b"recipient-photo")
    store = MemoryVaultMasterStore(default_asset_owner="owner")

    batch_id = enqueue_root(store, incoming, INCOMING_SOURCE)
    assert process_next_batch(
        store,
        owner_lookup=lambda path: "recipient" if path == uploaded else None,
    ) == batch_id
    item = store.list_items()[0]
    assert item.owner_username == "recipient"

    assert store.record_decision(item.id, "approved", "recipient") is not None
    assert store.queue_move(item.id, "recipient") is not None
    assert process_next_move(store, incoming, {"Gallery": gallery}) == item.id

    asset = store.get_catalogued_asset("/vault/Gallery/recipient-photo.jpg")
    assert asset is not None
    assert asset.owner_username == "recipient"


def test_arrival_hall_uuid_owner_survives_system_scan_without_admin_identity(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    uploaded = incoming / "recipient-photo.jpg"
    uploaded.write_bytes(b"recipient-photo")
    anita_user_id = uuid4()
    store = MemoryVaultMasterStore(default_asset_owner="owner")

    batch_id = enqueue_root(store, incoming, INCOMING_SOURCE)

    assert process_next_batch(
        store,
        owner_lookup=lambda path: anita_user_id if path == uploaded else None,
    ) == batch_id
    item = store.list_items()[0]
    assert item.owner_user_id == anita_user_id


def test_inventory_scan_publishes_discovered_permanent_file(
    tmp_path: Path,
) -> None:
    music = tmp_path / "Music"
    music.mkdir()
    track = music / "track.wma"
    track.write_bytes(b"music")
    store = MemoryVaultMasterStore()
    published: list[tuple[Path, ...]] = []
    batch_id = enqueue_root(store, music, INVENTORY_SOURCE)

    assert process_next_batch(store, published.append) == batch_id
    assert published == [(track,)]


def test_incoming_scan_does_not_publish_staged_file(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    (incoming / "track.wma").write_bytes(b"music")
    store = MemoryVaultMasterStore()
    published: list[tuple[Path, ...]] = []
    batch_id = enqueue_root(store, incoming, INCOMING_SOURCE)

    assert process_next_batch(store, published.append) == batch_id
    assert published == []


def test_incoming_scan_is_prioritised_over_inventory_backlog(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    library = tmp_path / "Documents"
    incoming.mkdir()
    library.mkdir()
    store = MemoryVaultMasterStore()
    inventory_batch = enqueue_root(store, library, INVENTORY_SOURCE)
    incoming_batch = enqueue_root(store, incoming, INCOMING_SOURCE)

    assert process_next_batch(store) == incoming_batch
    assert store.batches[inventory_batch]["status"] == "queued"


def test_catalogue_backfill_reuses_active_root_batches(
    tmp_path: Path,
) -> None:
    documents = tmp_path / "Documents"
    gallery = tmp_path / "Gallery"
    documents.mkdir()
    gallery.mkdir()
    store = MemoryVaultMasterStore()

    first_ids, first_reused = enqueue_catalogue_backfill(
        store,
        (documents, gallery),
    )
    second_ids, second_reused = enqueue_catalogue_backfill(
        store,
        (documents, gallery),
    )

    assert first_reused == 0
    assert second_reused == 2
    assert second_ids == first_ids
    assert len(store.list_batches()) == 2


def test_catalogue_backfill_queues_completed_roots_again(
    tmp_path: Path,
) -> None:
    documents = tmp_path / "Documents"
    documents.mkdir()
    store = MemoryVaultMasterStore()
    first_ids, _ = enqueue_catalogue_backfill(store, (documents,))
    assert process_next_batch(store) == first_ids[0]

    second_ids, reused = enqueue_catalogue_backfill(store, (documents,))

    assert reused == 0
    assert second_ids != first_ids
    assert len(store.list_batches()) == 2


def test_queued_move_is_restart_safe_until_worker_claims_it(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    gallery = tmp_path / "Gallery"
    incoming.mkdir()
    gallery.mkdir()
    source = incoming / "photo.jpeg"
    source.write_bytes(b"photo")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    store.update_metadata_overrides(
        item.id,
        {
            "display_title": "Corrected photograph",
            "captured_on": "1995-09-03",
            "location": "Starogard Gdanski, Polska",
        },
        "owner",
    )
    store.record_decision(item.id, "approved", "owner")

    queued = store.queue_move(item.id, "owner")

    assert queued is not None
    assert queued.state == "move_queued"
    assert source.exists()

    def unavailable_playback_service(paths: tuple[Path, ...]) -> None:
        assert paths == (gallery / "photo.jpeg",)
        raise RuntimeError("Jellyfin unavailable")

    assert (
        process_next_move(
            store,
            incoming,
            {"Gallery": gallery},
            unavailable_playback_service,
        )
        == item.id
    )
    assert not source.exists()
    assert (gallery / "photo.jpeg").read_bytes() == b"photo"
    asset = store.get_catalogued_asset("/vault/Gallery/photo.jpeg")
    assert asset is not None
    assert asset.asset_type == "Gallery"
    assert asset.display_title == "Corrected photograph"
    assert asset.captured_on is not None
    assert asset.captured_on.isoformat() == "1995-09-03"
    assert asset.location == "Starogard Gdanski, Polska"
    assert asset.sha256 == item.sha256
    assert asset.metadata_provenance == {
        "display_title": "user_override",
        "captured_on": "user_override",
        "location": "user_override",
    }
    assert asset.imported_metadata == {}
    assert asset.user_overrides == {
        "display_title": "Corrected photograph",
        "captured_on": "1995-09-03",
        "location": "Starogard Gdanski, Polska",
    }
    assert asset.effective_metadata["display_title"] == (
        "Corrected photograph"
    )
    assert asset.effective_metadata["captured_on"] == "1995-09-03"
    assert asset.effective_metadata["location"] == (
        "Starogard Gdanski, Polska"
    )

    restored = store.update_catalogued_asset_metadata(
        asset.id,
        {"location": None},
        "owner",
    )

    assert restored is not None
    assert "location" not in restored.user_overrides
    assert restored.location is None
    assert restored.effective_metadata["location"] is None


def test_memory_catalogue_visibility_is_owner_or_explicit_share(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gallery = tmp_path / "Gallery"
    gallery.mkdir()
    (gallery / "private.jpg").write_bytes(b"private")
    (gallery / "shared.jpg").write_bytes(b"shared")
    monkeypatch.setenv("PV_GALLERY_PATH", str(gallery))
    store = MemoryVaultMasterStore(default_asset_owner="owner")

    scan_root(store, gallery, INVENTORY_SOURCE)
    private_asset = store.get_catalogued_asset(
        "/vault/Gallery/private.jpg"
    )
    shared_asset = store.get_catalogued_asset(
        "/vault/Gallery/shared.jpg"
    )
    assert private_asset is not None
    assert shared_asset is not None
    owner = SimpleNamespace(user_id=uuid5(NAMESPACE_URL, "personal-vault-test:owner"))
    recipient = SimpleNamespace(user_id=uuid5(NAMESPACE_URL, "personal-vault-test:son"))
    store.catalogued_assets[shared_asset.vault_path] = replace(
        shared_asset,
        visibility="shared",
        shared_with=("son",),
        shared_with_user_ids=(recipient.user_id,),
    )

    assert store.get_visible_catalogued_asset_by_id(
        private_asset.id, owner
    ) == private_asset
    assert (
        store.get_visible_catalogued_asset_by_id(
            private_asset.id, recipient
        )
        is None
    )
    assert store.get_visible_catalogued_asset_by_id(
        shared_asset.id, recipient
    ) is not None
    assert [
        asset.filename
        for asset in store.search_visible_catalogued_assets(
            ".jpg", recipient
        )
    ] == ["shared.jpg"]


def test_arrival_hall_path_migration_is_idempotent() -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, "/vault/Incoming")
    store.record_file(
        batch_id,
        INCOMING_SOURCE,
        ScannedFile(
            source_path="/vault/Incoming/photo.jpg",
            relative_path="photo.jpg",
            filename="photo.jpg",
            size_bytes=5,
            mime_type="image/jpeg",
            modified_at=datetime.now(timezone.utc),
            sha256="a" * 64,
            metadata={},
        ),
    )

    assert (
        store.migrate_source_root(
            INCOMING_SOURCE,
            "/vault/Incoming",
            "/vault/Arrival Hall",
        )
        == 1
    )
    assert store.list_items()[0].source_path == (
        "/vault/Arrival Hall/photo.jpg"
    )
    assert store.list_batches()[0]["source_root"] == (
        "/vault/Arrival Hall"
    )
    assert (
        store.migrate_source_root(
            INCOMING_SOURCE,
            "/vault/Incoming",
            "/vault/Arrival Hall",
        )
        == 0
    )


@pytest.mark.parametrize(
    ("filename", "mime_type", "category", "confidence"),
    [
        ("record.pdf", "application/pdf", "Documents", "medium"),
        ("collection.zip", "application/zip", "Archives", "medium"),
        ("photo.jpeg", "image/jpeg", "Gallery", "low"),
        ("track.flac", "audio/flac", "Music", "low"),
        ("clip.mp4", "video/mp4", "Home Videos", "low"),
        ("unknown.bin", "application/octet-stream", "Archives", "low"),
    ],
)
def test_deterministic_proposals_are_explainable(
    filename: str,
    mime_type: str,
    category: str,
    confidence: str,
) -> None:
    scanned = ScannedFile(
        source_path=f"/vault/Incoming/{filename}",
        relative_path=filename,
        filename=filename,
        size_bytes=1,
        mime_type=mime_type,
        modified_at=datetime.now(timezone.utc),
        sha256="0" * 64,
        metadata={},
    )

    proposed_category, destination, reason, proposed_confidence = (
        create_deterministic_proposal(scanned)
    )

    assert proposed_category == category
    assert destination == f"/vault/{category}/{filename}"
    assert reason
    assert proposed_confidence == confidence


def test_tagged_audio_receives_explainable_music_proposal() -> None:
    scanned = ScannedFile(
        source_path="/vault/Arrival Hall/track.flac",
        relative_path="track.flac",
        filename="track.flac",
        size_bytes=1,
        mime_type="audio/flac",
        modified_at=datetime.now(timezone.utc),
        sha256="0" * 64,
        metadata={"artist": "Massive Attack", "album": "Mezzanine"},
    )
    category, destination, reason, confidence = create_deterministic_proposal(scanned)
    assert (category, destination, confidence) == (
        "Music",
        "/vault/Music/track.flac",
        "medium",
    )
    assert "Embedded audio tags" in reason


@pytest.mark.parametrize(
    ("filename", "metadata"),
    [
        ("Screenshot 2026-08-02.png", {}),
        ("IMG_4245.png", {"image_description": "Screenshot"}),
    ],
)
def test_screenshot_marker_is_a_low_confidence_archive_fallback(
    filename: str,
    metadata: dict[str, object],
) -> None:
    scanned = ScannedFile(
        source_path=f"/vault/Arrival Hall/{filename}",
        relative_path=filename,
        filename=filename,
        size_bytes=1,
        mime_type="image/png",
        modified_at=datetime.now(timezone.utc),
        sha256="0" * 64,
        metadata=metadata,
    )
    category, destination, reason, confidence = create_deterministic_proposal(scanned)
    assert category == "Archives"
    assert destination == f"/vault/Archives/{filename}"
    assert "Screenshot capture context is known" in reason
    assert confidence == "low"


def test_nested_audio_proposal_preserves_album_folder() -> None:
    scanned = ScannedFile(
        source_path="/vault/Arrival Hall/Artist/Album/01 Track.wma",
        relative_path="Artist/Album/01 Track.wma",
        filename="01 Track.wma",
        size_bytes=1,
        mime_type="audio/x-ms-wma",
        modified_at=datetime.now(timezone.utc),
        sha256="0" * 64,
        metadata={"artist": "Artist", "album": "Album"},
    )

    category, destination, _, confidence = create_deterministic_proposal(
        scanned
    )

    assert category == "Music"
    assert destination == "/vault/Music/Artist/Album/01 Track.wma"
    assert confidence == "medium"


def test_music_inventory_uses_canonical_music_vault_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = tmp_path / "Music"
    track = music / "Artist" / "Album" / "01 Track.wma"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    monkeypatch.setenv("PV_MUSIC_PATH", str(music))

    assert inventory_catalogue_location(str(track)) == (
        "Music",
        "/vault/Music/Artist/Album/01 Track.wma",
    )


def test_inventory_items_receive_no_relocation_proposal(
    tmp_path: Path,
) -> None:
    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "existing.pdf").write_bytes(b"existing")
    store = MemoryVaultMasterStore()

    scan_root(store, documents, INVENTORY_SOURCE)

    item = store.list_items()[0]
    assert item.proposed_destination is None
    assert item.proposal_reason is None


def test_activity_records_scan_decision_and_failure() -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    item = store.record_file(
        batch_id,
        INCOMING_SOURCE,
        ScannedFile(
            source_path="/vault/Arrival Hall/photo.jpg",
            relative_path="photo.jpg",
            filename="photo.jpg",
            size_bytes=5,
            mime_type="image/jpeg",
            modified_at=datetime.now(timezone.utc),
            sha256="a" * 64,
            metadata={},
        ),
    )
    store.complete_batch(batch_id, 1)
    store.record_decision(item.id, "approved", "owner")
    failed_batch_id = store.create_batch(
        INVENTORY_SOURCE,
        "/media/documents",
    )
    store.fail_batch(failed_batch_id, "Storage unavailable")

    events = store.list_activity()

    assert [event.action for event in events] == [
        "scan_failed",
        "proposal_approved",
        "scan_completed",
        "file_analysed",
    ]
    assert events[1].username == "owner"
    assert events[1].filename == "photo.jpg"
    assert events[1].source_kind == INCOMING_SOURCE
    assert events[0].succeeded is False
    assert events[0].detail == "Storage unavailable"
    assert store.list_activity(limit=0) == []


def test_safe_move_revalidates_checksum_and_never_overwrites(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    gallery = tmp_path / "Gallery"
    incoming.mkdir()
    gallery.mkdir()
    source = incoming / "photo.jpeg"
    source.write_bytes(b"approved")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    item = store.record_decision(item.id, "approved", "owner")
    assert item is not None

    destination = safely_move_approved_file(item, incoming, gallery)

    assert destination.read_bytes() == b"approved"
    assert not source.exists()


def test_safe_move_refuses_changed_source_and_existing_destination(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    gallery = tmp_path / "Gallery"
    incoming.mkdir()
    gallery.mkdir()
    source = incoming / "photo.jpeg"
    source.write_bytes(b"approved")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    item = store.record_decision(item.id, "approved", "owner")
    assert item is not None
    source.write_bytes(b"changed")

    with pytest.raises(ValueError, match="checksum changed"):
        safely_move_approved_file(item, incoming, gallery)

    source.write_bytes(b"approved")
    (gallery / "photo.jpeg").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        safely_move_approved_file(item, incoming, gallery)
    assert source.read_bytes() == b"approved"
    assert (gallery / "photo.jpeg").read_bytes() == b"existing"


def test_safe_music_move_preserves_album_folder(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    music = tmp_path / "Music"
    album = incoming / "Artist" / "Album"
    album.mkdir(parents=True)
    music.mkdir()
    source = album / "01 Track.wma"
    source.write_bytes(b"approved audio")
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    item = store.record_decision(item.id, "approved", "owner")
    assert item is not None

    destination = safely_move_approved_file(item, incoming, music)

    assert destination == music / "Artist" / "Album" / "01 Track.wma"
    assert destination.read_bytes() == b"approved audio"
    assert not source.exists()


def test_safe_music_move_refuses_symlinked_album_destination(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    music = tmp_path / "Music"
    outside = tmp_path / "Outside"
    album = incoming / "Artist" / "Album"
    album.mkdir(parents=True)
    music.mkdir()
    outside.mkdir()
    source = album / "01 Track.wma"
    source.write_bytes(b"approved audio")
    try:
        (music / "Artist").symlink_to(outside, target_is_directory=True)
    except OSError:
        return
    store = MemoryVaultMasterStore()
    scan_root(store, incoming, INCOMING_SOURCE)
    item = store.list_items()[0]
    item = store.record_decision(item.id, "approved", "owner")
    assert item is not None

    with pytest.raises(ValueError, match="symbolic link"):
        safely_move_approved_file(item, incoming, music)

    assert source.read_bytes() == b"approved audio"
    assert not list(outside.iterdir())


def test_duplicate_removal_revalidates_both_files(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    documents = tmp_path / "Documents"
    incoming.mkdir()
    documents.mkdir()
    vault_file = documents / "original.txt"
    incoming_file = incoming / "copy.txt"
    vault_file.write_bytes(b"same")
    incoming_file.write_bytes(b"same")
    store = MemoryVaultMasterStore()
    scan_root(store, documents, INVENTORY_SOURCE)
    scan_root(store, incoming, INCOMING_SOURCE)
    items = {item.filename: item for item in store.list_items()}

    safely_remove_exact_duplicate(
        items["copy.txt"],
        items["original.txt"],
        incoming,
        (documents,),
    )

    assert not incoming_file.exists()
    assert vault_file.read_bytes() == b"same"


def test_duplicate_removal_refuses_changed_vault_match(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    documents = tmp_path / "Documents"
    incoming.mkdir()
    documents.mkdir()
    vault_file = documents / "original.txt"
    incoming_file = incoming / "copy.txt"
    vault_file.write_bytes(b"same")
    incoming_file.write_bytes(b"same")
    store = MemoryVaultMasterStore()
    scan_root(store, documents, INVENTORY_SOURCE)
    scan_root(store, incoming, INCOMING_SOURCE)
    items = {item.filename: item for item in store.list_items()}
    vault_file.write_bytes(b"changed")

    with pytest.raises(ValueError, match="Vault file checksum"):
        safely_remove_exact_duplicate(
            items["copy.txt"],
            items["original.txt"],
            incoming,
            (documents,),
        )

    assert incoming_file.read_bytes() == b"same"
