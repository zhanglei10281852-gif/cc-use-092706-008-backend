"""再生水班次读数登记、修订、复核与批次汇总服务。

调用方负责开启事务（与 IdentityService 的分层约定一致）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.services.audit import AuditContext, AuditService
from app.water import calc
from app.water.store import WaterRepository

# 抄表时刻与班次起止、相邻班次交接允许的钟差。
TIME_TOLERANCE = timedelta(seconds=1)


def payload_digest(payload: Any) -> str:
    """对含 date/datetime 的请求体生成稳定指纹。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class WaterService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = WaterRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---- 设施与表计 -------------------------------------------------------

    def create_facility(self, payload: dict[str, Any], ctx: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        if self.repository.facility_by_code(payload["code"]):
            raise ConflictError("设施编码已存在")
        facility = self.repository.create_facility(code=payload["code"], name=payload["name"], now=now)
        self.audit.record(ctx, action="water.facility.create", resource_type="water_facility",
                          resource_id=facility["id"], after=facility)
        return facility

    def list_facilities(self) -> list[dict[str, Any]]:
        return self.repository.list_facilities()

    def upsert_meter_points(self, facility_code: str, payload: dict[str, Any], ctx: AuditContext) -> dict[str, Any]:
        facility = self._require_facility(facility_code)
        existing = {point["code"]: point for point in self.repository.meter_points(facility["id"])}
        for point in payload["points"]:
            old = existing.get(point["code"])
            if old is not None and (old["metric"] != point["metric"] or old["unit"] != point["unit"]):
                raise ConflictError(
                    f"表计 {point['code']} 已登记为 {old['metric']}/{old['unit']}，不能变更单位或指标",
                    context={"point_code": point["code"], "existing_metric": old["metric"],
                             "existing_unit": old["unit"]},
                )
        now = to_storage(self.clock.now())
        before = self.repository.meter_points(facility["id"])
        self.repository.upsert_meter_points(facility["id"], payload["points"], now=now)
        after = self.repository.meter_points(facility["id"])
        self.audit.record(ctx, action="water.meter_points.upsert", resource_type="water_facility",
                          resource_id=facility["id"], before={"points": before}, after={"points": after})
        return {"facility_code": facility_code, "meter_points": after}

    # ---- 班次登记 ---------------------------------------------------------

    def register_shift(self, entry: dict[str, Any], ctx: AuditContext, *, batch_key: str = "",
                       idempotency_key: str | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        facility = self._require_facility(entry["facility_code"])
        shift_date = entry["shift_date"].isoformat()
        shift_seq = int(entry["shift_seq"])

        if idempotency_key:
            existing = self.repository.shift_by_idempotency(idempotency_key)
            if existing is not None:
                if (int(existing["facility_id"]) != int(facility["id"])
                        or existing["shift_date"] != shift_date
                        or int(existing["shift_seq"]) != shift_seq):
                    raise ConflictError("同一幂等键对应了不同的设施班次")
                return self.shift_detail(int(existing["id"]))
        if self.repository.shift_by_key(facility["id"], shift_date, shift_seq):
            raise ConflictError("该设施班次已登记，差异说明请使用补录修订接口")

        points = self.repository.point_map(facility["id"])
        self._validate_readings_against_points(entry["readings"], points)
        shift_flags = self._shift_sequence_flags(facility["id"], entry, shift_date, shift_seq)

        registration_id = self.repository.insert_shift(
            facility_id=facility["id"], shift_date=shift_date, shift_seq=shift_seq,
            starts_at=to_storage(entry["starts_at"]), ends_at=to_storage(entry["ends_at"]),
            batch_key=batch_key, idempotency_key=idempotency_key,
            issue_flags=",".join(shift_flags), created_by=ctx.actor_name, now=now,
        )
        registration = self.repository.shift_by_id(registration_id)
        diff = self._write_version(
            registration, version=1, readings=entry["readings"], points=points,
            inherited=False, note=entry.get("reason", ""), actor=ctx.actor_name, now=now,
        )
        self.repository.add_event(registration_id=registration_id, action="register", from_version=None,
                                  to_version=1, actor=ctx.actor_name, reason=entry.get("reason", ""),
                                  detail={"batch_key": batch_key, "changes": diff}, now=now)
        self.audit.record(ctx, action="water.shift.register", resource_type="water_shift",
                          resource_id=registration_id,
                          after={"shift_date": shift_date, "shift_seq": shift_seq, "version": 1,
                                 "flags": shift_flags},
                          metadata={"batch_key": batch_key})
        return self.shift_detail(registration_id)

    def revise_shift(self, shift_id: int, payload: dict[str, Any], ctx: AuditContext) -> dict[str, Any]:
        registration = self._require_shift(shift_id)
        points = self.repository.point_map(int(registration["facility_id"]))
        self._validate_readings_against_points(payload["readings"], points)

        current_version = int(registration["current_version"])
        new_version = self.repository.max_version(shift_id) + 1
        now = to_storage(self.clock.now())
        diff = self._write_version(
            registration, version=new_version, readings=payload["readings"], points=points,
            inherited=True, note=payload["reason"], actor=ctx.actor_name, now=now,
        )
        if not diff:
            raise ValidationError("补录内容与当前版本完全一致，未形成新版本")
        self.repository.set_current_version(shift_id, new_version, now)
        self.repository.add_event(registration_id=shift_id, action="revise", from_version=current_version,
                                  to_version=new_version, actor=ctx.actor_name, reason=payload["reason"],
                                  detail={"changes": diff}, now=now)
        self.audit.record(ctx, action="water.shift.revise", resource_type="water_shift", resource_id=shift_id,
                          before={"version": current_version}, after={"version": new_version},
                          metadata={"changes": diff, "reason": payload["reason"]})
        return self.shift_detail(shift_id)

    def rollback_shift(self, shift_id: int, payload: dict[str, Any], ctx: AuditContext) -> dict[str, Any]:
        registration = self._require_shift(shift_id)
        target = int(payload["version"])
        current_version = int(registration["current_version"])
        if not self.repository.version_exists(shift_id, target):
            raise NotFoundError("目标版本不存在")
        if target == current_version:
            raise ConflictError("当前版本已经是目标版本，无需回滚")
        now = to_storage(self.clock.now())
        self.repository.set_current_version(shift_id, target, now)
        self.repository.add_event(registration_id=shift_id, action="rollback", from_version=current_version,
                                  to_version=target, actor=ctx.actor_name, reason=payload["reason"],
                                  detail={}, now=now)
        self.audit.record(ctx, action="water.shift.rollback", resource_type="water_shift", resource_id=shift_id,
                          before={"version": current_version}, after={"version": target},
                          metadata={"reason": payload["reason"]})
        return self.shift_detail(shift_id)

    def shift_detail(self, shift_id: int, version: int | None = None) -> dict[str, Any]:
        registration = self._require_shift(shift_id)
        current_version = int(registration["current_version"])
        target_version = current_version if version is None else version
        if not self.repository.version_exists(shift_id, target_version):
            raise NotFoundError("班次版本不存在")
        detail = self._compute_shift(registration, target_version)
        detail["events"] = self.repository.events(shift_id)
        return detail

    def shift_diff(self, shift_id: int, from_version: int, to_version: int) -> dict[str, Any]:
        registration = self._require_shift(shift_id)
        for number in (from_version, to_version):
            if not self.repository.version_exists(shift_id, number):
                raise NotFoundError(f"版本 {number} 不存在")
        before = self._compute_shift(registration, from_version)
        after = self._compute_shift(registration, to_version)
        before_rows = {row["point_code"]: row for row in before["readings"]}
        after_rows = {row["point_code"]: row for row in after["readings"]}
        changes: list[dict[str, Any]] = []
        for code in sorted(set(before_rows) | set(after_rows)):
            old = before_rows.get(code)
            new = after_rows.get(code)
            if self._reading_fingerprint(old) != self._reading_fingerprint(new):
                changes.append({"point_code": code, "metric": (new or old)["metric"], "before": old, "after": new})
        missing_before = sorted(set(before["missing_points"]) - set(after["missing_points"]))
        return {
            "shift_id": shift_id,
            "facility_code": registration["facility_code"],
            "shift_date": registration["shift_date"],
            "shift_seq": int(registration["shift_seq"]),
            "from_version": from_version,
            "to_version": to_version,
            "reading_changes": changes,
            "closed_missing_points": missing_before,
            "metric_summary": {
                "before": {"volumes": before["volumes"], "recycle_rate": before["recycle_rate"], "loss": before["loss"]},
                "after": {"volumes": after["volumes"], "recycle_rate": after["recycle_rate"], "loss": after["loss"]},
            },
        }

    def list_shifts(self, facility_code: str | None, limit: int) -> dict[str, Any]:
        if facility_code is not None:
            self._require_facility(facility_code)
        return {"items": self.repository.list_shifts(facility_code, max(1, min(limit, 500)))}

    # ---- 复核 -------------------------------------------------------------

    def pending_reviews(self, facility_code: str | None) -> dict[str, Any]:
        if facility_code is not None:
            self._require_facility(facility_code)
        return {"items": self.repository.pending_readings(facility_code)}

    def resolve_review(self, reading_id: int, payload: dict[str, Any], ctx: AuditContext) -> dict[str, Any]:
        row = self.repository.reading_by_id(reading_id)
        if row is None:
            raise NotFoundError("读数不存在")
        if int(row["version"]) != int(row["current_version"]):
            raise ConflictError("该读数属于历史版本，请在当前版本处理复核")
        if row["review_status"] != "pending":
            raise ConflictError("该读数不在待复核状态")
        new_status = "resolved" if payload["action"] == "confirm" else "rejected"
        self.repository.resolve_reading(reading_id, new_status, payload["resolution"])
        now = to_storage(self.clock.now())
        self.repository.add_event(registration_id=int(row["registration_id"]), action="review",
                                  from_version=None, to_version=None, actor=ctx.actor_name,
                                  reason=payload["resolution"],
                                  detail={"reading_id": reading_id, "point_id": row["point_id"],
                                          "action": payload["action"], "from_status": "pending",
                                          "to_status": new_status}, now=now)
        self.audit.record(ctx, action="water.review.resolve", resource_type="water_reading",
                          resource_id=reading_id, before={"review_status": "pending"},
                          after={"review_status": new_status}, metadata={"resolution": payload["resolution"]})
        return {"reading_id": reading_id, "review_status": new_status}

    # ---- 批次导入与汇总 ----------------------------------------------------

    def batch_import(self, payload: dict[str, Any], ctx: AuditContext) -> dict[str, Any]:
        batch_key = payload["batch_key"]
        fingerprint = payload_digest(payload)
        existing = self.repository.import_run(batch_key)
        if existing is not None:
            if existing["request_digest"] != fingerprint:
                raise ConflictError("同一批次号对应了不同的导入内容")
            result = json.loads(existing["result_json"])
            result["replayed"] = True
            return result

        result: dict[str, Any] = {"batch_key": batch_key, "replayed": False, "shifts": []}
        # 登记顺序影响跨班次连续性校验，批次内统一按设施、日期、班次排序处理。
        ordered_entries = sorted(
            payload["entries"], key=lambda item: (item["facility_code"], item["shift_date"].isoformat(), int(item["shift_seq"]))
        )
        for entry in ordered_entries:
            detail = self.register_shift(entry, ctx, batch_key=batch_key)
            result["shifts"].append({
                "shift_id": detail["id"],
                "facility_code": detail["facility_code"],
                "shift_date": detail["shift_date"],
                "shift_seq": detail["shift_seq"],
                "current_version": detail["current_version"],
                "pending_readings": detail["pending_readings"],
                "issue_flags": detail["issue_flags"],
            })
        self.repository.save_import_run(batch_key=batch_key, request_digest=fingerprint, result=result,
                                        created_by=ctx.actor_name, now=to_storage(self.clock.now()))
        self.audit.record(ctx, action="water.batch.import", resource_type="water_batch",
                          resource_id=batch_key, after={"shifts": len(result["shifts"])})
        return result

    def batch_summary(self, batch_key: str) -> dict[str, Any]:
        run = self.repository.import_run(batch_key)
        shifts = self.repository.shifts_of_batch(batch_key)
        if not shifts:
            raise NotFoundError("批次不存在或没有任何班次")
        scope_ids = {int(row["id"]) for row in shifts}
        facility_rows = [
            self.connection.execute(
                "SELECT * FROM ww_facilities WHERE id=?", (facility_id,)
            ).fetchone()
            for facility_id in sorted({int(row["facility_id"]) for row in shifts})
        ]
        per_facility: list[dict[str, Any]] = []
        totals = {metric: {"low": 0.0, "high": 0.0, "point": 0.0, "bounded": True,
                           "known_shifts": 0, "missing_shifts": 0} for metric in calc.METRICS}
        for facility_row in facility_rows:
            summary = self._summarize_scope(int(facility_row["id"]), scope_ids)
            summary["facility_code"] = facility_row["code"]
            summary["facility_name"] = facility_row["name"]
            per_facility.append(summary)
            for metric in calc.METRICS:
                interval = summary["volumes"][metric]
                target = totals[metric]
                target["low"] += interval["low"]
                target["known_shifts"] += interval["known_shifts"]
                target["missing_shifts"] += interval["missing_shifts"]
                if interval["bounded"] and interval["high"] is not None and target["high"] is not None:
                    target["high"] += interval["high"]
                else:
                    target["high"] = None
                    target["bounded"] = False
                if interval["point"] is None:
                    target["point"] = None
                elif target["point"] is not None:
                    target["point"] += interval["point"]
        return {
            "batch_key": batch_key,
            "boundary_respected": True,
            "imported_at": run["created_at"] if run else None,
            "shift_count": len(shifts),
            "facility_count": len(facility_rows),
            "facilities": per_facility,
            "totals": {
                "volumes": totals,
                "recycle_rate": calc.recycle_interval(totals["influent"], totals["return_flow"]),
                "loss": calc.loss_interval(totals["influent"], totals["effluent"], totals["return_flow"]),
            },
            "pending_readings": self._pending_in_scope(scope_ids),
        }

    def list_batches(self) -> dict[str, Any]:
        return {"items": self.repository.batch_keys()}

    # ---- 内部辅助 ---------------------------------------------------------

    def _require_facility(self, code: str) -> sqlite3.Row:
        facility = self.repository.facility_by_code(code)
        if facility is None:
            raise NotFoundError(f"设施不存在：{code}")
        return facility

    def _require_shift(self, shift_id: int) -> sqlite3.Row:
        registration = self.repository.shift_by_id(shift_id)
        if registration is None:
            raise NotFoundError("班次登记不存在")
        return registration

    def _next_shift(self, facility_id: int, shift_date: str, shift_seq: int) -> sqlite3.Row | None:
        return self.repository.next_shift(facility_id, shift_date, shift_seq)

    @staticmethod
    def _validate_readings_against_points(readings: list[dict[str, Any]], points: dict[str, sqlite3.Row]) -> None:
        for reading in readings:
            point = points.get(reading["point_code"])
            if point is None:
                raise ValidationError(f"表计未在该设施登记：{reading['point_code']}")
            if reading["unit"] != point["unit"]:
                raise ValidationError(
                    f"表计 {reading['point_code']} 单位必须为 {point['unit']}",
                    context={"point_code": reading["point_code"], "expected_unit": point["unit"],
                             "actual_unit": reading["unit"]},
                )

    def _shift_sequence_flags(self, facility_id: int, entry: dict[str, Any], shift_date: str, shift_seq: int) -> list[str]:
        flags: list[str] = []
        if shift_seq == 1:
            expected_date = (entry["shift_date"] - timedelta(days=1)).isoformat()
            expected_seq = 3
        else:
            expected_date = shift_date
            expected_seq = shift_seq - 1
        any_earlier = self.connection.execute(
            "SELECT 1 FROM ww_shift_registrations WHERE facility_id=? "
            "AND (shift_date<? OR (shift_date=? AND shift_seq<?)) LIMIT 1",
            (facility_id, shift_date, shift_date, shift_seq),
        ).fetchone()
        previous = self.repository.previous_shift(facility_id, shift_date, shift_seq)
        if any_earlier and (
            previous is None
            or previous["shift_date"] != expected_date
            or int(previous["shift_seq"]) != expected_seq
        ):
            flags.append("sequence_gap")
        if previous is not None and previous["shift_date"] == expected_date and int(previous["shift_seq"]) == expected_seq:
            gap = (entry["starts_at"] - from_storage(previous["ends_at"])).total_seconds()
            if abs(gap) > TIME_TOLERANCE.total_seconds():
                flags.append("time_gap")
        return flags

    def _write_version(self, registration: sqlite3.Row, *, version: int, readings: list[dict[str, Any]],
                       points: dict[str, sqlite3.Row], inherited: bool, note: str,
                       actor: str, now: str) -> list[dict[str, Any]]:
        registration_id = int(registration["id"])
        facility_id = int(registration["facility_id"])

        previous_rows: dict[int, sqlite3.Row] = {}
        if inherited:
            for row in self.repository.readings_of_version(registration_id, int(registration["current_version"])):
                previous_rows[int(row["point_id"])] = row

        # 相邻班次（当前版本）读数用于计数器连续性比对。
        prev_shift = self.repository.previous_shift(facility_id, registration["shift_date"], int(registration["shift_seq"]))
        next_shift = self._next_shift(facility_id, registration["shift_date"], int(registration["shift_seq"]))
        prev_rows: dict[int, sqlite3.Row] = {}
        next_rows: dict[int, sqlite3.Row] = {}
        if prev_shift is not None:
            for row in self.repository.readings_of_version(int(prev_shift["id"]), int(prev_shift["current_version"])):
                prev_rows[int(row["point_id"])] = row
        if next_shift is not None:
            for row in self.repository.readings_of_version(int(next_shift["id"]), int(next_shift["current_version"])):
                next_rows[int(row["point_id"])] = row

        supplied = {item["point_code"]: item for item in readings}
        diff: list[dict[str, Any]] = []

        # 未在本次补录中出现的表计：从上一版本原样继承，形成完整且不可变的快照。
        for point_id, old in previous_rows.items():
            point = points.get(old["point_code"])
            if point is None or point["code"] in supplied:
                continue
            self.repository.insert_reading(
                registration_id=registration_id, version=version, point_id=point_id,
                start_value=old["start_value"], end_value=old["end_value"], unit=old["unit"],
                start_observed_at=old["start_observed_at"], end_observed_at=old["end_observed_at"],
                carried_over=True, carried_from_version=int(old["version"]),
                issue_flags=old["issue_flags"], review_status=old["review_status"],
                created_by=actor, now=now,
            )

        for code, item in supplied.items():
            point = points[code]
            point_id = int(point["id"])
            flags = self._reading_flags(item, prev_rows.get(point_id), next_rows.get(point_id), registration)
            # 异常读数进入待复核，绝不静默修正或丢弃。
            review_status = "pending" if flags else "ok"
            old_row = previous_rows.get(point_id)
            if inherited:
                new_view = {
                    "start_value": item["start_value"], "end_value": item["end_value"], "unit": item["unit"],
                    "start_observed_at": to_storage(item["start_observed_at"]) if item["start_observed_at"] else None,
                    "end_observed_at": to_storage(item["end_observed_at"]) if item["end_observed_at"] else None,
                }
                old_view = None if old_row is None else {
                    "start_value": old_row["start_value"], "end_value": old_row["end_value"], "unit": old_row["unit"],
                    "start_observed_at": old_row["start_observed_at"], "end_observed_at": old_row["end_observed_at"],
                }
                if old_view != new_view:
                    diff.append({"point_code": code, "metric": point["metric"], "before": old_view, "after": new_view})
            self.repository.insert_reading(
                registration_id=registration_id, version=version, point_id=point_id,
                start_value=item["start_value"], end_value=item["end_value"], unit=item["unit"],
                start_observed_at=to_storage(item["start_observed_at"]) if item["start_observed_at"] else None,
                end_observed_at=to_storage(item["end_observed_at"]) if item["end_observed_at"] else None,
                carried_over=False, carried_from_version=None,
                issue_flags=",".join(flags), review_status=review_status, created_by=actor, now=now,
            )

        digest = self._snapshot_digest(registration_id, version)
        self.repository.insert_version(registration_id=registration_id, version=version,
                                       snapshot_digest=digest, note=note, created_by=actor, now=now)
        return diff

    @staticmethod
    def _reading_flags(item: dict[str, Any], previous_point_row: sqlite3.Row | None,
                       next_point_row: sqlite3.Row | None, registration: sqlite3.Row) -> list[str]:
        flags: list[str] = []
        start, end = item["start_value"], item["end_value"]
        if (start is not None and start < 0) or (end is not None and end < 0):
            flags.append("negative_value")
        if start is not None and end is not None and end < start:
            flags.append("negative_delta")
        if start is not None and previous_point_row is not None and previous_point_row["end_value"] is not None:
            current_m3 = calc.to_cubic_meters(start, item["unit"])
            previous_m3 = calc.to_cubic_meters(previous_point_row["end_value"], previous_point_row["unit"])
            if not calc.continuity_ok(previous_m3, current_m3):
                flags.append("continuity_gap")
        if end is not None and next_point_row is not None and next_point_row["start_value"] is not None:
            current_m3 = calc.to_cubic_meters(end, item["unit"])
            next_m3 = calc.to_cubic_meters(next_point_row["start_value"], next_point_row["unit"])
            if not calc.continuity_ok(current_m3, next_m3):
                flags.append("continuity_gap")
        starts_at = from_storage(registration["starts_at"])
        ends_at = from_storage(registration["ends_at"])
        for observed in (item["start_observed_at"], item["end_observed_at"]):
            if observed is not None and (observed < starts_at - TIME_TOLERANCE or observed > ends_at + TIME_TOLERANCE):
                flags.append("observed_time_mismatch")
        if (item["start_observed_at"] is not None and item["end_observed_at"] is not None
                and item["end_observed_at"] < item["start_observed_at"]):
            flags.append("observed_time_mismatch")
        return flags

    def _snapshot_digest(self, registration_id: int, version: int) -> str:
        payload = []
        for row in self.repository.readings_of_version(registration_id, version):
            payload.append({"point": row["point_code"], "start": row["start_value"], "end": row["end_value"],
                            "unit": row["unit"], "start_at": row["start_observed_at"], "end_at": row["end_observed_at"]})
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    # ---- 读取期计算 --------------------------------------------------------

    def _build_facility_chain(self, facility_id: int, *, target_id: int | None, target_version: int | None,
                              scope_ids: set[int] | None = None) -> tuple[dict[int, sqlite3.Row], dict[int, list[dict[str, Any]]]]:
        points = {int(row["id"]): row for row in self.connection.execute(
            "SELECT * FROM ww_meter_points WHERE facility_id=? AND active=1 ORDER BY id", (facility_id,)
        ).fetchall()}
        chain: dict[int, list[dict[str, Any]]] = {point_id: [] for point_id in points}
        rows = self.connection.execute(
            "SELECT * FROM ww_shift_registrations WHERE facility_id=? ORDER BY shift_date,shift_seq,id", (facility_id,)
        ).fetchall()
        for registration in rows:
            reg_id = int(registration["id"])
            if scope_ids is not None and reg_id not in scope_ids:
                continue
            version = target_version if (target_id is not None and reg_id == target_id) else int(registration["current_version"])
            by_point = {int(row["point_id"]): row for row in self.repository.readings_of_version(reg_id, version)}
            for point_id in points:
                row = by_point.get(point_id)
                chain[point_id].append({
                    "shift_id": reg_id,
                    "shift_date": registration["shift_date"],
                    "shift_seq": int(registration["shift_seq"]),
                    "start": calc.to_cubic_meters(row["start_value"], row["unit"]) if row is not None and row["start_value"] is not None else None,
                    "end": calc.to_cubic_meters(row["end_value"], row["unit"]) if row is not None and row["end_value"] is not None else None,
                    "review_status": row["review_status"] if row is not None else "ok",
                })
        return points, chain

    def _compute_shift(self, registration: sqlite3.Row, version: int) -> dict[str, Any]:
        shift_id = int(registration["id"])
        facility_id = int(registration["facility_id"])
        points, chain = self._build_facility_chain(facility_id, target_id=shift_id, target_version=version)
        metric_items: dict[str, list[dict[str, Any]]] = {metric: [] for metric in calc.METRICS}
        resolved_by_point: dict[int, dict[str, Any]] = {}
        for point_id, items in chain.items():
            resolved = calc.resolve_point_volumes(items)
            resolved_target = next(item for item in resolved if item["shift_id"] == shift_id)
            resolved_by_point[point_id] = resolved_target
            metric_items[points[point_id]["metric"]].append(resolved_target)
        volumes = {metric: calc.aggregate_volumes(items) for metric, items in metric_items.items()}

        version_rows = self.repository.readings_of_version(shift_id, version)
        recorded_point_ids = {int(row["point_id"]) for row in version_rows}
        readings = self._serialize_version_rows(version_rows, resolved_by_point)
        missing_points = [row["code"] for point_id, row in points.items()
                          if point_id not in recorded_point_ids
                          and resolved_by_point[point_id]["status"] == "missing"]
        return {
            "id": shift_id,
            "facility_id": facility_id,
            "facility_code": registration["facility_code"],
            "facility_name": registration["facility_name"],
            "shift_date": registration["shift_date"],
            "shift_seq": int(registration["shift_seq"]),
            "starts_at": registration["starts_at"],
            "ends_at": registration["ends_at"],
            "batch_key": registration["batch_key"],
            "current_version": int(registration["current_version"]),
            "viewing_version": version,
            "issue_flags": [flag for flag in registration["issue_flags"].split(",") if flag],
            "readings": readings,
            "missing_points": missing_points,
            "volumes": volumes,
            "recycle_rate": calc.recycle_interval(volumes["influent"], volumes["return_flow"]),
            "loss": calc.loss_interval(volumes["influent"], volumes["effluent"], volumes["return_flow"]),
            "pending_readings": sum(1 for row in readings if row["review_status"] == "pending"),
            "versions": self._version_list(shift_id),
        }

    def _serialize_version_rows(self, version_rows: list[sqlite3.Row],
                                resolved_by_point: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for row in version_rows:
            point_id = int(row["point_id"])
            resolved = resolved_by_point.get(point_id, {})
            inferred_flags = [flag for flag in resolved.get("flags", []) if flag not in row["issue_flags"].split(",")]
            result.append({
                "reading_id": row["id"],
                "point_id": point_id,
                "point_code": row["point_code"],
                "metric": row["metric"],
                "start_value": row["start_value"],
                "end_value": row["end_value"],
                "unit": row["unit"],
                "start_m3": calc.to_cubic_meters(row["start_value"], row["unit"]),
                "end_m3": calc.to_cubic_meters(row["end_value"], row["unit"]),
                "volume_m3": resolved.get("volume_m3"),
                "volume_status": resolved.get("status"),
                "inherited_from_version": row["carried_from_version"],
                "issue_flags": [flag for flag in row["issue_flags"].split(",") if flag] + inferred_flags,
                "review_status": row["review_status"],
                "review_note": row["review_note"],
                "start_observed_at": row["start_observed_at"],
                "end_observed_at": row["end_observed_at"],
            })
        return result

    def _version_list(self, shift_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT version,snapshot_digest,note,created_by,created_at FROM ww_shift_versions "
            "WHERE registration_id=? ORDER BY version", (shift_id,)
        ).fetchall()]

    def _summarize_scope(self, facility_id: int, scope_ids: set[int]) -> dict[str, Any]:
        points, chain = self._build_facility_chain(facility_id, target_id=None, target_version=None, scope_ids=scope_ids)
        metric_items: dict[str, list[dict[str, Any]]] = {metric: [] for metric in calc.METRICS}
        flags: set[str] = set()
        for point_id, items in chain.items():
            resolved = calc.resolve_point_volumes(items)
            metric_items[points[point_id]["metric"]].extend(resolved)
            for item in resolved:
                flags.update(item["flags"])
        volumes = {metric: calc.aggregate_volumes(items) for metric, items in metric_items.items()}
        return {
            "volumes": volumes,
            "recycle_rate": calc.recycle_interval(volumes["influent"], volumes["return_flow"]),
            "loss": calc.loss_interval(volumes["influent"], volumes["effluent"], volumes["return_flow"]),
            "flags": sorted(flags),
        }

    def _pending_in_scope(self, scope_ids: set[int]) -> int:
        if not scope_ids:
            return 0
        placeholders = ",".join("?" for _ in scope_ids)
        row = self.connection.execute(
            "SELECT COUNT(*) FROM ww_reading_versions rv JOIN ww_shift_registrations s ON s.id=rv.registration_id "
            f"WHERE rv.review_status='pending' AND rv.version=s.current_version AND rv.registration_id IN ({placeholders})",
            tuple(scope_ids),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _reading_fingerprint(row: dict[str, Any] | None) -> Any:
        if row is None:
            return None
        return (row["point_code"], row["start_value"], row["end_value"], row["unit"])
