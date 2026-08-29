import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import psycopg
from fastapi import HTTPException

from app.auth_store import PostgresAuthenticationStore
from app.tv_shows import PostgresTvShowStore, episode_artwork
from app.vault_master import CataloguedAsset, PostgresVaultMasterStore


@pytest.fixture
def postgres_conninfo() -> str:
    value = os.getenv("PV_TEST_DATABASE_URL")
    if not value:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture
def stores(postgres_conninfo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[PostgresAuthenticationStore, PostgresVaultMasterStore, PostgresTvShowStore]]:
    monkeypatch.setenv("PV_METADATA_STORAGE_PATH", str(tmp_path / "metadata"))
    auth = PostgresAuthenticationStore(postgres_conninfo)
    auth.initialize()
    auth.reset()
    auth.ensure_initial_administrator("owner", "test")
    auth.ensure_initial_administrator("second", "test")
    vault = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "metadata")
    vault.initialize()
    tv = PostgresTvShowStore(postgres_conninfo)
    tv.initialize()
    vault.reset()
    yield auth, vault, tv
    vault.reset()
    auth.reset()


def _asset(asset_id: UUID, number: int) -> CataloguedAsset:
    path = f"/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E{number:02d}.mp4"
    return CataloguedAsset(
        id=asset_id, asset_type="TV Shows", display_title=f"Foundation S01E{number:02d}",
        captured_on=None, location=None, vault_path=path, filename=Path(path).name,
        size_bytes=number, mime_type="video/mp4", sha256=f"{number:064x}",
        metadata={}, metadata_provenance={}, owner_username="owner", visibility="vault-wide",
    )


def test_postgres_tv_publication_is_atomic_visible_and_idempotent(stores: tuple[PostgresAuthenticationStore, PostgresVaultMasterStore, PostgresTvShowStore]) -> None:
    auth, vault, tv = stores
    owner = auth.get_account("owner")
    second = auth.get_account("second")
    assert owner and second
    first_asset, second_asset = uuid4(), uuid4()
    vault.restore_catalogued_asset(_asset(first_asset, 1), "owner")
    vault.restore_catalogued_asset(_asset(second_asset, 2), "owner")
    episodes = [
        (uuid4(), first_asset, 1, "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E01.mp4"),
        (uuid4(), second_asset, 2, "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E02.mp4"),
    ]
    show_id = tv.publish_complete_set(owner_user_id=owner.user_id, source_directory="Foundation Season 1", show_title="Foundation", season_number=1, audience="vault-wide", episodes=episodes)
    # A retry preserves the original canonical Show rather than making a duplicate.
    assert tv.publish_complete_set(owner_user_id=owner.user_id, source_directory="Foundation Season 1", show_title="Foundation", season_number=1, audience="private", episodes=episodes) == show_id
    visible = tv.get_visible(show_id, second.user_id)
    assert visible and len(visible.seasons) == 1 and [episode.episode_number for episode in visible.seasons[0].episodes] == [1, 2]
    assert tv.episode_is_visible(visible.seasons[0].episodes[0].id, second.user_id)
    tv.import_episode_metadata(
        visible.seasons[0].episodes[0].id,
        title="The Emperor's Peace", episode_number=1,
        imported_metadata={"overview": "Imported only after indexing", "runtime_minutes": 54},
        provider_ids={"Tmdb": "123"},
    )
    refreshed = tv.get_visible(show_id, second.user_id)
    assert refreshed and refreshed.seasons[0].episodes[0].title == "The Emperor's Peace"
    assert refreshed.seasons[0].episodes[0].runtime_minutes == 54
    show_document, season_documents = tv.hierarchy_sidecar_documents(show_id)
    assert show_document["show"]["owner_user_id"] == str(owner.user_id)
    assert show_document["show"]["visibility"] == "vault-wide"
    assert season_documents[0]["season"]["show_id"] == str(show_id)
    assert season_documents[0]["season"]["audience_inherited"] is True
    assert "recipient_user_ids" not in str(show_document)
    season_id = UUID(season_documents[0]["season"]["id"])
    metadata_root = Path(os.environ["PV_METADATA_STORAGE_PATH"])
    tv.retain_hierarchy_artwork(show_id, None, "poster", b"show-art", "image/jpeg", metadata_root)
    tv.retain_hierarchy_artwork(show_id, season_id, "poster", b"season-art", "image/jpeg", metadata_root)
    assert tv.visible_hierarchy_artwork(show_id, None, second.user_id, "poster", metadata_root)
    assert tv.visible_hierarchy_artwork(show_id, season_id, second.user_id, "poster", metadata_root)
    # Retaining the same canonical identity is an overwrite, never a fan-out record.
    tv.retain_hierarchy_artwork(show_id, None, "poster", b"show-art", "image/jpeg", metadata_root)
    assert len(list((metadata_root / "tv-artwork" / str(show_id)).iterdir())) == 1
    with psycopg.connect(tv.conninfo) as connection:
        connection.execute("UPDATE vault_tv_shows SET visibility='private' WHERE id=%s", (show_id,))
    assert tv.visible_hierarchy_artwork(show_id, None, second.user_id, "poster", metadata_root) is None
    assert tv.visible_hierarchy_artwork(show_id, season_id, second.user_id, "poster", metadata_root) is None
    assert tv.visible_hierarchy_artwork(show_id, None, owner.user_id, "poster", metadata_root)


def test_postgres_episode_artwork_is_owned_authorized_and_idempotent(
    stores: tuple[PostgresAuthenticationStore, PostgresVaultMasterStore, PostgresTvShowStore]
) -> None:
    auth, vault, tv = stores
    owner = auth.get_account("owner")
    second = auth.get_account("second")
    assert owner and second
    asset_id, arrival_id = uuid4(), uuid4()
    vault.restore_catalogued_asset(_asset(asset_id, 1), "owner")
    show_id = tv.publish_complete_set(
        owner_user_id=owner.user_id,
        source_directory="Foundation Season 1",
        show_title="Foundation",
        season_number=1,
        audience="vault-wide",
        episodes=[(arrival_id, asset_id, 1, "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E01.mp4")],
    )
    episode = tv.get_visible(show_id, owner.user_id).seasons[0].episodes[0]
    metadata_root = Path(os.environ["PV_METADATA_STORAGE_PATH"])
    assert episode.artwork_url is None
    assert [item.id for item in tv.pending_episode_artwork()] == [episode.id]
    tv.mark_episode_artwork_unavailable(episode.id)
    assert not tv.pending_episode_artwork()
    assert tv.get_visible(show_id, owner.user_id).seasons[0].episodes[0].artwork_url is None

    tv.import_episode_metadata(
        episode.id,
        title="The Emperor's Peace",
        episode_number=1,
        imported_metadata={"provider_ids": {"Tmdb": "123"}},
        provider_ids={"Tmdb": "123"},
    )
    tv.retain_episode_artwork(episode.id, "primary", b"first-still", "image/jpeg", metadata_root)
    record = tv.visible_episode_artwork(episode.id, second.user_id, "primary", metadata_root)
    assert record and record[0].read_bytes() == b"first-still"
    assert tv.get_visible(show_id, second.user_id).seasons[0].episodes[0].artwork_url == f"/api/tv-shows/episodes/{episode.id}/artwork/primary"
    assert episode_artwork(episode.id, "primary", SimpleNamespace(user_id=second.user_id), tv).path == record[0]

    # Same canonical identity replaces exactly one local still and keeps TV
    # identity/provider evidence untouched; no recipient fan-out is created.
    tv.retain_episode_artwork(episode.id, "primary", b"updated-still", "image/jpeg", metadata_root)
    assert record[0].read_bytes() == b"updated-still"
    assert len(list((metadata_root / "tv-artwork" / "episodes" / str(episode.id)).iterdir())) == 1
    with psycopg.connect(tv.conninfo, row_factory=psycopg.rows.dict_row) as connection:
        row = connection.execute("SELECT provider_ids, show_id, season_id FROM vault_tv_episodes WHERE id=%s", (episode.id,)).fetchone()
        assert row["provider_ids"] == {"Tmdb": "123"}
        assert row["show_id"] == show_id
        assert "recipient" not in str(row)
        connection.execute("UPDATE vault_tv_shows SET visibility='private' WHERE id=%s", (show_id,))
    assert tv.visible_episode_artwork(episode.id, second.user_id, "primary", metadata_root) is None
    assert tv.visible_episode_artwork(episode.id, uuid4(), "primary", metadata_root) is None
    assert tv.visible_episode_artwork(episode.id, owner.user_id, "primary", metadata_root)
    with pytest.raises(HTTPException) as denied:
        episode_artwork(episode.id, "primary", SimpleNamespace(user_id=second.user_id), tv)
    assert denied.value.status_code == 404
