from fastapi.testclient import TestClient

from tests.test_auth import login


def test_gallery_state_is_authenticated_and_persists_for_user(client: TestClient) -> None:
    assert client.get("/api/user-state/gallery").status_code == 401
    assert login(client).status_code == 200

    initial = client.get("/api/user-state/gallery")
    assert initial.json() == {"sort": "newest", "anchor_id": None, "anchor_offset": 0}

    payload = {"sort": "oldest", "anchor_id": "photo-42", "anchor_offset": -18}
    assert client.put("/api/user-state/gallery", json=payload).json() == payload
    assert client.get("/api/user-state/gallery").json() == payload


import pytest
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from app.auth import SESSION_COOKIE_NAME


@pytest.mark.parametrize("collection,identity", [("movies","example-film"),("tv-episodes","11111111-1111-4111-8111-111111111111")])
def test_theatre_state_lifecycle_is_viewer_scoped(client, authentication_store, collection, identity):
    endpoint=f"/api/user-state/{collection}/{identity}"
    assert client.get(endpoint).status_code == 401
    assert client.put(endpoint+"/watched",json={"watched":True}).status_code == 401
    assert login(client).status_code == 200
    first_token=client.cookies.get(SESSION_COOKIE_NAME)
    assert client.get(endpoint).json() is None
    def report(position, duration=600, completed=False):
        return client.put(endpoint,json={"position_seconds":position,"duration_seconds":duration,"completed":completed}).json()
    assert report(0)["state"] == "unwatched"
    assert report(20)["state"] == "unwatched"
    assert report(125)["state"] == "in_progress"
    assert report(569)["state"] == "in_progress"
    final=report(570)
    assert final == {"position_seconds":570,"duration_seconds":600,"completed":True,"state":"watched"}
    assert report(15)["state"] == "watched" # Replay never silently clears Watched.
    assert client.put(endpoint+"/watched",json={"watched":False}).json()["state"] == "unwatched"
    assert report(125)["state"] == "in_progress"
    assert client.put(endpoint+"/watched",json={"watched":True}).json()["position_seconds"] == 125
    unmarked=client.put(endpoint+"/watched",json={"watched":False}).json()
    assert unmarked["state"] == "in_progress" and unmarked["position_seconds"] == 125
    assert client.get(f"/api/user-state/{collection}").json()[identity] == unmarked
    other=authentication_store.ensure_initial_administrator("second-viewer","test-hash")
    authentication_store.create_session("second-viewer-session",other.user_id,other.username,datetime.now(timezone.utc)+timedelta(hours=1))
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME,"second-viewer-session")
    assert client.get(endpoint).json() is None
    assert client.get(f"/api/user-state/{collection}").json() == {}
    assert client.put(endpoint+"/watched",json={"watched":True,"user_id":str(uuid4())}).status_code == 422
    assert client.put(endpoint+"/watched",json={"watched":True}).json()["state"] == "watched"
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME,first_token)
    assert client.get(endpoint).json() == unmarked
    assert report(600,completed=True)["state"] == "watched"


@pytest.mark.parametrize("collection,identity", [("movies","short-example"),("tv-episodes","22222222-2222-4222-8222-222222222222")])
def test_short_media_does_not_complete_at_start_and_invalid_samples_fail(client,collection,identity):
    login(client)
    endpoint=f"/api/user-state/{collection}/{identity}"
    for position,state in [(0,"unwatched"),(10,"in_progress"),(19,"watched")]:
        assert client.put(endpoint,json={"position_seconds":position,"duration_seconds":20}).json()["state"] == state
    assert client.put(endpoint,json={"position_seconds":-1,"duration_seconds":20}).status_code == 422
    assert client.put(endpoint,json={"position_seconds":"Infinity","duration_seconds":20}).status_code == 422
    assert client.put(endpoint+"/watched",json={"watched":False}).json()["position_seconds"] == 19


def test_invalid_episode_identity_returns_validation_error(client):
    login(client)
    assert client.get("/api/user-state/tv-episodes/not-a-uuid").status_code == 422
