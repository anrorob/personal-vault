import os
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import psycopg
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.auth import AuthenticatedIdentity, get_authentication_store, require_authenticated_user
from app.auth_store import Account, MemoryAuthenticationStore, PostgresAuthenticationStore
import app.gallery as gallery_module
import app.main as main_module
from app.gallery import get_gallery_path, scan_gallery
from app.main import app
from app.vault_master import (
    INCOMING_SOURCE,
    INVENTORY_SOURCE,
    CataloguedAsset,
    PostgresVaultMasterStore,
    ScannedFile,
    enqueue_catalogue_backfill,
    enqueue_root,
    get_vault_master_store,
    process_next_batch,
    process_next_move,
    scan_root,
)
from app.share_grants import (
    ACTIVE_GRANT_STATE,
    LOCAL_ALL_TARGET,
    LOCAL_USER_TARGET,
    PENDING_GRANT_STATE,
    REMOTE_VAULT_TARGET,
    PostgresShareGrantStore,
)
from app import vault_control_users
from app import vault_storage_control
import app.share_grants as share_grants
import app.vault_master as vault_master
import app.vault_master_api as vault_master_api
from app.federation import FederationStore
from app.federation import FEDERATION_PROTOCOL_VERSION, sign_envelope
from app.vault_master_ai import PostgresAiStore
from app.vault_master_autopilot import PostgresAutopilotStore
from app.vault_master_ingestion_ai import (
    PostgresIngestionAiStore,
    assess_destination,
)
from app.vault_master_intake import PostgresIntakeStore
from app.gallery_intelligence import PostgresGalleryIntelligenceStore
from app.gallery_people import MemoryGalleryPeopleStore, PostgresGalleryPeopleStore
import app.gallery_people_worker as gallery_people_worker
import app.gallery_intelligence as gallery_intelligence
import app.vault_master_ingestion_ai as ingestion_ai
import app.video_intelligence as video_intelligence
from app.video_intelligence import PostgresVideoIntelligenceStore, process_next_video_analysis_job
from app.vault_master_api import get_catalogue_preview_roots
from app.tv_shows import PostgresTvShowStore


@pytest.fixture
def postgres_conninfo() -> str:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")

    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")

    return conninfo


def _reset_auxiliary_postgres_state(conninfo: str) -> None:
    """Clear explicitly bootstrapped auxiliary state before the master reset.

    The production bootstrap initializes these stores once at startup.  This
    shared PostgreSQL fixture creates the same schema for every test, so its
    controlled reset must remove dependent rows before ``vault_assets``.
    """
    with psycopg.connect(conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE TABLE
                    vault_autopilot_policies,
                    vault_intake_sources,
                    vault_ingestion_ai_jobs,
                    vault_ingestion_analysis_batches,
                    vault_ingestion_review_batches,
                    vault_routing_memory_rules,
                    vault_ai_jobs,
                    vault_gallery_intelligence_bulk_runs,
                    vault_gallery_intelligence_jobs,
                    vault_metadata_terms,
                    vault_gallery_intelligence_concepts,
                    vault_people,
                    vault_video_analysis_jobs
                RESTART IDENTITY CASCADE
                """
            )
            cursor.execute(
                """
                UPDATE vault_intake_control
                SET enabled = FALSE, active_transfers = 0, updated_at = CURRENT_TIMESTAMP
                WHERE singleton
                """
            )


@pytest.fixture
def postgres_store(
    postgres_conninfo: str,
    tmp_path: Path,
) -> Iterator[PostgresVaultMasterStore]:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    authentication.initialize()
    for username in ("owner", "owner", "son"):
        authentication.ensure_initial_administrator(username, "test-hash")
    store = PostgresVaultMasterStore(
        postgres_conninfo,
        sidecar_root=tmp_path / "metadata",
    )
    store.initialize()
    for auxiliary_store in (
        PostgresAutopilotStore(postgres_conninfo),
        PostgresIntakeStore(postgres_conninfo),
        PostgresIngestionAiStore(postgres_conninfo),
        PostgresAiStore(postgres_conninfo),
        PostgresGalleryIntelligenceStore(postgres_conninfo),
        PostgresGalleryPeopleStore(postgres_conninfo),
        PostgresVideoIntelligenceStore(postgres_conninfo),
    ):
        auxiliary_store.initialize()
    _reset_auxiliary_postgres_state(postgres_conninfo)
    store.reset()
    yield store
    _reset_auxiliary_postgres_state(postgres_conninfo)
    store.reset()


def _catalogued_asset(
    asset_id: UUID,
    vault_path: str,
    owner_username: str,
    *,
    size_bytes: int = 8,
) -> CataloguedAsset:
    return CataloguedAsset(
        id=asset_id,
        asset_type="Gallery",
        display_title=Path(vault_path).stem,
        captured_on=None,
        location=None,
        vault_path=vault_path,
        filename=Path(vault_path).name,
        size_bytes=size_bytes,
        mime_type="image/jpeg",
        sha256=f"{asset_id.int:064x}"[-64:],
        metadata={},
        metadata_provenance={},
        owner_username=owner_username,
    )


def _arrival_hall_owner_user_id(conninfo: str, username: str = "owner") -> UUID:
    account = PostgresAuthenticationStore(conninfo).get_account(username)
    assert account is not None
    return account.user_id


def test_postgres_duplicate_detection_is_owner_scoped_and_ignores_rejected_arrivals(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    owner, other = (
        _arrival_hall_owner_user_id(postgres_conninfo, name)
        for name in ("owner", "son")
    )
    sha256 = "d" * 64
    inventory = postgres_store.record_file(
        postgres_store.create_batch(INVENTORY_SOURCE, "/inventory"),
        INVENTORY_SOURCE,
        ScannedFile("/inventory/original.txt", "original.txt", "original.txt", 4,
                    "text/plain", datetime.now(timezone.utc), sha256, {}, owner_user_id=owner),
    )
    rejected = postgres_store.record_file(
        postgres_store.create_batch(INCOMING_SOURCE, "/arrival"), INCOMING_SOURCE,
        ScannedFile("/arrival/rejected.txt", "rejected.txt", "rejected.txt", 4,
                    "text/plain", datetime.now(timezone.utc), sha256, {}, owner_user_id=owner),
    )
    assert rejected.duplicate_of_id == inventory.id
    assert postgres_store.record_decision(rejected.id, "rejected", "owner")
    other_item = postgres_store.record_file(
        postgres_store.create_batch(INCOMING_SOURCE, "/arrival"), INCOMING_SOURCE,
        ScannedFile("/arrival/other.txt", "other.txt", "other.txt", 4,
                    "text/plain", datetime.now(timezone.utc), sha256, {}, owner_user_id=other),
    )

    assert other_item.duplicate_of_id is None
    assert other_item.state == "needs_review"


def test_postgres_theatre_duplicate_remains_vault_wide_after_theatre_selection(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    owner, other = (
        _arrival_hall_owner_user_id(postgres_conninfo, name)
        for name in ("owner", "son")
    )
    sha256 = "e" * 64
    inventory = postgres_store.record_file(
        postgres_store.create_batch(INVENTORY_SOURCE, "/inventory"),
        INVENTORY_SOURCE,
        ScannedFile("/inventory/film.mkv", "film.mkv", "film.mkv", 9,
                    "video/x-matroska", datetime.now(timezone.utc), sha256, {}, owner_user_id=owner),
    )
    postgres_store.restore_catalogued_asset(CataloguedAsset(
        id=uuid4(), asset_type="Movies", display_title="Film", captured_on=None,
        location=None, vault_path="/vault/Theatre/Movies/film.mkv", filename="film.mkv",
        size_bytes=9, mime_type="video/x-matroska", sha256=sha256,
        metadata={}, metadata_provenance={}, owner_username="owner", owner_user_id=owner,
    ), "owner")
    arrival = postgres_store.record_file(
        postgres_store.create_batch(INCOMING_SOURCE, "/arrival"), INCOMING_SOURCE,
        ScannedFile("/arrival/film.mkv", "film.mkv", "film.mkv", 9,
                    "video/x-matroska", datetime.now(timezone.utc), sha256, {}, owner_user_id=other),
    )

    assert arrival.duplicate_of_id is None
    theatre = postgres_store.update_proposal(arrival.id, "Movies", "son")
    assert theatre is not None and theatre.duplicate_of_id == inventory.id
    personal = postgres_store.update_proposal(arrival.id, "Home Videos", "son")
    assert personal is not None and personal.duplicate_of_id is None


def test_vm070_movie_vault_wide_migration_is_scoped_and_idempotent(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    """VM-070 changes only legacy private Movie visibility, exactly once."""
    movie = postgres_store.restore_catalogued_asset(
        CataloguedAsset(
            **{
                **_catalogued_asset(
                    UUID("10000000-0000-0000-0000-000000000701"),
                    "/vault/Theatre/Movies/legacy-private.mkv",
                    "owner",
                ).__dict__,
                "asset_type": "Movies",
                "mime_type": "video/x-matroska",
            }
        ),
        "owner",
    )
    shared_movie = postgres_store.restore_catalogued_asset(
        CataloguedAsset(
            **{
                **_catalogued_asset(
                    UUID("10000000-0000-0000-0000-000000000702"),
                    "/vault/Theatre/Movies/explicit-shared.mkv",
                    "owner",
                ).__dict__,
                "asset_type": "Movies",
                "mime_type": "video/x-matroska",
                "visibility": "shared",
                "shared_with": ("son",),
            }
        ),
        "owner",
    )
    gallery = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000703"),
            "/vault/Gallery/unaffected.jpg",
            "owner",
        ),
        "owner",
    )

    migrated = PostgresVaultMasterStore(postgres_conninfo)
    migrated.initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        rows = connection.execute(
            "SELECT id, visibility, updated_at FROM vault_assets WHERE id = ANY(%s)",
            ([movie.id, shared_movie.id, gallery.id],),
        ).fetchall()
    values = {UUID(str(row[0])): row for row in rows}
    assert values[movie.id][1] == "vault-wide"
    assert values[shared_movie.id][1] == "shared"
    assert values[gallery.id][1] == "private"
    migrated_at = values[movie.id][2]

    PostgresVaultMasterStore(postgres_conninfo).initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        repeated = connection.execute(
            "SELECT visibility, updated_at FROM vault_assets WHERE id = %s",
            (movie.id,),
        ).fetchone()
    assert repeated is not None
    assert repeated[0] == "vault-wide"
    assert repeated[1] == migrated_at


def test_postgres_theatre_proposals_preserve_audience_without_creating_tv_set(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "Foundation S01E01.mp4").write_bytes(b"episode")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]

    movie = postgres_store.update_proposal(
        item.id, "Movies", "owner", publication_audience="private"
    )
    tv_show = postgres_store.update_proposal(
        item.id, "TV Shows", "owner", publication_audience="vault-wide"
    )

    assert movie is not None
    assert movie.proposed_destination == "/vault/Theatre/Movies/Foundation S01E01.mp4"
    assert movie.publication_audience == "private"
    assert tv_show is not None
    assert tv_show.proposed_destination == "/vault/Theatre/TV Shows/Foundation S01E01.mp4"
    assert tv_show.publication_audience == "vault-wide"
    assert tv_show.state == "needs_review"
    assert tv_show.metadata.get("tv_publication_set") is None

    with psycopg.connect(postgres_conninfo) as connection:
        row = connection.execute(
            "SELECT proposed_category, proposed_destination, publication_audience, state, "
            "metadata ? 'tv_publication_set' FROM vault_master_items WHERE id = %s",
            (item.id,),
        ).fetchone()
    assert row == (
        "TV Shows",
        "/vault/Theatre/TV Shows/Foundation S01E01.mp4",
        "vault-wide",
        "needs_review",
        False,
    )


def test_postgres_bulk_tv_approval_requires_complete_season_before_any_decision(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall" / "Foundation Season 1"
    incoming.mkdir(parents=True)
    for number in (1, 2):
        (incoming / f"Foundation S01E{number:02d}.mp4").write_bytes(
            f"episode-{number}".encode()
        )
    scan_root(
        postgres_store,
        incoming.parent,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    identity = AuthenticatedIdentity(owner)
    reviewed = [
        postgres_store.update_proposal(
            item.id, "TV Shows", "owner", publication_audience="vault-wide"
        )
        for item in postgres_store.list_items()
    ]
    assert all(item is not None for item in reviewed)
    items = [item for item in reviewed if item is not None]

    with pytest.raises(HTTPException, match="Select every Episode") as partial:
        vault_master_api.bulk_approve_proposals(
            vault_master_api.ItemSelection(item_ids=[items[0].id]),
            identity,
            postgres_store,
            PostgresIngestionAiStore(postgres_conninfo),
        )
    assert partial.value.status_code == 409
    assert {item.state for item in postgres_store.list_items()} == {"needs_review"}

    approved = vault_master_api.bulk_approve_proposals(
        vault_master_api.ItemSelection(item_ids=[item.id for item in items]),
        identity,
        postgres_store,
        PostgresIngestionAiStore(postgres_conninfo),
    )
    assert len(approved.items) == 2
    persisted = postgres_store.list_items()
    assert {item.state for item in persisted} == {"approved"}
    markers = [item.metadata.get("tv_publication_set") for item in persisted]
    assert markers[0] == markers[1]
    assert all(
        item.proposed_destination
        and "/Theatre/TV Shows/" in item.proposed_destination
        for item in persisted
    )


def test_local_vault_identity_bootstraps_once_and_reuses_its_uuid(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    first = postgres_store.get_local_vault_id()
    repeated = PostgresVaultMasterStore(postgres_conninfo)

    assert isinstance(first, UUID)
    assert repeated.get_local_vault_id() == first
    with psycopg.connect(postgres_conninfo) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM vaults WHERE is_local = TRUE"
        ).fetchone()
    assert count == (1,)


def test_federated_share_lifecycle_is_durable_and_preserves_origin_ownership(
    postgres_store: PostgresVaultMasterStore, postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    asset = _catalogued_asset(uuid4(), "/vault/Gallery/federation-origin.jpg", "owner")
    asset = replace(asset, owner_user_id=owner.user_id)
    postgres_store.restore_catalogued_asset(asset, "owner")
    federation = FederationStore(postgres_conninfo)
    peer = federation.pair_vault(uuid4(), "Vault B", "https://vault-b.test", "k" * 32)
    share_id = federation.create_outgoing_shares(owner.user_id, [asset.id], peer.remote_vault_id, "standard")[0]
    assert federation.list_outgoing(owner.user_id)[0].state == "pending"
    federation.transition_outgoing([share_id], owner.user_id, "activate")
    assert federation.list_outgoing(owner.user_id)[0].state == "active"
    federation.transition_outgoing([share_id], owner.user_id, "revoke")
    assert federation.list_outgoing(owner.user_id)[0].state == "revoked"
    assert postgres_store.get_catalogued_asset_by_id(asset.id).owner_user_id == owner.user_id


def test_managed_theatre_rename_updates_path_and_placement_atomically(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    source = "/vault/Theatre/Movies/G1_t00.mkv"
    destination = "/vault/Theatre/Movies/Terminator 2 - Judgment Day (1991)/Terminator 2 - Judgment Day (1991).mkv"
    asset = replace(
        _catalogued_asset(uuid4(), source, "owner"),
        owner_user_id=owner.user_id,
        asset_type="Movies",
        display_title="G1 t00",
        size_bytes=12,
        sha256="a" * 64,
        metadata={
            "movie_identity_provisional": {"state": "provisional"},
            "storage_placement": {"slot_id": "PV-DISK-001", "relative_path": "Theatre/Movies/G1_t00.mkv"},
        },
        detected_metadata={"movie_identity_provisional": {"state": "provisional"}},
        imported_metadata={
            "display_title": "Terminator 2: Judgment Day",
            "release_year": 1991,
            "provider_ids": {"Tmdb": "280"},
        },
        metadata_provenance={
            "display_title": "import:jellyfin",
            "release_year": "import:jellyfin",
        },
        effective_metadata={
            "display_title": "Terminator 2: Judgment Day",
            "release_year": 1991,
            "storage_placement": {"slot_id": "PV-DISK-001", "relative_path": "Theatre/Movies/G1_t00.mkv"},
        },
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("INSERT INTO vault_storage_slots(slot_id,state,assigned_areas) VALUES ('PV-DISK-001','active','[\"Theatre / Movies\"]') ON CONFLICT (slot_id) DO NOTHING")
        file_id = connection.execute("SELECT id FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchone()[0]
        connection.execute("INSERT INTO vault_file_storage_placements(file_id,slot_id,relative_path,assigned_by,placement_reason) VALUES (%s,'PV-DISK-001','Theatre/Movies/G1_t00.mkv','test','test')", (file_id,))
    snapshot = postgres_store.theatre_movie_rename_snapshot(asset.id, owner.user_id)
    assert snapshot is not None
    receipt = {
        "schema": "personal-vault.theatre-movie-rename.v1",
        "request_id": str(uuid4()), "asset_id": str(asset.id),
        "file_id": str(file_id), "owner_user_id": str(owner.user_id),
        "slot_id": "PV-DISK-001", "source_logical_path": source,
        "destination_logical_path": destination,
        "source_relative_path": "Theatre/Movies/G1_t00.mkv",
        "destination_relative_path": destination.removeprefix("/vault/"),
        "title": "Terminator 2: Judgment Day", "release_year": 1991,
        "expected_sha256": "a" * 64, "expected_size_bytes": 12,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = postgres_store.complete_theatre_movie_rename(receipt)
    assert updated is not None
    assert updated.id == asset.id and updated.owner_user_id == owner.user_id
    assert updated.vault_path == destination
    assert updated.filename == Path(destination).name
    assert updated.user_overrides == {}
    assert updated.metadata_provenance["display_title"] == "import:jellyfin"
    assert updated.effective_metadata["release_year"] == 1991
    assert updated.effective_metadata["storage_placement"]["slot_id"] == "PV-DISK-001"
    with psycopg.connect(postgres_conninfo) as connection:
        row = connection.execute("SELECT file.id,file.size_bytes,file.sha256,placement.slot_id,placement.relative_path FROM vault_files file JOIN vault_file_storage_placements placement ON placement.file_id=file.id WHERE file.asset_id=%s", (asset.id,)).fetchone()
        history = connection.execute("SELECT action,previous_values,current_values FROM vault_asset_history WHERE asset_id=%s AND action='theatre_movie_renamed'", (asset.id,)).fetchone()
    assert row == (file_id, 12, "a" * 64, "PV-DISK-001", destination.removeprefix("/vault/"))
    assert history is not None
    assert history[1]["vault_path"] == source and history[2]["vault_path"] == destination
    assert postgres_store.complete_theatre_movie_rename(receipt) is None


def test_federated_cache_and_progress_are_recipient_scoped_and_revocation_safe(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secondary=os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary: pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT","https://vault-a.test")
    auth_a,auth_b=PostgresAuthenticationStore(postgres_conninfo),PostgresAuthenticationStore(secondary)
    for auth in (auth_a,auth_b):
        for name in ("owner","owner","son"): auth.ensure_initial_administrator(name,"test-hash")
    a,b=PostgresVaultMasterStore(postgres_conninfo,sidecar_root=tmp_path/'a'),PostgresVaultMasterStore(secondary,sidecar_root=tmp_path/'b'); a.reset(); b.reset()
    owner=auth_a.get_account('owner'); owner=auth_b.get_account('owner'); son=auth_b.get_account('son'); assert owner and owner and son
    asset=replace(_catalogued_asset(uuid4(),'/vault/Theatre/Movies/stage8.mp4','owner'),owner_user_id=owner.user_id,asset_type='Movies',mime_type='video/mp4',size_bytes=12,sha256='a'*64); a.restore_catalogued_asset(asset,'owner')
    fa,fb=FederationStore(postgres_conninfo),FederationStore(secondary); peer=fa.pair_vault(fb.local_vault_id(),'B','https://vault-b.test','z'*32); fb.pair_vault(fa.local_vault_id(),'A','https://vault-a.test','z'*32)
    share_id=fa.create_outgoing_shares(owner.user_id,[asset.id],peer.remote_vault_id,'quick')[0]
    event={"protocol_version":FEDERATION_PROTOCOL_VERSION,"event_id":str(uuid4()),"origin_vault_id":str(fa.local_vault_id()),"target_vault_id":str(fb.local_vault_id()),"event_type":"share_activated","timestamp":datetime.now(timezone.utc).isoformat(),"share":{"share_id":str(share_id),"asset_id":str(asset.id),"state":"active","asset_type":"Movies","display_title":"Stage 8","owner_label":"Owner","origin_endpoint":"https://vault-a.test"}}
    assert fb.receive_event(event,sign_envelope(event,'z'*32)); incoming=fb.list_incoming_admin()[0]; fb.set_distribution(incoming.incoming_share_id,owner.user_id,False,[owner.user_id]); share=fb.list_incoming_for_user(owner.user_id)[0]
    assert fb.begin_cache(share,12,'a'*64); assert not fb.begin_cache(share,12,'a'*64); assert fb.cache_entry(share.origin_vault_id,share.origin_asset_id).state=='incomplete'
    fb.set_progress(share,owner.user_id,12.5,60); fb.set_progress(share,son.user_id,3,60)
    with psycopg.connect(secondary) as connection: assert connection.execute("SELECT count(*) FROM vault_federation_viewer_progress WHERE origin_asset_id=%s",(asset.id,)).fetchone()[0]==2
    revoke={**event,"event_id":str(uuid4()),"event_type":"share_revoked","timestamp":datetime.now(timezone.utc).isoformat()}; assert fb.receive_event(revoke,sign_envelope(revoke,'z'*32))
    assert fb.list_incoming_for_user(owner.user_id)==[]; assert fb.cache_entry(share.origin_vault_id,share.origin_asset_id).state=='invalidated'
    with psycopg.connect(secondary) as connection: assert connection.execute("SELECT count(*) FROM vault_federation_viewer_progress WHERE origin_asset_id=%s",(asset.id,)).fetchone()[0]==0
    a.reset(); b.reset()


def test_two_vault_federated_collection_lifecycle_is_logical_live_and_deduplicated(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote collection carries paths, never copied assets or receiver ownership."""
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a, auth_b = PostgresAuthenticationStore(postgres_conninfo), PostgresAuthenticationStore(secondary)
    auth_a.initialize(); auth_b.initialize()
    for auth in (auth_a, auth_b):
        for username in ("owner", "owner"):
            auth.ensure_initial_administrator(username, "test-hash")
    store_a = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "collections-a")
    store_b = PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "collections-b")
    store_a.initialize(); store_b.initialize()
    store_a.reset(); store_b.reset()
    owner, owner = auth_a.get_account("owner"), auth_b.get_account("owner")
    assert owner and owner
    assets = [
        store_a.restore_catalogued_asset(
            replace(_catalogued_asset(uuid4(), f"/vault/Gallery/federated-collection-{index}.jpg", "owner"), owner_user_id=owner.user_id),
            "owner",
        )
        for index in (1, 2)
    ]
    collection = PostgresShareGrantStore(postgres_conninfo).create_collection(
        owner.user_id, "Federated holiday", [asset.id for asset in assets]
    )
    origin, recipient = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer = origin.pair_vault(recipient.local_vault_id(), "Vault B", "https://vault-b.test", "c" * 32)
    recipient.pair_vault(origin.local_vault_id(), "Vault A", "https://vault-a.test", "c" * 32)
    collection_share_id = origin.create_outgoing_collection_share(owner.user_id, collection.collection_id, peer.remote_vault_id, "quick")

    def receive_latest() -> dict[str, object]:
        with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as connection:
            payload = connection.execute(
                "SELECT event_type,payload FROM vault_federation_collection_deliveries WHERE federation_collection_share_id=%s ORDER BY created_at DESC LIMIT 1",
                (collection_share_id,),
            ).fetchone()
        assert payload is not None
        event = {
            "protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()),
            "origin_vault_id": str(origin.local_vault_id()), "target_vault_id": str(recipient.local_vault_id()),
            "event_type": payload["event_type"], "timestamp": datetime.now(timezone.utc).isoformat(), "share": dict(payload["payload"]),
        }
        assert recipient.receive_event(event, sign_envelope(event, "c" * 32))
        return event

    initial = receive_latest()
    assert recipient.list_incoming_collections_for_user(owner.user_id) == []
    incoming_collection = recipient.list_incoming_collections_admin()[0]
    recipient.set_collection_distribution(incoming_collection.incoming_collection_id, owner.user_id, False, [owner.user_id])
    visible = recipient.list_incoming_for_user(owner.user_id, "Gallery")
    assert {item.origin_asset_id for item in visible} == {asset.id for asset in assets}
    # The same origin asset is also directly shared.  It remains one Commons
    # identity despite two current authorized access paths.
    direct_share_id = origin.create_outgoing_shares(owner.user_id, [assets[0].id], peer.remote_vault_id, "quick")[0]
    direct_event = {
        "protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()),
        "origin_vault_id": str(origin.local_vault_id()), "target_vault_id": str(recipient.local_vault_id()),
        "event_type": "share_activated", "timestamp": datetime.now(timezone.utc).isoformat(),
        "share": {"share_id": str(direct_share_id), "asset_id": str(assets[0].id), "state": "active", "asset_type": "Gallery", "display_title": assets[0].display_title, "owner_label": "owner", "origin_endpoint": "https://vault-a.test"},
    }
    assert recipient.receive_event(direct_event, sign_envelope(direct_event, "c" * 32))
    direct_incoming = next(item for item in recipient.list_incoming_admin() if item.origin_share_id == direct_share_id)
    recipient.set_distribution(direct_incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    deduplicated = recipient.list_incoming_for_user(owner.user_id, "Gallery")
    assert len(deduplicated) == 2
    assert {item.origin_asset_id for item in deduplicated} == {asset.id for asset in assets}
    recipient.clear_distribution(direct_incoming.incoming_share_id, owner.user_id)
    preview_time = datetime.now(timezone.utc).isoformat()
    preview_request = {"share_id": str(collection_share_id), "asset_id": str(assets[0].id), "requester_vault_id": str(recipient.local_vault_id()), "timestamp": preview_time}
    assert origin.authorizes_origin_preview(collection_share_id, assets[0].id, recipient.local_vault_id(), preview_time, sign_envelope(preview_request, "c" * 32))

    PostgresShareGrantStore(postgres_conninfo).remove_collection_member(collection.collection_id, owner.user_id, assets[1].id)
    assert origin.reconcile_collections(peer.remote_vault_id) == 1
    receive_latest()
    assert [item.origin_asset_id for item in recipient.list_incoming_collection_members(incoming_collection.incoming_collection_id, owner.user_id)] == [assets[0].id]
    # A delayed snapshot cannot restore a removed member after a newer revision.
    delayed = {**initial, "event_id": str(uuid4()), "timestamp": datetime.now(timezone.utc).isoformat()}
    assert recipient.receive_event(delayed, sign_envelope(delayed, "c" * 32))
    assert [item.origin_asset_id for item in recipient.list_incoming_collection_members(incoming_collection.incoming_collection_id, owner.user_id)] == [assets[0].id]

    origin.transition_outgoing_collection(collection_share_id, owner.user_id, "revoke")
    receive_latest()
    assert recipient.list_incoming_for_user(owner.user_id, "Gallery") == []
    assert not origin.authorizes_origin_preview(collection_share_id, assets[0].id, recipient.local_vault_id(), preview_time, sign_envelope(preview_request, "c" * 32))
    # Explicit re-share advances lifecycle in place; it does not create a
    # second remote collection or require the receiver to redistribute it.
    assert origin.create_outgoing_collection_share(owner.user_id, collection.collection_id, peer.remote_vault_id, "quick") == collection_share_id
    receive_latest()
    assert [item.origin_asset_id for item in recipient.list_incoming_for_user(owner.user_id, "Gallery")] == [assets[0].id]
    PostgresShareGrantStore(postgres_conninfo).archive_collection(collection.collection_id, owner.user_id)
    assert origin.reconcile_collections(peer.remote_vault_id) == 1
    receive_latest()
    assert recipient.list_incoming_for_user(owner.user_id, "Gallery") == []
    assert recipient.list_incoming_collections_admin()[0].state == "archived"
    store_a.reset(); store_b.reset()


def test_two_vault_federation_receiving_distribution_and_revocation(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a = PostgresAuthenticationStore(postgres_conninfo)
    auth_b = PostgresAuthenticationStore(secondary)
    for auth in (auth_a, auth_b):
        for username in ("owner", "owner", "son"):
            auth.ensure_initial_administrator(username, "test-hash")
    store_a = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "a")
    store_b = PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "b")
    store_a.reset(); store_b.reset()
    owner = auth_a.get_account("owner"); owner = auth_b.get_account("owner"); uninvolved_user = auth_b.get_account("son")
    assert owner and owner and uninvolved_user
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/two-vault.jpg", "owner"), owner_user_id=owner.user_id)
    store_a.restore_catalogued_asset(asset, "owner")
    federation_a, federation_b = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer_b = federation_a.pair_vault(federation_b.local_vault_id(), "Vault B", "https://vault-b.test", "p" * 32)
    federation_b.pair_vault(federation_a.local_vault_id(), "Vault A", "https://vault-a.test", "p" * 32)
    share_id = federation_a.create_outgoing_shares(owner.user_id, [asset.id], peer_b.remote_vault_id, "quick")[0]
    event = {"protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()), "origin_vault_id": str(federation_a.local_vault_id()), "target_vault_id": str(federation_b.local_vault_id()), "event_type": "share_activated", "timestamp": datetime.now(timezone.utc).isoformat(), "share": {"share_id": str(share_id), "asset_id": str(asset.id), "state": "active", "asset_type": "Gallery", "display_title": "Two Vault", "owner_label": "Owner", "origin_endpoint": "https://vault-a.test"}}
    assert federation_b.receive_event(event, sign_envelope(event, "p" * 32))
    incoming = federation_b.list_incoming_admin()[0]
    assert federation_b.list_incoming_for_user(owner.user_id) == []
    federation_b.set_distribution(incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    assert [share.origin_asset_id for share in federation_b.list_incoming_for_user(owner.user_id)] == [asset.id]
    assert federation_b.list_incoming_for_user(uninvolved_user.user_id) == []
    federation_b.clear_distribution(incoming.incoming_share_id, owner.user_id)
    assert federation_b.list_incoming_for_user(owner.user_id) == []
    federation_b.set_distribution(incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    preview_time = datetime.now(timezone.utc).isoformat()
    preview_request = {"share_id": str(share_id), "asset_id": str(asset.id), "requester_vault_id": str(federation_b.local_vault_id()), "timestamp": preview_time}
    assert federation_a.authorizes_origin_preview(share_id, asset.id, federation_b.local_vault_id(), preview_time, sign_envelope(preview_request, "p" * 32))
    federation_a.transition_outgoing([share_id], owner.user_id, "revoke")
    revocation = {**event, "event_id": str(uuid4()), "event_type": "share_revoked", "timestamp": datetime.now(timezone.utc).isoformat()}
    assert federation_b.receive_event(revocation, sign_envelope(revocation, "p" * 32))
    assert federation_b.list_incoming_for_user(owner.user_id) == []
    assert not federation_a.authorizes_origin_preview(share_id, asset.id, federation_b.local_vault_id(), preview_time, sign_envelope(preview_request, "p" * 32))
    store_a.reset(); store_b.reset()


def test_federated_lifecycle_revision_prevents_out_of_order_activation_after_revocation(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A delayed pre-revocation activation must never restore effective access."""
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a, auth_b = PostgresAuthenticationStore(postgres_conninfo), PostgresAuthenticationStore(secondary)
    for auth in (auth_a, auth_b):
        auth.ensure_initial_administrator("owner", "test-hash")
        auth.ensure_initial_administrator("owner", "test-hash")
    store_a = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "lifecycle-a")
    store_b = PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "lifecycle-b")
    store_a.reset(); store_b.reset()


def test_federation_reconciliation_requeues_current_revocation_with_priority(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a, auth_b = PostgresAuthenticationStore(postgres_conninfo), PostgresAuthenticationStore(secondary)
    for auth in (auth_a, auth_b):
        auth.ensure_initial_administrator("owner", "test-hash")
    store_a = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "reconcile-a")
    store_b = PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "reconcile-b")
    store_a.reset(); store_b.reset()


def test_unpair_fails_closed_for_incoming_access_without_touching_download_provenance(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a, auth_b = PostgresAuthenticationStore(postgres_conninfo), PostgresAuthenticationStore(secondary)
    for auth in (auth_a, auth_b):
        auth.ensure_initial_administrator("owner", "test-hash")
        auth.ensure_initial_administrator("owner", "test-hash")
    store_a = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "unpair-a")
    store_b = PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "unpair-b")
    store_a.reset(); store_b.reset()
    owner, owner = auth_a.get_account("owner"), auth_b.get_account("owner")
    assert owner and owner
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/unpair.jpg", "owner"), owner_user_id=owner.user_id)
    store_a.restore_catalogued_asset(asset, "owner")
    origin, recipient = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer = origin.pair_vault(recipient.local_vault_id(), "B", "https://vault-b.test", "t" * 32)
    recipient.pair_vault(origin.local_vault_id(), "A", "https://vault-a.test", "t" * 32)
    share_id = origin.create_outgoing_shares(owner.user_id, [asset.id], peer.remote_vault_id, "quick")[0]
    event = {"protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()), "origin_vault_id": str(origin.local_vault_id()), "target_vault_id": str(recipient.local_vault_id()), "event_type": "share_activated", "timestamp": datetime.now(timezone.utc).isoformat(), "share": {"share_id": str(share_id), "asset_id": str(asset.id), "state": "active", "asset_type": "Gallery", "display_title": "Unpair", "owner_label": "Owner", "origin_endpoint": "https://vault-a.test", "lifecycle_revision": 1}}
    assert recipient.receive_event(event, sign_envelope(event, "t" * 32))
    incoming = recipient.list_incoming_admin()[0]
    recipient.set_distribution(incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    assert recipient.list_incoming_for_user(owner.user_id)
    recipient.unpair_vault(origin.local_vault_id())
    assert recipient.list_incoming_for_user(owner.user_id) == []
    with pytest.raises(ValueError):
        recipient.incoming_for_preview(incoming.incoming_share_id, owner.user_id)
    store_a.reset(); store_b.reset()
    owner = auth_a.get_account("owner")
    assert owner
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/reconcile.jpg", "owner"), owner_user_id=owner.user_id)
    store_a.restore_catalogued_asset(asset, "owner")
    origin, recipient = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer = origin.pair_vault(recipient.local_vault_id(), "B", "https://vault-b.test", "s" * 32)
    recipient.pair_vault(origin.local_vault_id(), "A", "https://vault-a.test", "s" * 32)
    share_id = origin.create_outgoing_shares(owner.user_id, [asset.id], peer.remote_vault_id, "quick")[0]
    origin.transition_outgoing([share_id], owner.user_id, "revoke")
    assert origin.reconcile_authoritative_state(peer.remote_vault_id) == 1
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as connection:
        delivery = connection.execute("SELECT event_type,priority,payload FROM vault_federation_deliveries WHERE federation_share_id=%s ORDER BY created_at DESC LIMIT 1", (share_id,)).fetchone()
    assert delivery["event_type"] == "share_revoked" and delivery["priority"] == 100
    assert delivery["payload"]["lifecycle_revision"] >= 1
    store_a.reset(); store_b.reset()
    owner, owner = auth_a.get_account("owner"), auth_b.get_account("owner")
    assert owner and owner
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/lifecycle.jpg", "owner"), owner_user_id=owner.user_id)
    store_a.restore_catalogued_asset(asset, "owner")
    origin, recipient = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer = origin.pair_vault(recipient.local_vault_id(), "B", "https://vault-b.test", "r" * 32)
    recipient.pair_vault(origin.local_vault_id(), "A", "https://vault-a.test", "r" * 32)
    share_id = origin.create_outgoing_shares(owner.user_id, [asset.id], peer.remote_vault_id, "quick")[0]
    def event(kind: str, revision: int) -> dict[str, object]:
        return {"protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()), "origin_vault_id": str(origin.local_vault_id()), "target_vault_id": str(recipient.local_vault_id()), "event_type": kind, "timestamp": datetime.now(timezone.utc).isoformat(), "share": {"share_id": str(share_id), "asset_id": str(asset.id), "state": "revoked" if kind == "share_revoked" else "active", "asset_type": "Gallery", "display_title": "Lifecycle", "owner_label": "Owner", "origin_endpoint": "https://vault-a.test", "lifecycle_revision": revision}}
    activated = event("share_activated", 1)
    assert recipient.receive_event(activated, sign_envelope(activated, "r" * 32))
    incoming = recipient.list_incoming_admin()[0]
    recipient.set_distribution(incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    revoked = event("share_revoked", 2)
    assert recipient.receive_event(revoked, sign_envelope(revoked, "r" * 32))
    stale = event("share_activated", 1)
    assert recipient.receive_event(stale, sign_envelope(stale, "r" * 32))
    assert recipient.list_incoming_for_user(owner.user_id) == []
    with psycopg.connect(secondary) as connection:
        row = connection.execute("SELECT state,lifecycle_revision FROM vault_federation_incoming_shares WHERE origin_share_id=%s", (share_id,)).fetchone()
    assert row == ("revoked", 2)
    store_a.reset(); store_b.reset()


def test_stage9_download_permission_and_reservation_are_recipient_scoped_and_idempotent(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a, auth_b = PostgresAuthenticationStore(postgres_conninfo), PostgresAuthenticationStore(secondary)
    for auth in (auth_a, auth_b):
        auth.ensure_initial_administrator("owner", "test-hash")
        auth.ensure_initial_administrator("owner", "test-hash")
    store_a, store_b = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "stage9-a"), PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "stage9-b")
    store_a.reset(); store_b.reset()
    owner, owner = auth_a.get_account("owner"), auth_b.get_account("owner")
    assert owner and owner
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/stage9.jpg", "owner"), owner_user_id=owner.user_id, size_bytes=12, sha256="d" * 64)
    store_a.restore_catalogued_asset(asset, "owner")
    origin, recipient = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer = origin.pair_vault(recipient.local_vault_id(), "B", "https://vault-b.test", "q" * 32)
    recipient.pair_vault(origin.local_vault_id(), "A", "https://vault-a.test", "q" * 32)
    share_id = origin.create_outgoing_shares(owner.user_id, [asset.id], peer.remote_vault_id, "quick")[0]
    origin.set_download_allowed(share_id, owner.user_id, True)
    event = {"protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()), "origin_vault_id": str(origin.local_vault_id()), "target_vault_id": str(recipient.local_vault_id()), "event_type": "share_activated", "timestamp": datetime.now(timezone.utc).isoformat(), "share": {"share_id": str(share_id), "asset_id": str(asset.id), "state": "active", "asset_type": "Gallery", "display_title": "Stage 9", "owner_label": "Owner", "origin_endpoint": "https://vault-a.test", "download_allowed": True}}
    assert recipient.receive_event(event, sign_envelope(event, "q" * 32))
    origin.backfill_active_metadata()
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as connection:
        payload = connection.execute("SELECT payload FROM vault_federation_deliveries WHERE federation_share_id=%s AND event_type='metadata_snapshot' ORDER BY created_at DESC LIMIT 1", (share_id,)).fetchone()["payload"]
    metadata_event = {**event, "event_id": str(uuid4()), "event_type": "metadata_snapshot", "timestamp": datetime.now(timezone.utc).isoformat(), "share": dict(payload)}
    assert recipient.receive_event(metadata_event, sign_envelope(metadata_event, "q" * 32))
    incoming = recipient.list_incoming_admin()[0]
    recipient.set_distribution(incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    key = uuid4(); first = recipient.reserve_download(incoming.incoming_share_id, owner.user_id, key); second = recipient.reserve_download(incoming.incoming_share_id, owner.user_id, key)
    assert first.operation_id == second.operation_id and first.local_asset_id != asset.id
    request_time = datetime.now(timezone.utc).isoformat(); signed = {"share_id": str(share_id), "asset_id": str(asset.id), "requester_vault_id": str(recipient.local_vault_id()), "timestamp": request_time}
    assert origin.authorizes_origin_download(share_id, asset.id, recipient.local_vault_id(), request_time, sign_envelope(signed, "q" * 32))
    origin.set_download_allowed(share_id, owner.user_id, False)
    assert not origin.authorizes_origin_download(share_id, asset.id, recipient.local_vault_id(), request_time, sign_envelope(signed, "q" * 32))
    store_a.reset(); store_b.reset()


def test_federated_metadata_backfill_is_versioned_origin_authoritative_and_user_scoped(
    postgres_conninfo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secondary = os.getenv("PV_TEST_DATABASE_URL_SECONDARY")
    if not secondary:
        pytest.skip("PV_TEST_DATABASE_URL_SECONDARY is not configured")
    monkeypatch.setenv("PV_FEDERATION_ENDPOINT", "https://vault-a.test")
    auth_a, auth_b = PostgresAuthenticationStore(postgres_conninfo), PostgresAuthenticationStore(secondary)
    for auth in (auth_a, auth_b):
        for username in ("owner", "owner", "son"):
            auth.ensure_initial_administrator(username, "test-hash")
    store_a = PostgresVaultMasterStore(postgres_conninfo, sidecar_root=tmp_path / "metadata-a")
    store_b = PostgresVaultMasterStore(secondary, sidecar_root=tmp_path / "metadata-b")
    store_a.reset(); store_b.reset()
    owner, owner, son = auth_a.get_account("owner"), auth_b.get_account("owner"), auth_b.get_account("son")
    assert owner and owner and son
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/stage7.jpg", "owner"), owner_user_id=owner.user_id, captured_on=date(2020, 1, 2), location="London", effective_metadata={"florence_description": "A family picnic", "tags": ["family", "picnic"], "ocr_text": "private invitation", "federation_share_location": True, "federation_share_ocr": True})
    store_a.restore_catalogued_asset(asset, "owner")
    federation_a, federation_b = FederationStore(postgres_conninfo), FederationStore(secondary)
    peer_b = federation_a.pair_vault(federation_b.local_vault_id(), "Vault B", "https://vault-b.test", "m" * 32)
    federation_b.pair_vault(federation_a.local_vault_id(), "Vault A", "https://vault-a.test", "m" * 32)
    # Create an active Stage 6-style share, then run the restart-safe Stage 7 backfill.
    share_id = federation_a.create_outgoing_shares(owner.user_id, [asset.id], peer_b.remote_vault_id, "quick")[0]
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("DELETE FROM vault_federation_deliveries WHERE federation_share_id=%s AND event_type='metadata_snapshot'", (share_id,))
        connection.execute("DELETE FROM vault_federation_origin_metadata WHERE origin_vault_id=%s AND origin_asset_id=%s", (federation_a.local_vault_id(), asset.id))
    share_event = {"protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()), "origin_vault_id": str(federation_a.local_vault_id()), "target_vault_id": str(federation_b.local_vault_id()), "event_type": "share_activated", "timestamp": datetime.now(timezone.utc).isoformat(), "share": {"share_id": str(share_id), "asset_id": str(asset.id), "state": "active", "asset_type": "Gallery", "display_title": "stage7", "owner_label": "Owner", "origin_endpoint": "https://vault-a.test"}}
    assert federation_b.receive_event(share_event, sign_envelope(share_event, "m" * 32))
    assert federation_a.backfill_active_metadata() == 1
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as connection:
        row = connection.execute("SELECT payload FROM vault_federation_deliveries WHERE federation_share_id=%s AND event_type='metadata_snapshot' ORDER BY created_at DESC LIMIT 1", (share_id,)).fetchone()
    assert row is not None
    metadata_event = {"protocol_version": FEDERATION_PROTOCOL_VERSION, "event_id": str(uuid4()), "origin_vault_id": str(federation_a.local_vault_id()), "target_vault_id": str(federation_b.local_vault_id()), "event_type": "metadata_snapshot", "timestamp": datetime.now(timezone.utc).isoformat(), "share": dict(row["payload"])}
    assert federation_b.receive_event(metadata_event, sign_envelope(metadata_event, "m" * 32))
    incoming = federation_b.list_incoming_admin()[0]
    assert incoming.origin_metadata and incoming.origin_metadata["description"] == "A family picnic"
    assert incoming.origin_metadata["location"] == "London"
    assert incoming.origin_metadata["tags"] == ["family", "picnic"]
    federation_b.set_distribution(incoming.incoming_share_id, owner.user_id, False, [owner.user_id])
    federation_b.set_local_annotation(incoming.origin_vault_id, incoming.origin_asset_id, owner.user_id, note="Owner note", alias="Picnic", tags=["local"])
    assert federation_b.local_annotation(incoming.origin_vault_id, incoming.origin_asset_id, owner.user_id)["note"] == "Owner note"
    assert federation_b.local_annotation(incoming.origin_vault_id, incoming.origin_asset_id, son.user_id) is None
    stale = {**metadata_event, "event_id": str(uuid4()), "share": {**metadata_event["share"], "metadata_revision": 1, "origin_metadata": {**metadata_event["share"]["origin_metadata"], "description": "stale"}}}
    assert federation_b.receive_event(stale, sign_envelope(stale, "m" * 32))
    assert federation_b.list_incoming_for_user(owner.user_id)[0].origin_metadata["description"] == "A family picnic"
    store_a.reset(); store_b.reset()


def test_autopilot_policy_owner_migration_backfills_or_fails_closed_idempotently(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    del postgres_store
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    assert owner is not None
    PostgresAutopilotStore(postgres_conninfo)
    recipient = Account(
        username="recipient",
        display_name="Recipient",
        email=None,
        password_hash="test-hash",
        role="user",
        active=True,
        password_change_required=False,
        created_at=datetime.now(timezone.utc),
        last_sign_in_at=None,
    )
    authentication.create_account(recipient)
    known_policy, unknown_policy = uuid4(), uuid4()
    with psycopg.connect(postgres_conninfo) as connection:
        # Reproduce the deployed pre-VM-030 schema: its generated legacy
        # username constraint was truncated by PostgreSQL, while its UUID key
        # exists only as a partial index after migration.
        connection.execute(
            """
            DO $$
            DECLARE owner_constraint TEXT;
            BEGIN
                SELECT conname INTO owner_constraint
                FROM pg_constraint
                WHERE conrelid = 'vault_autopilot_policies'::regclass
                  AND contype = 'u'
                  AND pg_get_constraintdef(oid) =
                      'UNIQUE (owner_user_id, source, content_type, destination)';
                IF owner_constraint IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE vault_autopilot_policies DROP CONSTRAINT %I',
                        owner_constraint
                    );
                END IF;
            END $$;
            """
        )
        connection.execute(
            """
            ALTER TABLE vault_autopilot_policies
            ADD CONSTRAINT vault_autopilot_policies_requested_by_source_content_type_d_key
            UNIQUE (requested_by, source, content_type, destination)
            """
        )
        connection.execute(
            """
            INSERT INTO vault_autopilot_policies (
                id, owner_user_id, requested_by, source, content_type, destination,
                threshold, max_items, max_failures, max_failure_percent, status, policy_version
            ) VALUES
                (%s, NULL, 'owner', 'arrival_hall', 'personal_photo', 'Gallery', 80, 10, 1, 5, 'enabled', 'legacy'),
                (%s, NULL, 'unknown-legacy-user', 'arrival_hall', 'receipt', 'Documents', 80, 10, 1, 5, 'enabled', 'legacy')
            """,
            (known_policy, unknown_policy),
        )
    first = PostgresAutopilotStore(postgres_conninfo)
    first.initialize()
    second = PostgresAutopilotStore(postgres_conninfo)
    second.initialize()
    policies = {policy.id: policy for policy in second.list_policies()}
    assert policies[known_policy].owner_user_id == owner.user_id
    assert policies[known_policy].status == "enabled"
    assert policies[unknown_policy].owner_user_id is None
    assert policies[unknown_policy].status == "disabled"
    assert first.list_policies(owner.user_id)[0].owner_user_id == owner.user_id
    assert first.upsert_policy(
        recipient.user_id, recipient.username, "personal_photo", "Gallery", 80, 50, 2, 5
    ).owner_user_id == recipient.user_id
    with psycopg.connect(postgres_conninfo) as connection:
        legacy_constraint = connection.execute(
            """
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'vault_autopilot_policies'::regclass
              AND contype = 'u'
              AND pg_get_constraintdef(oid) =
                  'UNIQUE (requested_by, source, content_type, destination)'
            """
        ).fetchone()
    assert legacy_constraint is None


def test_share_grants_are_owner_bound_validated_and_inert_for_legacy_access(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    son = authentication.get_account("son")
    assert owner is not None and son is not None
    duplicate_account = Account(
        username=f"owner-duplicate-name-{uuid4()}",
        display_name=owner.display_name,
        email=None,
        password_hash="test-hash",
        role="user",
        active=True,
        password_change_required=False,
        created_at=datetime.now(timezone.utc),
        last_sign_in_at=None,
    )
    authentication.create_account(duplicate_account)
    duplicate_display_name = authentication.get_account(
        duplicate_account.username
    )
    assert duplicate_display_name is not None
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000101"),
            "/vault/Gallery/share-grant.jpg",
            "owner",
        ),
        "owner",
    )
    assert asset.owner_user_id == owner.user_id
    assert asset.origin_vault_id is not None
    grants = PostgresShareGrantStore(postgres_conninfo)

    local_all = grants.create_grant(
        asset.id, owner.user_id, LOCAL_ALL_TARGET, allow_download=True
    )
    assert local_all.target_local_user_id is None
    assert local_all.target_vault_id is None
    assert local_all.allow_download is True
    assert local_all.state == "pending"
    assert local_all.origin_vault_id == asset.origin_vault_id

    local_user = grants.create_grant(
        asset.id,
        owner.user_id,
        LOCAL_USER_TARGET,
        target_local_user_id=son.user_id,
        state=ACTIVE_GRANT_STATE,
    )
    assert local_user.state == "active"
    assert local_user.activated_at is not None
    assert local_user.target_local_user_id == son.user_id

    remote_vault_id = uuid4()
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "INSERT INTO vaults (vault_id, is_local) VALUES (%s, FALSE)",
            (remote_vault_id,),
        )
    remote = grants.create_grant(
        asset.id,
        owner.user_id,
        REMOTE_VAULT_TARGET,
        target_vault_id=remote_vault_id,
    )
    assert remote.target_vault_id == remote_vault_id
    assert remote.target_local_user_id is None

    with pytest.raises(ValueError, match="immutable owner"):
        grants.create_grant(asset.id, son.user_id, LOCAL_ALL_TARGET)
    with pytest.raises(ValueError, match="immutable owner"):
        grants.create_grant(
            asset.id, duplicate_display_name.user_id, LOCAL_ALL_TARGET
        )
    with pytest.raises(ValueError, match="exactly one local user"):
        grants.create_grant(asset.id, owner.user_id, LOCAL_USER_TARGET)
    with pytest.raises(ValueError, match="known remote Vault"):
        grants.create_grant(
            asset.id,
            owner.user_id,
            REMOTE_VAULT_TARGET,
            target_vault_id=postgres_store.get_local_vault_id(),
        )
    with pytest.raises(ValueError, match="local_all"):
        grants.create_grant(
            asset.id,
            owner.user_id,
            LOCAL_ALL_TARGET,
            target_local_user_id=son.user_id,
        )
    with pytest.raises(ValueError, match="already exists"):
        grants.create_grant(asset.id, owner.user_id, LOCAL_ALL_TARGET)

    with psycopg.connect(postgres_conninfo) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO vault_share_grants (
                    grant_id, asset_id, grantor_user_id, origin_vault_id,
                    target_type, state
                )
                VALUES (%s, %s, %s, %s, 'local_all', 'unknown')
                """,
                (uuid4(), asset.id, owner.user_id, asset.origin_vault_id),
            )

    revoked = grants.revoke_grant(local_all.grant_id, owner.user_id)
    assert revoked.state == "revoked"
    assert revoked.revoked_at is not None
    regranted = grants.create_grant(asset.id, owner.user_id, LOCAL_ALL_TARGET)
    assert regranted.grant_id != local_all.grant_id
    with pytest.raises(ValueError, match="grantor"):
        grants.activate_grant(regranted.grant_id, son.user_id)
    activated = grants.activate_grant(regranted.grant_id, owner.user_id)
    assert activated.state == "active"
    assert [grant.grant_id for grant in grants.list_outgoing_grants(owner.user_id)] == [
        local_all.grant_id,
        local_user.grant_id,
        remote.grant_id,
        regranted.grant_id,
    ]

    unchanged = postgres_store.update_catalogued_asset_access(
        asset.id, "shared", ("son",), "owner"
    )
    assert unchanged is not None
    assert unchanged.visibility == "shared"
    assert unchanged.shared_with == ("son",)
    assert (
        postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son")
        == unchanged
    )


def test_active_local_grants_are_uuid_authoritative_and_fail_closed(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    son = authentication.get_account("son")
    assert owner is not None and son is not None
    other = Account(
        username=f"same-name-{uuid4()}",
        display_name=son.display_name,
        email=None,
        password_hash="test-hash",
        role="user",
        active=True,
        password_change_required=False,
        created_at=datetime.now(timezone.utc),
        last_sign_in_at=None,
    )
    authentication.create_account(other)
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000102"),
            "/vault/Gallery/grant-authority.jpg",
            "owner",
        ),
        "owner",
    )
    grants = PostgresShareGrantStore(postgres_conninfo)
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "owner")
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is None

    pending = grants.create_grant(
        asset.id,
        owner.user_id,
        LOCAL_USER_TARGET,
        target_local_user_id=son.user_id,
        state=PENDING_GRANT_STATE,
    )
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is None
    active = grants.activate_grant(pending.grant_id, owner.user_id)
    assert active.state == ACTIVE_GRANT_STATE
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son")
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, other.username) is None
    grants.revoke_grant(active.grant_id, owner.user_id)
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is None
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "owner")

    local_all = grants.create_grant(
        asset.id, owner.user_id, LOCAL_ALL_TARGET, state=ACTIVE_GRANT_STATE
    )
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son")
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, other.username)
    grants.revoke_grant(local_all.grant_id, owner.user_id)
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, "son") is None

    with pytest.raises(ValueError, match="owner cannot receive"):
        grants.create_grant(
            asset.id,
            owner.user_id,
            LOCAL_USER_TARGET,
            target_local_user_id=owner.user_id,
        )


def test_local_sharing_modes_replace_active_grants_without_affecting_owner(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    son = authentication.get_account("son")
    assert owner is not None and son is not None
    other = Account(
        username=f"sharing-recipient-{uuid4()}",
        display_name="Owner",
        email=None,
        password_hash="test-hash",
        role="user",
        active=True,
        password_change_required=False,
        created_at=datetime.now(timezone.utc),
        last_sign_in_at=None,
    )
    authentication.create_account(other)
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000104"),
            "/vault/Gallery/local-sharing-modes.jpg",
            "owner",
        ),
        "owner",
    )

    specific = postgres_store.update_catalogued_asset_access(
        asset.id, "shared", (son.username,), "owner"
    )
    assert specific is not None
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, son.username)
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, other.username) is None

    everyone = postgres_store.update_catalogued_asset_access(
        asset.id, "shared", (son.username, other.username), "owner", local_all=True
    )
    assert everyone is not None
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, son.username)
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, other.username)
    private = postgres_store.update_catalogued_asset_access(asset.id, "private", (), "owner")
    assert private is not None
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, owner.username)
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, son.username) is None
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, other.username) is None


def test_bulk_local_sharing_commits_all_or_rolls_back_every_related_write(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.get_account("son")
    assert owner is not None and recipient is not None
    assets = [
        postgres_store.restore_catalogued_asset(
            _catalogued_asset(UUID(f"10000000-0000-0000-0000-000000000{number}"), f"/vault/Gallery/bulk-{number}.jpg", "owner"),
            "owner",
        )
        for number in (401, 402)
    ]

    committed = postgres_store.update_catalogued_assets_access(
        [asset.id for asset in assets], "shared", (recipient.username,), "owner"
    )
    assert [asset.id for asset in committed] == [asset.id for asset in assets]
    with psycopg.connect(postgres_conninfo) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM vault_assets WHERE id = ANY(%s) AND visibility = 'shared'), "
            "(SELECT COUNT(*) FROM vault_share_grants WHERE asset_id = ANY(%s) AND state IN ('pending', 'active')), "
            "(SELECT COUNT(*) FROM vault_share_operations WHERE operation_id IN (SELECT operation_id FROM vault_share_grants WHERE asset_id = ANY(%s)) AND state IN ('pending', 'active')), "
            "(SELECT COUNT(*) FROM vault_asset_history WHERE asset_id = ANY(%s) AND action = 'access_policy_updated')",
            ([asset.id for asset in assets],) * 4,
        ).fetchone()
    assert counts == (2, 2, 2, 2)

    postgres_store.update_catalogued_assets_access(
        [asset.id for asset in assets], "private", (), "owner"
    )
    import app.vault_master as vault_master_module
    original_sync = vault_master_module.sync_stage2c_local_share_grants
    calls = 0

    def fail_on_second_sync(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated grant write failure")
        return original_sync(*args, **kwargs)

    monkeypatch.setattr(vault_master_module, "sync_stage2c_local_share_grants", fail_on_second_sync)
    with pytest.raises(RuntimeError, match="simulated grant write failure"):
        postgres_store.update_catalogued_assets_access(
            [asset.id for asset in assets], "shared", (recipient.username,), "owner"
        )
    with psycopg.connect(postgres_conninfo) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM vault_assets WHERE id = ANY(%s) AND visibility = 'shared'), "
            "(SELECT COUNT(*) FROM vault_share_grants WHERE asset_id = ANY(%s) AND state IN ('pending', 'active')), "
            "(SELECT COUNT(*) FROM vault_share_operations WHERE operation_id IN (SELECT operation_id FROM vault_share_grants WHERE asset_id = ANY(%s)) AND state IN ('pending', 'active')), "
            "(SELECT COUNT(*) FROM vault_asset_history WHERE asset_id = ANY(%s) AND action = 'access_policy_updated')",
            ([asset.id for asset in assets],) * 4,
        ).fetchone()
    assert counts == (0, 0, 0, 4)


def test_standard_share_operations_are_inert_until_due_and_owner_controlled(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.get_account("son")
    assert owner is not None and recipient is not None
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000114"),
            "/vault/Gallery/standard-share.jpg",
            "owner",
        ),
        "owner",
    )

    pending = postgres_store.update_catalogued_asset_access(
        asset.id, "shared", (recipient.username,), "owner", share_mode="standard"
    )
    assert pending is not None
    grants = PostgresShareGrantStore(postgres_conninfo)
    operation, operation_grants = grants.list_outgoing_operations(owner.user_id)[0]
    assert operation.share_mode == "standard"
    assert operation.state == PENDING_GRANT_STATE
    assert len(operation_grants) == 1
    assert operation_grants[0].asset_id == asset.id
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, recipient.username) is None

    released = grants.transition_operation(operation.operation_id, owner.user_id, "activate")
    assert released.state == ACTIVE_GRANT_STATE
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, recipient.username)
    with pytest.raises(ValueError, match="once"):
        grants.transition_operation(operation.operation_id, owner.user_id, "activate")
    revoked = grants.transition_operation(operation.operation_id, owner.user_id, "revoke")
    assert revoked.state == "revoked"
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, recipient.username) is None

    due = postgres_store.update_catalogued_asset_access(
        asset.id, "shared", (recipient.username,), "owner", share_mode="standard"
    )
    assert due is not None
    due_operation = grants.list_outgoing_operations(owner.user_id)[0][0]
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_share_operations SET release_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE operation_id = %s",
            (due_operation.operation_id,),
        )
    assert postgres_store.get_visible_catalogued_asset_by_id(asset.id, recipient.username)
    assert grants.list_outgoing_operations(owner.user_id)[0][0].state == ACTIVE_GRANT_STATE


def test_shared_with_me_listing_is_active_uuid_scoped_and_excludes_owner(
    postgres_store: PostgresVaultMasterStore, postgres_conninfo: str
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.get_account("son")
    assert owner is not None and recipient is not None
    gallery = postgres_store.restore_catalogued_asset(
        _catalogued_asset(UUID("10000000-0000-0000-0000-000000000115"), "/vault/Gallery/commons.jpg", "owner"), "owner"
    )
    theatre = postgres_store.restore_catalogued_asset(
        CataloguedAsset(**{**_catalogued_asset(UUID("10000000-0000-0000-0000-000000000116"), "/vault/Theatre/Movies/commons.mkv", "owner").__dict__, "asset_type": "Movies"}), "owner"
    )
    assert postgres_store.update_catalogued_asset_access(gallery.id, "shared", (recipient.username,), "owner")
    assert postgres_store.update_catalogued_asset_access(theatre.id, "shared", (recipient.username,), "owner")
    grants = PostgresShareGrantStore(postgres_conninfo)
    assert [asset.asset_id for asset in grants.list_assets_shared_with_user(recipient.user_id, ("gallery",))] == [gallery.id]
    assert [asset.asset_id for asset in grants.list_assets_shared_with_user(recipient.user_id, ("movies",))] == [theatre.id]
    assert grants.list_assets_shared_with_user(owner.user_id) == []
    assert postgres_store.update_catalogued_asset_access(gallery.id, "private", (), "owner")
    assert grants.list_assets_shared_with_user(recipient.user_id, ("gallery",)) == []

def test_legacy_share_migration_has_exact_parity_and_fails_on_orphans(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000103"),
            "/vault/Documents/legacy-share.pdf",
            "owner",
        ),
        "owner",
    )
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_assets SET visibility = 'shared', shared_with = %s WHERE id = %s",
            (Jsonb(["son"]), asset.id),
        )
    migrated = PostgresVaultMasterStore(postgres_conninfo)
    migrated.initialize()
    assert migrated.get_visible_catalogued_asset_by_id(asset.id, "son") is not None
    with psycopg.connect(postgres_conninfo) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM vault_share_grants WHERE asset_id = %s "
            "AND target_type = 'local_user' AND state = 'active'",
            (asset.id,),
        ).fetchone()
    assert count == (1,)
    repeated = PostgresVaultMasterStore(postgres_conninfo)
    repeated.initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        repeated_count = connection.execute(
            "SELECT COUNT(*) FROM vault_share_grants WHERE asset_id = %s "
            "AND target_type = 'local_user' AND state = 'active'",
            (asset.id,),
        ).fetchone()
        connection.execute(
            "UPDATE vault_assets SET shared_with = %s WHERE id = %s",
            (Jsonb(["missing-recipient"]), asset.id),
        )
    assert repeated_count == (1,)
    with pytest.raises(RuntimeError, match="could not resolve active recipient"):
        PostgresVaultMasterStore(postgres_conninfo).initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_assets SET visibility = 'private', shared_with = '[]'::jsonb WHERE id = %s",
            (asset.id,),
        )


def test_share_grant_schema_is_initialized_by_controlled_vault_master_bootstrap(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = share_grants.initialize_share_grants

    def record_schema_bootstrap(cursor) -> None:
        nonlocal calls
        calls += 1
        original(cursor)

    monkeypatch.setattr(
        vault_master, "initialize_share_grants", record_schema_bootstrap
    )
    PostgresVaultMasterStore(postgres_conninfo).initialize()

    assert calls == 1
    assert postgres_store.get_local_vault_id() is not None


def test_share_grant_request_construction_never_runs_schema_ddl(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def schema_ddl_must_not_run(_cursor) -> None:
        raise AssertionError("request-time share grant access attempted schema DDL")

    # The fixture has already performed the controlled, additive bootstrap.
    monkeypatch.setattr(
        share_grants, "initialize_share_grants", schema_ddl_must_not_run
    )

    def normal_request_path(index: int) -> list[object]:
        grants = PostgresShareGrantStore(postgres_conninfo)
        if index % 2:
            return grants.list_outgoing_grants(uuid4())
        return grants.list_assets_shared_with_user(uuid4())

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(normal_request_path, range(32)))

    assert results == [[] for _ in range(32)]
    assert (
        PostgresShareGrantStore(postgres_conninfo).list_outgoing_grants(uuid4())
        == []
    )


def test_concurrent_postgres_gallery_and_commons_previews_are_deadlock_free(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the actual request-time PostgreSQL share evaluator concurrently."""
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.get_account("son")
    assert owner is not None and recipient is not None
    gallery_root = tmp_path / "Gallery"
    gallery_root.mkdir()
    image_path = gallery_root / "shared.jpg"
    image_bytes = b"\xff\xd8" + b"authoritative-image" + b"\xff\xd9"
    image_path.write_bytes(image_bytes)
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("10000000-0000-0000-0000-000000000901"),
            "/vault/Gallery/shared.jpg",
            owner.username,
            size_bytes=len(image_bytes),
        ),
        owner.username,
    )
    assert postgres_store.update_catalogued_asset_access(
        asset.id, "shared", (recipient.username,), owner.username
    ) is not None
    grants = PostgresShareGrantStore(postgres_conninfo)
    assert grants.set_gallery_shared_preference(recipient.user_id, True) is True

    request_authentication = MemoryAuthenticationStore()
    request_recipient = Account(
        username=recipient.username,
        display_name=recipient.display_name,
        email=recipient.email,
        password_hash="test-hash",
        role=recipient.role,
        active=True,
        password_change_required=False,
        created_at=recipient.created_at,
        last_sign_in_at=None,
        user_id=recipient.user_id,
    )
    request_authentication.create_account(request_recipient)
    app.dependency_overrides[get_authentication_store] = lambda: request_authentication
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(request_recipient)
    app.dependency_overrides[get_vault_master_store] = lambda: postgres_store
    app.dependency_overrides[get_gallery_path] = lambda: gallery_root
    app.dependency_overrides[get_catalogue_preview_roots] = lambda: {"/vault/Gallery": gallery_root}
    monkeypatch.setattr(gallery_module, "get_database_conninfo", lambda: postgres_conninfo)
    monkeypatch.setattr(vault_master_api, "get_database_conninfo", lambda: postgres_conninfo)
    monkeypatch.setenv("PV_WEBAUTHN_RP_ID", "testserver")
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", "https://testserver")
    monkeypatch.setenv("PV_VAULT_MASTER_WORKER_ENABLED", "false")
    monkeypatch.setattr(main_module, "bootstrap_application_schema", lambda: None)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            gallery_id = scan_gallery(gallery_root)[0].id
            gallery_url = f"/api/gallery/{gallery_id}/preview"
            commons_url = f"/api/vault-master/commons/shared-with-me/{asset.id}/preview"

            def fetch(url: str) -> tuple[int, str, bytes]:
                response = client.get(url)
                return response.status_code, response.headers["content-type"], response.content

            with ThreadPoolExecutor(max_workers=12) as executor:
                responses = list(executor.map(fetch, [gallery_url, commons_url] * 12))

            assert responses == [(200, "image/jpeg", image_bytes)] * 24
            assert postgres_store.update_catalogued_asset_access(
                asset.id, "private", (), owner.username
            ) is not None
            assert client.get(gallery_url).status_code == 404
            assert client.get(commons_url).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_shared_collections_are_live_uuid_scoped_and_preserve_independent_grants(
    postgres_store: PostgresVaultMasterStore, postgres_conninfo: str
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = Account(
        username=f"recipient-collection-{uuid4()}", display_name="Recipient", email=None,
        password_hash="test-hash", role="user", active=True, password_change_required=False,
        created_at=datetime.now(timezone.utc), last_sign_in_at=None,
    )
    authentication.create_account(recipient)
    recipient = authentication.get_account(recipient.username)
    assert owner is not None and recipient is not None
    assets = [
        postgres_store.restore_catalogued_asset(
            _catalogued_asset(UUID(f"10000000-0000-0000-0000-000000000{number}"), f"/vault/Gallery/athens-{number}.jpg", "owner"), "owner"
        )
        for number in (201, 202, 203)
    ]
    grants = PostgresShareGrantStore(postgres_conninfo)
    collection = grants.create_collection(owner.user_id, "Athens Trip", [asset.id for asset in assets[:2]])
    assert collection.owner_user_id == owner.user_id
    assert collection.member_count == 2
    renamed = grants.update_collection(collection.collection_id, owner.user_id, "Athens Trip", "Historical Gallery assets")
    assert renamed.description == "Historical Gallery assets"
    operation = grants.share_collection(collection.collection_id, owner.user_id, LOCAL_USER_TARGET,
                                        target_local_user_ids=[recipient.user_id], share_mode="standard")
    assert operation.state == PENDING_GRANT_STATE
    assert postgres_store.get_visible_catalogued_asset_by_id(assets[0].id, recipient.username) is None
    grants.transition_operation(operation.operation_id, owner.user_id, "activate")
    assert postgres_store.get_visible_catalogued_asset_by_id(assets[0].id, recipient.username)
    outgoing_operation, outgoing_collection, outgoing_grants = grants.list_outgoing_collection_operations(owner.user_id)[0]
    assert outgoing_operation.operation_id == operation.operation_id
    assert outgoing_collection.collection_id == collection.collection_id
    assert [grant.target_local_user_id for grant in outgoing_grants] == [recipient.user_id]
    with pytest.raises(ValueError, match="Confirm"):
        grants.add_collection_members(collection.collection_id, owner.user_id, [assets[2].id])
    grants.add_collection_members(collection.collection_id, owner.user_id, [assets[2].id], confirm_live_share=True)
    assert postgres_store.get_visible_catalogued_asset_by_id(assets[2].id, recipient.username)
    assert postgres_store.update_catalogued_asset_access(assets[0].id, "shared", (recipient.username,), "owner")
    grants.remove_collection_member(collection.collection_id, owner.user_id, assets[0].id)
    assert postgres_store.get_visible_catalogued_asset_by_id(assets[0].id, recipient.username)
    grants.remove_collection_member(collection.collection_id, owner.user_id, assets[2].id)
    assert postgres_store.get_visible_catalogued_asset_by_id(assets[2].id, recipient.username) is None
    with pytest.raises(ValueError, match="owner"):
        grants.add_collection_members(collection.collection_id, recipient.user_id, [assets[2].id], confirm_live_share=True)
    assert [entry.collection_id for entry in grants.list_shared_collections_for_user(recipient.user_id)] == [collection.collection_id]
    assert grants.list_collection_members(collection.collection_id, recipient.user_id) == [assets[1].id]
    grants.transition_operation(operation.operation_id, owner.user_id, "revoke")
    assert grants.list_shared_collections_for_user(recipient.user_id) == []
    grants.archive_collection(collection.collection_id, owner.user_id)
    with pytest.raises(ValueError, match="owner"):
        grants.add_collection_members(collection.collection_id, owner.user_id, [assets[2].id])


def test_gallery_shared_inclusion_is_uuid_scoped_live_and_fail_closed(
    postgres_store: PostgresVaultMasterStore, postgres_conninfo: str
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = Account(
        username=f"recipient-gallery-{uuid4()}", display_name="Recipient", email=None,
        password_hash="test-hash", role="user", active=True, password_change_required=False,
        created_at=datetime.now(timezone.utc), last_sign_in_at=None,
    )
    authentication.create_account(recipient)
    recipient = authentication.get_account(recipient.username)
    assert owner is not None and recipient is not None
    assets = [
        postgres_store.restore_catalogued_asset(
            _catalogued_asset(
                UUID(f"10000000-0000-0000-0000-000000000{number}"),
                f"/vault/Gallery/timeline-{number}.jpg", "owner",
            ),
            "owner",
        )
        for number in (251, 252, 253)
    ]
    grants = PostgresShareGrantStore(postgres_conninfo)

    pending = grants.create_grant(
        assets[0].id, owner.user_id, LOCAL_USER_TARGET,
        target_local_user_id=recipient.user_id,
    )
    assert grants.set_gallery_shared_preference(recipient.user_id, True) is True
    assert grants.included_gallery_assets(recipient.user_id) == {}
    grants.revoke_grant(pending.grant_id, owner.user_id)
    active = grants.create_grant(
        assets[0].id, owner.user_id, LOCAL_USER_TARGET,
        target_local_user_id=recipient.user_id, state=ACTIVE_GRANT_STATE,
    )
    assert grants.included_gallery_assets(recipient.user_id) == {assets[0].id: owner.display_name}

    collection = grants.create_collection(owner.user_id, "Recipient's Gallery", [assets[0].id, assets[1].id])
    pending_collection = grants.share_collection(
        collection.collection_id, owner.user_id, LOCAL_USER_TARGET,
        target_local_user_ids=[recipient.user_id], share_mode="standard",
    )
    assert assets[1].id not in grants.included_gallery_assets(recipient.user_id)
    grants.transition_operation(pending_collection.operation_id, owner.user_id, "activate")
    assert grants.set_gallery_collection_preference(recipient.user_id, collection.collection_id, True) is True
    included = grants.included_gallery_assets(recipient.user_id)
    assert included == {assets[0].id: owner.display_name, assets[1].id: owner.display_name}
    assert [asset.asset_id for asset in grants.list_assets_shared_with_user(recipient.user_id, ("gallery",))] == [
        assets[0].id,
        assets[1].id,
    ]
    assert [entry.collection_id for entry in grants.list_gallery_shared_collections(recipient.user_id)] == [collection.collection_id]

    grants.add_collection_members(collection.collection_id, owner.user_id, [assets[2].id], confirm_live_share=True)
    assert assets[2].id in grants.included_gallery_assets(recipient.user_id)
    grants.transition_operation(pending_collection.operation_id, owner.user_id, "revoke")
    assert grants.included_gallery_assets(recipient.user_id) == {assets[0].id: owner.display_name}
    assert [asset.asset_id for asset in grants.list_assets_shared_with_user(recipient.user_id, ("gallery",))] == [assets[0].id]
    grants.revoke_grant(active.grant_id, owner.user_id)
    assert grants.included_gallery_assets(recipient.user_id) == {}


def test_local_shared_gallery_metadata_preserves_origin_and_recipient_layers(
    postgres_store: PostgresVaultMasterStore, postgres_conninfo: str
) -> None:
    """Local annotations are UUID-scoped and never become canonical metadata."""
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.get_account("son")
    assert owner is not None and recipient is not None
    asset = postgres_store.restore_catalogued_asset(
        replace(
            _catalogued_asset(
                UUID("10000000-0000-0000-0000-000000000254"),
                "/vault/Gallery/shared-effective.jpg",
                owner.username,
            ),
            display_title="Owner corrected title",
            captured_on=date(2024, 5, 6),
            location="Owner corrected place",
            metadata={"captured_at": "2020-01-02T03:04:05+00:00"},
            detected_metadata={"captured_at": "2020-01-02T03:04:05+00:00"},
            imported_metadata={"display_title": "Imported title", "location": "Imported place"},
            user_overrides={"display_title": "Owner corrected title", "captured_on": "2024-05-06", "location": "Owner corrected place"},
            effective_metadata={
                "display_title": "Owner corrected title",
                "captured_on": "2024-05-06",
                "location": "Owner corrected place",
                "description": "Owner caption",
                "tags": ["owner-tag"],
            },
            metadata_provenance={
                "display_title": "user_override",
                "captured_on": "user_override",
                "location": "user_override",
            },
        ),
        owner.username,
    )
    people = PostgresGalleryPeopleStore(postgres_conninfo)
    origin_person = people.create_person(owner.username, "Owner", owner.user_id)
    local_person = people.create_person(recipient.username, "Owner", recipient.user_id)
    people.associate(asset.id, origin_person.id, "user")
    assert postgres_store.update_catalogued_asset_access(
        asset.id, "shared", (recipient.username,), owner.username
    ) is not None
    grants = PostgresShareGrantStore(postgres_conninfo)
    assert grants.set_gallery_shared_preference(recipient.user_id, True) is True
    assert grants.included_gallery_assets(recipient.user_id) == {asset.id: owner.display_name}

    updated = postgres_store.update_catalogued_asset_metadata(
        asset.id,
        {"captured_at": "2024-05-06T03:04:05+00:00"},
        owner.username,
    )
    assert updated is not None
    assert updated.captured_on == date(2024, 5, 6)
    assert updated.effective_metadata["captured_at"] == "2024-05-06T03:04:05+00:00"

    # Origin data remains the owner's effective canonical record. Same-name
    # People are different immutable UUIDs and are never auto-merged.
    restored = postgres_store.get_catalogued_asset_by_id(asset.id)
    assert restored is not None
    assert restored.display_title == "Owner corrected title"
    assert restored.captured_on == date(2024, 5, 6)
    assert restored.effective_metadata["captured_at"] == "2024-05-06T03:04:05+00:00"
    assert restored.location == "Owner corrected place"
    assert restored.effective_metadata["description"] == "Owner caption"
    assert [value.person_id for value in people.effective_people(asset.id, owner.user_id)] == [origin_person.id]
    assert origin_person.id != local_person.id
    assert grants.get_local_gallery_annotation(asset.id, recipient.user_id) == {
        "note": None, "tags": [], "people": []
    }

    local = grants.set_local_gallery_annotation(
        asset.id,
        recipient.user_id,
        note="My private note",
        tags=["recipient-tag"],
        person_ids=[local_person.id],
    )
    assert local == {
        "note": "My private note",
        "tags": ["recipient-tag"],
        "people": [{"id": str(local_person.id), "display_name": "Owner"}],
    }
    assert grants.included_gallery_assets_for_local_people(
        recipient.user_id, (local_person.id,)
    ) == {asset.id}
    # Same display names and owner-origin assignments are not an identity link.
    assert grants.included_gallery_assets_for_local_people(
        recipient.user_id, (origin_person.id,)
    ) == set()
    assert [value.person_id for value in people.effective_people(asset.id, owner.user_id)] == [origin_person.id]
    assert people.effective_people(asset.id, recipient.user_id) == []
    with pytest.raises(ValueError, match="Local Person"):
        grants.set_local_gallery_annotation(
            asset.id, recipient.user_id, note=None, tags=[], person_ids=[origin_person.id]
        )

    grant = next(
        grant for grant in grants.list_outgoing_grants(owner.user_id)
        if grant.asset_id == asset.id and grant.state == ACTIVE_GRANT_STATE
    )
    grants.revoke_grant(grant.grant_id, owner.user_id)
    assert grants.included_gallery_assets(recipient.user_id) == {}
    assert grants.included_gallery_assets_for_local_people(
        recipient.user_id, (local_person.id,)
    ) == set()
    assert grants.get_local_gallery_annotation(asset.id, recipient.user_id) is None
    with pytest.raises(ValueError, match="unavailable"):
        grants.set_local_gallery_annotation(
            asset.id, recipient.user_id, note="must not restore access", tags=[], person_ids=[]
        )

    # Repeated additive bootstrap preserves existing local rows rather than
    # copying or rewriting canonical owner metadata.
    PostgresVaultMasterStore(postgres_conninfo).initialize()
    PostgresVaultMasterStore(postgres_conninfo).initialize()
    assert postgres_store.get_catalogued_asset_by_id(asset.id).display_title == "Owner corrected title"  # type: ignore[union-attr]


def test_multiple_local_vault_identities_fail_closed(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    local_vault_id = postgres_store.get_local_vault_id()
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("DROP INDEX vaults_one_local_idx")
        connection.execute(
            "INSERT INTO vaults (vault_id, is_local) VALUES (%s, TRUE)",
            (uuid4(),),
        )

    with pytest.raises(RuntimeError, match="multiple local Vaults"):
        PostgresVaultMasterStore(postgres_conninfo).initialize()

    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "DELETE FROM vaults WHERE is_local = TRUE AND vault_id <> %s",
            (local_vault_id,),
        )
    restored = PostgresVaultMasterStore(postgres_conninfo)
    restored.initialize()
    assert restored.get_local_vault_id() == local_vault_id


def test_legacy_asset_origin_backfill_is_idempotent_and_preserves_ownership(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    local_vault_id = postgres_store.get_local_vault_id()
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    assert owner is not None
    asset = _catalogued_asset(
        UUID("10000000-0000-0000-0000-000000000005"),
        "/vault/Gallery/legacy-origin.jpg",
        "owner",
    )
    restored = postgres_store.restore_catalogued_asset(asset, "owner")
    assert restored.owner_user_id == owner.user_id
    assert restored.origin_vault_id == local_vault_id

    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "ALTER TABLE vault_assets DROP CONSTRAINT "
            "vault_assets_origin_vault_id_fkey"
        )
        connection.execute("ALTER TABLE vault_assets DROP COLUMN origin_vault_id")

    migrated = PostgresVaultMasterStore(postgres_conninfo)
    migrated.initialize()
    migrated_asset = migrated.get_catalogued_asset(asset.vault_path)
    assert migrated_asset is not None
    assert migrated_asset.id == asset.id
    assert migrated_asset.owner_user_id == owner.user_id
    assert migrated_asset.origin_vault_id == local_vault_id
    remote_vault_id = uuid4()
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "INSERT INTO vaults (vault_id, is_local) VALUES (%s, FALSE)",
            (remote_vault_id,),
        )
        connection.execute(
            "UPDATE vault_assets SET origin_vault_id = %s WHERE id = %s",
            (remote_vault_id, asset.id),
        )
    repeated_store = PostgresVaultMasterStore(postgres_conninfo)
    repeated_store.initialize()
    repeated_asset = repeated_store.get_catalogued_asset(asset.vault_path)
    assert repeated_asset is not None
    assert repeated_asset.origin_vault_id == remote_vault_id


def test_legacy_owner_user_id_migration_is_idempotent_and_propagates_arrival_ownership(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    assert owner is not None
    asset = _catalogued_asset(
        UUID("10000000-0000-0000-0000-000000000001"),
        "/vault/Gallery/legacy-owner.jpg",
        "owner",
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    batch_id = postgres_store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    item = postgres_store.record_file(
        batch_id,
        INCOMING_SOURCE,
        ScannedFile(
            source_path="/vault/Arrival Hall/legacy-owner.jpg",
            relative_path="legacy-owner.jpg",
            filename="legacy-owner.jpg",
            size_bytes=8,
            mime_type="image/jpeg",
            modified_at=datetime.now(timezone.utc),
            sha256="a" * 64,
            metadata={},
            owner_username="owner",
        ),
    )
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "ALTER TABLE vault_master_items DROP CONSTRAINT "
            "vault_master_items_owner_user_id_fkey"
        )
        connection.execute(
            "ALTER TABLE vault_master_items DROP COLUMN owner_user_id"
        )
        connection.execute(
            "ALTER TABLE vault_assets DROP CONSTRAINT "
            "vault_assets_owner_user_id_fkey"
        )
        connection.execute("ALTER TABLE vault_assets DROP COLUMN owner_user_id")

    migrated = PostgresVaultMasterStore(postgres_conninfo)
    migrated.initialize()
    migrated_asset = migrated.get_catalogued_asset(asset.vault_path)
    migrated_item = migrated.get_item(item.id)
    assert migrated_asset is not None and migrated_asset.owner_user_id == owner.user_id
    assert migrated_item is not None and migrated_item.owner_user_id == owner.user_id

    repeated = PostgresVaultMasterStore(postgres_conninfo)
    repeated.initialize()
    assert repeated.get_catalogued_asset(asset.vault_path).owner_user_id == owner.user_id  # type: ignore[union-attr]
    assert repeated.get_item(item.id).owner_user_id == owner.user_id  # type: ignore[union-attr]


def test_orphaned_legacy_asset_owner_fails_closed(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = _catalogued_asset(
        UUID("10000000-0000-0000-0000-000000000002"),
        "/vault/Gallery/orphan-owner.jpg",
        "owner",
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "ALTER TABLE vault_assets DROP CONSTRAINT "
            "vault_assets_owner_user_id_fkey"
        )
        connection.execute("ALTER TABLE vault_assets DROP COLUMN owner_user_id")
        connection.execute(
            "UPDATE vault_assets SET owner_username = 'orphaned-owner' "
            "WHERE id = %s",
            (asset.id,),
        )

    with pytest.raises(RuntimeError, match="unknown account"):
        PostgresVaultMasterStore(postgres_conninfo).initialize()

    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_assets SET owner_username = 'owner' WHERE id = %s",
            (asset.id,),
        )
    restored = PostgresVaultMasterStore(postgres_conninfo)
    restored.initialize()
    assert restored.get_catalogued_asset(asset.vault_path) is not None


def test_duplicate_display_names_keep_asset_owners_and_storage_attribution_distinct(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    created_at = datetime.now(timezone.utc)
    suffix = uuid4().hex
    first = Account(
        f"alex-one-{suffix}",
        "Alex",
        f"alex-one-{suffix}@example.test",
        "hash",
        "user",
        True,
        False,
        created_at,
        None,
    )
    second = Account(
        f"alex-two-{suffix}",
        "Alex",
        f"alex-two-{suffix}@example.test",
        "hash",
        "user",
        True,
        False,
        created_at,
        None,
    )
    authentication.create_account(first)
    authentication.create_account(second)
    first_asset = _catalogued_asset(
        UUID("10000000-0000-0000-0000-000000000003"),
        "/vault/Gallery/alex-one.jpg",
        first.username,
        size_bytes=11,
    )
    second_asset = _catalogued_asset(
        UUID("10000000-0000-0000-0000-000000000004"),
        "/vault/Gallery/alex-two.jpg",
        second.username,
        size_bytes=17,
    )
    postgres_store.restore_catalogued_asset(first_asset, first.username)
    postgres_store.restore_catalogued_asset(second_asset, second.username)

    persisted_first = postgres_store.get_catalogued_asset(first_asset.vault_path)
    persisted_second = postgres_store.get_catalogued_asset(second_asset.vault_path)
    assert (
        persisted_first is not None
        and persisted_first.owner_user_id == first.user_id
    )
    assert (
        persisted_second is not None
        and persisted_second.owner_user_id == second.user_id
    )
    assert persisted_first.owner_user_id != persisted_second.owner_user_id
    assert persisted_first.origin_vault_id == postgres_store.get_local_vault_id()
    assert persisted_second.origin_vault_id == postgres_store.get_local_vault_id()
    assert [
        asset.id
        for asset in postgres_store.list_owned_catalogued_assets(first.username)
    ] == [first_asset.id]
    assert [
        asset.id
        for asset in postgres_store.list_owned_catalogued_assets(second.username)
    ] == [second_asset.id]

    monkeypatch.setattr(vault_control_users, "get_database_conninfo", lambda: postgres_conninfo)
    assert vault_control_users._usage(first.user_id) == 11
    assert vault_control_users._usage(second.user_id) == 17


def test_queue_state_survives_store_recreation(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    (incoming / "persistent.txt").write_text(
        "persistent",
        encoding="utf-8",
    )
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item_id = postgres_store.list_items()[0].id

    recreated_store = PostgresVaultMasterStore(postgres_conninfo)
    items = recreated_store.list_items()

    assert len(items) == 1
    assert items[0].id == item_id
    assert items[0].sha256


def test_forced_ai_proposal_does_not_override_postgres_user_destination(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "Screenshot 2026-08-02.png").write_bytes(b"screenshot")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]
    manually_changed = postgres_store.update_proposal(
        item.id,
        "Gallery",
        "owner",
    )
    assert manually_changed is not None
    assert manually_changed.proposed_category == "Gallery"

    forced = postgres_store.apply_ai_proposal(
        item.id,
        "Archives",
        "Hard-coded screenshot routing requires Archives/Screenshots.",
        "Screenshots",
        force=True,
    )
    assert forced is None
    current = postgres_store.get_item(item.id)
    assert current is not None
    assert current.proposed_category == "Gallery"


def test_intake_source_gate_and_receipt_survive_store_recreation(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    del postgres_store
    first = PostgresIntakeStore(postgres_conninfo)
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("DELETE FROM vault_intake_receipts")
        connection.execute("DELETE FROM vault_intake_sources")
        connection.execute("UPDATE vault_intake_control SET enabled=FALSE WHERE singleton")

    source, token = first.create_source("owner", "Restart persistence source")
    first.set_source_status(source.id, "owner", "enabled")
    first.set_global_enabled(True)
    receipt, replayed = first.reserve(
        source.id,
        token,
        "postgres-restart-0001",
        "photo.jpg",
        5,
        "0" * 64,
    )
    assert replayed is False
    first.complete(receipt.id, "photo.jpg", 5, "0" * 64)

    restarted = PostgresIntakeStore(postgres_conninfo)
    assert restarted.global_enabled() is True
    assert restarted.list_sources("owner")[0].status == "enabled"
    restored_receipt, replayed = restarted.reserve(
        source.id,
        token,
        "postgres-restart-0001",
        "photo.jpg",
        5,
        "0" * 64,
    )
    assert replayed is True
    assert restored_receipt.status == "completed"

    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("DELETE FROM vault_intake_receipts")
        connection.execute("DELETE FROM vault_intake_sources")
        connection.execute("UPDATE vault_intake_control SET enabled=FALSE WHERE singleton")


def test_ingestion_ai_queue_and_evidence_survive_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "statement.jpg").write_bytes(b"image")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]

    first = PostgresIngestionAiStore(postgres_conninfo)
    queued = first.queue_analysis(item.id, "owner")
    claimed = first.claim_next_job()
    assert claimed is not None and claimed.id == queued.id

    restarted = PostgresIngestionAiStore(postgres_conninfo)
    restarted.initialize()
    recovered = restarted.claim_next_job()
    assert recovered is not None and recovered.id == queued.id
    restarted.complete_job(
        recovered.id,
        "financial_document",
        "A statement",
        "BANK STATEMENT",
        0.94,
        ("Financial statement indicator",),
        1000,
        assess_destination(
            item,
            "financial_document",
            0.94,
            "BANK STATEMENT",
        ),
    )

    final = PostgresIngestionAiStore(postgres_conninfo)
    evidence = final.list_evidence(item.id, "owner")
    assert evidence[0].ocr_text == "BANK STATEMENT"
    assert evidence[0].recommended_destination == "Ledger"
    assert evidence[0].decision_model_version == "intelligent-routing-v5"
    assert final.list_evidence(item.id, "someone-else") == []


def test_routing_memory_survives_restart_and_never_crosses_users(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "statement.jpg").write_bytes(b"image")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]
    first = PostgresIngestionAiStore(postgres_conninfo)
    for _ in range(3):
        queued = first.queue_analysis(item.id, "owner")
        claimed = first.claim_next_job()
        assert claimed is not None and claimed.id == queued.id
        assessment = assess_destination(item, "financial_document", 0.95, "BANK STATEMENT")
        first.complete_job(
            claimed.id, "financial_document", "A statement", "BANK STATEMENT",
            0.95, ("Financial statement indicator",), 20, assessment,
        )
        rule = first.remember_decision(item, "Ledger", "approved", "owner")
    assert rule is not None and rule.maturity == "suggestion"

    restarted = PostgresIngestionAiStore(postgres_conninfo)
    assert restarted.list_routing_rules(item.owner_user_id)[0].example_count == 3
    assert restarted.list_routing_rules(UUID(int=999)) == []
    learned = restarted.apply_routing_memory(
        item, "financial_document", "BANK STATEMENT", assessment
    )
    assert learned == assessment
    assert restarted.update_routing_rule(rule.id, UUID(int=999), "disabled") is None


def test_routing_memory_legacy_cross_owner_evidence_is_quarantined_idempotently(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "recipient-photo.jpg").write_bytes(b"image")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.get_account("son")
    assert owner is not None and recipient is not None
    PostgresIngestionAiStore(postgres_conninfo)
    rule_id, example_id = uuid4(), uuid4()
    features = {"content_type": "personal_photo", "extension": "jpg", "folder_pattern": "root", "ocr_concept": "none"}
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_master_items SET owner_user_id=%s, owner_username='son' WHERE id=%s",
            (recipient.user_id, item.id),
        )
        connection.execute(
            "INSERT INTO vault_routing_memory_rules (id, requested_by, feature_signature, features, destination, maturity) VALUES (%s, 'owner', 'legacy-cross-owner', %s, 'Gallery', 'suggestion')",
            (rule_id, Jsonb(features)),
        )
        connection.execute(
            "INSERT INTO vault_routing_memory_examples (id, rule_id, item_id, requested_by, action, chosen_destination, features) VALUES (%s, %s, %s, 'owner', 'approved', 'Gallery', %s)",
            (example_id, rule_id, item.id, Jsonb(features)),
        )

    migrated = PostgresIngestionAiStore(postgres_conninfo)
    migrated.initialize()
    repeated = PostgresIngestionAiStore(postgres_conninfo)
    repeated.initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        rule = connection.execute(
            "SELECT owner_user_id, example_count, status FROM vault_routing_memory_rules WHERE id=%s",
            (rule_id,),
        ).fetchone()
        example = connection.execute(
            "SELECT owner_user_id, active FROM vault_routing_memory_examples WHERE id=%s",
            (example_id,),
        ).fetchone()
    assert rule == (owner.user_id, 0, "disabled")
    assert example == (None, False)


def test_ingestion_analysis_batch_progress_and_controls_survive_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "one.jpg").write_bytes(b"one")
    (incoming / "two.jpg").write_bytes(b"two")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    items = postgres_store.list_items()

    first = PostgresIngestionAiStore(postgres_conninfo)
    batch = first.create_analysis_batch(
        tuple(item.id for item in items), "owner"
    )
    assert batch.total_items == 2
    paused = first.set_analysis_batch_status(batch.id, "owner", "paused")
    assert paused is not None and paused.status == "paused"
    assert first.claim_next_job() is None

    restarted = PostgresIngestionAiStore(postgres_conninfo)
    assert restarted.list_analysis_batches("owner")[0].status == "paused"
    assert restarted.list_analysis_batch_item_ids(batch.id, "someone-else") == ()
    resumed = restarted.set_analysis_batch_status(batch.id, "owner", "running")
    assert resumed is not None
    claimed = restarted.claim_next_job()
    assert claimed is not None
    item = postgres_store.get_item(claimed.item_id)
    assert item is not None
    restarted.complete_job(
        claimed.id,
        "personal_photo",
        "A photo of people outdoors",
        "",
        0.9,
        ("Photograph indicators",),
        100,
        assess_destination(item, "personal_photo", 0.9, ""),
    )
    failed = restarted.claim_next_job()
    assert failed is not None
    restarted.fail_job(failed.id, "temporary failure")
    completed = restarted.list_analysis_batches("owner")[0]
    assert completed.status == "completed_with_failures"
    assert completed.completed_items == 1
    assert completed.failed_items == 1

    retried = restarted.retry_analysis_batch(batch.id, "owner")
    assert retried is not None and retried.status == "running"
    assert retried.queued_items == 1
    audit_id = restarted.record_review_batch(
        "approve", (items[0].id,), {str(items[0].id): "approved"}, "owner"
    )
    assert audit_id


def test_activity_history_survives_store_recreation(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    batch_id = postgres_store.create_batch(
        INCOMING_SOURCE,
        "/vault/Arrival Hall",
    )
    item = postgres_store.record_file(
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
            owner_user_id=_arrival_hall_owner_user_id(postgres_conninfo),
        ),
    )
    postgres_store.complete_batch(batch_id, 1)
    postgres_store.record_decision(item.id, "approved", "owner")

    recreated_store = PostgresVaultMasterStore(postgres_conninfo)
    events = recreated_store.list_activity()

    assert [event.action for event in events[:3]] == [
        "proposal_approved",
        "scan_completed",
        "file_analysed",
    ]
    assert events[0].username == "owner"
    assert events[0].filename == "photo.jpg"
    assert events[0].source_kind == INCOMING_SOURCE
    assert events[1].batch_id == batch_id
    assert events[1].detail == "1 file(s) analysed"


def test_canonical_relationship_survives_store_recreation(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    assets = [
        CataloguedAsset(
            id=UUID(f"00000000-0000-0000-0000-0000000000{suffix}"),
            asset_type="Movies",
            display_title="Family Film",
            captured_on=None,
            location=None,
            vault_path=f"/vault/Theatre/Movies/family-{suffix}.mkv",
            filename=f"family-{suffix}.mkv",
            size_bytes=1_000,
            mime_type="video/x-matroska",
            sha256=checksum * 64,
            metadata={},
            metadata_provenance={},
            owner_username="owner",
        )
        for suffix, checksum in (("11", "a"), ("12", "b"))
    ]
    for asset in assets:
        postgres_store.restore_catalogued_asset(asset, "owner")
    assert postgres_store.request_asset_relationship_review(
        assets[0].id,
        assets[1].id,
        "probable_duplicate",
        "high",
        ("Filename identity matches",),
        "owner",
    ) is not None
    assert postgres_store.approve_asset_relationship_review(
        assets[0].id,
        assets[1].id,
        "duplicate",
        "high",
        ("Filename identity matches",),
        "owner",
    ) is not None

    recreated_store = PostgresVaultMasterStore(postgres_conninfo)
    relationships = recreated_store.list_catalogued_asset_relationships(
        assets[1].id
    )

    assert len(relationships) == 1
    assert relationships[0].relationship_type == "duplicate"
    assert relationships[0].evidence == ("Filename identity matches",)
    assert recreated_store.list_catalogued_asset_history(assets[0].id)[0][
        "action"
    ] == "relationship_review_linked"


def test_restored_catalogue_asset_and_audit_survive_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = CataloguedAsset(
        id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        asset_type="Documents",
        display_title="Recovered record",
        captured_on=date(2020, 5, 4),
        location="London",
        vault_path="/vault/Documents/recovered.pdf",
        filename="recovered.pdf",
        size_bytes=8,
        mime_type="application/pdf",
        sha256="a" * 64,
        metadata={"author": "Owner"},
        metadata_provenance={
            "display_title": "user_override",
            "captured_on": "detected",
            "location": "user_override",
        },
        detected_metadata={"captured_on": "2020-05-04"},
        imported_metadata={"author": "Owner"},
        user_overrides={
            "display_title": "Recovered record",
            "location": "London",
        },
        effective_metadata={
            "display_title": "Recovered record",
            "captured_on": "2020-05-04",
            "location": "London",
            "author": "Owner",
        },
        owner_username="owner",
        visibility="shared",
        shared_with=("son",),
    )

    asset = postgres_store.restore_catalogued_asset(asset, "owner")
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)
    recreated_store.initialize()

    assert recreated_store.get_catalogued_asset(asset.vault_path) == asset
    assert recreated_store.get_visible_catalogued_asset_by_id(
        asset.id, "owner"
    ) == asset
    assert recreated_store.get_visible_catalogued_asset_by_id(
        asset.id, "son"
    ) == asset
    assert (
        recreated_store.get_visible_catalogued_asset_by_id(
            asset.id, "guest"
        )
        is None
    )
    assert [
        result.id
        for result in recreated_store.search_visible_catalogued_assets(
            "recovered", "son"
        )
    ] == [asset.id]
    event = recreated_store.list_activity(limit=1)[0]
    assert event.action == "sidecar_restored"
    assert event.username == "owner"


def test_asset_access_policy_update_survives_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = CataloguedAsset(
        id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        asset_type="Gallery",
        display_title="Private photo",
        captured_on=None,
        location=None,
        vault_path="/vault/Gallery/private.jpg",
        filename="private.jpg",
        size_bytes=8,
        mime_type="image/jpeg",
        sha256="b" * 64,
        metadata={},
        metadata_provenance={},
        detected_metadata={},
        imported_metadata={},
        user_overrides={},
        # Restored sidecars carry the composed canonical view, not an empty
        # placeholder.  The access-policy change must preserve that view.
        effective_metadata={"display_title": "Private photo"},
        owner_username="owner",
        visibility="private",
        shared_with=(),
    )
    asset = postgres_store.restore_catalogued_asset(asset, "owner")

    updated = postgres_store.update_catalogued_asset_access(
        asset.id,
        "shared",
        ("son",),
        "owner",
    )
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)

    assert updated is not None
    assert updated.visibility == "shared"
    assert updated.shared_with == ("son",)
    assert recreated_store.get_catalogued_asset(asset.vault_path) == updated
    assert recreated_store.get_visible_catalogued_asset_by_id(
        asset.id, "son"
    ) == updated
    history = recreated_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "access_policy_updated"
    assert history[0]["current_values"] == {
        "visibility": "shared",
        "shared_with": "son",
    }


def test_quarantine_review_request_survives_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = CataloguedAsset(
        id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
        asset_type="Gallery",
        display_title="Duplicate candidate",
        captured_on=None,
        location=None,
        vault_path="/vault/Gallery/duplicate.jpg",
        filename="duplicate.jpg",
        size_bytes=8,
        mime_type="image/jpeg",
        sha256="c" * 64,
        metadata={},
        metadata_provenance={},
        detected_metadata={},
        imported_metadata={},
        user_overrides={},
        effective_metadata={"display_title": "Duplicate candidate"},
        owner_username="owner",
        visibility="private",
        shared_with=(),
    )
    asset = postgres_store.restore_catalogued_asset(asset, "owner")

    request = postgres_store.request_catalogued_asset_quarantine_review(
        asset.id,
        "owner",
        "Review duplicate before quarantine",
    )
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)

    assert request is not None
    assert recreated_store.get_catalogued_asset(asset.vault_path) == asset
    history = recreated_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "quarantine_review_requested"
    assert history[0]["username"] == "owner"
    assert history[0]["current_values"] == {
        "reason": "Review duplicate before quarantine",
        "state": "pending_review",
    }

    withdrawn = recreated_store.cancel_catalogued_asset_quarantine_review(
        asset.id,
        "owner",
    )
    restarted_store = PostgresVaultMasterStore(postgres_conninfo)

    assert withdrawn is not None
    assert restarted_store.get_catalogued_asset(asset.vault_path) == asset
    history = restarted_store.list_catalogued_asset_history(asset.id)
    assert [entry["action"] for entry in history[:2]] == [
        "quarantine_review_cancelled",
        "quarantine_review_requested",
    ]
    assert history[0]["previous_values"] == {"state": "pending_review"}
    assert history[0]["current_values"] == {"state": "cancelled"}
    assert restarted_store.cancel_catalogued_asset_quarantine_review(asset.id, "owner") is None


def test_confirmed_quarantine_survives_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = CataloguedAsset(
        id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        asset_type="Documents",
        display_title="Duplicate candidate",
        captured_on=None,
        location=None,
        vault_path="/vault/Documents/duplicate.pdf",
        filename="duplicate.pdf",
        size_bytes=8,
        mime_type="application/pdf",
        sha256="d" * 64,
        metadata={},
        metadata_provenance={},
        detected_metadata={},
        imported_metadata={},
        user_overrides={},
        effective_metadata={"display_title": "Duplicate candidate"},
        owner_username="owner",
        visibility="private",
        shared_with=(),
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    assert postgres_store.request_catalogued_asset_quarantine_review(
        asset.id,
        "owner",
        "Review duplicate before quarantine",
    ) is not None

    quarantined = postgres_store.confirm_catalogued_asset_quarantine(
        asset.id,
        asset.vault_path,
        "/vault/Quarantine/Documents/duplicate.pdf",
        "owner",
    )
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)

    assert quarantined is not None
    assert quarantined.vault_path == "/vault/Quarantine/Documents/duplicate.pdf"
    assert recreated_store.get_catalogued_asset(asset.vault_path) is None
    assert recreated_store.get_catalogued_asset(quarantined.vault_path) == quarantined
    assert [entry["action"] for entry in recreated_store.list_catalogued_asset_history(asset.id)[:2]] == [
        "quarantined",
        "quarantine_review_requested",
    ]

    eligible_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    request = recreated_store.request_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
        "A verified duplicate is retained elsewhere",
        eligible_at,
    )
    restarted_store = PostgresVaultMasterStore(postgres_conninfo)

    assert request is not None
    assert restarted_store.get_catalogued_asset(quarantined.vault_path) == quarantined
    history = restarted_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "permanent_deletion_review_requested"
    assert history[0]["previous_values"] == {"state": "quarantined"}
    assert history[0]["current_values"] == {
        "reason": "A verified duplicate is retained elsewhere",
        "state": "pending_permanent_deletion_review",
        "eligible_at": eligible_at.isoformat(),
    }

    cancelled = restarted_store.cancel_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
    )
    final_store = PostgresVaultMasterStore(postgres_conninfo)

    assert cancelled is not None
    assert final_store.get_catalogued_asset(quarantined.vault_path) == quarantined
    history = final_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "permanent_deletion_review_cancelled"
    assert history[0]["previous_values"] == {
        "state": "pending_permanent_deletion_review"
    }
    assert history[0]["current_values"] == {"state": "quarantined"}
    assert final_store.cancel_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
    ) is None

    assert final_store.request_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
        "A verified duplicate is retained elsewhere",
        eligible_at,
    ) is not None
    confirmed = final_store.confirm_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
        asset.sha256,
    )
    confirmed_store = PostgresVaultMasterStore(postgres_conninfo)

    assert confirmed is not None
    assert confirmed_store.get_catalogued_asset(quarantined.vault_path) == quarantined
    history = confirmed_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "permanent_deletion_confirmed"
    assert history[0]["previous_values"] == {
        "state": "pending_permanent_deletion_review"
    }
    assert history[0]["current_values"] == {
        "state": "approved_for_permanent_deletion",
        "checksum": asset.sha256,
    }
    assert confirmed_store.confirm_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
        asset.sha256,
    ) is None
    assert confirmed_store.cancel_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
    ) is None

    deleted = confirmed_store.record_catalogued_asset_permanent_deletion(
        asset.id,
        quarantined.vault_path,
        asset.sha256,
        "owner",
    )
    deletion_store = PostgresVaultMasterStore(postgres_conninfo)

    assert deleted is not None
    assert deleted["action"] == "permanently_deleted"
    assert deletion_store.get_catalogued_asset(quarantined.vault_path) is None
    history = deletion_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "permanently_deleted"
    assert history[0]["previous_values"] == {
        "state": "approved_for_permanent_deletion",
        "vault_path": quarantined.vault_path,
        "checksum": asset.sha256,
    }
    assert history[0]["current_values"] == {"state": "deleted"}
    with psycopg.connect(postgres_conninfo, row_factory=psycopg.rows.dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT vault_path, sha256, deleted_by FROM vault_asset_deletions WHERE asset_id = %s",
                (asset.id,),
            )
            tombstone = cursor.fetchone()
            cursor.execute("SELECT id FROM vault_assets WHERE id = %s", (asset.id,))
            retained_asset = cursor.fetchone()
    assert tombstone == {
        "vault_path": quarantined.vault_path,
        "sha256": asset.sha256,
        "deleted_by": "owner",
    }
    assert retained_asset == {"id": asset.id}
    assert deletion_store.record_catalogued_asset_permanent_deletion(
        asset.id,
        quarantined.vault_path,
        asset.sha256,
        "owner",
    ) is None


def test_permanent_deletion_review_retains_active_lifecycle_state(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(
            UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            "/vault/Gallery/retained-photo.jpg",
            "owner",
        ),
        "owner",
    )
    eligible_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    request = postgres_store.request_catalogued_asset_permanent_deletion_review(
        asset.id,
        "owner",
        "Owner requested permanent purge",
        eligible_at,
    )
    restarted_store = PostgresVaultMasterStore(postgres_conninfo)

    assert request is not None
    assert request["previous_values"] == {"state": "active"}
    assert restarted_store.get_catalogued_asset(asset.vault_path) == asset
    history = restarted_store.list_catalogued_asset_history(asset.id)
    assert history[0]["action"] == "permanent_deletion_review_requested"
    assert history[0]["previous_values"] == {"state": "active"}


def test_hidden_lifecycle_survives_postgres_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    owner_user_id = _arrival_hall_owner_user_id(postgres_conninfo)
    asset = CataloguedAsset(
        id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        asset_type="Gallery",
        display_title="Hidden photo",
        captured_on=None,
        location=None,
        vault_path="/vault/Gallery/hidden-photo.jpg",
        filename="hidden-photo.jpg",
        size_bytes=1,
        mime_type="image/jpeg",
        sha256="f" * 64,
        metadata={},
        metadata_provenance={},
        owner_username="owner",
        owner_user_id=owner_user_id,
        visibility="private",
        shared_with=(),
    )
    postgres_store.restore_catalogued_asset(asset, "owner")

    hidden = postgres_store.set_catalogued_asset_lifecycle_state(
        asset.id, owner_user_id, "owner", "hidden"
    )
    restarted = PostgresVaultMasterStore(postgres_conninfo)
    persisted = restarted.get_catalogued_asset_by_id(asset.id)
    restored = restarted.set_catalogued_asset_lifecycle_state(
        asset.id, owner_user_id, "owner", "active"
    )

    assert hidden is not None and hidden.lifecycle_state == "hidden"
    assert hidden.vault_path == asset.vault_path
    assert persisted is not None and persisted.lifecycle_state == "hidden"
    assert persisted.vault_path == asset.vault_path
    assert restored is not None and restored.lifecycle_state == "active"


def test_queued_job_survives_store_recreation(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    (incoming / "queued.txt").write_text("queued", encoding="utf-8")
    batch_id = enqueue_root(postgres_store, incoming, INCOMING_SOURCE)

    recreated_store = PostgresVaultMasterStore(postgres_conninfo)

    assert process_next_batch(
        recreated_store,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    ) == batch_id
    assert recreated_store.list_items()[0].filename == "queued.txt"


def test_arrival_hall_uuid_owner_is_resolved_by_system_worker(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    uploaded = incoming / "recipient-photo.jpg"
    uploaded.write_bytes(b"recipient-photo")
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    recipient = Account(
        username="system-worker-user",
        display_name="Recipient",
        email="system-worker-user@example.test",
        password_hash="test-hash",
        role="user",
        active=True,
        password_change_required=False,
        created_at=datetime.now(timezone.utc),
        last_sign_in_at=None,
    )
    authentication.create_account(recipient)
    batch_id = enqueue_root(postgres_store, incoming, INCOMING_SOURCE)

    assert process_next_batch(
        postgres_store,
        owner_lookup=lambda path: recipient.user_id if path == uploaded else None,
    ) == batch_id
    item = postgres_store.list_items()[0]
    assert item.owner_user_id == recipient.user_id
    assert item.owner_username == "system-worker-user"


def test_catalogue_backfill_reuses_persisted_active_batch(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    documents = tmp_path / "Documents"
    documents.mkdir()
    first_ids, first_reused = enqueue_catalogue_backfill(
        postgres_store,
        (documents,),
    )
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)

    second_ids, second_reused = enqueue_catalogue_backfill(
        recreated_store,
        (documents,),
    )

    assert first_reused == 0
    assert second_reused == 1
    assert second_ids == first_ids


def test_arrival_hall_path_migration_survives_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    batch_id = postgres_store.create_batch(
        INCOMING_SOURCE,
        "/vault/Incoming",
    )
    postgres_store.record_file(
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
            owner_user_id=_arrival_hall_owner_user_id(postgres_conninfo),
        ),
    )

    assert (
        postgres_store.migrate_source_root(
            INCOMING_SOURCE,
            "/vault/Incoming",
            "/vault/Arrival Hall",
        )
        == 1
    )
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)

    assert recreated_store.list_items()[0].source_path == (
        "/vault/Arrival Hall/photo.jpg"
    )
    assert recreated_store.list_batches()[0]["source_root"] == (
        "/vault/Arrival Hall"
    )
    assert (
        recreated_store.migrate_source_root(
            INCOMING_SOURCE,
            "/vault/Incoming",
            "/vault/Arrival Hall",
        )
        == 0
    )


def test_metadata_overrides_survive_rescan_and_store_recreation(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    (incoming / "persistent.jpg").write_bytes(b"persistent")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]

    postgres_store.update_metadata_overrides(
        item.id,
        {
            "display_title": "Corrected title",
            "captured_on": "2012-07-15",
        },
        "owner",
    )
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    recreated_store = PostgresVaultMasterStore(postgres_conninfo)
    persisted = recreated_store.get_item(item.id)

    assert persisted is not None
    assert persisted.metadata_overrides == {
        "display_title": "Corrected title",
        "captured_on": "2012-07-15",
    }
    assert persisted.state == "needs_review"


def test_approved_move_publishes_permanent_catalogue_record(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    gallery = tmp_path / "Gallery"
    incoming.mkdir()
    gallery.mkdir()
    (incoming / "photo.jpg").write_bytes(b"catalogued-photo")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]
    postgres_store.update_metadata_overrides(
        item.id,
        {
            "display_title": "Family photograph",
            "captured_on": "1995-09-03",
        },
        "owner",
    )
    postgres_store.record_decision(item.id, "approved", "owner")
    postgres_store.queue_move(item.id, "owner")

    assert (
        process_next_move(
            postgres_store,
            incoming,
            {"Gallery": gallery},
        )
        == item.id
    )
    asset = postgres_store.get_catalogued_asset(
        "/vault/Gallery/photo.jpg"
    )

    assert asset is not None
    assert asset.display_title == "Family photograph"
    assert asset.captured_on is not None
    assert asset.captured_on.isoformat() == "1995-09-03"
    assert asset.sha256 == item.sha256
    assert asset.imported_metadata == {}
    assert asset.user_overrides == {
        "display_title": "Family photograph",
        "captured_on": "1995-09-03",
    }
    assert asset.effective_metadata["display_title"] == "Family photograph"
    assert asset.effective_metadata["captured_on"] == "1995-09-03"
    assert asset.owner_username == "owner"
    assert item.owner_user_id is not None
    assert asset.owner_user_id == item.owner_user_id
    assert asset.origin_vault_id == postgres_store.get_local_vault_id()
    assert asset.visibility == "private"
    assert asset.shared_with == ()
    sidecar = (
        tmp_path / "metadata" / "sidecars" / f"{asset.id}.json"
    )
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["metadata"]["user_overrides"] == asset.user_overrides
    assert postgres_store.reconcile_sidecars().current == 1
    sidecar.unlink()
    reconciliation = postgres_store.reconcile_sidecars()
    assert reconciliation.repaired == 1
    assert sidecar.is_file()

    recreated_store = PostgresVaultMasterStore(postgres_conninfo)
    persisted = recreated_store.get_catalogued_asset(
        "/vault/Gallery/photo.jpg"
    )
    assert persisted is not None
    assert persisted.user_overrides == asset.user_overrides
    assert persisted.effective_metadata == asset.effective_metadata


def test_arrival_theatre_receipt_publishes_catalogue_and_placement_exactly_once(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "manual.mkv").write_bytes(b"manual-theatre")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]
    selected = postgres_store.update_proposal(item.id, "Movies", "owner")
    assert selected is not None
    queued = postgres_store.record_decision(item.id, "approved", "owner")
    assert queued is not None
    assert postgres_store.queue_move(item.id, "owner") is not None
    assert postgres_store.claim_next_move() is not None
    assert postgres_store.mark_theatre_promotion_pending(item.id) is not None
    receipt = {
        "request_id": str(uuid4()),
        "item_id": str(item.id),
        "owner_user_id": str(item.owner_user_id),
        "logical_destination": "/vault/Theatre/Movies/manual.mkv",
        "logical_area": "Theatre / Movies",
        "slot_id": "PV-DISK-005",
        "relative_path": "Theatre/Movies/manual.mkv",
        "expected_sha256": item.sha256,
        "expected_size_bytes": item.size_bytes,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }

    published = postgres_store.publish_arrival_theatre_receipt(item.id, receipt)
    assert published is not None
    assert published.vault_path == "/vault/Theatre/Movies/manual.mkv"
    assert published.effective_metadata["storage_placement"] == {
        "slot_id": "PV-DISK-005", "relative_path": "Theatre/Movies/manual.mkv"
    }
    # This simulates a worker restart after the root copy/source removal and
    # after the first committed catalogue transaction.
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_master_items SET state='needs_review' WHERE id=%s",
            (item.id,),
        )
    repeated = postgres_store.publish_arrival_theatre_receipt(item.id, receipt)
    assert repeated is None
    with psycopg.connect(postgres_conninfo) as connection:
        activity_count = connection.execute(
            "SELECT count(*) FROM vault_master_activity WHERE item_id=%s AND action='file_moved'",
            (item.id,),
        ).fetchone()[0]
    assert activity_count == 1
    with psycopg.connect(postgres_conninfo) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vault_assets").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM vault_files").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM vault_file_storage_placements").fetchone() == (1,)
        assert connection.execute("SELECT owner_user_id, logical_destination, logical_area FROM vault_arrival_managed_publications").fetchone() == (item.owner_user_id, "/vault/Theatre/Movies/manual.mkv", "Theatre / Movies")
        assert connection.execute("SELECT slot_id, relative_path FROM vault_file_storage_placements").fetchone() == ("PV-DISK-005", "Theatre/Movies/manual.mkv")
    assert postgres_store.get_item(item.id).state == "moved"


def _reviewed_foundation_items(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
    *,
    count: int = 10,
) -> list:
    incoming = tmp_path / "Arrival Hall" / "Foundation Season 1"
    incoming.mkdir(parents=True)
    for number in range(1, count + 1):
        (incoming / f"Foundation S01E{number:02d}.mp4").write_bytes(
            f"foundation-{number}".encode()
        )
    scan_root(
        postgres_store,
        incoming.parent,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    reviewed = [
        postgres_store.update_proposal(
            item.id, "TV Shows", "owner", publication_audience="vault-wide"
        )
        for item in postgres_store.list_items()
    ]
    assert all(item is not None for item in reviewed)
    for item in reviewed:
        assert item is not None
        assert postgres_store.record_decision(item.id, "approved", "owner") is not None
    return sorted(
        (postgres_store.get_item(item.id) for item in reviewed if item is not None),
        key=lambda item: item.filename if item is not None else "",
    )


def _foundation_receipt(item) -> dict[str, object]:
    assert item.owner_user_id is not None and item.proposed_destination is not None
    destination = item.proposed_destination
    return {
        "request_id": str(uuid4()),
        "item_id": str(item.id),
        "owner_user_id": str(item.owner_user_id),
        "logical_destination": destination,
        "logical_area": "Theatre / TV Shows",
        "slot_id": "PV-DISK-003",
        "relative_path": destination.removeprefix("/vault/"),
        "expected_sha256": item.sha256,
        "expected_size_bytes": item.size_bytes,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def test_postgres_unmarked_tv_receipt_fails_closed_before_cataloguing(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    PostgresTvShowStore(postgres_conninfo).initialize()
    item = _reviewed_foundation_items(postgres_store, postgres_conninfo, tmp_path, count=1)[0]
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            """UPDATE vault_master_items
               SET state='theatre_promotion_pending', publication_audience=NULL,
                   metadata=metadata - 'tv_publication_set'
               WHERE id=%s""",
            (item.id,),
        )
    assert postgres_store.publish_arrival_managed_receipt(item.id, _foundation_receipt(item)) is None
    with psycopg.connect(postgres_conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM vault_assets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM vault_arrival_managed_publications").fetchone() == (0,)
    persisted = postgres_store.get_item(item.id)
    assert persisted is not None and persisted.state == "theatre_promotion_pending"


def test_postgres_recovery_reconciles_existing_foundation_receipts_once_without_media_duplication(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PV_METADATA_STORAGE_PATH", str(tmp_path / "metadata"))
    PostgresTvShowStore(postgres_conninfo).initialize()
    items = _reviewed_foundation_items(postgres_store, postgres_conninfo, tmp_path)
    owner = items[0].owner_user_id
    assert owner is not None
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_master_items SET state='theatre_promotion_pending' WHERE id=ANY(%s)",
            ([item.id for item in items],),
        )
    for item in items:
        assert postgres_store.publish_arrival_managed_receipt(item.id, _foundation_receipt(item)) is not None
    with psycopg.connect(postgres_conninfo) as connection:
        before = connection.execute(
            """SELECT publication.item_id, publication.asset_id, publication.file_id,
                      file.sha256, placement.slot_id, placement.relative_path
               FROM vault_arrival_managed_publications publication
               JOIN vault_files file ON file.id=publication.file_id
               JOIN vault_file_storage_placements placement ON placement.file_id=file.id
               ORDER BY publication.item_id"""
        ).fetchall()
        connection.execute("DELETE FROM vault_tv_publication_set_members")
        connection.execute("DELETE FROM vault_tv_publication_sets")
        connection.execute("DELETE FROM vault_tv_episodes")
        connection.execute("DELETE FROM vault_tv_seasons")
        connection.execute("DELETE FROM vault_tv_shows")
        connection.execute(
            """UPDATE vault_master_items
               SET publication_audience=NULL, metadata=metadata - 'tv_publication_set'
               WHERE id=ANY(%s)""",
            ([item.id for item in items],),
        )

    show_id = postgres_store.recover_moved_tv_publication_set(
        owner_user_id=owner, source_directory="Foundation Season 1"
    )
    assert postgres_store.recover_moved_tv_publication_set(
        owner_user_id=owner, source_directory="Foundation Season 1"
    ) == show_id

    with psycopg.connect(postgres_conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM vault_tv_shows").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM vault_tv_seasons").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM vault_tv_episodes").fetchone() == (10,)
        assert connection.execute(
            "SELECT audience, state, expected_episode_count FROM vault_tv_publication_sets"
        ).fetchone() == ("vault-wide", "published", 10)
        after = connection.execute(
            """SELECT publication.item_id, publication.asset_id, publication.file_id,
                      file.sha256, placement.slot_id, placement.relative_path
               FROM vault_arrival_managed_publications publication
               JOIN vault_files file ON file.id=publication.file_id
               JOIN vault_file_storage_placements placement ON placement.file_id=file.id
               ORDER BY publication.item_id"""
        ).fetchall()
        moved_events = connection.execute(
            "SELECT count(*) FROM vault_master_activity WHERE action='file_moved'"
        ).fetchone()[0]
        recovery_events = connection.execute(
            "SELECT count(*) FROM vault_master_activity WHERE action='tv_publication_recovered'"
        ).fetchone()[0]
    assert after == before
    assert moved_events == 10
    assert recovery_events == 10
    recovered = [postgres_store.get_item(item.id) for item in items]
    assert all(item is not None for item in recovered)
    assert {item.publication_audience for item in recovered if item is not None} == {"vault-wide"}
    assert len({json.dumps(item.metadata["tv_publication_set"], sort_keys=True) for item in recovered if item is not None}) == 1


def test_postgres_movie_approval_persists_provisional_folder_identity(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    source = incoming / "TRON" / "Tron_t00.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tron")
    scan_root(
        postgres_store,
        incoming,
        INCOMING_SOURCE,
        owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo),
    )
    item = postgres_store.list_items()[0]
    assert postgres_store.update_proposal(item.id, "Movies", "owner") is not None

    approved = postgres_store.record_decision(item.id, "approved", "owner")

    assert approved is not None
    assert approved.proposed_destination == "/vault/Theatre/Movies/TRON/TRON.mkv"
    assert approved.metadata["movie_identity_provisional"] == {
        "state": "provisional",
        "hint": "TRON",
        "source_relative_path": "TRON/Tron_t00.mkv",
        "source_filename": "Tron_t00.mkv",
    }
    with psycopg.connect(postgres_conninfo) as connection:
        row = connection.execute(
            "SELECT proposed_destination, metadata FROM vault_master_items WHERE id=%s",
            (item.id,),
        ).fetchone()
    assert row == (approved.proposed_destination, approved.metadata)


def test_postgres_tron_set_publishes_one_main_and_checksum_bound_companions(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    batch_id = postgres_store.create_batch(
        INCOMING_SOURCE, "/vault/Arrival Hall"
    )
    items = []
    for index in range(3):
        filename = f"Tron_t{index:02d}.mkv"
        items.append(
            postgres_store.record_file(
                batch_id,
                INCOMING_SOURCE,
                ScannedFile(
                    source_path=f"/vault/Arrival Hall/TRON/{filename}",
                    relative_path=f"TRON/{filename}",
                    filename=filename,
                    size_bytes=30_000 if index == 0 else 4_000 - index,
                    mime_type="video/x-matroska",
                    modified_at=datetime.now(timezone.utc),
                    sha256=f"{index + 1:064x}",
                    metadata={
                        "duration_seconds": 5700 if index == 0 else 180 - index,
                        "width": 1920 if index == 0 else 720,
                        "height": 1080 if index == 0 else 480,
                    },
                    owner_user_id=owner,
                ),
            )
        )
    postgres_store.complete_batch(batch_id, len(items))
    for item in items:
        assert postgres_store.update_proposal(
            item.id, "Movies", "owner"
        ) is not None

    approved_main = postgres_store.record_decision(
        items[0].id, "approved", "owner"
    )
    assert approved_main is not None
    assert postgres_store.queue_move(approved_main.id, "owner") is None
    approved = [approved_main]
    for item in items[1:]:
        member = postgres_store.record_decision(
            item.id, "approved", "owner"
        )
        assert member is not None
        approved.append(member)

    assert [member.proposed_destination for member in approved] == [
        "/vault/Theatre/Movies/TRON/TRON.mkv",
        "/vault/Theatre/Movies/TRON/extras/Tron_t01.mkv",
        "/vault/Theatre/Movies/TRON/extras/Tron_t02.mkv",
    ]
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            "UPDATE vault_master_items SET state='theatre_promotion_pending' WHERE id=ANY(%s)",
            ([item.id for item in items],),
        )

    for member in approved:
        current = postgres_store.get_item(member.id)
        assert current is not None
        destination = str(current.proposed_destination)
        receipt = {
            "request_id": str(uuid4()),
            "item_id": str(current.id),
            "owner_user_id": str(owner),
            "logical_destination": destination,
            "logical_area": "Theatre / Movies",
            "slot_id": "PV-DISK-005",
            "relative_path": destination.removeprefix("/vault/"),
            "expected_sha256": current.sha256,
            "expected_size_bytes": current.size_bytes,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        assert postgres_store.publish_arrival_managed_receipt(
            current.id, receipt
        ) is not None

    main_asset = postgres_store.get_catalogued_asset(
        "/vault/Theatre/Movies/TRON/TRON.mkv"
    )
    assert main_asset is not None
    assert postgres_store.theatre_movie_rename_snapshot(
        main_asset.id, owner
    ) is None

    with psycopg.connect(postgres_conninfo) as connection:
        rows = connection.execute(
            """
            SELECT file.vault_path, file.sha256, file.size_bytes,
                   placement.slot_id, placement.relative_path,
                   publication.item_id
            FROM vault_files file
            JOIN vault_file_storage_placements placement
              ON placement.file_id=file.id
            JOIN vault_arrival_managed_publications publication
              ON publication.file_id=file.id
            WHERE file.vault_path LIKE '/vault/Theatre/Movies/TRON/%'
            ORDER BY file.vault_path
            """
        ).fetchall()
    assert len(rows) == 3
    expected = {item.id: item for item in items}
    for vault_path, sha256, size_bytes, slot_id, relative_path, item_id in rows:
        assert sha256 == expected[item_id].sha256
        assert size_bytes == expected[item_id].size_bytes
        assert slot_id == "PV-DISK-005"
        assert relative_path == vault_path.removeprefix("/vault/")


def test_postgres_missing_companion_joins_partially_published_tron_set(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    batch_id = postgres_store.create_batch(
        INCOMING_SOURCE, "/vault/Arrival Hall"
    )
    items = []
    for index in range(3):
        filename = f"Tron_t{index:02d}.mkv"
        items.append(
            postgres_store.record_file(
                batch_id,
                INCOMING_SOURCE,
                ScannedFile(
                    source_path=f"/vault/Arrival Hall/TRON/{filename}",
                    relative_path=f"TRON/{filename}",
                    filename=filename,
                    size_bytes=30_000 if index == 0 else 4_000 - index,
                    mime_type="video/x-matroska",
                    modified_at=datetime.now(timezone.utc),
                    sha256=f"{index + 1:064x}",
                    metadata={
                        "duration_seconds": 5700 if index == 0 else 180 - index,
                        "width": 1920 if index == 0 else 720,
                        "height": 1080 if index == 0 else 480,
                    },
                    owner_user_id=owner,
                ),
            )
        )
    postgres_store.complete_batch(batch_id, len(items))
    for item in items:
        assert postgres_store.update_proposal(
            item.id, "Movies", "owner"
        ) is not None
    approved = [
        postgres_store.record_decision(item.id, "approved", "owner")
        for item in items
    ]
    assert all(member is not None for member in approved)
    approved_members = [member for member in approved if member is not None]

    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            """
            UPDATE vault_master_items
            SET state='theatre_promotion_pending'
            WHERE id=ANY(%s)
            """,
            ([items[0].id, items[1].id],),
        )
    for member in approved_members[:2]:
        current = postgres_store.get_item(member.id)
        assert current is not None
        destination = str(current.proposed_destination)
        assert postgres_store.publish_arrival_managed_receipt(
            current.id,
            {
                "request_id": str(uuid4()),
                "item_id": str(current.id),
                "owner_user_id": str(owner),
                "logical_destination": destination,
                "logical_area": "Theatre / Movies",
                "slot_id": "PV-DISK-005",
                "relative_path": destination.removeprefix("/vault/"),
                "expected_sha256": current.sha256,
                "expected_size_bytes": current.size_bytes,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            },
        ) is not None

    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute(
            """
            UPDATE vault_master_items
            SET state='needs_review', metadata='{}'::jsonb
            WHERE id=%s
            """,
            (items[0].id,),
        )
        connection.execute(
            """
            UPDATE vault_master_items
            SET state='needs_review',
                proposed_destination=%s,
                metadata=%s
            WHERE id=%s
            """,
            (
                "/vault/Theatre/Movies/Tron_t02.mkv",
                Jsonb(items[2].metadata),
                items[2].id,
            ),
        )

    joined = postgres_store.record_decision(
        items[2].id, "approved", "owner"
    )

    assert joined is not None
    assert joined.state == "approved"
    assert joined.proposed_destination == (
        "/vault/Theatre/Movies/TRON/extras/Tron_t02.mkv"
    )
    assert postgres_store.get_item(items[0].id).state == "moved"
    assert postgres_store.get_item(items[1].id).state == "moved"
    assert postgres_store.queue_move(joined.id, "owner") is not None
    with psycopg.connect(postgres_conninfo) as connection:
        publications = connection.execute(
            """
            SELECT count(*)
            FROM vault_arrival_managed_publications
            WHERE item_id=ANY(%s)
            """,
            ([item.id for item in items],),
        ).fetchone()[0]
        main_asset_id, main_marker = connection.execute(
            """
            SELECT asset.id,
                   asset.detected_metadata->'movie_publication_set'
            FROM vault_arrival_managed_publications publication
            JOIN vault_assets asset ON asset.id=publication.asset_id
            WHERE publication.item_id=%s
            """,
            (items[0].id,),
        ).fetchone()
        reconciliations = connection.execute(
            """
            SELECT count(*)
            FROM vault_master_activity
            WHERE item_id=%s AND action='publication_state_reconciled'
            """,
            (items[0].id,),
        ).fetchone()[0]
    assert publications == 2
    assert main_marker["companion_count"] == 2
    assert reconciliations == 1
    sidecar = json.loads(
        (
            tmp_path
            / "metadata"
            / "sidecars"
            / f"{main_asset_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert sidecar["metadata"]["detected"]["movie_publication_set"] == (
        main_marker
    )


def test_arrival_theatre_receipt_refuses_existing_logical_path(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "collision.mkv").write_bytes(b"candidate")
    scan_root(postgres_store, incoming, INCOMING_SOURCE, owner_lookup=lambda _: _arrival_hall_owner_user_id(postgres_conninfo))
    item = postgres_store.list_items()[0]
    assert postgres_store.update_proposal(item.id, "Movies", "owner") is not None
    existing = replace(_catalogued_asset(uuid4(), "/vault/Theatre/Movies/collision.mkv", "owner"), owner_user_id=_arrival_hall_owner_user_id(postgres_conninfo), sha256="f" * 64)
    postgres_store.restore_catalogued_asset(existing, "owner")
    receipt = {"request_id": str(uuid4()), "item_id": str(item.id), "owner_user_id": str(item.owner_user_id), "logical_destination": "/vault/Theatre/Movies/collision.mkv", "logical_area": "Theatre / Movies", "slot_id": "PV-DISK-005", "relative_path": "Theatre/Movies/collision.mkv", "expected_sha256": item.sha256, "expected_size_bytes": item.size_bytes, "verified_at": datetime.now(timezone.utc).isoformat()}
    assert postgres_store.publish_arrival_theatre_receipt(item.id, receipt) is None
    assert postgres_store.get_item(item.id).state != "moved"


def test_inventory_catalogues_existing_file_without_moving_it(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = tmp_path / "Documents"
    documents.mkdir()
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    existing = documents / "existing.pdf"
    existing.write_bytes(b"existing-document")

    scan_root(postgres_store, documents, INVENTORY_SOURCE)
    asset = postgres_store.get_catalogued_asset(
        "/vault/Documents/existing.pdf"
    )

    assert asset is not None
    assert asset.asset_type == "Documents"
    assert asset.display_title == "existing"
    assert existing.read_bytes() == b"existing-document"
    assert [
        result.id
        for result in postgres_store.search_catalogued_assets(
            "EXISTING.PDF"
        )
    ] == [asset.id]
    assert postgres_store.search_catalogued_assets("not-present") == []

    updated = postgres_store.update_catalogued_asset_metadata(
        asset.id,
        {
            "display_title": "Corrected record",
            "captured_on": "1995-09-03",
            "location": "Gdansk",
        },
        "owner",
    )

    assert updated is not None
    assert updated.display_title == "Corrected record"
    assert updated.captured_on is not None
    assert updated.captured_on.isoformat() == "1995-09-03"
    assert updated.location == "Gdansk"
    assert updated.sha256 == asset.sha256
    assert updated.user_overrides == {
        "display_title": "Corrected record",
        "captured_on": "1995-09-03",
        "location": "Gdansk",
    }
    assert updated.effective_metadata["display_title"] == "Corrected record"
    assert updated.effective_metadata["captured_on"] == "1995-09-03"
    assert updated.effective_metadata["location"] == "Gdansk"
    sidecar = (
        tmp_path / "metadata" / "sidecars" / f"{asset.id}.json"
    )
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["metadata"]["effective"]["location"] == "Gdansk"
    with psycopg.connect(postgres_conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT username, previous_values, current_values
                FROM vault_asset_history
                WHERE asset_id = %s
                """,
                (asset.id,),
            )
            history = cursor.fetchone()
    assert history is not None
    assert history[0] == "owner"
    assert history[2]["captured_on"] == "1995-09-03"
    recorded_history = postgres_store.list_catalogued_asset_history(asset.id)
    assert len(recorded_history) == 1
    assert recorded_history[0]["username"] == "owner"
    assert recorded_history[0]["previous_values"]["display_title"] == (
        "existing"
    )
    assert recorded_history[0]["current_values"]["location"] == "Gdansk"

    with psycopg.connect(postgres_conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE vault_assets
                SET detected_metadata = '{}'::jsonb,
                    imported_metadata = '{}'::jsonb,
                    user_overrides = '{}'::jsonb,
                    effective_metadata = '{}'::jsonb
                WHERE id = %s
                """,
                (asset.id,),
            )

    migrated_store = PostgresVaultMasterStore(postgres_conninfo)
    migrated_store.initialize()
    migrated = migrated_store.get_catalogued_asset(
        "/vault/Documents/existing.pdf"
    )
    assert migrated is not None
    assert migrated.user_overrides == updated.user_overrides
    assert migrated.effective_metadata["display_title"] == "Corrected record"
    assert migrated.effective_metadata["captured_on"] == "1995-09-03"
    assert migrated.effective_metadata["location"] == "Gdansk"


def test_inventory_rescan_publishes_newly_detected_location(
    postgres_store: PostgresVaultMasterStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gallery = tmp_path / "Gallery"
    gallery.mkdir()
    monkeypatch.setenv("PV_GALLERY_PATH", str(gallery))
    photo = gallery / "IMG_1675.JPG"
    photo.write_bytes(b"original")

    scan_root(postgres_store, gallery, INVENTORY_SOURCE)
    original = postgres_store.get_catalogued_asset(
        "/vault/Gallery/IMG_1675.JPG"
    )
    assert original is not None
    assert original.location is None

    monkeypatch.setattr(
        "app.vault_master.extract_basic_metadata",
        lambda _: {
            "captured_at": "2016-08-30",
            "capture_date_source": "embedded",
            "location": "Stegna, Poland",
        },
    )
    scan_root(postgres_store, gallery, INVENTORY_SOURCE)
    refreshed = postgres_store.get_catalogued_asset(
        "/vault/Gallery/IMG_1675.JPG"
    )

    assert refreshed is not None
    assert refreshed.id == original.id
    assert refreshed.location == "Stegna, Poland"
    assert refreshed.metadata_provenance["location"] == "embedded"
    assert refreshed.detected_metadata["location"] == "Stegna, Poland"
    assert refreshed.effective_metadata["location"] == "Stegna, Poland"


def test_ai_queue_and_evidence_survive_restart(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gallery = tmp_path / "Gallery"
    gallery.mkdir()
    monkeypatch.setenv("PV_GALLERY_PATH", str(gallery))
    (gallery / "letter.jpg").write_bytes(b"image")
    scan_root(postgres_store, gallery, INVENTORY_SOURCE)
    asset = postgres_store.get_catalogued_asset("/vault/Gallery/letter.jpg")
    assert asset is not None

    first = PostgresAiStore(postgres_conninfo)
    queued = first.queue_ocr(asset.id, asset.owner_username)
    assert first.claim_next_job() is not None

    restarted = PostgresAiStore(postgres_conninfo)
    restarted.initialize()
    recovered = restarted.list_jobs(asset.id, asset.owner_username)[0]
    assert recovered.id == queued.id
    assert recovered.status == "queued"
    claimed = restarted.claim_next_job()
    assert claimed is not None and claimed.attempts == 2
    suggestion = restarted.complete_job(claimed.id, "A LOCAL LETTER", None, 900)

    final = PostgresAiStore(postgres_conninfo)
    persisted = final.list_suggestions(asset.id, asset.owner_username)[0]
    assert persisted.id == suggestion.id
    reviewed = final.review_suggestion(
        persisted.id, asset.owner_username, "deferred", None
    )
    assert reviewed is not None and reviewed.status == "deferred"


def test_intelligence_owner_uuid_migration_is_idempotent_and_never_uses_requester_identity(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
) -> None:
    """A Owner audit requester must never make Recipient's evidence Owner-owned."""
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    recipient = authentication.ensure_initial_administrator("recipient", "test-hash")
    assert owner is not None
    incoming = tmp_path / "Arrival Hall"
    incoming.mkdir()
    (incoming / "recipient.jpg").write_bytes(b"recipient-image")
    scan_root(postgres_store, incoming, INCOMING_SOURCE, owner_lookup=lambda _: recipient.user_id)
    item = postgres_store.list_items()[0]
    asset = postgres_store.restore_catalogued_asset(
        _catalogued_asset(uuid4(), "/vault/Gallery/recipient-gallery.jpg", "recipient"), "recipient"
    )
    assert asset is not None and item.owner_user_id == recipient.user_id and asset.owner_user_id == recipient.user_id

    ingestion, legacy_ai = PostgresIngestionAiStore(postgres_conninfo), PostgresAiStore(postgres_conninfo)
    gallery_ai = PostgresGalleryIntelligenceStore(postgres_conninfo)
    people, video = PostgresGalleryPeopleStore(postgres_conninfo), PostgresVideoIntelligenceStore(postgres_conninfo)
    ids = [uuid4() for _ in range(8)]
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("""INSERT INTO vault_ingestion_ai_jobs(id,item_id,requested_by,status,owner_user_id)
            VALUES(%s,%s,'owner','completed',NULL)""", (ids[0], item.id))
        connection.execute("""INSERT INTO vault_ingestion_ai_evidence(id,job_id,item_id,content_type,caption,ocr_text,confidence,reasons,model_id,model_revision,task_version,processing_ms,requested_by,owner_user_id)
            VALUES(%s,%s,%s,'personal_photo','Recipient retained Florence caption','',0.9,'[]'::jsonb,'florence','test','semantic-intake-v5',1,'owner',NULL)""", (ids[1], ids[0], item.id))
        connection.execute("INSERT INTO vault_ai_jobs(id,asset_id,requested_by,status,owner_user_id) VALUES(%s,%s,'owner','completed',NULL)", (ids[2], asset.id))
        connection.execute("""INSERT INTO vault_ai_suggestions(id,job_id,asset_id,suggestion_type,raw_value,confidence,model_id,model_revision,task_version,processing_ms,status,requested_by,owner_user_id)
            VALUES(%s,%s,%s,'text_transcription','Recipient OCR',0.9,'florence','test','gallery-ocr-v1',1,'pending','owner',NULL)""", (ids[3], ids[2], asset.id))
        connection.execute("INSERT INTO vault_gallery_intelligence_jobs(id,asset_id,requested_by,status,owner_user_id) VALUES(%s,%s,'owner','completed',NULL)", (ids[4], asset.id))
        connection.execute("INSERT INTO vault_video_analysis_jobs(id,asset_id,requested_by,status,task_version,sampling_version,owner_user_id) VALUES(%s,%s,'owner','completed','video','sample',NULL)", (ids[5], asset.id))
    # Reinitialisation is the controlled additive migration and must be safe
    # repeatedly; construction itself is deliberately read-only.
    first_pass = (
        PostgresIngestionAiStore(postgres_conninfo),
        PostgresAiStore(postgres_conninfo),
        PostgresGalleryIntelligenceStore(postgres_conninfo),
        PostgresGalleryPeopleStore(postgres_conninfo),
        PostgresVideoIntelligenceStore(postgres_conninfo),
    )
    for store in first_pass:
        store.initialize()
    for store in (
        PostgresIngestionAiStore(postgres_conninfo),
        PostgresAiStore(postgres_conninfo),
    ):
        store.initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        rows = connection.execute("""SELECT owner_user_id FROM vault_ingestion_ai_jobs WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_ingestion_ai_evidence WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_ai_jobs WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_ai_suggestions WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_gallery_intelligence_jobs WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_video_analysis_jobs WHERE id=%s""", (ids[0], ids[1], ids[2], ids[3], ids[4], ids[5])).fetchall()
    assert [row[0] for row in rows] == [recipient.user_id] * 6
    assert ingestion.list_evidence(item.id, recipient.user_id)[0].caption == "Recipient retained Florence caption"
    assert ingestion.list_evidence(item.id, owner.user_id) == []
    assert legacy_ai.list_suggestions(asset.id, recipient.user_id)[0].raw_value == "Recipient OCR"
    assert legacy_ai.list_suggestions(asset.id, owner.user_id) == []
    # This shared database fixture predates these additive intelligence tables.
    # Remove only this test's disposable rows before its Vault Master teardown.
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("DELETE FROM vault_ingestion_ai_evidence WHERE job_id=%s", (ids[0],))
        connection.execute("DELETE FROM vault_ingestion_ai_jobs WHERE id=%s", (ids[0],))
        connection.execute("DELETE FROM vault_ai_suggestions WHERE job_id=%s", (ids[2],))
        connection.execute("DELETE FROM vault_ai_jobs WHERE id=%s", (ids[2],))
        connection.execute("DELETE FROM vault_gallery_intelligence_jobs WHERE id=%s", (ids[4],))
        connection.execute("DELETE FROM vault_video_analysis_jobs WHERE id=%s", (ids[5],))


def test_video_worker_persists_canonical_owner_from_job_to_frame_and_evidence(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    source_root = tmp_path / "Home Videos"
    source_root.mkdir()
    (source_root / "owner-clip.mp4").write_bytes(b"test-video")
    asset = replace(
        _catalogued_asset(uuid4(), "/vault/Home Videos/owner-clip.mp4", "owner"),
        asset_type="Home Videos",
        mime_type="video/mp4",
        owner_user_id=owner.user_id,
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    store = PostgresVideoIntelligenceStore(postgres_conninfo)
    job = store.queue(asset.id, owner.username, owner.user_id)

    monkeypatch.setenv("PV_PERSONAL_VIDEOS_PATH", str(source_root))
    monkeypatch.setenv("PV_VIDEO_ANALYSIS_CACHE_PATH", str(tmp_path / "frame-cache"))
    monkeypatch.setattr(video_intelligence, "probe_video_duration_ms", lambda _: 1_000)
    monkeypatch.setattr(video_intelligence, "_scene_candidates", lambda *_: ())
    monkeypatch.setattr(
        video_intelligence,
        "extract_frame",
        lambda _source, _timestamp, destination: (
            destination.parent.mkdir(parents=True, exist_ok=True), destination.write_bytes(b"jpeg")
        ),
    )
    monkeypatch.setattr(ingestion_ai, "request_florence_analysis", lambda _: ("A test video frame.", "video-frame-v1", 1))
    monkeypatch.setattr(
        gallery_intelligence,
        "request_rampp_tags",
        lambda _: SimpleNamespace(provider="rampp", model_id="test", model_revision=None, task_version="test", raw_response="[]", processing_ms=1),
    )
    monkeypatch.setattr(gallery_people_worker, "request_people_analysis", lambda *_: {"task_version": "test", "faces": {"boxes": []}})

    assert process_next_video_analysis_job(store, postgres_store, MemoryGalleryPeopleStore()) == job.id
    with psycopg.connect(postgres_conninfo) as connection:
        owners = connection.execute("""SELECT owner_user_id FROM vault_video_analysis_jobs WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_video_analysis_frames WHERE job_id=%s
            UNION ALL SELECT evidence.owner_user_id FROM vault_video_frame_evidence evidence
                JOIN vault_video_analysis_frames frames ON frames.id=evidence.frame_id WHERE frames.job_id=%s""", (job.id, job.id, job.id)).fetchall()
    assert owners and [row[0] for row in owners] == [owner.user_id] * len(owners)


def test_people_face_identification_persists_canonical_owner_uuid_and_filters_by_uuid(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    owner = authentication.get_account("owner")
    other = authentication.get_account("recipient")
    assert owner is not None and other is not None
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/owner-face.jpg", "owner"), owner_user_id=owner.user_id)
    postgres_store.restore_catalogued_asset(asset, "owner")
    people = PostgresGalleryPeopleStore(postgres_conninfo)
    person = people.create_person(owner.username, "Owner", owner.user_id)
    face_id = people.add_face_detection(asset.id, bounding_box={"x": 1, "y": 2, "w": 3, "h": 4})
    people.identify_face(asset.id, face_id, person.id, owner.user_id)

    with psycopg.connect(postgres_conninfo) as connection:
        rows = connection.execute("""SELECT owner_user_id FROM vault_asset_people
            WHERE asset_id=%s AND person_id=%s AND source='user_face'
            UNION ALL SELECT owner_user_id FROM vault_asset_people_decisions
            WHERE asset_id=%s AND person_id=%s""", (asset.id, person.id, asset.id, person.id)).fetchall()
    assert [row[0] for row in rows] == [owner.user_id, owner.user_id]
    assert people.matching_asset_ids((person.id,), owner.user_id) == {asset.id}
    assert people.matching_asset_ids((person.id,), other.user_id) == set()


def test_video_and_people_derived_owner_uuid_backfill_covers_every_touched_table(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
) -> None:
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    asset = replace(_catalogued_asset(uuid4(), "/vault/Gallery/owner-derived.jpg", "owner"), owner_user_id=owner.user_id)
    postgres_store.restore_catalogued_asset(asset, "owner")
    job_id, frame_id, evidence_id, result_id, person_id, face_id, body_id, association_id, decision_id = (uuid4() for _ in range(9))
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("INSERT INTO vault_video_analysis_jobs(id,asset_id,requested_by,status,task_version,sampling_version,owner_user_id) VALUES(%s,%s,'owner','completed','test','test',NULL)", (job_id, asset.id))
        connection.execute("INSERT INTO vault_video_analysis_frames(id,job_id,asset_id,timestamp_ms,ordinal,selection_reason,status,owner_user_id) VALUES(%s,%s,%s,0,1,'start','completed',NULL)", (frame_id, job_id, asset.id))
        connection.execute("INSERT INTO vault_video_frame_evidence(id,frame_id,provider,model_id,task_version,raw_evidence,owner_user_id) VALUES(%s,%s,'florence','test','test','{}'::jsonb,NULL)", (evidence_id, frame_id))
        connection.execute("INSERT INTO vault_video_reconciliation_results(id,asset_id,job_id,reconciliation_version,owner_user_id) VALUES(%s,%s,%s,'test',NULL)", (result_id, asset.id, job_id))
        connection.execute("INSERT INTO vault_people(id,owner_username,display_name,full_name,owner_user_id) VALUES(%s,'owner','Owner','Owner',NULL)", (person_id,))
        connection.execute("INSERT INTO vault_face_detections(id,asset_id,bounding_box,detector_provider,detector_model,task_version,raw_evidence,owner_user_id) VALUES(%s,%s,'{}'::jsonb,'test','test','test','{}'::jsonb,NULL)", (face_id, asset.id))
        connection.execute("INSERT INTO vault_person_detections(id,asset_id,bounding_box,detector_provider,detector_model,task_version,raw_evidence,owner_user_id) VALUES(%s,%s,'{}'::jsonb,'test','test','test','{}'::jsonb,NULL)", (body_id, asset.id))
        connection.execute("INSERT INTO vault_asset_people(id,asset_id,person_id,source,created_by,owner_user_id) VALUES(%s,%s,%s,'user','owner',NULL)", (association_id, asset.id, person_id))
        connection.execute("INSERT INTO vault_asset_people_decisions(id,asset_id,person_id,decision,decided_by,owner_user_id) VALUES(%s,%s,%s,'include','owner',NULL)", (decision_id, asset.id, person_id))
    PostgresVideoIntelligenceStore(postgres_conninfo).initialize()
    PostgresGalleryPeopleStore(postgres_conninfo).initialize()
    with psycopg.connect(postgres_conninfo) as connection:
        rows = connection.execute("""SELECT owner_user_id FROM vault_video_analysis_jobs WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_video_analysis_frames WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_video_frame_evidence WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_video_reconciliation_results WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_people WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_face_detections WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_person_detections WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_asset_people WHERE id=%s
            UNION ALL SELECT owner_user_id FROM vault_asset_people_decisions WHERE id=%s""", (job_id, frame_id, evidence_id, result_id, person_id, face_id, body_id, association_id, decision_id)).fetchall()
    assert [row[0] for row in rows] == [owner.user_id] * 9


def test_same_slot_swap_receipt_updates_hardware_history_once_without_changing_placement(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The database-side completion boundary preserves canonical identity."""
    slot, operation_id = "PV-DISK-001", uuid4()
    old = {"hardware_id": "serial:old", "filesystem_uuid": "old-fs"}
    new = {"hardware_id": "serial:new", "filesystem_uuid": "new-fs"}
    with psycopg.connect(postgres_conninfo) as connection:
        connection.execute("INSERT INTO vault_storage_slots(slot_id, state, hardware, assigned_areas) VALUES(%s, 'active', %s, '[]'::jsonb)", (slot, Jsonb(old)))
    key = tmp_path / "storage.key"; key.write_bytes(b"swap-test-key")
    monkeypatch.setattr(vault_storage_control, "get_database_conninfo", lambda: postgres_conninfo)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    receipt = {"operation_id": str(operation_id), "slot_id": slot, "old_hardware": old, "new_hardware": new, "files": [], "reboot_verified": True, "safe_to_disconnect": False}
    signature = hmac.new(key.read_bytes(), vault_storage_control._canonical(receipt), hashlib.sha256).hexdigest()
    digest = vault_storage_control._record_swap_hardware(receipt, signature)
    assert vault_storage_control._record_swap_hardware(receipt, signature) == digest
    with psycopg.connect(postgres_conninfo) as connection:
        hardware = connection.execute("SELECT hardware FROM vault_storage_slots WHERE slot_id=%s", (slot,)).fetchone()
        history = connection.execute("SELECT previous_hardware, replacement_hardware, receipt_sha256 FROM vault_storage_slot_hardware_history WHERE operation_id=%s", (operation_id,)).fetchall()
        placements = connection.execute("SELECT COUNT(*) FROM vault_file_storage_placements WHERE slot_id=%s", (slot,)).fetchone()
    assert hardware == (new,)
    assert history == [(old, new, digest)]
    assert placements == (0,)


def test_same_slot_swap_receipt_refuses_placement_drift_before_hardware_update(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A verified root receipt cannot integrate after canonical placement drift."""
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    asset = replace(
        _catalogued_asset(uuid4(), "/vault/Theatre/Movies/canonical.mkv", "owner"),
        owner_user_id=owner.user_id,
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    slot, operation_id = "PV-DISK-001", uuid4()
    old = {"hardware_id": "serial:old", "filesystem_uuid": "old-fs"}
    new = {"hardware_id": "serial:new", "filesystem_uuid": "new-fs"}
    with psycopg.connect(postgres_conninfo) as connection:
        file_row = connection.execute("SELECT id FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchone()
        assert file_row is not None
        file_id = file_row[0]
        connection.execute(
            "INSERT INTO vault_storage_slots(slot_id, state, hardware, assigned_areas) VALUES(%s, 'active', %s, '[]'::jsonb)",
            (slot, Jsonb(old)),
        )
        connection.execute(
            """INSERT INTO vault_file_storage_placements(file_id, slot_id, relative_path, assigned_by, placement_reason)
               VALUES(%s, %s, %s, %s, %s)""",
            (file_id, slot, "Theatre/Movies/current.mkv", "test", "test"),
        )
    key = tmp_path / "storage.key"
    key.write_bytes(b"swap-test-key")
    monkeypatch.setattr(vault_storage_control, "get_database_conninfo", lambda: postgres_conninfo)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    receipt = {
        "operation_id": str(operation_id),
        "slot_id": slot,
        "old_hardware": old,
        "new_hardware": new,
        "files": [{"file_id": str(file_id), "relative_path": "Theatre/Movies/stale.mkv"}],
        "reboot_verified": True,
        "safe_to_disconnect": False,
    }
    signature = hmac.new(key.read_bytes(), vault_storage_control._canonical(receipt), hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException, match="Canonical storage placement changed"):
        vault_storage_control._record_swap_hardware(receipt, signature)
    with psycopg.connect(postgres_conninfo) as connection:
        hardware = connection.execute("SELECT hardware FROM vault_storage_slots WHERE slot_id=%s", (slot,)).fetchone()
        history = connection.execute(
            "SELECT COUNT(*) FROM vault_storage_slot_hardware_history WHERE operation_id=%s", (operation_id,)
        ).fetchone()
    assert hardware == (old,)
    assert history == (0,)


def test_swap_snapshot_uses_canonical_slot_hardware_not_inventory_wwn(
    postgres_store: PostgresVaultMasterStore,
    postgres_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed Swap request binds source identity to the durable slot record."""
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account("owner")
    assert owner is not None
    asset = replace(
        _catalogued_asset(uuid4(), "/vault/Theatre/Movies/canonical.mkv", "owner"),
        owner_user_id=owner.user_id,
    )
    postgres_store.restore_catalogued_asset(asset, "owner")
    canonical_hardware = {"hardware_id": "serial:canonical", "filesystem_uuid": "canonical-fs"}
    with psycopg.connect(postgres_conninfo) as connection:
        file_id = connection.execute("SELECT id FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchone()
        assert file_id is not None
        connection.execute(
            "INSERT INTO vault_storage_slots(slot_id,state,hardware,assigned_areas) VALUES(%s,'active',%s,'[]'::jsonb)",
            ("PV-DISK-001", Jsonb(canonical_hardware)),
        )
        connection.execute(
            """INSERT INTO vault_file_storage_placements(file_id,slot_id,relative_path,assigned_by,placement_reason)
                VALUES(%s,%s,%s,%s,%s)""",
            (file_id[0], "PV-DISK-001", "Theatre/Movies/canonical.mkv", "test", "test"),
        )
    monkeypatch.setattr(vault_storage_control, "get_database_conninfo", lambda: postgres_conninfo)

    snapshot = vault_storage_control._swap_snapshot(
        "PV-DISK-001",
        {"wwn": "0xinventory-only", "filesystem_uuid": "canonical-fs"},
        {"capacity_bytes": asset.size_bytes + 1},
    )

    assert snapshot["source_hardware_id"] == "serial:canonical"
    assert snapshot["source_filesystem_uuid"] == "canonical-fs"
    assert snapshot["old_hardware"] == canonical_hardware
