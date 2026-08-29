import asyncio
from io import BytesIO
import os
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from transformers import AutoProcessor

from ov_florence2_helper import OVFlorence2Model


MODEL_ID = "microsoft/Florence-2-large"
MODEL_REVISION = "21a599d414c4d928c9032694c424fb94458e3594"
TASK_VERSION = "gallery-ocr-v1"
ANALYSIS_TASK_VERSION = "arrival-image-analysis-v1"
MODEL_PATH = Path(
    os.getenv("PV_FLORENCE_MODEL_PATH", "/models/florence2/openvino")
)
DEVICE = os.getenv("PV_FLORENCE_DEVICE", "CPU").upper()
MAX_IMAGE_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Vault Master Florence-2", docs_url=None, redoc_url=None)
model = None
processor = None
active_requests = 0


def run_task(image: Image.Image, task: str, max_new_tokens: int) -> str:
    inputs = processor(text=task, images=image, return_tensors="pt")
    generated_ids = model.generate(
        input_ids=inputs["input_ids"],
        pixel_values=inputs["pixel_values"],
        max_new_tokens=max_new_tokens,
        num_beams=3,
        do_sample=False,
    )
    generated = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
    result = processor.post_process_generation(
        generated, task=task, image_size=image.size
    )
    value = result.get(task, "")
    return value.strip() if isinstance(value, str) else str(value).strip()


async def read_image(request: Request) -> Image.Image:
    body = await request.body()
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is empty or too large")
    try:
        return Image.open(BytesIO(body)).convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise HTTPException(status_code=422, detail="Invalid image") from error


@app.on_event("startup")
def load_model() -> None:
    global model, processor
    if DEVICE not in {"CPU", "GPU"}:
        raise RuntimeError("PV_FLORENCE_DEVICE must be CPU or GPU")
    if not MODEL_PATH.is_dir():
        raise RuntimeError(f"Converted model is absent: {MODEL_PATH}")
    model = OVFlorence2Model(MODEL_PATH, device=DEVICE)
    processor = AutoProcessor.from_pretrained(
        MODEL_PATH, trust_remote_code=True, local_files_only=True
    )


@app.get("/health")
def health() -> dict[str, str | int]:
    return {
        "status": "ok",
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "device": DEVICE,
        "active_requests": active_requests,
    }


@app.post("/ocr")
async def ocr(request: Request) -> dict[str, object]:
    image = await read_image(request)
    started = monotonic()
    global active_requests
    active_requests += 1
    try:
        text = await asyncio.to_thread(run_task, image, "<OCR>", 1024)
    finally:
        active_requests -= 1
    return {
        "text": text,
        "confidence": None,
        "processing_ms": round((monotonic() - started) * 1000),
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "task_version": TASK_VERSION,
        "device": DEVICE,
    }


@app.post("/analyse")
async def analyse(request: Request) -> dict[str, object]:
    image = await read_image(request)
    started = monotonic()
    global active_requests
    active_requests += 1
    try:
        caption = await asyncio.to_thread(run_task, image, "<MORE_DETAILED_CAPTION>", 512)
        text = await asyncio.to_thread(run_task, image, "<OCR>", 1024)
    finally:
        active_requests -= 1
    return {
        "caption": caption,
        "text": text,
        "processing_ms": round((monotonic() - started) * 1000),
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "task_version": ANALYSIS_TASK_VERSION,
        "device": DEVICE,
    }
