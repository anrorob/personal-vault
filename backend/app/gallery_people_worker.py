"""Vault Master interpretation of local Stage B specialist evidence.

This module owns the service boundary and persistence.  It deliberately does
not create People from unknown evidence and has no routing dependency.
"""
import base64
import json
import mimetypes
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from app.gallery_people import PersonReference

PEOPLE_TASK_VERSION = "gallery-people-v2"


def _references_header(references: list[PersonReference]) -> str:
    return json.dumps([
        {
            "person_id": str(reference.person_id),
            "embedding_b64": base64.b64encode(reference.embedding).decode("ascii"),
            "embedding_model": reference.embedding_model,
            "embedding_revision": reference.embedding_revision,
        }
        for reference in references
    ])


def request_face_detection(source: Path) -> dict[str, object]:
    request = Request(
        os.getenv("PV_FACE_DETECTOR_URL", "http://pv-face-detector:8080/detect"),
        data=source.read_bytes(),
        method="POST",
        headers={"Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream"},
    )
    try:
        with urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Local MediaPipe face detection service is unavailable") from error
    if not isinstance(payload, dict) or payload.get("provider") != "mediapipe" or not isinstance(payload.get("boxes"), list):
        raise RuntimeError("Local MediaPipe face detection service returned invalid evidence")
    return payload


def request_people_analysis(source: Path, references: list[PersonReference]) -> dict[str, object]:
    face_evidence = request_face_detection(source)
    request = Request(
        os.getenv("PV_PEOPLE_URL", "http://pv-people:8080/analyse"),
        data=source.read_bytes(),
        method="POST",
        headers={
            "Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "X-PV-People-References": _references_header(references),
            "X-PV-Face-Detection": json.dumps(face_evidence),
        },
    )
    try:
        with urlopen(request, timeout=180) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Local People recognition service is unavailable") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), dict) or not isinstance(payload.get("faces"), dict):
        raise RuntimeError("Local People recognition service returned invalid evidence")
    return payload


def persist_people_evidence(
    people_store,
    asset_id: UUID,
    owner: UUID | str,
    payload: dict[str, object],
    producing_job_id: UUID | None = None,
) -> tuple[int, int]:
    """Persist raw specialist evidence and only publish service-known matches.

    A face without a verified known Person remains ``unknown`` evidence.  A
    YOLOX box is never converted into a Person association.
    """
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    faces = payload.get("faces") if isinstance(payload.get("faces"), dict) else {}
    task_version = str(payload.get("task_version") or PEOPLE_TASK_VERSION)
    body_count = face_count = 0
    for box in body.get("boxes", []) if isinstance(body.get("boxes"), list) else []:
        if not isinstance(box, dict) or not isinstance(box.get("box"), dict):
            continue
        people_store.add_person_detection(
            asset_id,
            producing_job_id=producing_job_id,
            bounding_box=box["box"],
            detector_provider=str(body.get("provider") or "yolox"),
            detector_model=str(body.get("model") or "yolox-tiny"),
            detector_revision=body.get("revision"),
            task_version=task_version,
            raw_evidence=box,
        )
        body_count += 1
    for face in faces.get("boxes", []) if isinstance(faces.get("boxes"), list) else []:
        if not isinstance(face, dict) or not isinstance(face.get("box"), dict):
            continue
        embedding_b64 = face.get("embedding_b64")
        try:
            embedding = base64.b64decode(embedding_b64, validate=True) if isinstance(embedding_b64, str) else None
        except ValueError:
            embedding = None
        candidate = face.get("candidate_person_id")
        known_person = None
        if face.get("recognition_result") == "known" and isinstance(candidate, str):
            try:
                possible = UUID(candidate)
                if people_store.get_person(possible, owner) is not None:
                    known_person = possible
            except ValueError:
                pass
        detection_id = people_store.add_face_detection(
            asset_id,
            producing_job_id=producing_job_id,
            bounding_box=face["box"],
            detector_provider=str(faces.get("provider") or "yunet"),
            detector_model=str(faces.get("model") or "face_detection_yunet_2023mar"),
            detector_revision=faces.get("revision"),
            task_version=task_version,
            embedding=embedding,
            embedding_dimension=face.get("embedding_dimension"),
            embedding_model=faces.get("embedding_model") or "facenet512",
            embedding_revision=faces.get("embedding_revision"),
            recognition_candidate_person_id=known_person,
            native_distance=face.get("native_distance"),
            recognition_result="known" if known_person else "unknown",
            raw_evidence={key: value for key, value in face.items() if key != "embedding_b64"},
        )
        # An active exclusion is retained by effective_people; preserving this
        # association separately maintains audit evidence without overriding it.
        if known_person is not None:
            people_store.associate(asset_id, known_person, "vault_master", detection_id)
        face_count += 1
    return body_count, face_count
