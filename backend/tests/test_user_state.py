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


def test_movie_progress_is_authenticated_and_completion_resets_position(client: TestClient) -> None:
    assert client.get("/api/user-state/movies/movie-1").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/user-state/movies/movie-1").json() is None

    progress = {"position_seconds": 125, "duration_seconds": 600, "completed": False}
    assert client.put("/api/user-state/movies/movie-1", json=progress).json() == progress
    assert client.get("/api/user-state/movies/movie-1").json() == progress

    completed = {"position_seconds": 590, "duration_seconds": 600, "completed": False}
    assert client.put("/api/user-state/movies/movie-1", json=completed).json() == {
        "position_seconds": 0,
        "duration_seconds": 600,
        "completed": True,
    }
