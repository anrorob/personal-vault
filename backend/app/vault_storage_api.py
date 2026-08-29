from fastapi import APIRouter, Depends, Response

from app.auth import AuthenticatedAdministrator, require_vault_control_elevated_administrator
from app.vault_storage_control import StorageOperation, get_add_drive_context, get_inventory, get_operations, get_retire_preflight, get_swap_context, queue_operation, reconcile_completed_operation

router = APIRouter(prefix="/api/vault-control/storage", tags=["vault-control-storage"], dependencies=[Depends(require_vault_control_elevated_administrator)])


@router.get("")
def storage_inventory(response: Response, _: AuthenticatedAdministrator) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return get_inventory()


@router.post("/operations")
def storage_operation(body: StorageOperation, username: AuthenticatedAdministrator) -> dict[str, str]:
    return queue_operation(body, username)


@router.get("/add-drive")
def add_drive_context(response: Response, _: AuthenticatedAdministrator) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return get_add_drive_context()


@router.get("/swap/{slot_id}")
def swap_context(slot_id: str, response: Response, _: AuthenticatedAdministrator) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return get_swap_context(slot_id)


@router.get("/retire/{slot_id}")
def retire_preflight(slot_id: str, response: Response, _: AuthenticatedAdministrator) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return get_retire_preflight(slot_id)


@router.get("/operations")
def storage_operations(response: Response, _: AuthenticatedAdministrator) -> list[dict[str, object]]:
    response.headers["Cache-Control"] = "private, no-store"
    return get_operations()


@router.post("/operations/{operation_id}/reconcile")
def reconcile_storage_operation(operation_id: str, username: AuthenticatedAdministrator) -> dict[str, object]:
    return reconcile_completed_operation(operation_id, username)
