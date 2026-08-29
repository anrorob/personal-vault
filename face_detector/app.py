"""Internal MediaPipe face-box service; it never performs recognition."""
import asyncio
import hashlib
import os
from io import BytesIO
from pathlib import Path
from time import monotonic

import mediapipe as mp
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, ImageOps, UnidentifiedImageError

MODEL_ROOT = Path(os.getenv("PV_FACE_DETECTOR_MODEL_ROOT", "/models/face-detector"))
MODEL_ASSET = MODEL_ROOT / "face_detection_full_range_sparse.tflite"
PACKAGE_ASSET = Path(mp.__file__).parent / "modules" / "face_detection" / "face_detection_full_range_sparse.tflite"
MAX_IMAGE_BYTES = 50 * 1024 * 1024
TASK_VERSION = "gallery-face-detection-mediapipe-v1"
app = FastAPI(title="Vault Master Face Detector", docs_url=None, redoc_url=None)
analysis_lock = asyncio.Lock()
model_sha256 = ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@app.on_event("startup")
def verify_model_asset() -> None:
    """Fail closed unless the read-only provisioned asset overlays MediaPipe's asset."""
    global model_sha256
    if not MODEL_ASSET.is_file() or not PACKAGE_ASSET.is_file():
        raise RuntimeError("MediaPipe full-range face detector asset is absent")
    provisioned = _sha256(MODEL_ASSET)
    if provisioned != _sha256(PACKAGE_ASSET):
        raise RuntimeError("Provisioned MediaPipe asset does not match the mounted runtime asset")
    model_sha256 = provisioned


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "provider": "mediapipe",
        "model": "face_detection_full_range_sparse",
        "mediapipe_version": mp.__version__,
        "model_sha256": model_sha256,
        "task_version": TASK_VERSION,
    }


@app.post("/detect")
async def detect(request: Request) -> dict[str, object]:
    """Return only native MediaPipe face boxes for an image byte stream."""
    async with analysis_lock:
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
        height, width = rgb.shape[:2]
        with mp.solutions.face_detection.FaceDetection(model_selection=1) as detector:
            result = detector.process(rgb)
        boxes = []
        for detection in result.detections or []:
            box = detection.location_data.relative_bounding_box
            boxes.append({
                "box": {
                    "x": round(float(box.xmin * width)),
                    "y": round(float(box.ymin * height)),
                    "w": round(float(box.width * width)),
                    "h": round(float(box.height * height)),
                },
                "native_score": float(detection.score[0]),
            })
        return {
            "task_version": TASK_VERSION,
            "provider": "mediapipe",
            "model": "face_detection_full_range_sparse",
            "model_revision": mp.__version__,
            "model_sha256": model_sha256,
            "boxes": boxes,
            "processing_ms": round((monotonic() - started) * 1000),
        }
