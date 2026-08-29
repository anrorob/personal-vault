from __future__ import annotations

import ast
from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = REPOSITORY_ROOT / "backend" / "app"
FRONTEND_SOURCE = REPOSITORY_ROOT / "src"

PROVIDER_MODULES = frozenset({"app.jellyfin"})
EXTRACTOR_MODULE_PREFIXES = (
    "PIL",
    "reverse_geocode",
    "exifread",
    "ffmpeg",
    "hachoir",
    "mediainfo",
    "mutagen",
    "pypdf",
    "pymediainfo",
)

PLAYBACK_ADAPTER_MODULES = frozenset({"movie_playback.py", "music_playback.py", "tv_playback.py"})

METADATA_PRODUCER_MODULES = frozenset(
    {
        "jellyfin.py",
        "photo_dates.py",
        "vault_master.py",
        "vault_master_api.py",
        "vault_master_ingestion_ai.py",
        "vault_master_jellyfin.py",
        "tv_jellyfin_import.py",
        "vault_master_music.py",
        "vault_master_reading_extraction.py",
    }
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _metadata_authority_imports(path: Path) -> set[str]:
    return {
        imported
        for imported in _imports(path)
        if imported in PROVIDER_MODULES
        or imported.startswith(EXTRACTOR_MODULE_PREFIXES)
    }


def test_only_vault_master_modules_gain_metadata_dependencies() -> None:
    observed_exceptions: dict[str, set[str]] = {}

    for path in sorted(BACKEND_APP.glob("*.py")):
        if (
            path.name in METADATA_PRODUCER_MODULES
            or path.name in PLAYBACK_ADAPTER_MODULES
        ):
            continue

        restricted = _metadata_authority_imports(path)
        if restricted:
            observed_exceptions[path.name] = restricted

    assert observed_exceptions == {}, (
        "Product sections must consume the Vault catalogue instead of adding "
        "metadata extractors or provider dependencies. Implement producers "
        "inside Vault Master and playback dependencies inside declared "
        "playback adapters."
    )


def test_frontend_never_depends_on_metadata_providers_or_extractors() -> None:
    forbidden_modules = "jellyfin|tmdb|themoviedb|ffprobe|mediainfo|exifread"
    provider_import = re.compile(
        rf'''(?:from\s*["'][^"']*(?:{forbidden_modules})|import\s*\(\s*["'][^"']*(?:{forbidden_modules}))''',
        re.IGNORECASE,
    )
    violations: list[str] = []

    for path in sorted(FRONTEND_SOURCE.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        # Provider names may appear in honest operational status copy (for
        # example, a storage reconciliation progress step).  Only an import is
        # a frontend dependency that would violate metadata authority.
        if provider_import.search(source):
            violations.append(path.relative_to(REPOSITORY_ROOT).as_posix())

    assert violations == [], (
        "Frontend code must consume Personal Vault APIs and must not depend "
        f"on metadata providers or extractors: {violations}"
    )


def test_movies_never_reads_descriptive_metadata_live_from_jellyfin() -> None:
    source = (BACKEND_APP / "movies.py").read_text(encoding="utf-8")

    assert ".get_movie_details(" not in source, (
        "Movies may use Jellyfin for playback, but descriptive metadata must "
        "be read from Vault Master's retained canonical catalogue."
    )
    assert "/resources/images/" not in source, (
        "Movie artwork and person portraits must be served from retained "
        "Vault Master storage, never proxied live from Jellyfin."
    )


def test_music_never_reads_descriptive_metadata_live_from_jellyfin() -> None:
    source = (BACKEND_APP / "music.py").read_text(encoding="utf-8")
    assert "app.jellyfin" not in source
    assert "get_audio_details" not in source
    assert "pv-jellyfin" not in source


def test_music_browser_downloads_one_complete_private_stream_before_playback() -> None:
    source = (FRONTEND_SOURCE / "routes" / "app.music.tsx").read_text(
        encoding="utf-8"
    )

    assert "response.blob()" in source
    assert "URL.createObjectURL" in source
    assert "URL.revokeObjectURL" in source
    assert "src={active.playback_url}" not in source


def test_music_player_is_inline_and_advances_after_a_two_second_gap() -> None:
    source = (FRONTEND_SOURCE / "routes" / "app.music.tsx").read_text(
        encoding="utf-8"
    )

    assert "onEnded={playNextTrack}" in source
    assert "MUSIC_INTER_TRACK_DELAY_MS" in source
    assert "nextAlbumTrack(albumQueue.current, active.id)" in source
    assert "if (nextTrackTimer.current) return;" in source
    assert "void player.play()" in source
    assert 'type="range"' in source
    assert "setPlaybackPosition" in source
    assert "ring-[var(--pv-gold)]" in source
    assert 'className="fixed bottom-0' not in source


def test_gallery_state_is_server_side_and_timeline_uses_photo_anchors() -> None:
    source = (FRONTEND_SOURCE / "routes" / "app.gallery.index.tsx").read_text(encoding="utf-8")

    assert 'fetch("/api/user-state/gallery"' in source
    assert "sessionStorage" not in source
    assert "localStorage" not in source
    assert "data-gallery-id" in source
    assert "Gallery timeline" in source
    assert "if (!openingPhoto.current) persistState(sort, true)" in source


def test_movie_page_exposes_resume_and_two_authenticated_download_modes() -> None:
    source = (FRONTEND_SOURCE / "routes" / "app.movies.$movieId.tsx").read_text(encoding="utf-8")

    assert "/api/user-state/movies/" in source
    assert "/download/original" in source
    assert "/download/compressed.mp4" in source
    assert "Continue at" in source


def test_theatre_subtitle_control_is_track_scoped_and_preserves_position() -> None:
    page = (FRONTEND_SOURCE / "routes" / "app.movies.$movieId.tsx").read_text(
        encoding="utf-8"
    )
    player = (FRONTEND_SOURCE / "components" / "pv" / "MoviePlayer.tsx").read_text(
        encoding="utf-8"
    )

    assert "playbackSubtitles.map" in page
    assert "?subtitle_index=" in page
    assert "is_forced" in page
    assert "is_default" in page
    assert "is_hearing_impaired" in page
    assert 'aria-label="Subtitles"' not in page
    assert '<option value="off">Off</option>' in player
    assert 'aria-label="Subtitles"' in player
    assert 'controlsList="nofullscreen"' in player
    assert "requestFullscreen" in player
    assert 'aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}' in player
    assert 'document.fullscreenElement === playerRef.current' in player
    assert "preservedPosition.current = video.currentTime" in player
    assert "OpenSubtitles" not in page


def test_florence_runtime_is_internal_pinned_and_not_frontend_accessible() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY_ROOT / "ai" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "ai-internal:\n    internal: true" in compose
    florence_service = compose.split("\n  pv-florence2:\n", 1)[1].split(
        "  pv-database:", 1
    )[0]
    assert "ports:" not in florence_service
    assert "PV_FLORENCE_DEVICE: ${PV_FLORENCE_DEVICE:-CPU}" in florence_service
    assert "openvino/ubuntu22_dev@sha256:" in dockerfile
    assert "microsoft/Florence-2-large" not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in FRONTEND_SOURCE.rglob("*.tsx")
    )


def test_arrival_semantic_analysis_remains_review_only_and_backend_orchestrated() -> None:
    service = (REPOSITORY_ROOT / "ai" / "app.py").read_text(encoding="utf-8")
    rampp_service = (REPOSITORY_ROOT / "rampp" / "app.py").read_text(encoding="utf-8")
    backend = (BACKEND_APP / "vault_master_ingestion_ai.py").read_text(
        encoding="utf-8"
    )
    frontend = (FRONTEND_SOURCE / "routes" / "app.arrival-hall.tsx").read_text(
        encoding="utf-8"
    )

    assert '@app.post("/analyse")' in service
    assert '@app.post("/gallery-classify")' not in service
    assert '@app.post("/tag")' in rampp_service
    assert "<MORE_DETAILED_CAPTION>" in service
    assert 'INGESTION_TASK_VERSION = "semantic-intake-v5"' in backend
    assert "MAX_SEMANTIC_PDF_PAGES = 3" in backend
    assert "sha256_file(source) != item.sha256" in backend
    assert "private_review_evidence_only" in frontend
    assert "confidence_components" in frontend
    assert "automatic_disqualifiers" in frontend
    assert 'ROUTING_MODEL_VERSION = "intelligent-routing-v5"' in backend
    assert "pv-florence2" not in frontend


def test_people_foundation_is_post_publication_metadata_only() -> None:
    people = (BACKEND_APP / "gallery_people.py").read_text(encoding="utf-8")
    gallery = (BACKEND_APP / "gallery.py").read_text(encoding="utf-8")
    assert "assess_destination" not in people
    assert "vault_master_ingestion_ai" not in people
    assert "DeepFace" not in people
    assert "YOLOX" not in people
    assert "embedding" in people
    assert "/people/assets/{asset_id}" in gallery


def test_people_service_is_internal_evidence_only_without_demographic_analysis() -> None:
    service = (REPOSITORY_ROOT / "people" / "app.py").read_text(encoding="utf-8").casefold()
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'app.post("/analyse")' in service
    assert "gender" not in service and "race" not in service and "emotion" not in service
    assert "assess_destination" not in service
    people_service = compose.split("\n  pv-people:\n", 1)[1].split("\n  pv-database:\n", 1)[0]
    assert "networks:\n      - ai-internal" in people_service
    assert "read_only: true" in people_service
    assert "mem_limit: 4g" in people_service and "cpus: 4" in people_service
    assert "asyncio.lock" in service
    assert "ports:" not in people_service


def test_mediapipe_face_detector_is_separate_internal_box_only_service() -> None:
    service = (REPOSITORY_ROOT / "face_detector" / "app.py").read_text(encoding="utf-8").casefold()
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'app.post("/detect")' in service
    assert "deepface" not in service and "facenet" not in service and "yolox" not in service
    assert "assess_destination" not in service and "gender" not in service and "emotion" not in service
    detector_service = compose.split("\n  pv-face-detector:\n", 1)[1].split("\n  pv-database:\n", 1)[0]
    assert "networks:\n      - ai-internal" in detector_service
    assert "read_only: true" in detector_service
    assert "mem_limit: 512m" in detector_service and "cpus: 2" in detector_service
    assert "ports:" not in detector_service
    assert ":/models/face-detector:ro" in detector_service


def test_frontend_analysis_copy_is_direct_and_uses_analyse_action() -> None:
    frontend = "\n".join(
        path.read_text(encoding="utf-8") for path in FRONTEND_SOURCE.rglob("*.tsx")
    )
    gallery = (FRONTEND_SOURCE / "routes" / "app.gallery.$photoId.tsx").read_text(
        encoding="utf-8"
    )

    for removed_copy in (
        "Local AI text recognition",
        "Recognise text locally",
        "Local semantic evidence",
        "Local Florence classification",
        "runs inside your server",
        "Nothing moves during analysis.",
    ):
        assert removed_copy not in frontend
    assert "Analyse" in gallery
    assert "Analyse photo" in gallery
    assert "Analyse text / OCR" in gallery
    assert "No text found." in gallery
    assert 'setDialog("ocr")' in gallery
    assert "/api/gallery/intelligence/assets/${photo.asset_id}/reanalyse" in gallery
    assert "/api/vault-master/assets/${assetId}/ai/ocr" in gallery
    assert "Florence visual description" in gallery
    assert "GalleryVisualDescription" in gallery
    assert "if (!photo.can_edit || !photo.asset_id)" in gallery
    assert "evidence.visual_description.caption" in gallery
    assert "request_florence_analysis" not in gallery


def test_bulk_ingestion_remains_owner_controlled_restart_safe_and_audited() -> None:
    backend = (BACKEND_APP / "vault_master_ingestion_ai.py").read_text(
        encoding="utf-8"
    )
    api = (BACKEND_APP / "vault_master_api.py").read_text(encoding="utf-8")
    frontend = (FRONTEND_SOURCE / "routes" / "app.arrival-hall.tsx").read_text(
        encoding="utf-8"
    )

    assert "vault_ingestion_analysis_batches" in backend
    assert "vault_ingestion_review_batches" in backend
    assert "Recovered after worker restart" in backend
    assert "batches.status='paused'" in backend
    assert 'Literal["pause", "resume", "retry"]' in api
    assert "individual_review_required" in api
    assert "record_review_batch" in api
    assert "Every selected file must be a reviewable Arrival Hall image" in api
    assert "Eligible images and PDFs are analysed automatically after inventory." in frontend
    assert "Nothing moves during analysis" not in frontend
    assert "filenames.join" in frontend
    assert "Automatic semantic analysis" in frontend
    assert 'className="animate-spin"' in frontend
    assert "Retry analysis" in frontend


def test_arrival_hall_item_mutations_keep_the_scoped_list_visible() -> None:
    frontend = (FRONTEND_SOURCE / "routes" / "app.arrival-hall.tsx").read_text(
        encoding="utf-8"
    )

    assert "itemActionsRef.current.has(itemId)" in frontend
    assert 'itemAction === "approving" ? "Approving…"' in frontend
    assert "await responseError(response" in frontend
    assert "itemActionErrors[analysis.id]" in frontend
    assert "{listing && listing.files.length > 0 && (" in frontend
    assert "{!error && listing && listing.files.length > 0 && (" not in frontend
    assert "if (loadVersion !== loadVersionRef.current) return;" in frontend


def test_routing_memory_is_owner_scoped_safe_and_manageable() -> None:
    backend = (BACKEND_APP / "vault_master_ingestion_ai.py").read_text(encoding="utf-8")
    api = (BACKEND_APP / "vault_master_api.py").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "routes" / "app.routing-memory.tsx").read_text(encoding="utf-8")
    assert "vault_routing_memory_rules" in backend
    assert "vault_routing_memory_examples" in backend
    assert "SAFE_OCR_CONCEPTS" in backend
    assert 'ROUTING_MEMORY_VERSION = "routing-memory-v1"' in backend
    assert "owner_user_id=%s AND feature_signature=%s" in backend
    assert '@router.get("/routing-memory"' in api
    assert '@router.delete("/routing-memory/{rule_id}"' in api
    assert "these rules only improve suggestions" in page
    assert "Forget rule" in page
