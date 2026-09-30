from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.errors import PermissionDeniedError
from app.core.security import Principal
from app.database import get_connection
from app.services.audit import AuditContext, AuditService
from app.water.schemas import (
    BatchClose,
    BatchCreate,
    FacilityCreate,
    ImportRequest,
    ShiftAmend,
    ShiftRegister,
    ShiftReview,
    ShiftRollback,
)
from app.water.service import WaterLedgerService

router = APIRouter(prefix="/api/water", tags=["再生水班次台账"])


def service() -> WaterLedgerService:
    return WaterLedgerService()


def actor_of(principal: Principal) -> AuditContext:
    return AuditContext(principal.user_id, principal.display_name)


def require_water(permission: str):
    """权限守卫：拒绝时写入 outcome=denied 的审计事件，而不是静默丢弃。"""

    def guard(principal: Principal = Depends(current_principal)) -> None:
        if principal.can(permission):
            return
        AuditService(get_connection()).record(
            AuditContext(principal.user_id, principal.display_name),
            action="water.access_denied",
            resource_type="water",
            resource_id=permission,
            outcome="denied",
            metadata={"required_permission": permission},
        )
        raise PermissionDeniedError(f"缺少权限：{permission}")

    return guard


@router.post("/facilities", status_code=201)
def create_facility(
    payload: FacilityCreate,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.write")),
) -> dict:
    return service().create_facility(payload.model_dump(), actor_of(principal))


@router.get("/facilities")
def list_facilities(
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.read")),
) -> dict:
    return {"items": service().list_facilities()}


@router.post("/shifts", status_code=201)
def register_shift(
    payload: ShiftRegister,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.write")),
) -> dict:
    return service().register(payload.model_dump(), actor_of(principal))


@router.get("/shifts")
def list_shifts(
    facility_code: str | None = None,
    batch_code: str | None = None,
    status: str | None = Query(default=None, pattern="^(draft|confirmed|anomaly|anomaly_reviewed|corrected)$"),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.read")),
) -> dict:
    items = service().list_entries(facility_code=facility_code, batch_code=batch_code, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/shifts/{entry_id}")
def get_shift(
    entry_id: int,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.read")),
) -> dict:
    return service().detail(entry_id)


@router.post("/shifts/{entry_id}/amend")
def amend_shift(
    entry_id: int,
    payload: ShiftAmend,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.write")),
) -> dict:
    return service().amend(entry_id, payload.model_dump(), actor_of(principal))


@router.post("/shifts/{entry_id}/review")
def review_shift(
    entry_id: int,
    payload: ShiftReview,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.review")),
) -> dict:
    return service().review(entry_id, payload.model_dump(), actor_of(principal))


@router.post("/shifts/{entry_id}/rollback")
def rollback_shift(
    entry_id: int,
    payload: ShiftRollback,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.review")),
) -> dict:
    return service().rollback(entry_id, payload.model_dump(), actor_of(principal))


@router.get("/shifts/{entry_id}/versions/{version}")
def get_shift_version(
    entry_id: int,
    version: int,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.read")),
) -> dict:
    return service().version_detail(entry_id, version)


@router.get("/shifts/{entry_id}/diff")
def diff_shift(
    entry_id: int,
    from_version: int = Query(..., ge=1),
    to_version: int = Query(..., ge=1),
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.read")),
) -> dict:
    return service().diff_versions(entry_id, from_version, to_version)


@router.post("/batches", status_code=201)
def create_batch(
    payload: BatchCreate,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.write")),
) -> dict:
    return service().create_batch(payload.model_dump(), actor_of(principal))


@router.get("/batches/{batch_code}/summary")
def batch_summary(
    batch_code: str,
    facility_code: str | None = None,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.read")),
) -> dict:
    return service().batch_summary(batch_code, facility_code=facility_code)


@router.post("/batches/{batch_code}/close")
def close_batch(
    batch_code: str,
    payload: BatchClose,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.review")),
) -> dict:
    return service().close_batch(batch_code, payload.model_dump(), actor_of(principal))


@router.post("/imports", status_code=201)
def import_entries(
    payload: ImportRequest,
    principal: Principal = Depends(current_principal),
    _guard: None = Depends(require_water("water.write")),
) -> dict:
    return service().import_entries(payload.model_dump(), actor_of(principal))
