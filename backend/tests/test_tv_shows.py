from uuid import uuid4
from datetime import datetime, timezone

from app.tv_shows import parse_reviewed_episode
from app.auth_store import EpisodeProgress, MemoryAuthenticationStore
from app.vault_master import (
    INCOMING_SOURCE,
    ImportItem,
    TV_PUBLICATION_SET_SCHEMA,
    tv_publication_set_destination,
    tv_publication_set_has_consistent_audience,
    tv_publication_set_is_ready,
)


def _episode(number: int, *, audience: str | None = "vault-wide") -> ImportItem:
    return ImportItem(
        id=uuid4(), batch_id=uuid4(), source_kind=INCOMING_SOURCE,
        source_path=f"/arrival/Foundation Season 1/Foundation S01E{number:02d}.mp4",
        relative_path=f"Foundation Season 1/Foundation S01E{number:02d}.mp4",
        filename=f"Foundation S01E{number:02d}.mp4", size_bytes=100,
        mime_type="video/mp4", sha256=f"{number:064x}", state="approved",
        modified_at=datetime.now(timezone.utc), proposal_reason="reviewed", proposal_confidence=1.0,
        proposed_category="TV Shows", proposed_destination=None,
        owner_username="owner", owner_user_id=uuid4(), publication_audience=audience,
        metadata={}, metadata_overrides={}, duplicate_of_id=None,
    )


def test_tv_episode_parser_requires_explicit_show_season_episode_evidence() -> None:
    parsed = parse_reviewed_episode("Foundation Season 1/Foundation S01E01.mp4", "Foundation S01E01.mp4")
    assert parsed and (parsed.show_title, parsed.season_number, parsed.episode_number) == ("Foundation", 1, 1)
    assert parse_reviewed_episode("Foundation Season 1/episode.mp4", "episode.mp4") is None


def test_reviewed_season_has_one_stable_show_season_publication_set() -> None:
    owner = uuid4()
    first = _episode(1)
    second = _episode(2)
    first = first.__class__(**{**first.__dict__, "owner_user_id": owner})
    second = second.__class__(**{**second.__dict__, "owner_user_id": owner})
    first_destination, marker = tv_publication_set_destination(first, [first, second])
    second_destination, second_marker = tv_publication_set_destination(second, [first, second])
    assert marker == second_marker
    assert marker["schema"] == TV_PUBLICATION_SET_SCHEMA
    assert marker["show_title"] == "Foundation"
    assert marker["season_number"] == 1
    assert first_destination.endswith("Foundation/Season 01/Foundation - S01E01.mp4")
    assert second_destination.endswith("Foundation/Season 01/Foundation - S01E02.mp4")


def test_tv_set_requires_all_episodes_and_one_show_audience() -> None:
    owner = uuid4()
    first, second = _episode(1), _episode(2)
    first = first.__class__(**{**first.__dict__, "owner_user_id": owner})
    second = second.__class__(**{**second.__dict__, "owner_user_id": owner})
    _, marker = tv_publication_set_destination(first, [first, second])
    first = first.__class__(**{**first.__dict__, "metadata": {"tv_publication_set": marker}})
    second = second.__class__(**{**second.__dict__, "metadata": {"tv_publication_set": marker}})
    assert tv_publication_set_is_ready(first, [first, second])
    assert tv_publication_set_has_consistent_audience(first, [first, second])
    private_second = second.__class__(**{**second.__dict__, "publication_audience": "private"})
    assert not tv_publication_set_has_consistent_audience(first, [first, private_second])


def test_episode_progress_is_scoped_to_user_and_episode() -> None:
    store = MemoryAuthenticationStore()
    episode_id, first_user, second_user = uuid4(), uuid4(), uuid4()
    store.save_episode_progress(first_user, EpisodeProgress(episode_id, 42, 100, False))
    store.save_episode_progress(second_user, EpisodeProgress(episode_id, 7, 100, False))
    assert store.get_episode_progress(first_user, episode_id).position_seconds == 42
    assert store.get_episode_progress(second_user, episode_id).position_seconds == 7
