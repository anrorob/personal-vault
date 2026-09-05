from datetime import datetime, timezone
from uuid import uuid4

from app.tv_disc_resolver import discover_tv_disc_batches, parse_disc_track, resolve_tv_disc_batch
from app.vault_master import INCOMING_SOURCE, ImportItem


def item(name: str, duration: int, *, season: int = 1, digest: str | None = None) -> ImportItem:
    return ImportItem(uuid4(), uuid4(), INCOMING_SOURCE, f"/arrival/Example Series/Season {season}/{name}", f"Example Series/Season {season}/{name}", name, 1, "video/x-matroska", datetime.now(timezone.utc), digest or f"{uuid4().int:064x}"[-64:], "needs_review", None, "Home Videos", None, "generic", "low", {"duration_seconds": duration}, {}, owner_user_id=uuid4())


def test_disc_parser_and_long_form_cluster_propose_sequential_episodes() -> None:
    assert parse_disc_track("Example Series Season 1 - Disc 2_t03.mkv") == (2, 3)
    proposal = resolve_tv_disc_batch([
        item("Example Series Season 1 - Disc 1_t00.mkv", 300),
        item("Example Series Season 1 - Disc 1_t01.mkv", 3480),
        item("Example Series Season 1 - Disc 1_t02.mkv", 3360),
        item("Example Series Season 1 - Disc 2_t01.mkv", 3540),
        item("Example Series Season 1 - Disc 2_t02.mkv", 5100),
        item("Example Series Season 1 - Disc 2_t03.mkv", 120),
    ])
    episodes = [track for track in proposal.tracks if track.classification == "likely_episode"]
    assert [(track.disc_number, track.track_number, track.episode_number) for track in episodes] == [(1, 1, 1), (1, 2, 2), (2, 1, 3), (2, 2, 4)]
    assert proposal.show_title == "Example Series"
    assert proposal.confidence == "high"


def test_short_form_is_not_forced_through_a_forty_minute_threshold() -> None:
    proposal = resolve_tv_disc_batch([
        item("Example Series Season 1 - Disc 1_t01.mkv", 1500),
        item("Example Series Season 1 - Disc 1_t02.mkv", 1560),
        item("Example Series Season 1 - Disc 1_t03.mkv", 1620),
        item("Example Series Season 1 - Disc 1_t04.mkv", 120),
    ])
    assert [track.episode_number for track in proposal.tracks if track.classification == "likely_episode"] == [1, 2, 3]


def test_conflicting_folder_and_filename_stays_review_only() -> None:
    conflict = item("Example Series Season 2 - Disc 1_t01.mkv", 3500, season=1)
    proposal = resolve_tv_disc_batch([conflict, item("Example Series Season 1 - Disc 1_t02.mkv", 3500), item("Example Series Season 1 - Disc 1_t03.mkv", 3500)])
    assert proposal.tracks[0].classification == "unresolved"
    assert proposal.needs_review


def test_exact_duplicate_is_not_assigned_a_second_episode() -> None:
    digest = "a" * 64
    proposal = resolve_tv_disc_batch([
        item("Example Series Season 1 - Disc 1_t01.mkv", 3500, digest=digest),
        item("Example Series Season 1 - Disc 1_t02.mkv", 3500, digest=digest),
        item("Example Series Season 1 - Disc 1_t03.mkv", 3500),
        item("Example Series Season 1 - Disc 1_t04.mkv", 3500),
    ])
    assert proposal.tracks[0].episode_number == 1
    assert proposal.tracks[1].classification == "duplicate"
    assert proposal.tracks[1].episode_number is None


def test_supplier_context_groups_one_show_across_seasons_and_rejects_unsafe_paths() -> None:
    items = []
    owner = uuid4()
    for season, filename in ((1, "Disc 1_t01.mkv"), (1, "Disc 1_t02.mkv"), (2, "Disc 1_t01.mkv"), (3, "Disc 2_t03.mkv")):
        entry = item(filename, 3500, season=season)
        items.append(entry.__class__(**{**entry.__dict__, "owner_user_id": owner, "metadata": {"duration_seconds": 3500, "source_context": {"source_id": "example-series", "source_label": "Example Series", "relative_path": f"Season {season}\\{filename}"}}}))
    proposal = resolve_tv_disc_batch(items)
    assert len(discover_tv_disc_batches(items)) == 1
    assert proposal.show_title == "Example Series"
    assert {track.season_number for track in proposal.tracks} == {1, 2, 3}
    assert [track.episode_number for track in proposal.tracks if track.season_number == 1] == [1, 2]
    unsafe = items[0].__class__(**{**items[0].__dict__, "metadata": {"duration_seconds": 3500, "source_context": {"source_label": "Example Series", "relative_path": "C:\\OtherShow\\Disc 1_t01.mkv"}}})
    assert resolve_tv_disc_batch([unsafe]).tracks[0].season_number is None


def test_supplier_label_conflict_remains_review_only() -> None:
    entries = []
    for number in range(1, 4):
        entry = item(f"Foundation Season 1 - Disc 1_t0{number}.mkv", 3500)
        entries.append(entry.__class__(**{**entry.__dict__, "metadata": {"duration_seconds": 3500, "source_context": {"source_id": "example-series", "source_label": "Example Series", "relative_path": f"Season 1/Disc 1_t0{number}.mkv"}}}))
    proposal = resolve_tv_disc_batch(entries)
    assert proposal.show_title is None
    assert proposal.needs_review
