"""Synthetic worker admission: source provenance never implies operator trust."""
from pathlib import Path

import pytest

from app import gallery_section_move as mover, ken_service as ken
from app.runtime_identity import KEY, source_repository_allowed


def configure(monkeypatch, identity="example-owner/personal-vault", allowed=None, environment="development"):
    monkeypatch.setenv("PV_ENVIRONMENT", environment)
    monkeypatch.setenv("PV_REPOSITORY", identity)
    monkeypatch.setenv(KEY, identity if allowed is None else allowed)
    monkeypatch.setenv("PV_KEN_ENABLED", "true")
    monkeypatch.setenv("PV_KEN_URL", "http://analyser.example.test:8080")
    monkeypatch.setenv("PV_KEN_WORK_ROOT", "/example/isolated-work")
    monkeypatch.setenv("PV_SECTION_MOVE_ENABLED", "true")


@pytest.mark.parametrize("identity", ["example-owner/development", "anrorob/personal-vault", "example-owner/private-source"])
@pytest.mark.parametrize("environment", ["development", "production"])
def test_explicit_source_identity_admits_both_workers(monkeypatch, identity, environment):
    configure(monkeypatch, identity, environment=environment)
    assert source_repository_allowed() and ken.enabled() and mover.runtime_enabled()
    mover.validate_worker_environment()
    assert ken.LocalModelAdapter().endpoint() == "http://analyser.example.test:8080"


@pytest.mark.parametrize("allowed", ["", "*", "example-owner/*", "*/personal-vault", "example-owner/personal-vault,",
    "example-owner/personal-vault,,other/source", "https://example.test/owner/repo", "owner/repo.git", "owner/..",
    "owner/repo/extra", "owner /repo", "owner/repo\n", "owner/repo\r", "öwner/repo", "owner/repo,invalid",
    "-owner/repo", "owner-/repo", "own--er/repo", "o" * 40 + "/repo", "owner/" + "r" * 101])
def test_invalid_allowlist_denies_all_worker_admission(monkeypatch, allowed):
    configure(monkeypatch, allowed=allowed)
    assert not source_repository_allowed() and not ken.enabled() and not mover.runtime_enabled()
    with pytest.raises(RuntimeError): mover.validate_worker_environment()
    with pytest.raises(ValueError): ken.LocalModelAdapter().endpoint()


def test_missing_configuration_has_no_source_default(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv(KEY)
    assert not source_repository_allowed() and not ken.enabled() and not mover.runtime_enabled()


@pytest.mark.parametrize("identity", ["", "unknown", "example-owner/personal-vault-extra", "other/personal-vault",
    "Example-owner/personal-vault", " example-owner/personal-vault", "example-owner/personal-vault\n",
    "example-owner/personal-vault.git", "example-owner/personal-vault/extra", "example-owner/*"])
def test_unauthorized_or_malformed_live_identity_denied(monkeypatch, identity):
    configure(monkeypatch, identity, allowed="example-owner/personal-vault")
    assert not source_repository_allowed() and not ken.enabled() and not mover.runtime_enabled()
    with pytest.raises(RuntimeError): mover.validate_worker_environment()
    with pytest.raises(ValueError): ken.LocalModelAdapter().endpoint()


def test_multiple_identities_whitespace_and_duplicates_are_explicit(monkeypatch):
    configure(monkeypatch, allowed=" other/source,\texample-owner/personal-vault ,example-owner/personal-vault")
    assert source_repository_allowed() and ken.enabled()
    monkeypatch.setenv("PV_REPOSITORY", "other/source")
    assert source_repository_allowed() and mover.runtime_enabled()
    monkeypatch.setenv("PV_REPOSITORY", "source")
    assert not source_repository_allowed()


def test_feature_flags_and_environment_guards_remain(monkeypatch):
    configure(monkeypatch, environment="production")
    monkeypatch.delenv("PV_SECTION_MOVE_ENABLED")
    assert not mover.runtime_enabled()
    monkeypatch.setenv("PV_KEN_ENABLED", "false")
    assert not ken.enabled()
    monkeypatch.setenv("PV_KEN_ENABLED", "true")
    monkeypatch.delenv("PV_KEN_WORK_ROOT")
    with pytest.raises(RuntimeError): ken.enabled()
    configure(monkeypatch, environment="test")
    assert not ken.enabled()
    with pytest.raises(RuntimeError): mover.validate_worker_environment()
    with pytest.raises(ValueError): ken.LocalModelAdapter().endpoint()
    configure(monkeypatch, environment="invalid")
    assert not ken.enabled() and not mover.runtime_enabled()


def test_endpoint_is_still_an_explicit_operator_input(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("PV_KEN_URL")
    with pytest.raises(ValueError): ken.LocalModelAdapter().endpoint()


def test_application_runtime_has_no_private_repository_or_hostname_constants():
    root = Path(__file__).parents[1] / "app"
    private_repositories = ("anrorob/" + "pv-development", "anrorob/" + "pv-hq")
    private_host = "pv-srv-" + "001.local"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(identity in text for identity in (*private_repositories, private_host)), path.name
