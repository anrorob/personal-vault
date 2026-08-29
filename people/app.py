"""Internal Stage B evidence service: bodies, faces, and face matching only."""
import base64
import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
from time import monotonic

import cv2
import numpy as np
import torch
from deepface import DeepFace
from deepface.modules import verification
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, ImageOps, UnidentifiedImageError
from yolox.data.data_augment import ValTransform
from yolox.exp import get_exp
from yolox.utils import postprocess

MODEL_ROOT = Path(os.getenv("PV_PEOPLE_MODEL_ROOT", "/models/people"))
CACHE_ROOT = Path(os.getenv("PV_PEOPLE_CACHE_ROOT", "/tmp/deepface"))
YOLOX_CHECKPOINT = MODEL_ROOT / "yolox_tiny.pth"
FACENET512_WEIGHTS = MODEL_ROOT / "facenet512_weights.h5"
YUNET_MODEL = MODEL_ROOT / "face_detection_yunet_2023mar.onnx"
MAX_IMAGE_BYTES = 50 * 1024 * 1024
TASK_VERSION = "gallery-people-v2"
app = FastAPI(title="Vault Master People", docs_url=None, redoc_url=None)
yolox_model = None
analysis_lock = asyncio.Lock()


def _require_models() -> None:
    missing = [str(path) for path in (YOLOX_CHECKPOINT, FACENET512_WEIGHTS, YUNET_MODEL) if not path.is_file()]
    if missing:
        raise RuntimeError("People model files are absent: " + ", ".join(missing))


def _install_deepface_weight_link() -> None:
    """Provide the pre-provisioned weight at DeepFace's documented cache path.

    The source models mount remains read-only; only the disposable /tmp link is
    created.  This prevents DeepFace from attempting a network download.
    """
    os.environ["DEEPFACE_HOME"] = str(CACHE_ROOT)
    weights = CACHE_ROOT / ".deepface" / "weights"
    weights.mkdir(parents=True, exist_ok=True)
    target = weights / "facenet512_weights.h5"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(FACENET512_WEIGHTS)


@app.on_event("startup")
def load_models() -> None:
    global yolox_model
    _require_models()
    _install_deepface_weight_link()
    exp = get_exp(exp_name="yolox-tiny")
    yolox_model = exp.get_model()
    checkpoint = torch.load(YOLOX_CHECKPOINT, map_location="cpu", weights_only=False)
    yolox_model.load_state_dict(checkpoint["model"])
    yolox_model.eval()
    DeepFace.build_model("Facenet512")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "body_model": "yolox-tiny", "face_detector": "mediapipe-external", "diagnostic_face_detector": "yunet", "embedding_model": "facenet512", "task_version": TASK_VERSION}


def _parse_references(request: Request) -> list[dict[str, object]]:
    raw = request.headers.get("X-PV-People-References", "[]")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(400, "Invalid reference evidence") from error
    return values if isinstance(values, list) else []


def _parse_mediapipe_faces(request: Request) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Accept only the internal detector's native face evidence.

    YuNet remains provisioned for diagnostics, but is deliberately not a
    normal-recognition fallback. A missing detector payload is therefore an
    error rather than an implicit switch to another detector.
    """
    raw = request.headers.get("X-PV-Face-Detection")
    if not raw:
        raise HTTPException(400, "MediaPipe face detection evidence is required")
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(400, "Invalid MediaPipe face detection evidence") from error
    if not isinstance(evidence, dict) or evidence.get("provider") != "mediapipe":
        raise HTTPException(400, "Invalid MediaPipe face detector provider")
    boxes = evidence.get("boxes")
    if not isinstance(boxes, list):
        raise HTTPException(400, "Invalid MediaPipe face detector boxes")
    valid = []
    for value in boxes:
        box = value.get("box") if isinstance(value, dict) else None
        if not isinstance(box, dict):
            continue
        try:
            if int(box["w"]) > 0 and int(box["h"]) > 0:
                valid.append(value)
        except (KeyError, TypeError, ValueError):
            continue
    return valid, evidence


def _known_match(embedding: list[float], references: list[dict[str, object]]) -> tuple[str | None, float | None]:
    """Use the model family's native FaceNet512 cosine acceptance behaviour."""
    threshold = verification.find_threshold("Facenet512", "cosine")
    selected_id, selected_distance = None, None
    vector = np.asarray(embedding, dtype=np.float32)
    for reference in references:
        try:
            reference_id = str(reference["person_id"])
            raw = base64.b64decode(str(reference["embedding_b64"]), validate=True)
            other = np.frombuffer(raw, dtype=np.float32)
            distance = float(1 - np.dot(vector, other) / (np.linalg.norm(vector) * np.linalg.norm(other)))
        except (KeyError, ValueError, TypeError):
            continue
        if selected_distance is None or distance < selected_distance:
            selected_id, selected_distance = reference_id, distance
    return (selected_id, selected_distance) if selected_distance is not None and selected_distance <= threshold else (None, selected_distance)


@app.post("/analyse")
async def analyse(request: Request) -> dict[str, object]:
    # A single CPU-bound model pass at a time keeps the initial 4 GiB service
    # within its approved operating envelope even if two jobs are requested.
    async with analysis_lock:
        return await _analyse_one(request)


async def _analyse_one(request: Request) -> dict[str, object]:
    body = await request.body()
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image is empty or too large")
    try:
        with Image.open(BytesIO(body)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise HTTPException(422, "Invalid image") from error
    started = monotonic()
    rgb = np.asarray(image)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    height, width = bgr.shape[:2]
    exp = get_exp(exp_name="yolox-tiny")
    ratio = min(exp.test_size[0] / height, exp.test_size[1] / width)
    tensor, _ = ValTransform(legacy=False)(bgr, None, exp.test_size)
    with torch.no_grad():
        raw = postprocess(yolox_model(torch.from_numpy(tensor).unsqueeze(0).float()), exp.num_classes, 0.001, exp.nmsthre)[0]
    bodies = []
    if raw is not None:
        for detection in raw.cpu().numpy():
            if int(detection[6]) == 0:
                bodies.append({"box": {"x": round(float(detection[0]) / ratio), "y": round(float(detection[1]) / ratio), "w": round(float(detection[2] - detection[0]) / ratio), "h": round(float(detection[3] - detection[1]) / ratio)}, "native_score": float(detection[4] * detection[5])})
    faces, face_evidence = _parse_mediapipe_faces(request)
    references = _parse_references(request)
    face_rows = []
    for face in faces:
        box = face["box"]
        x, y, face_width, face_height = (int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"]))
        crop = rgb[max(0, y):min(height, y + face_height), max(0, x):min(width, x + face_width)]
        if crop.size == 0:
            continue
        embedding = DeepFace.represent(crop, model_name="Facenet512", detector_backend="skip", enforce_detection=False)[0]["embedding"]
        candidate, distance = _known_match(embedding, references)
        face_rows.append({"box": {"x": x, "y": y, "w": face_width, "h": face_height}, "embedding_b64": base64.b64encode(np.asarray(embedding, dtype=np.float32).tobytes()).decode("ascii"), "embedding_dimension": len(embedding), "native_score": face.get("native_score"), "candidate_person_id": candidate, "native_distance": distance, "recognition_result": "known" if candidate else "unknown"})
    return {"task_version": TASK_VERSION, "body": {"provider": "yolox", "model": "yolox-tiny", "boxes": bodies}, "faces": {"provider": "mediapipe", "model": str(face_evidence.get("model") or "face_detection_full_range_sparse"), "revision": face_evidence.get("model_revision"), "task_version": face_evidence.get("task_version"), "embedding_model": "facenet512", "boxes": face_rows}, "processing_ms": round((monotonic() - started) * 1000)}
