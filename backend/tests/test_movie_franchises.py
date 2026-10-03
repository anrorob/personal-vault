"""Synthetic franchise authority, persistence and automatic ordering proof."""
from copy import deepcopy
from dataclasses import replace
import os
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app.auth import require_authenticated_user
from app.main import app
from app import movie_chronology
from app.movie_franchises import (
    MemoryMovieFranchiseStore, PostgresMovieFranchiseStore,
    get_movie_franchise_store, release_key,
)
from app.vault_master import MemoryVaultMasterStore
from tests.test_movies import authenticate, catalogue_movie, create_video, owner_identity


@pytest.fixture
def franchise_fixture(client, tmp_path, monkeypatch):
    reference = {"key": "example", "label": "Example publisher chronology",
                 "url": "https://example.invalid/chronology", "version": "example-v1",
                 "positions": {"10001": 10, "10002": 20, "10003": 30}}
    monkeypatch.setattr(movie_chronology, "REFERENCES", (reference,))
    catalogue = MemoryVaultMasterStore()
    franchises = MemoryMovieFranchiseStore()
    app.dependency_overrides[get_movie_franchise_store] = lambda: franchises
    movies = []
    for index, year in enumerate((2003, 2002, 2001), start=1):
        path = f"Example Movie {index}.mkv"
        create_video(tmp_path, path, b"synthetic video")
        asset = catalogue_movie(catalogue, tmp_path, path, metadata={
            "display_title": f"Example Movie {index}", "release_year": year,
            "provider_ids": {"Tmdb": str(10000 + index)}, "collections": ["Example provider collection"],
        })
        asset = replace(asset, id=UUID(f"00000000-0000-4000-8000-{index:012d}"))
        catalogue.catalogued_assets[asset.vault_path] = asset
        movies.append(asset)
    authenticate(client)
    return catalogue, franchises, movies, tmp_path


def create(client, name="Example Franchise"):
    response = client.post("/api/movie-franchises", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add(client, franchise, movies):
    response = client.post(f"/api/movie-franchises/{franchise}/members", json={"asset_ids": [str(movie.id) for movie in movies]})
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_membership_multiple_groups_remove_delete_never_touch_movies(client, franchise_fixture):
    catalogue, franchises, movies, root = franchise_fixture
    original = deepcopy(catalogue.catalogued_assets)
    files = {path: path.read_bytes() for path in root.glob("*.mkv")}
    first, second = create(client), create(client, "Example Second Franchise")
    assert client.get(f"/api/movie-franchises/{first}").json()["movies"] == []
    assert len(add(client, first, movies[:2])["movies"]) == 2
    add(client, second, movies[:1]); add(client, first, movies[:1])
    assert len(franchises.get(owner_identity().user_id, UUID(first)).members) == 2
    response = client.patch(f"/api/movie-franchises/{first}", json={"name": "Example Renamed", "description": "Example description"})
    assert response.json()["id"] == first
    assert response.json()["name"] == "Example Renamed"
    assert client.delete(f"/api/movie-franchises/{first}/members/{movies[0].id}").status_code == 200
    assert client.get(f"/api/movie-franchises/{second}").json()["member_ids"] == [str(movies[0].id)]
    assert client.delete(f"/api/movie-franchises/{first}").status_code == 204
    assert catalogue.catalogued_assets == original
    assert {path: path.read_bytes() for path in root.glob("*.mkv")} == files
    assert len(client.get("/api/movies").json()) == 3


def test_no_provider_auto_membership_and_metadata_refresh_preserves_user_state(client, franchise_fixture):
    catalogue, franchises, movies, _ = franchise_fixture
    franchise = create(client)
    assert client.get(f"/api/movie-franchises/{franchise}").json()["member_count"] == 0
    add(client, franchise, movies[:1])
    before = franchises.get(owner_identity().user_id, UUID(franchise))
    for movie in movies:
        catalogue.import_catalogued_asset_metadata(movie.id, {
            "display_title": "Example Refreshed Title", "provider_ids": {"Tmdb": "20001"},
            "collections": ["Example Franchise"], "artwork": {}, "release_year": 2024,
        }, "jellyfin")
    assert franchises.get(owner_identity().user_id, UUID(franchise)) == before
    view = client.get(f"/api/movie-franchises/{franchise}").json()
    assert view["member_ids"] == [str(movies[0].id)]
    assert view["timeline_available"] is True
    assert len(client.get("/api/movies").json()) == 3


def test_timeline_persisted_incomplete_collection_later_insertion_and_outage(client, franchise_fixture, monkeypatch):
    _, franchises, movies, _ = franchise_fixture
    franchise = create(client)
    view = add(client, franchise, [movies[2], movies[0]])
    assert view["effective_order"] == "timeline"
    assert view["member_ids"] == [str(movies[0].id), str(movies[2].id)]
    snapshot = deepcopy(franchises.get(owner_identity().user_id, UUID(franchise)).chronology)
    assert snapshot["positions"]["10002"] == 20
    monkeypatch.setattr(movie_chronology, "REFERENCES", ())
    view = add(client, franchise, [movies[1]])
    assert view["member_ids"] == [str(movie.id) for movie in movies]
    assert view["timeline_available"] is True
    assert franchises.get(owner_identity().user_id, UUID(franchise)).chronology == snapshot
    release = client.put(f"/api/movie-franchises/{franchise}/order", json={"order": "release"}).json()
    assert release["member_ids"] == [str(movie.id) for movie in reversed(movies)]
    assert franchises.get(owner_identity().user_id, UUID(franchise)).chronology == snapshot
    client.delete(f"/api/movie-franchises/{franchise}/members/{movies[1].id}")
    add(client, franchise, [movies[1]])
    timeline = client.put(f"/api/movie-franchises/{franchise}/order", json={"order": "timeline"}).json()
    assert timeline["member_ids"] == [str(movie.id) for movie in movies]
    refreshed = client.post(f"/api/movie-franchises/{franchise}/chronology/refresh").json()
    assert refreshed["timeline_available"] is True
    assert refreshed["chronology_version"] == "example-v1"


def test_unresolved_or_mixed_chronology_honestly_falls_back(client, franchise_fixture, monkeypatch):
    catalogue, _, movies, _ = franchise_fixture
    movie = replace(movies[1], effective_metadata={"display_title": "Example Unknown", "release_year": 2002, "provider_ids": {"Tmdb": "20001"}})
    catalogue.catalogued_assets[movie.vault_path] = movie
    franchise = create(client)
    view = add(client, franchise, [movies[0], movie])
    assert view["timeline_available"] is False
    assert view["effective_order"] == "release"
    assert view["member_count"] == 2
    selected = client.put(f"/api/movie-franchises/{franchise}/order", json={"order": "timeline"}).json()
    assert selected["selected_order"] == "timeline"
    assert selected["effective_order"] == "release"
    assert selected["timeline_status"] == "unresolved"
    monkeypatch.setattr(movie_chronology, "REFERENCES", (*movie_chronology.REFERENCES, {
        "key": "other", "positions": {"20001": 10}}))
    mixed = create(client, "Example Mixed")
    assert add(client, mixed, [movies[0], movie])["timeline_available"] is False


def test_release_dates_missing_values_and_uuid_ties_are_deterministic(client, franchise_fixture):
    _, _, movies, _ = franchise_fixture
    values = [replace(movies[0], effective_metadata={"release_date": "2001-02-01T00:00:00Z"}),
              replace(movies[1], effective_metadata={"release_date": "2001-01-01"}),
              replace(movies[2], effective_metadata={"release_year": 2001})]
    assert sorted(values, key=release_key) == [values[1], values[0], values[2]]
    missing = [replace(movie, effective_metadata={"release_date": "invalid", "release_year": None}) for movie in reversed(movies)]
    assert [movie.id for movie in sorted(missing, key=release_key)] == [movie.id for movie in movies]
    assert release_key(replace(movies[0], effective_metadata={"release_year": "2001"}))[0] == 10000


def test_private_franchise_authority_and_visibility_enforced(client, franchise_fixture):
    catalogue, _, movies, _ = franchise_fixture
    franchise = create(client)
    add(client, franchise, [movies[0]])
    app.dependency_overrides[require_authenticated_user] = lambda: owner_identity("another")
    assert client.get("/api/movie-franchises").json() == []
    for method, suffix, body in (("get", "", None), ("patch", "", {"name": "Denied"}),
                                 ("delete", "", None), ("put", "/order", {"order": "release"}),
                                 ("post", "/members", {"asset_ids": [str(movies[0].id)]}),
                                 ("post", "/chronology/refresh", None),
                                 ("delete", f"/members/{movies[0].id}", None)):
        response = getattr(client, method)(f"/api/movie-franchises/{franchise}{suffix}", **({"json": body} if body else {}))
        assert response.status_code == 404
    other = create(client, "Another Example")
    assert client.post(f"/api/movie-franchises/{other}/members", json={"asset_ids": [str(movies[0].id)]}).status_code == 404
    app.dependency_overrides.pop(require_authenticated_user)
    catalogue.catalogued_assets[movies[0].vault_path] = replace(movies[0], lifecycle_state="hidden")
    view = client.get(f"/api/movie-franchises/{franchise}").json()
    assert view["member_ids"] == [] and view["movies"] == [] and view["poster_url"] is None
    assert client.post("/api/movie-franchises", json={"name": "   "}).status_code == 422
    assert client.post("/api/movie-franchises", json={"name": "Example", "user_id": str(uuid4())}).status_code == 422


def test_franchise_routes_require_authentication(client):
    assert client.get("/api/movie-franchises").status_code == 401
    assert client.post("/api/movie-franchises", json={"name": "Example"}).status_code == 401


def test_provider_release_date_and_original_title_are_imported_without_membership(monkeypatch):
    from app.jellyfin import JellyfinClient, JellyfinMovie
    from app.vault_master_jellyfin import jellyfin_movie_metadata
    client = JellyfinClient("http://example.invalid", "example-test-key")
    def response(path, *_args):
        if path == "/Users":
            return [{"Id": "example-provider-user"}]
        if path.endswith(("/SpecialFeatures", "/LocalTrailers")):
            return []
        return {"Name": "Example Film", "ProductionYear": 2001,
                "PremiereDate": "2001-04-05T00:00:00.000Z", "OriginalTitle": "Example Original",
                "ProviderIds": {"Tmdb": "10001"}, "CollectionName": "Example Provider Collection"}
    monkeypatch.setattr(client, "_get_json", response)
    movie = JellyfinMovie("example-provider-movie", "example-source", "/example/movie.mkv", "mkv", "h264", ())
    details = client.get_movie_details(movie)
    metadata = jellyfin_movie_metadata(movie, details)
    assert metadata["release_date"] == "2001-04-05T00:00:00.000Z"
    assert metadata["original_title"] == "Example Original"
    assert metadata["provider_ids"] == {"Tmdb": "10001"}
    assert "franchise" not in metadata and "members" not in metadata


def test_postgres_durable_membership_atomicity_and_logical_only_deletion():
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("Disposable PostgreSQL required")
    schema = "test_franchises_" + uuid4().hex
    with psycopg.connect(conninfo, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(conninfo, options=f"-c search_path={schema}")
    user, other, movie = (UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(101, 104))
    try:
        with psycopg.connect(scoped) as connection:
            connection.execute("CREATE TABLE auth_accounts(user_id UUID PRIMARY KEY)")
            connection.execute("CREATE TABLE vault_assets(id UUID PRIMARY KEY)")
            connection.execute("INSERT INTO auth_accounts VALUES (%s),(%s)", (user, other))
            connection.execute("INSERT INTO vault_assets VALUES (%s)", (movie,))
        store = PostgresMovieFranchiseStore(scoped)
        store.initialize(); store.initialize()
        first = store.create(user, "Example Franchise", "")
        second = store.create(user, "Example Second", "")
        def add_member(entry):
            entry.members[movie] = (30, "example")
            entry.chronology = {"key": "example", "positions": {"10001": 10, "10003": 30}}
        store.mutate(user, first.id, add_member); store.mutate(user, second.id, add_member)
        restored = PostgresMovieFranchiseStore(scoped)
        assert restored.get(user, first.id).members[movie] == (30, "example")
        assert restored.get(user, first.id).chronology["positions"]["10001"] == 10
        assert restored.get(other, first.id) is None
        assert restored.mutate(other, first.id, add_member) is None
        assert restored.delete(other, first.id) is False
        def fail(entry):
            entry.name = "Should roll back"
            entry.members[uuid4()] = (None, None)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            restored.mutate(user, first.id, fail)
        assert restored.get(user, first.id).name == "Example Franchise"
        assert restored.delete(user, first.id)
        with psycopg.connect(scoped) as connection:
            assert connection.execute("SELECT count(*) FROM vault_assets").fetchone()[0] == 1
        assert restored.get(user, second.id).members[movie] == (30, "example")
        restored.mutate(user, second.id, lambda entry: entry.members.clear())
        assert restored.get(user, second.id).members == {}
    finally:
        with psycopg.connect(conninfo, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
