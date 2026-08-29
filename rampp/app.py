"""Local-only RAM++ evidence service for Vault Master Gallery Intelligence."""
from io import BytesIO
import os
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from ram import get_transform, inference_ram
from ram.models import ram_plus

MODEL = "ram_plus_swin_large_14m"
CHECKPOINT = Path(os.getenv("PV_RAMPP_MODEL_PATH", "/models/rampp/ram_plus_swin_large_14m.pth"))
CHECKPOINT_SHA256 = os.getenv("PV_RAMPP_CHECKPOINT_SHA256", "497c178836ba66698ca226c7895317e6e800034be986452dbd2593298d50e87d")
MAX_IMAGE_BYTES = 50 * 1024 * 1024
app = FastAPI(title="Vault Master RAM++", docs_url=None, redoc_url=None)
model = None
transform = None

@app.on_event("startup")
def load_model() -> None:
    global model, transform
    if not CHECKPOINT.is_file(): raise RuntimeError(f"RAM++ checkpoint is absent: {CHECKPOINT}")
    model = ram_plus(pretrained=str(CHECKPOINT), image_size=384, vit="swin_l").eval()
    transform = get_transform(image_size=384)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL, "checkpoint_sha256": CHECKPOINT_SHA256, "license": "Apache-2.0"}

@app.post("/tag")
async def tag(request: Request) -> dict[str, object]:
    body = await request.body()
    if not body or len(body) > MAX_IMAGE_BYTES: raise HTTPException(413, "Image is empty or too large")
    try: image = Image.open(BytesIO(body)).convert("RGB")
    except (UnidentifiedImageError, OSError) as error: raise HTTPException(422, "Invalid image") from error
    started = monotonic()
    tags, _ = inference_ram(transform(image).unsqueeze(0), model)
    return {"tags": [tag.strip() for tag in tags.split("|") if tag.strip()], "processing_ms": round((monotonic()-started)*1000), "model": MODEL, "checkpoint_sha256": CHECKPOINT_SHA256, "task_version": "gallery-intelligence-rampp-v1"}
