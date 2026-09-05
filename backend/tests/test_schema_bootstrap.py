"""Regression coverage for controlled PostgreSQL schema bootstrap.

The application used to hide DDL in request-time store constructors.  These
tests keep construction side-effect free and exercise the real worker-disabled
API lifecycle against PostgreSQL when the disposable test database is present.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

import app.main as main_module
from app.auth import get_authentication_store
from app.auth_store import PostgresAuthenticationStore
from app.gallery_intelligence import (
    PostgresGalleryIntelligenceStore,
    get_gallery_intelligence_store,
)
from app.gallery_people import PostgresGalleryPeopleStore, get_gallery_people_store
from app.main import app
from app.video_intelligence import PostgresVideoIntelligenceStore, get_video_intelligence_store
from app.vault_master import PostgresVaultMasterStore, get_vault_master_store
from app.vault_master_ai import PostgresAiStore, get_ai_store
from app.vault_master_autopilot import PostgresAutopilotStore, get_autopilot_store
from app.vault_master_ingestion_ai import PostgresIngestionAiStore, get_ingestion_ai_store
from app.vault_master_intake import PostgresIntakeStore, get_intake_store
from app.vault_master_reading import PostgresReadingRoomStore, get_reading_room_store
from app.vault_master_reading_review import (
    PostgresPublicationReviewStore,
    get_publication_review_store,
)


from app.vault_supplier import PostgresVaultSupplierStore
from app.vault_supplier_transfer import PostgresTransferStore, get_transfer_store
from app.tv_resolver_publication import PostgresTvResolverStore

POSTGRES_STORE_TYPES = (
    PostgresVaultSupplierStore,
    PostgresAuthenticationStore,
    PostgresVaultMasterStore,
    PostgresGalleryIntelligenceStore,
    PostgresGalleryPeopleStore,
    PostgresVideoIntelligenceStore,
    PostgresAiStore,
    PostgresIngestionAiStore,
    PostgresAutopilotStore,
    PostgresIntakeStore,
    PostgresReadingRoomStore,
    PostgresPublicationReviewStore,
    PostgresTransferStore,
    PostgresTvResolverStore,
)


def _clear_store_caches() -> None:
    for getter in (
        get_authentication_store,
        get_vault_master_store,
        get_gallery_intelligence_store,
        get_gallery_people_store,
        get_video_intelligence_store,
        get_ai_store,
        get_ingestion_ai_store,
        get_autopilot_store,
        get_intake_store,
        get_reading_room_store,
        get_publication_review_store,
        get_transfer_store,
    ):
        clear = getattr(getter, "cache_clear", None)
        if clear is not None:
            clear()


def test_postgres_store_construction_never_calls_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every runtime store constructor is configuration-only."""
    for store_type in POSTGRES_STORE_TYPES:
        monkeypatch.setattr(
            store_type,
            "initialize",
            lambda self: pytest.fail(f"{type(self).__name__} ran schema DDL in __init__"),
        )

    PostgresVaultSupplierStore("postgresql://invalid")
    PostgresAuthenticationStore("postgresql://invalid")
    PostgresVaultMasterStore("postgresql://invalid")
    PostgresGalleryIntelligenceStore("postgresql://invalid")
    PostgresGalleryPeopleStore("postgresql://invalid")
    PostgresVideoIntelligenceStore("postgresql://invalid")
    PostgresAiStore("postgresql://invalid")
    PostgresIngestionAiStore("postgresql://invalid")
    PostgresAutopilotStore("postgresql://invalid")
    PostgresIntakeStore("postgresql://invalid")
    PostgresReadingRoomStore("postgresql://invalid")
    PostgresPublicationReviewStore("postgresql://invalid")
    PostgresTransferStore("postgresql://invalid")
    PostgresTvResolverStore("postgresql://invalid")


def test_tv_resolver_schema_bootstrap_is_additive_and_idempotent() -> None:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    PostgresAuthenticationStore(conninfo).initialize()
    PostgresVaultMasterStore(conninfo).initialize()
    resolver = PostgresTvResolverStore(conninfo)
    resolver.initialize()
    resolver.initialize()
    expected = {
        "vault_tv_resolver_batches",
        "vault_tv_resolver_seasons",
        "vault_tv_resolver_tracks",
    }
    with psycopg.connect(conninfo) as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    assert expected <= {row[0] for row in rows}


def test_worker_disabled_lifespan_bootstraps_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("PV_VAULT_MASTER_WORKER_ENABLED", "false")
    monkeypatch.setattr(main_module, "bootstrap_application_schema", lambda: calls.append("bootstrap"))

    with TestClient(app):
        assert calls == ["bootstrap"]


def test_worker_enabled_lifespan_bootstraps_before_starting_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def idle_worker() -> None:
        calls.append("worker")
        await __import__("asyncio").Event().wait()

    monkeypatch.setenv("PV_VAULT_MASTER_WORKER_ENABLED", "true")
    monkeypatch.setattr(main_module, "bootstrap_application_schema", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(main_module, "run_vault_master_worker", idle_worker)

    with TestClient(app):
        assert calls == ["bootstrap", "worker"]


def test_worker_disabled_people_requests_are_concurrent_and_ddl_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PostgreSQL proves request traffic cannot resurrect bootstrap DDL."""
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")

    settings = conninfo_to_dict(conninfo)
    monkeypatch.setenv("POSTGRES_HOST", settings["host"])
    monkeypatch.setenv("POSTGRES_PORT", settings["port"])
    monkeypatch.setenv("POSTGRES_DB", settings["dbname"])
    monkeypatch.setenv("POSTGRES_USER", settings["user"])
    monkeypatch.setenv("POSTGRES_PASSWORD", settings["password"])
    monkeypatch.setenv("PV_ADMIN_USERNAME", "schema-test-owner")
    monkeypatch.setenv("PV_ADMIN_PASSWORD_HASH", "test-hash")
    monkeypatch.setenv("PV_SESSION_SECRET", "schema-test-secret")
    monkeypatch.setenv("PV_WEBAUTHN_RP_ID", "testserver")
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", "https://testserver")
    monkeypatch.setenv("PV_VAULT_MASTER_WORKER_ENABLED", "false")
    _clear_store_caches()

    audit_table = "pv_schema_bootstrap_ddl_audit"
    trigger_name = "pv_schema_bootstrap_ddl_trigger"
    function_name = "pv_schema_bootstrap_ddl_log"
    with psycopg.connect(conninfo) as connection:
        connection.execute(f"DROP EVENT TRIGGER IF EXISTS {trigger_name}")
        connection.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
        connection.execute(f"DROP TABLE IF EXISTS {audit_table}")

    try:
        with TestClient(app) as client:
            authentication = PostgresAuthenticationStore(conninfo)
            authentication.ensure_initial_administrator("schema-test-owner", "test-hash")
            account = authentication.get_account("schema-test-owner")
            assert account is not None
            authentication.create_session(
                "schema-bootstrap-test", account.user_id, account.username,
                datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            client.cookies.set("pv_session", "schema-bootstrap-test")

            with psycopg.connect(conninfo) as connection:
                connection.execute(f"CREATE TABLE {audit_table}(command_tag TEXT NOT NULL)")
                connection.execute(
                    f"CREATE FUNCTION {function_name}() RETURNS event_trigger LANGUAGE plpgsql "
                    f"AS $$ BEGIN INSERT INTO {audit_table}(command_tag) VALUES (TG_TAG); END; $$"
                )
                connection.execute(
                    f"CREATE EVENT TRIGGER {trigger_name} ON ddl_command_end "
                    f"EXECUTE FUNCTION {function_name}()"
                )
                connection.execute(f"TRUNCATE {audit_table}")

            with ThreadPoolExecutor(max_workers=6) as executor:
                responses = list(executor.map(lambda i: client.get("/api/vault-supplier/installations" if i % 2 else "/api/gallery/people"), range(12)))
            assert [response.status_code for response in responses] == [200] * 12

            with psycopg.connect(conninfo) as connection:
                assert connection.execute(f"SELECT command_tag FROM {audit_table}").fetchall() == []
    finally:
        with psycopg.connect(conninfo) as connection:
            connection.execute(f"DROP EVENT TRIGGER IF EXISTS {trigger_name}")
            connection.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
            connection.execute(f"DROP TABLE IF EXISTS {audit_table}")
        _clear_store_caches()
