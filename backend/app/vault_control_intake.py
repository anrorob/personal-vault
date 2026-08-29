"""Stage 3 administrative view over the existing persistent Intake store."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import AuthenticatedAdministrator, require_vault_control_elevated_administrator
from app.config import get_admin_username
from app.vault_master import get_vault_master_store
from app.vault_master_autopilot import (
    audit_recent_gallery_screenshots,
    get_autopilot_store,
)
from app.vault_master_ingestion_ai import get_ingestion_ai_store
from app.vault_master_intake import IntakeStore, get_intake_store


router = APIRouter(prefix="/api/vault-control/intake", tags=["vault-control-intake"], dependencies=[Depends(require_vault_control_elevated_administrator)])



def _receipt(receipt: object) -> dict[str, object]:
    return dict(receipt.__dict__)  # dataclass-backed existing record


def get_gallery_screenshot_findings() -> list[object]:
    return audit_recent_gallery_screenshots(
        get_autopilot_store(), get_ingestion_ai_store(), get_vault_master_store(), get_admin_username()
    )


@router.get("")
def get_intake(response: Response, username: AuthenticatedAdministrator, limit: int = 20,
               store: IntakeStore = Depends(get_intake_store), findings: list[object] = Depends(get_gallery_screenshot_findings)) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    if limit not in {20, 50}:
        raise HTTPException(status_code=422, detail="Receipt limit must be 20 or 50")
    receipts = [_receipt(item) for item in store.list_receipts(username, limit=limit)]
    sources = [dict(item.__dict__) for item in store.list_sources(username)]
    active = [item for item in receipts if item["status"] == "reserved"]
    failed = [item for item in receipts if item["status"] in {"failed", "rejected"}]
    return {
        "gate": store.gate_status(),
        "sources": sources,
        "arrival_hall": {"waiting": store.pending_count(), "processing": len(active), "needs_review": None},
        "processing": active,
        "failed": failed,
        "receipts": receipts,
        "audits": [{"name": "Gallery Screenshot Audit", "status": "attention" if findings else "clear", "findings": len(findings)}],
    }


@router.post("/gate/{action}")
def update_gate(action: str, username: AuthenticatedAdministrator, store: IntakeStore = Depends(get_intake_store)) -> dict[str, int | str]:
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=422, detail="Unsupported intake gate action")
    return store.request_gate(action)
