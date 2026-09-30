"""再生水班次读数登记 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService
from app.core.errors import PermissionDeniedError
from app.water.schemas import (
    BatchImport, FacilityCreate, MeterPointsUpsert, ReviewResolution, RevisionRequest,
    RollbackRequest, ShiftRegistration,
)
from app.water.service import WaterService

router = APIRouter(prefix="/api/water", tags=["再生水班次水量"])


def require(permission: str):
    def guard(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(permission):
            # 权限拒绝同样留痕，避免越权探测被静默吞掉。
            AuditService(get_connection()).record(
                AuditContext(principal.user_id, principal.display_name),
                action=f"water.access_denied:{permission}", resource_type="water",
                outcome="denied", metadata={"required_permission": permission},
            )
            raise PermissionDeniedError(f"缺少权限：{permission}")
        return principal

    return guard


def context(principal: Principal) -> AuditContext:
    return AuditContext(principal.user_id, principal.display_name)


@router.post("/facilities", status_code=201)
def create_facility(payload: FacilityCreate, principal: Principal = Depends(require("water.write"))):
    with transaction(immediate=True) as connection:
        return WaterService(connection).create_facility(payload.model_dump(), context(principal))


@router.get("/facilities")
def list_facilities(principal: Principal = Depends(require("water.read"))):
    return {"items": WaterService(get_connection()).list_facilities()}


@router.put("/facilities/{facility_code}/meter-points")
def upsert_meter_points(facility_code: str, payload: MeterPointsUpsert,
                        principal: Principal = Depends(require("water.write"))):
    with transaction(immediate=True) as connection:
        return WaterService(connection).upsert_meter_points(facility_code, payload.model_dump(), context(principal))


@router.post("/shifts", status_code=201)
def register_shift(payload: ShiftRegistration, principal: Principal = Depends(require("water.write"))):
    data = payload.model_dump()
    idempotency_key = data.pop("idempotency_key")
    with transaction(immediate=True) as connection:
        return WaterService(connection).register_shift(
            data, context(principal), idempotency_key=idempotency_key,
        )


@router.get("/shifts")
def list_shifts(facility_code: str | None = None, limit: int = Query(100, ge=1, le=500),
                principal: Principal = Depends(require("water.read"))):
    return WaterService(get_connection()).list_shifts(facility_code, limit)


@router.get("/shifts/{shift_id}")
def get_shift(shift_id: int, version: int | None = Query(default=None, ge=1),
              principal: Principal = Depends(require("water.read"))):
    return WaterService(get_connection()).shift_detail(shift_id, version)


@router.get("/shifts/{shift_id}/diff")
def shift_diff(shift_id: int, from_version: int = Query(..., ge=1), to_version: int = Query(..., ge=1),
               principal: Principal = Depends(require("water.read"))):
    return WaterService(get_connection()).shift_diff(shift_id, from_version, to_version)


@router.post("/shifts/{shift_id}/revisions", status_code=201)
def revise_shift(shift_id: int, payload: RevisionRequest,
                 principal: Principal = Depends(require("water.write"))):
    with transaction(immediate=True) as connection:
        return WaterService(connection).revise_shift(shift_id, payload.model_dump(), context(principal))


@router.post("/shifts/{shift_id}/rollback")
def rollback_shift(shift_id: int, payload: RollbackRequest,
                   principal: Principal = Depends(require("water.write"))):
    with transaction(immediate=True) as connection:
        return WaterService(connection).rollback_shift(shift_id, payload.model_dump(), context(principal))


@router.get("/reviews/pending")
def pending_reviews(facility_code: str | None = None, principal: Principal = Depends(require("water.review"))):
    return WaterService(get_connection()).pending_reviews(facility_code)


@router.post("/reviews/{reading_id}/resolution")
def resolve_review(reading_id: int, payload: ReviewResolution,
                   principal: Principal = Depends(require("water.review"))):
    with transaction(immediate=True) as connection:
        return WaterService(connection).resolve_review(reading_id, payload.model_dump(), context(principal))


@router.post("/batches/import", status_code=201)
def batch_import(payload: BatchImport, principal: Principal = Depends(require("water.write"))):
    with transaction(immediate=True) as connection:
        return WaterService(connection).batch_import(payload.model_dump(), context(principal))


@router.get("/batches")
def list_batches(principal: Principal = Depends(require("water.read"))):
    return WaterService(get_connection()).list_batches()


@router.get("/batches/{batch_key}/summary")
def batch_summary(batch_key: str, principal: Principal = Depends(require("water.read"))):
    return WaterService(get_connection()).batch_summary(batch_key)
