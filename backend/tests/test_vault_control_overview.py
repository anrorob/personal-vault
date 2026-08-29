from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import vault_control
from app.auth import get_authentication_store
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())


def test_overview_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/vault-control/overview").status_code == 401


def test_overview_returns_private_current_status(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vault_control,
        "collect_overview",
        lambda: {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall_health": "healthy",
            "database": {"status": "healthy", "response_ms": 12.0},
            "capacity": {"total_bytes": 100, "free_bytes": 80, "low_space": []},
            "cpu": {"load": 0.12, "temperature_c": None},
            "gpu": {"load": None, "temperature_c": None},
            "jobs": {"running": 0, "queued": 0, "failed": 0, "unfinished": 0},
            "issues": [],
            "attention": [],
        },
    )

    _login(client)
    response = client.get("/api/vault-control/overview")

    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["database"] == {"status": "healthy", "response_ms": 12.0}
    assert response.json()["gpu"] == {"load": None, "temperature_c": None}


def test_worst_issue_controls_overall_health() -> None:
    assert vault_control.evaluate_overall_health([]) == "healthy"
    assert (
        vault_control.evaluate_overall_health(
            [vault_control.OverviewIssue("warning", "A warning")]
        )
        == "attention_required"
    )
    assert (
        vault_control.evaluate_overall_health(
            [
                vault_control.OverviewIssue("warning", "A warning"),
                vault_control.OverviewIssue("critical", "A critical issue"),
            ]
        )
        == "critical"
    )


def test_database_unavailable_is_reported_without_crashing(monkeypatch) -> None:
    def unavailable_conninfo() -> str:
        raise RuntimeError("POSTGRES_PASSWORD is not configured")

    monkeypatch.setattr(vault_control, "get_database_conninfo", unavailable_conninfo)

    database, jobs, issues = vault_control._collect_database_and_jobs()

    assert database == {"status": "offline", "response_ms": None}
    assert jobs is None
    assert issues == [vault_control.OverviewIssue("critical", "Database is offline.")]


def test_job_summary_only_counts_failures_from_the_last_three_days(
    monkeypatch,
) -> None:
    recorded: dict[str, object] = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, query, parameters):
            recorded["query"] = query
            recorded["parameters"] = parameters

        def fetchone(self):
            return (0, 0, 0, 0)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(vault_control, "get_database_conninfo", lambda: "test")
    monkeypatch.setattr(vault_control.psycopg, "connect", lambda _: Connection())

    assert vault_control._collect_jobs() == {
        "running": 0,
        "queued": 0,
        "failed": 0,
        "unfinished": 0,
    }
    assert "status = 'failed'" in str(recorded["query"])
    assert "created_at >= CURRENT_TIMESTAMP" in str(recorded["query"])
    assert recorded["parameters"] == (
        3,
        vault_control.UNFINISHED_JOB_MINUTES,
        3,
        vault_control.UNFINISHED_JOB_MINUTES,
        3,
        vault_control.UNFINISHED_JOB_MINUTES,
    )


def test_read_only_playback_mount_is_not_treated_as_storage_failure(
    monkeypatch,
    tmp_path,
) -> None:
    movie_root = tmp_path / "movies"
    movie_root.mkdir()

    monkeypatch.setattr(
        vault_control,
        "get_catalogue_preview_roots",
        lambda: {"/vault/Theatre/Movies": movie_root},
    )
    monkeypatch.setattr(vault_control, "get_destination_paths", lambda: {})
    monkeypatch.setattr(vault_control, "get_quarantine_root", lambda: tmp_path / "quarantine")

    class ReadOnlyStats:
        f_flag = getattr(vault_control.os, "ST_RDONLY", 1)
        f_blocks = 100
        f_bavail = 50
        f_frsize = 1024

    monkeypatch.setattr(
        vault_control.os,
        "statvfs",
        lambda _: ReadOnlyStats(),
        raising=False,
    )

    _, issues = vault_control._collect_storage()

    assert issues == []
