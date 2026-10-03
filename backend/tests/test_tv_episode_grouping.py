from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import Response

from app.tv_disc_resolver import discover_tv_disc_batches, resolve_tv_disc_batch
from app.vault_master import INCOMING_SOURCE, ImportItem, MemoryVaultMasterStore
from app.vault_master_api import list_tv_resolver_batches


OWNER = UUID("11111111-1111-4111-8111-111111111111")


def episode(number: int, *, title: str = "Example Show", season: int = 1) -> ImportItem:
    original = f"{title} S{season:02d}E{number:02d}.mp4"
    physical = f"{original[:-4]} (Vault Supplier {UUID(int=number)}).mp4"
    context = {
        "source_kind": "automatic_source",
        "source_id": "example-source",
        "source_label": title,
        "relative_path": f"{title} Season {season}/{original}",
        "original_filename": original,
    }
    return ImportItem(
        UUID(int=number), UUID(int=100), INCOMING_SOURCE, f"/arrival/{physical}",
        physical, physical, 10, "video/mp4", datetime.now(timezone.utc),
        f"{number:064x}", "needs_review", None, "Home Videos", None,
        "generic video", "low", {"source_context": context, "logical_filename": original},
        {}, owner_user_id=OWNER,
    )


def test_supplier_season_groups_from_retained_logical_names_without_disc_names():
    entries = [episode(number) for number in range(1, 11)]
    before = deepcopy(entries)
    batches = discover_tv_disc_batches(entries)
    assert batches == (tuple(entries),)
    proposal = resolve_tv_disc_batch(batches[0])
    assert proposal.show_title == "Example Show"
    assert {track.season_number for track in proposal.tracks} == {1}
    assert [track.episode_number for track in proposal.tracks] == list(range(1, 11))
    assert all(track.classification == "likely_episode" for track in proposal.tracks)
    assert all("Vault Supplier" not in track.destination for track in proposal.tracks)
    assert entries == before


def test_explicit_episode_numbers_are_preserved_without_runtime_inference():
    proposal = resolve_tv_disc_batch([episode(7), episode(2), episode(11)])
    assert [track.episode_number for track in proposal.tracks] == [7, 2, 11]
    assert all(track.duration_seconds is None for track in proposal.tracks)


def test_other_show_and_owner_do_not_join_the_season_even_with_same_source_id():
    first = [episode(number) for number in range(1, 4)]
    other = replace(episode(4, title="Another Show"), id=UUID(int=104))
    other_owner = replace(episode(5), owner_user_id=UUID(int=200))
    batches = discover_tv_disc_batches(first + [other, other_owner])
    assert sorted(len(batch) for batch in batches) == [1, 1, 3]
    assert tuple(first) in batches


def test_movie_and_ordinary_single_file_uploads_are_unchanged():
    template = episode(1)
    movie = replace(template, proposed_category="Movies")
    ordinary = replace(template, metadata={"logical_filename": "Example Movie.mp4", "source_context": template.metadata["source_context"]})
    single = replace(template, filename="one-video.mp4", relative_path="one-video.mp4", metadata={})
    assert discover_tv_disc_batches([movie, ordinary, single]) == ()


def test_non_video_with_episode_like_name_is_not_grouped():
    assert discover_tv_disc_batches([replace(episode(1), mime_type="text/plain")]) == ()


def test_folder_season_conflict_stays_review_only():
    entry = episode(1)
    context = {**entry.metadata["source_context"], "relative_path": "Example Show Season 2/Example Show S01E01.mp4"}
    entry = replace(entry, metadata={**entry.metadata, "source_context": context})
    proposal = resolve_tv_disc_batch([entry, episode(2), episode(3)])
    assert proposal.tracks[0].classification == "unresolved"
    assert proposal.needs_review


def test_duplicate_explicit_episode_numbers_are_not_renumbered_or_publishable():
    repeated = replace(episode(2), id=UUID(int=102), sha256="f" * 64)
    proposal = resolve_tv_disc_batch([episode(1), episode(2), repeated, episode(3)])
    assert all(track.classification == "unresolved" and track.episode_number is None for track in proposal.tracks[1:3])
    assert proposal.needs_review


def test_completed_or_removed_items_do_not_reenter_a_group():
    assert discover_tv_disc_batches([
        replace(episode(1), state="moved"), replace(episode(2), state="arrival_removed"),
    ]) == ()


def test_arrival_listing_returns_one_owner_scoped_review_group_without_mutating_items():
    entries = [episode(number) for number in range(1, 4)]
    store = MemoryVaultMasterStore()
    for entry in entries + [replace(episode(4), owner_user_id=UUID(int=200))]:
        store.items[entry.source_path] = entry
    response = Response()
    result = list_tv_resolver_batches(response, SimpleNamespace(user_id=OWNER), store)
    assert len(result["batches"]) == 1
    assert [track["proposed_episode_number"] for track in result["batches"][0]["tracks"]] == [1, 2, 3]
    assert all(store.get_item(entry.id) == entry for entry in entries)
    assert response.headers["Cache-Control"] == "private, no-store"
