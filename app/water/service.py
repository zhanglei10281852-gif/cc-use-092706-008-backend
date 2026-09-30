from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService
from app.water.calculations import METRICS, METRIC_LABELS, combine_intervals, compute_balance
from app.water.schema_sql import ensure_schema

REASON_INITIAL = "initial"
REASON_AMEND = "amend"
REASON_ROLLBACK = "rollback"
REASON_CORRECTION = "correction"
REASON_IMPORT = "import"


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class WaterLedgerService:
    """再生水班次台账：读数登记、校验、修订版本、复核与批次汇总。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------ 设施

    def create_facility(self, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM water_facilities WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("设施编码已存在")
            cursor = connection.execute(
                "INSERT INTO water_facilities(code,name,unit,capacity_per_shift,nominal_shift_hours,continuity_tolerance_minutes,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    payload["code"], payload["name"], payload["unit"], payload["capacity_per_shift"],
                    payload["nominal_shift_hours"], payload["continuity_tolerance_minutes"],
                    actor.actor_name, now, now,
                ),
            )
            row = self._facility_by_id(connection, cursor.lastrowid)
            AuditService(connection, self.clock).record(
                actor, action="water.facility.create", resource_type="water_facility", resource_id=row["id"], after=row,
            )
            return row

    def list_facilities(self) -> list[dict[str, Any]]:
        ensure_schema()
        rows = self.connection.execute("SELECT * FROM water_facilities ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 登记

    def register(self, payload: dict[str, Any], actor: AuditContext, *, reason: str = REASON_INITIAL) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            return self._register_in_tx(connection, payload, actor, reason=reason)

    def amend(self, entry_id: int, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            replay = self._replay_version_key(connection, payload["idempotency_key"], entry_id)
            if replay is not None:
                return replay
            entry = self._require_entry(connection, entry_id)
            self._require_open_batch(connection, entry["batch_code"])
            if entry["status"] == "anomaly":
                raise ConflictError("该班次存在待复核异常，请先通过复核流程确认或纠正，不能直接补录")
            facility = self._facility_row_by_id(connection, entry["facility_id"])
            self._check_unit(facility, payload["unit"])
            current = self._version_row(connection, entry_id, int(entry["current_version"]))
            readings = self._readings(payload)
            if digest(readings) == current["readings_digest"]:
                raise ConflictError("补录读数与当前版本完全一致，未形成修订版本")

            computed = compute_balance(readings, float(facility["capacity_per_shift"]))
            status = "anomaly" if computed["violations"] else "confirmed"
            new_version = int(entry["current_version"]) + 1
            self._insert_version(
                connection=connection, entry_id=entry_id, version=new_version, readings=readings, computed=computed,
                status=status, reason=REASON_AMEND, based_on=int(entry["current_version"]),
                idem_key=payload["idempotency_key"], request_digest="", actor=actor.actor_name, now=now,
            )
            connection.execute(
                "UPDATE water_shift_entries SET status=?,note=?,updated_at=? WHERE id=?",
                (status, payload.get("note", "") or entry["note"], now, entry_id),
            )
            diff = self._diff(current, readings, computed, int(entry["current_version"]), new_version, REASON_AMEND)
            result = self._detail(connection, entry_id)
            result["diff"] = diff
            AuditService(connection, self.clock).record(
                actor, action="water.shift.amend", resource_type="water_shift_entry", resource_id=entry_id,
                before={"version": int(entry["current_version"]), "readings": json.loads(current["readings_json"])},
                after={"version": new_version, "status": status, "readings": readings, "violations": computed["violations"]},
                metadata={"reason_text": payload["reason"], "diff": diff},
            )
            return result

    def review(self, entry_id: int, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            replay = self._replay_review_key(connection, payload["idempotency_key"], entry_id)
            if replay is not None:
                return replay
            entry = self._require_entry(connection, entry_id)
            self._require_open_batch(connection, entry["batch_code"])
            facility = self._facility_row_by_id(connection, entry["facility_id"])
            if entry["status"] != "anomaly":
                raise ConflictError("只有待复核状态的班次可以复核")

            new_version: int | None = None
            diff: dict[str, Any] | None = None
            if payload["decision"] == "confirm":
                connection.execute(
                    "UPDATE water_shift_entries SET status='anomaly_reviewed',updated_at=? WHERE id=?", (now, entry_id),
                )
                final_status = "anomaly_reviewed"
            else:
                corrected = payload.get("corrected_readings")
                if corrected is None:
                    raise ValidationError("判定为纠正时必须提供 corrected_readings")
                self._check_unit(facility, payload["unit"])
                readings = self._readings({"readings": corrected})
                current_version = int(entry["current_version"])
                current = self._version_row(connection, entry_id, current_version)
                computed = compute_balance(readings, float(facility["capacity_per_shift"]))
                final_status = "anomaly" if computed["violations"] else "corrected"
                new_version = current_version + 1
                self._insert_version(
                    connection=connection, entry_id=entry_id, version=new_version, readings=readings, computed=computed,
                    status=final_status, reason=REASON_CORRECTION, based_on=current_version,
                    idem_key=payload["idempotency_key"] + ":version", request_digest="",
                    actor=actor.actor_name, now=now,
                )
                connection.execute("UPDATE water_shift_entries SET status=?,updated_at=? WHERE id=?", (final_status, now, entry_id))
                diff = self._diff(current, readings, computed, current_version, new_version, REASON_CORRECTION)

            try:
                connection.execute(
                    "INSERT INTO water_shift_reviews(entry_id,version_ref,decision,note,idempotency_key,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (entry_id, new_version or int(entry["current_version"]), payload["decision"],
                     payload.get("note", ""), payload["idempotency_key"], actor.actor_name, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该复核幂等键已被使用") from exc
            result = self._detail(connection, entry_id)
            if diff is not None:
                result["diff"] = diff
            AuditService(connection, self.clock).record(
                actor, action="water.shift.review", resource_type="water_shift_entry", resource_id=entry_id,
                after={"decision": payload["decision"], "status": final_status, "version": new_version},
                metadata={"note": payload.get("note", ""), "diff": diff},
            )
            return result

    def rollback(self, entry_id: int, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            replay = self._replay_version_key(connection, payload["idempotency_key"], entry_id)
            if replay is not None:
                return replay
            entry = self._require_entry(connection, entry_id)
            self._require_open_batch(connection, entry["batch_code"])
            target = self._version_row(connection, entry_id, payload["target_version"])
            current_version = int(entry["current_version"])
            if payload["target_version"] == current_version:
                raise ConflictError("目标版本就是当前版本，无需回滚")
            readings = json.loads(target["readings_json"])
            computed = json.loads(target["computed_json"])
            new_version = current_version + 1
            self._insert_version(
                connection=connection, entry_id=entry_id, version=new_version, readings=readings, computed=computed,
                status=target["status_snapshot"], reason=REASON_ROLLBACK, based_on=payload["target_version"],
                idem_key=payload["idempotency_key"], request_digest="", actor=actor.actor_name, now=now,
            )
            connection.execute(
                "UPDATE water_shift_entries SET status=?,updated_at=? WHERE id=?",
                (target["status_snapshot"], now, entry_id),
            )
            current = self._version_row(connection, entry_id, current_version)
            diff = self._diff(
                current, readings, computed, current_version, new_version, REASON_ROLLBACK,
                target_version=payload["target_version"],
            )
            result = self._detail(connection, entry_id)
            result["diff"] = diff
            AuditService(connection, self.clock).record(
                actor, action="water.shift.rollback", resource_type="water_shift_entry", resource_id=entry_id,
                before={"version": current_version},
                after={"version": new_version, "restored_from": payload["target_version"],
                       "status": target["status_snapshot"]},
                metadata={"reason_text": payload["reason"], "diff": diff},
            )
            return result

    # ------------------------------------------------------------------ 查询

    def list_entries(self, *, facility_code: str | None = None, batch_code: str | None = None,
                     status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        ensure_schema()
        clauses: list[str] = []
        values: list[Any] = []
        if facility_code:
            clauses.append("f.code=?")
            values.append(facility_code.strip().upper())
        if batch_code:
            clauses.append("e.batch_code=?")
            values.append(batch_code.strip())
        if status:
            clauses.append("e.status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT e.*,f.code AS facility_code,f.name AS facility_name,f.unit AS unit,"
            "f.capacity_per_shift AS capacity_per_shift FROM water_shift_entries e"
            " JOIN water_facilities f ON f.id=e.facility_id" + where
            + " ORDER BY e.batch_code,f.code,e.starts_at LIMIT ?",
            values,
        ).fetchall()
        return [self._attach_current(self.connection, dict(row)) for row in rows]

    def detail(self, entry_id: int) -> dict[str, Any]:
        with transaction() as connection:
            return self._detail(connection, entry_id)

    def version_detail(self, entry_id: int, version: int) -> dict[str, Any]:
        ensure_schema()
        self._require_entry(self.connection, entry_id)
        return self._version_payload(self._version_row(self.connection, entry_id, version))

    def diff_versions(self, entry_id: int, from_version: int, to_version: int) -> dict[str, Any]:
        ensure_schema()
        self._require_entry(self.connection, entry_id)
        old = self._version_row(self.connection, entry_id, from_version)
        new = self._version_row(self.connection, entry_id, to_version)
        return self._diff(
            old, json.loads(new["readings_json"]), json.loads(new["computed_json"]),
            from_version, to_version, new["change_reason"],
        )

    # ------------------------------------------------------------------ 批次

    def create_batch(self, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM water_batches WHERE batch_code=?", (payload["batch_code"],)).fetchone():
                raise ConflictError("批次编码已存在")
            self._ensure_batch(connection, payload["batch_code"], actor.actor_name, now, note=payload.get("note", ""))
            row = connection.execute("SELECT * FROM water_batches WHERE batch_code=?", (payload["batch_code"],)).fetchone()
            result = dict(row)
            AuditService(connection, self.clock).record(
                actor, action="water.batch.create", resource_type="water_batch", resource_id=payload["batch_code"],
                after={"batch_code": payload["batch_code"]},
            )
            return result

    def batch_summary(self, batch_code: str, facility_code: str | None = None) -> dict[str, Any]:
        ensure_schema()
        batch = self.connection.execute("SELECT * FROM water_batches WHERE batch_code=?", (batch_code,)).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        entries = self.list_entries(batch_code=batch_code, facility_code=facility_code, limit=500)
        rows = []
        anomalies = 0
        gaps = 0
        for item in entries:
            rows.append({"facility_code": item["facility_code"], "entry_id": item["id"],
                         "starts_at": item["starts_at"], "status": item["status"], "computed": item["computed"]})
            if item["status"] == "anomaly":
                anomalies += 1
            if item["continuity_flag"] == "gap":
                gaps += 1
        summary = combine_intervals(rows) if rows else None

        active_facilities = {
            str(row["code"])
            for row in self.connection.execute("SELECT code FROM water_facilities WHERE active=1").fetchall()
        }
        covered = {item["facility_code"] for item in entries}
        missing_facilities = sorted(active_facilities - covered) if facility_code is None else []

        return {
            "batch_code": batch_code,
            "status": batch["status"],
            "period_start": batch["period_start"],
            "period_end": batch["period_end"],
            "entry_count": len(entries),
            "registered_facilities": sorted(covered),
            "missing_active_facilities": missing_facilities,
            "anomaly_entries": anomalies,
            "continuity_gaps": gaps,
            "summary": summary,
            "entries": rows,
            "boundary": "同一批次内汇总；跨设施只合并相同 batch_code 的当前版本",
        }

    def close_batch(self, batch_code: str, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            batch = connection.execute("SELECT * FROM water_batches WHERE batch_code=?", (batch_code,)).fetchone()
            if batch is None:
                raise NotFoundError("批次不存在")
            if batch["status"] == "closed":
                raise ConflictError("批次已关闭，不能重复关闭")
            summary = self.batch_summary(batch_code)
            if summary["anomaly_entries"]:
                raise ConflictError("批次内仍存在待复核异常读数，不能关账")
            period_rows = connection.execute(
                "SELECT MIN(starts_at) AS lo,MAX(ends_at) AS hi FROM water_shift_entries WHERE batch_code=?",
                (batch_code,),
            ).fetchone()
            connection.execute(
                "UPDATE water_batches SET status='closed',summary_json=?,closed_at=?,period_start=?,period_end=?,"
                "note=?,updated_at=? WHERE batch_code=?",
                (json.dumps(summary["summary"], ensure_ascii=False, sort_keys=True), now,
                 period_rows["lo"], period_rows["hi"], payload.get("note", "") or batch["note"], now, batch_code),
            )
            result = self.batch_summary(batch_code)
            AuditService(connection, self.clock).record(
                actor, action="water.batch.close", resource_type="water_batch", resource_id=batch_code,
                after={"status": "closed", "summary": summary["summary"]},
                metadata={"note": payload.get("note", "")},
            )
            return result

    def import_entries(self, payload: dict[str, Any], actor: AuditContext) -> dict[str, Any]:
        """整批导入：所有条目共用同一批次边界；重复导入重放，不产生重复数据。"""
        now = to_storage(self.clock.now())
        canonical_entries = [
            {
                "facility_code": item["facility_code"],
                "starts_at": item["starts_at"],
                "ends_at": item["ends_at"],
                "readings": item["readings"].model_dump() if hasattr(item["readings"], "model_dump") else dict(item["readings"]),
                "unit": item["unit"],
                "note": item.get("note", ""),
            }
            for item in payload["entries"]
        ]
        payload_digest = digest({"batch_code": payload["batch_code"], "entries": canonical_entries})
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM water_imports WHERE import_key=?", (payload["import_key"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != payload_digest:
                    raise ConflictError("同一导入键对应了不同的导入内容")
                return {**json.loads(existing["result_json"]), "replayed": True}

            self._ensure_batch(connection, payload["batch_code"], actor.actor_name, now)
            if connection.execute(
                "SELECT status FROM water_batches WHERE batch_code=?", (payload["batch_code"],),
            ).fetchone()["status"] == "closed":
                raise ConflictError("批次已关闭，不能再导入")

            imported: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for index, item in enumerate(payload["entries"], start=1):
                key = f"imp:{payload['import_key']}:{index}"
                body = item.model_dump() if hasattr(item, "model_dump") else dict(item)
                body.update({"batch_code": payload["batch_code"], "idempotency_key": key})
                try:
                    result = self._register_in_tx(connection, body, actor, reason=REASON_IMPORT)
                    imported.append({
                        "entry_id": result["id"], "facility_code": body["facility_code"],
                        "starts_at": body["starts_at"], "version": result["current_version"],
                        "status": result["status"],
                    })
                except ConflictError as exc:
                    skipped.append({"facility_code": body["facility_code"], "starts_at": body["starts_at"],
                                    "code": exc.code, "message": exc.message})
                except (ValidationError, NotFoundError) as exc:
                    errors.append({"facility_code": body["facility_code"], "starts_at": body["starts_at"],
                                   "code": exc.code, "message": exc.message})

            result = {
                "replayed": False,
                "import_key": payload["import_key"],
                "batch_code": payload["batch_code"],
                "payload_digest": payload_digest,
                "imported": imported,
                "skipped": skipped,
                "errors": errors,
            }
            connection.execute(
                "INSERT INTO water_imports(import_key,batch_code,payload_digest,result_json,created_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (payload["import_key"], payload["batch_code"], payload_digest,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), actor.actor_name, now),
            )
            AuditService(connection, self.clock).record(
                actor, action="water.import", resource_type="water_batch", resource_id=payload["batch_code"],
                after={"imported": len(imported), "skipped": len(skipped), "errors": len(errors)},
                metadata={"import_key": payload["import_key"], "payload_digest": payload_digest},
            )
            return result

    # ------------------------------------------------------------------ 内部

    def _register_in_tx(self, connection: sqlite3.Connection, payload: dict[str, Any],
                        actor: AuditContext, *, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        replay = self._replay_register_key(connection, payload)
        if replay is not None:
            replay["replayed"] = True
            return replay

        facility = self._require_facility(connection, payload["facility_code"])
        self._check_unit(facility, payload["unit"])
        starts, ends = self._parse_window(payload["starts_at"], payload["ends_at"], facility)
        starts_s, ends_s = to_storage(starts), to_storage(ends)

        existing = connection.execute(
            "SELECT id FROM water_shift_entries WHERE facility_id=? AND starts_at=?",
            (facility["id"], starts_s),
        ).fetchone()
        if existing is not None:
            raise ConflictError("该设施班次已登记，请通过补录接口形成修订版本")

        readings = self._readings(payload)
        computed = compute_balance(readings, float(facility["capacity_per_shift"]))
        status = "anomaly" if computed["violations"] else "confirmed"
        continuity_flag, continuity_note = self._continuity(connection, facility["id"], starts_s, ends_s)

        cursor = connection.execute(
            "INSERT INTO water_shift_entries(facility_id,batch_code,starts_at,ends_at,current_version,status,"
            "continuity_flag,continuity_note,note,created_at,updated_at) VALUES(?,?,?, ?,1,?,?,?,?,?,?)",
            (
                facility["id"], payload["batch_code"], starts_s, ends_s, status,
                continuity_flag, continuity_note, payload.get("note", ""), now, now,
            ),
        )
        entry_id = int(cursor.lastrowid)
        self._ensure_batch(connection, payload["batch_code"], actor.actor_name, now)
        request_digest = self._request_digest(payload)
        self._insert_version(
            connection=connection, entry_id=entry_id, version=1, readings=readings, computed=computed,
            status=status, reason=reason, based_on=None, idem_key=payload["idempotency_key"],
            request_digest=request_digest, actor=actor.actor_name, now=now,
        )
        self._refresh_neighbor_continuity(connection, facility["id"], entry_id)
        result = self._detail(connection, entry_id)
        AuditService(connection, self.clock).record(
            actor, action="water.shift.register", resource_type="water_shift_entry", resource_id=entry_id,
            after={"version": 1, "status": status, "readings": readings, "violations": computed["violations"]},
            metadata={"batch_code": payload["batch_code"], "facility_code": facility["code"],
                      "continuity": continuity_flag, "reason": reason},
        )
        return result

    def _require_facility(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        facility = connection.execute("SELECT * FROM water_facilities WHERE code=?", (code.strip().upper(),)).fetchone()
        if facility is None:
            raise NotFoundError(f"设施不存在：{code}")
        if not facility["active"]:
            raise ConflictError(f"设施已停用：{code}")
        return facility

    def _facility_row_by_id(self, connection: sqlite3.Connection, facility_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM water_facilities WHERE id=?", (facility_id,)).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return row

    def _facility_by_id(self, connection: sqlite3.Connection, facility_id: int) -> dict[str, Any]:
        return dict(self._facility_row_by_id(connection, facility_id))

    @staticmethod
    def _check_unit(facility: sqlite3.Row, unit: str) -> None:
        if unit.strip().lower() != facility["unit"]:
            raise ValidationError(
                "读数单位与设施登记单位不一致",
                context={"submitted": unit, "facility_unit": facility["unit"], "facility_code": facility["code"]},
            )

    @staticmethod
    def _readings(payload: dict[str, Any]) -> dict[str, float | None]:
        raw = payload["readings"]
        readings = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
        return {metric: (None if readings.get(metric) is None else float(readings[metric])) for metric in METRICS}

    @staticmethod
    def _request_digest(payload: dict[str, Any]) -> str:
        return digest({
            "facility_code": payload["facility_code"],
            "batch_code": payload["batch_code"],
            "starts_at": payload["starts_at"],
            "ends_at": payload["ends_at"],
            "readings": payload["readings"].model_dump() if hasattr(payload["readings"], "model_dump") else dict(payload["readings"]),
            "unit": payload["unit"],
            "note": payload.get("note", ""),
        })

    @staticmethod
    def _parse_window(starts_text: str, ends_text: str, facility: sqlite3.Row) -> tuple[datetime, datetime]:
        try:
            starts = from_storage(starts_text.strip())
            ends = from_storage(ends_text.strip())
        except (ValueError, TypeError) as exc:
            raise ValidationError("班次时间格式不合法，需要 ISO 8601 时间") from exc
        if starts is None or ends is None or ends <= starts:
            raise ValidationError("班次结束时间必须晚于开始时间")
        minutes = (ends - starts).total_seconds() / 60
        nominal = float(facility["nominal_shift_hours"]) * 60
        if not (nominal - 180) <= minutes <= (nominal + 180):
            raise ValidationError(
                "班次时长偏离名义班次过多",
                context={"duration_minutes": round(minutes, 1), "nominal_minutes": nominal},
            )
        return starts, ends

    def _continuity(self, connection: sqlite3.Connection, facility_id: int,
                    starts_at: str, ends_at: str, *, exclude_entry_id: int | None = None) -> tuple[str, str]:
        tolerance = int(connection.execute(
            "SELECT continuity_tolerance_minutes FROM water_facilities WHERE id=?", (facility_id,),
        ).fetchone()[0])
        predecessor = connection.execute(
            "SELECT ends_at FROM water_shift_entries WHERE facility_id=? AND starts_at<? "
            + ("AND id<>? " if exclude_entry_id is not None else "")
            + "ORDER BY starts_at DESC LIMIT 1",
            ((facility_id, starts_at, exclude_entry_id) if exclude_entry_id is not None else (facility_id, starts_at)),
        ).fetchone()
        successor = connection.execute(
            "SELECT starts_at FROM water_shift_entries WHERE facility_id=? AND starts_at>? "
            + ("AND id<>? " if exclude_entry_id is not None else "")
            + "ORDER BY starts_at ASC LIMIT 1",
            ((facility_id, starts_at, exclude_entry_id) if exclude_entry_id is not None else (facility_id, starts_at)),
        ).fetchone()
        notes: list[str] = []
        flag = "ok"
        if predecessor is not None:
            gap = (from_storage(starts_at) - from_storage(predecessor["ends_at"])).total_seconds() / 60
            if abs(gap) > tolerance:
                flag = "gap"
                notes.append(f"与上一班次相差 {round(gap)} 分钟（容差 {tolerance}）")
        if successor is not None:
            gap = (from_storage(successor["starts_at"]) - from_storage(ends_at)).total_seconds() / 60
            if abs(gap) > tolerance:
                flag = "gap"
                notes.append(f"与下一班次相差 {round(gap)} 分钟（容差 {tolerance}）")
        return flag, "；".join(notes)

    def _refresh_neighbor_continuity(self, connection: sqlite3.Connection, facility_id: int, entry_id: int) -> None:
        """新班次插入后，重新计算时间上前后相邻班次的连续性标志（它们之前看到的是缺口）。

        存储时间均为归一化 UTC ISO 字符串，字典序即时间序。
        """
        row = connection.execute(
            "SELECT starts_at,ends_at FROM water_shift_entries WHERE id=?", (entry_id,),
        ).fetchone()
        if row is None:
            return
        neighbors = connection.execute(
            "SELECT id,starts_at,ends_at FROM water_shift_entries WHERE facility_id=? AND ("
            "id=(SELECT id FROM water_shift_entries WHERE facility_id=? AND starts_at<? AND id<>? ORDER BY starts_at DESC LIMIT 1)"
            " OR id=(SELECT id FROM water_shift_entries WHERE facility_id=? AND starts_at>? AND id<>? ORDER BY starts_at ASC LIMIT 1))",
            (facility_id, facility_id, row["starts_at"], entry_id, facility_id, row["starts_at"], entry_id),
        ).fetchall()
        for neighbor in neighbors:
            flag, note = self._continuity(connection, facility_id, neighbor["starts_at"], neighbor["ends_at"],
                                          exclude_entry_id=neighbor["id"])
            connection.execute(
                "UPDATE water_shift_entries SET continuity_flag=?,continuity_note=? WHERE id=?",
                (flag, note, neighbor["id"]),
            )

    def _ensure_batch(self, connection: sqlite3.Connection, batch_code: str, actor: str, now: str,
                      note: str = "") -> None:
        connection.execute(
            "INSERT INTO water_batches(batch_code,note,created_by,created_at,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(batch_code) DO NOTHING",
            (batch_code, note, actor, now, now),
        )

    @staticmethod
    def _require_open_batch(connection: sqlite3.Connection, batch_code: str) -> None:
        batch = connection.execute("SELECT status FROM water_batches WHERE batch_code=?", (batch_code,)).fetchone()
        if batch is not None and batch["status"] == "closed":
            raise ConflictError("批次已关账，历史版本不可修改；请在新批次中登记")

    @staticmethod
    def _require_entry(connection: sqlite3.Connection, entry_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM water_shift_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise NotFoundError("班次记录不存在")
        return row

    @staticmethod
    def _insert_version(*, connection: sqlite3.Connection, entry_id: int, version: int,
                        readings: dict[str, float | None], computed: dict[str, Any], status: str,
                        reason: str, based_on: int | None, idem_key: str, request_digest: str,
                        actor: str, now: str) -> None:
        try:
            connection.execute(
                "INSERT INTO water_shift_versions(entry_id,version,readings_json,computed_json,formula_version,"
                "readings_digest,request_digest,status_snapshot,change_reason,based_on_version,idempotency_key,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, version,
                    json.dumps(readings, ensure_ascii=False, sort_keys=True),
                    json.dumps(computed, ensure_ascii=False, sort_keys=True),
                    computed["formula_version"], digest(readings), request_digest, status, reason, based_on,
                    idem_key, actor, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该幂等键已被使用，重复提交请保持参数一致") from exc
        connection.execute(
            "UPDATE water_shift_entries SET current_version=?,status=?,updated_at=? WHERE id=?",
            (version, status, now, entry_id),
        )

    def _version_row(self, connection: sqlite3.Connection, entry_id: int, version: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM water_shift_versions WHERE entry_id=? AND version=?", (entry_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"班次版本不存在：v{version}")
        return row

    def _replay_register_key(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any] | None:
        key = payload["idempotency_key"]
        row = connection.execute(
            "SELECT v.entry_id AS entry_id,v.request_digest AS request_digest FROM water_shift_versions v"
            " WHERE v.idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] and row["request_digest"] != self._request_digest(payload):
            raise ConflictError("同一幂等键不能用于不同的登记请求")
        return self._detail(connection, int(row["entry_id"]))

    def _replay_version_key(self, connection: sqlite3.Connection, key: str, entry_id: int) -> dict[str, Any] | None:
        """命中返回重放结果；键属于其他班次返回冲突；未占用返回 None。"""
        row = connection.execute(
            "SELECT entry_id FROM water_shift_versions WHERE idempotency_key=?", (key,),
        ).fetchone()
        if row is None:
            return None
        if int(row["entry_id"]) != entry_id:
            raise ConflictError("同一幂等键已经用于其他班次的修订操作")
        result = self._detail(connection, entry_id)
        result["replayed"] = True
        return result

    def _replay_review_key(self, connection: sqlite3.Connection, key: str, entry_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT entry_id FROM water_shift_reviews WHERE idempotency_key=?", (key,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT entry_id FROM water_shift_versions WHERE idempotency_key=?", (key + ":version",),
            ).fetchone()
        if row is None:
            return None
        if int(row["entry_id"]) != entry_id:
            raise ConflictError("同一幂等键已经用于其他班次的复核操作")
        result = self._detail(connection, entry_id)
        result["replayed"] = True
        return result

    def _attach_current(self, connection: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
        version = connection.execute(
            "SELECT readings_json,computed_json FROM water_shift_versions WHERE entry_id=? AND version=?",
            (row["id"], row["current_version"]),
        ).fetchone()
        row["readings"] = json.loads(version["readings_json"])
        row["computed"] = json.loads(version["computed_json"])
        return row

    def _detail(self, connection: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT e.*,f.code AS facility_code,f.name AS facility_name,f.unit AS unit,"
            "f.capacity_per_shift AS capacity_per_shift FROM water_shift_entries e"
            " JOIN water_facilities f ON f.id=e.facility_id WHERE e.id=?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("班次记录不存在")
        result = dict(row)
        current = self._version_row(connection, entry_id, int(row["current_version"]))
        result["readings"] = json.loads(current["readings_json"])
        result["computed"] = json.loads(current["computed_json"])
        result["versions"] = [
            self._version_payload(item)
            for item in connection.execute(
                "SELECT * FROM water_shift_versions WHERE entry_id=? ORDER BY version", (entry_id,),
            ).fetchall()
        ]
        result["reviews"] = [
            dict(item) for item in connection.execute(
                "SELECT id,entry_id,version_ref,decision,note,created_by,created_at"
                " FROM water_shift_reviews WHERE entry_id=? ORDER BY id", (entry_id,),
            ).fetchall()
        ]
        return result

    @staticmethod
    def _version_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": row["version"],
            "change_reason": row["change_reason"],
            "based_on_version": row["based_on_version"],
            "status_snapshot": row["status_snapshot"],
            "formula_version": row["formula_version"],
            "readings": json.loads(row["readings_json"]),
            "computed": json.loads(row["computed_json"]),
            "readings_digest": row["readings_digest"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _diff(self, old_row: sqlite3.Row, new_readings: dict[str, float | None], new_computed: dict[str, Any],
              from_version: int, to_version: int, reason: str, *, target_version: int | None = None) -> dict[str, Any]:
        old_readings = json.loads(old_row["readings_json"])
        old_computed = json.loads(old_row["computed_json"])
        metrics: dict[str, dict[str, Any]] = {}
        changed: list[str] = []
        for metric in METRICS:
            before = old_readings.get(metric)
            after = new_readings.get(metric)
            if before != after:
                changed.append(metric)
            metrics[metric] = {
                "label": METRIC_LABELS[metric],
                "before": before,
                "after": after,
                "delta": (None if before is None or after is None else round(after - before, 4)),
            }
        return {
            "from_version": from_version,
            "to_version": to_version,
            "change_reason": reason,
            "restored_from_version": target_version,
            "changed_metrics": changed,
            "metrics": metrics,
            "before_loss": old_computed["loss"],
            "after_loss": new_computed["loss"],
            "before_return_rate": old_computed["return_rate"],
            "after_return_rate": new_computed["return_rate"],
            "before_violations": old_computed["violations"],
            "after_violations": new_computed["violations"],
        }
