"""再生水班次读数领域的 SQLite 表结构与仓储。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS ww_facilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ww_meter_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id INTEGER NOT NULL REFERENCES ww_facilities(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    metric TEXT NOT NULL CHECK(metric IN ('influent','effluent','return_flow')),
    unit TEXT NOT NULL CHECK(unit IN ('m3','L')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(facility_id, code)
);
CREATE TABLE IF NOT EXISTS ww_shift_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id INTEGER NOT NULL REFERENCES ww_facilities(id) ON DELETE RESTRICT,
    shift_date TEXT NOT NULL,
    shift_seq INTEGER NOT NULL CHECK(shift_seq IN (1,2,3)),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    current_version INTEGER NOT NULL DEFAULT 1,
    issue_flags TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(facility_id, shift_date, shift_seq),
    UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ww_shifts_facility ON ww_shift_registrations(facility_id,shift_date,shift_seq);
CREATE INDEX IF NOT EXISTS idx_ww_shifts_batch ON ww_shift_registrations(batch_key);
CREATE TABLE IF NOT EXISTS ww_shift_versions (
    registration_id INTEGER NOT NULL REFERENCES ww_shift_registrations(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot_digest TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(registration_id, version)
);
CREATE TABLE IF NOT EXISTS ww_reading_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES ww_shift_registrations(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    point_id INTEGER NOT NULL REFERENCES ww_meter_points(id) ON DELETE RESTRICT,
    start_value REAL,
    end_value REAL,
    unit TEXT NOT NULL CHECK(unit IN ('m3','L')),
    start_observed_at TEXT,
    end_observed_at TEXT,
    carried_over INTEGER NOT NULL DEFAULT 0 CHECK(carried_over IN (0,1)),
    carried_from_version INTEGER,
    issue_flags TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'ok' CHECK(review_status IN ('ok','pending','resolved','rejected')),
    review_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(registration_id, version, point_id)
);
CREATE INDEX IF NOT EXISTS idx_ww_readings_review ON ww_reading_versions(review_status,registration_id);
CREATE TABLE IF NOT EXISTS ww_shift_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER REFERENCES ww_shift_registrations(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK(action IN ('register','revise','rollback','review')),
    from_version INTEGER,
    to_version INTEGER,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ww_events_reg ON ww_shift_events(registration_id,id);
CREATE TABLE IF NOT EXISTS ww_import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_schema(connection: sqlite3.Connection | None = None) -> None:
    if connection is None:
        from app.database import transaction

        with transaction(immediate=True) as conn:
            conn.executescript(SCHEMA)
    else:
        connection.executescript(SCHEMA)


class WaterRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 设施与表计 -------------------------------------------------------

    def facility_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ww_facilities WHERE code=?", (code,)).fetchone()

    def create_facility(self, *, code: str, name: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute("INSERT INTO ww_facilities(code,name,created_at) VALUES(?,?,?)", (code, name, now))
        return dict(self.connection.execute("SELECT * FROM ww_facilities WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_facilities(self) -> list[dict[str, Any]]:
        facilities = [dict(row) for row in self.connection.execute("SELECT * FROM ww_facilities ORDER BY code").fetchall()]
        for facility in facilities:
            facility["meter_points"] = self.meter_points(facility["id"])
        return facilities

    def meter_points(self, facility_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ww_meter_points WHERE facility_id=? AND active=1 ORDER BY metric,code", (facility_id,)
        ).fetchall()]

    def point_map(self, facility_id: int) -> dict[str, sqlite3.Row]:
        return {row["code"]: row for row in self.connection.execute(
            "SELECT * FROM ww_meter_points WHERE facility_id=? AND active=1", (facility_id,)
        ).fetchall()}

    def upsert_meter_points(self, facility_id: int, points: Iterable[dict[str, Any]], *, now: str) -> None:
        for point in points:
            self.connection.execute(
                "INSERT INTO ww_meter_points(facility_id,code,metric,unit,active,created_at) VALUES(?,?,?,?,1,?)"
                " ON CONFLICT(facility_id,code) DO UPDATE SET active=1",
                (facility_id, point["code"], point["metric"], point["unit"], now),
            )

    # ---- 班次登记 ---------------------------------------------------------

    def shift_by_key(self, facility_id: int, shift_date: str, shift_seq: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ww_shift_registrations WHERE facility_id=? AND shift_date=? AND shift_seq=?",
            (facility_id, shift_date, shift_seq),
        ).fetchone()

    def shift_by_id(self, shift_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT s.*,f.code AS facility_code,f.name AS facility_name FROM ww_shift_registrations s "
            "JOIN ww_facilities f ON f.id=s.facility_id WHERE s.id=?",
            (shift_id,),
        ).fetchone()

    def previous_shift(self, facility_id: int, shift_date: str, shift_seq: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ww_shift_registrations WHERE facility_id=? AND "
            "(shift_date<? OR (shift_date=? AND shift_seq<?)) ORDER BY shift_date DESC,shift_seq DESC LIMIT 1",
            (facility_id, shift_date, shift_date, shift_seq),
        ).fetchone()

    def next_shift(self, facility_id: int, shift_date: str, shift_seq: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ww_shift_registrations WHERE facility_id=? AND "
            "(shift_date>? OR (shift_date=? AND shift_seq>?)) ORDER BY shift_date ASC,shift_seq ASC LIMIT 1",
            (facility_id, shift_date, shift_date, shift_seq),
        ).fetchone()

    def shifts_of_batch(self, batch_key: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT s.*,f.code AS facility_code,f.name AS facility_name FROM ww_shift_registrations s "
            "JOIN ww_facilities f ON f.id=s.facility_id "
            "WHERE s.batch_key=? ORDER BY f.code,s.shift_date,s.shift_seq",
            (batch_key,),
        ).fetchall()

    def shift_by_idempotency(self, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ww_shift_registrations WHERE idempotency_key=?", (key,)).fetchone()

    def insert_shift(self, *, facility_id: int, shift_date: str, shift_seq: int, starts_at: str, ends_at: str,
                     batch_key: str, idempotency_key: str | None, issue_flags: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO ww_shift_registrations(facility_id,shift_date,shift_seq,starts_at,ends_at,batch_key,"
            "idempotency_key,current_version,issue_flags,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
            (facility_id, shift_date, shift_seq, starts_at, ends_at, batch_key, idempotency_key,
             issue_flags, created_by, now, now),
        )
        return int(cursor.lastrowid)

    def update_shift_flags(self, registration_id: int, issue_flags: str, now: str) -> None:
        self.connection.execute(
            "UPDATE ww_shift_registrations SET issue_flags=?,updated_at=? WHERE id=?",
            (issue_flags, now, registration_id),
        )

    def set_current_version(self, registration_id: int, version: int, now: str) -> None:
        self.connection.execute(
            "UPDATE ww_shift_registrations SET current_version=?,updated_at=? WHERE id=?",
            (version, now, registration_id),
        )

    def insert_version(self, *, registration_id: int, version: int, snapshot_digest: str, note: str,
                       created_by: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO ww_shift_versions(registration_id,version,snapshot_digest,note,created_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (registration_id, version, snapshot_digest, note, created_by, now),
        )

    def version_exists(self, registration_id: int, version: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM ww_shift_versions WHERE registration_id=? AND version=?", (registration_id, version)
        ).fetchone() is not None

    def max_version(self, registration_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM ww_shift_versions WHERE registration_id=?", (registration_id,)
        ).fetchone()[0])

    def insert_reading(self, *, registration_id: int, version: int, point_id: int,
                       start_value: float | None, end_value: float | None, unit: str,
                       start_observed_at: str | None, end_observed_at: str | None,
                       carried_over: bool, carried_from_version: int | None, issue_flags: str,
                       review_status: str, created_by: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO ww_reading_versions(registration_id,version,point_id,start_value,end_value,unit,"
            "start_observed_at,end_observed_at,carried_over,carried_from_version,issue_flags,"
            "review_status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (registration_id, version, point_id, start_value, end_value, unit,
             start_observed_at, end_observed_at, int(carried_over), carried_from_version, issue_flags,
             review_status, created_by, now),
        )

    def readings_of_version(self, registration_id: int, version: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT rv.*,mp.code AS point_code,mp.metric AS metric FROM ww_reading_versions rv "
            "JOIN ww_meter_points mp ON mp.id=rv.point_id "
            "WHERE rv.registration_id=? AND rv.version=? ORDER BY mp.metric,mp.code",
            (registration_id, version),
        ).fetchall()

    def add_event(self, *, registration_id: int | None, action: str, from_version: int | None, to_version: int | None,
                  actor: str, reason: str, detail: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO ww_shift_events(registration_id,action,from_version,to_version,actor,reason,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (registration_id, action, from_version, to_version, actor, reason,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, registration_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ww_shift_events WHERE registration_id=? ORDER BY id", (registration_id,)
        ).fetchall()]

    def pending_readings(self, facility_code: str | None) -> list[dict[str, Any]]:
        sql = (
            "SELECT rv.id,rv.registration_id,rv.version,rv.point_id,rv.start_value,rv.end_value,rv.unit,"
            "rv.issue_flags,rv.review_status,f.code AS facility_code,s.shift_date,s.shift_seq,mp.code AS point_code "
            "FROM ww_reading_versions rv "
            "JOIN ww_shift_registrations s ON s.id=rv.registration_id "
            "JOIN ww_facilities f ON f.id=s.facility_id "
            "JOIN ww_meter_points mp ON mp.id=rv.point_id "
            "WHERE rv.review_status='pending' AND rv.version=s.current_version"
        )
        params: list[Any] = []
        if facility_code:
            sql += " AND f.code=?"
            params.append(facility_code)
        sql += " ORDER BY f.code,s.shift_date,s.shift_seq,mp.code"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def reading_by_id(self, reading_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT rv.*,s.facility_id,s.current_version,f.code AS facility_code FROM ww_reading_versions rv "
            "JOIN ww_shift_registrations s ON s.id=rv.registration_id "
            "JOIN ww_facilities f ON f.id=s.facility_id "
            "WHERE rv.id=?",
            (reading_id,),
        ).fetchone()

    def current_reading_row(self, reading_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT rv.*,s.current_version FROM ww_reading_versions rv "
            "JOIN ww_shift_registrations s ON s.id=rv.registration_id "
            "WHERE rv.id=? AND rv.version=s.current_version",
            (reading_id,),
        ).fetchone()

    def resolve_reading(self, reading_id: int, review_status: str, note: str) -> None:
        self.connection.execute(
            "UPDATE ww_reading_versions SET review_status=?,review_note=? WHERE id=?",
            (review_status, note, reading_id),
        )

    # ---- 批次 -------------------------------------------------------------

    def import_run(self, batch_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ww_import_runs WHERE batch_key=?", (batch_key,)).fetchone()

    def save_import_run(self, *, batch_key: str, request_digest: str, result: dict[str, Any], created_by: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO ww_import_runs(batch_key,request_digest,result_json,created_by,created_at) VALUES(?,?,?,?,?)",
            (batch_key, request_digest, json.dumps(result, ensure_ascii=False, sort_keys=True), created_by, now),
        )

    def list_shifts(self, facility_code: str | None, limit: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.*,f.code AS facility_code,f.name AS facility_name FROM ww_shift_registrations s "
            "JOIN ww_facilities f ON f.id=s.facility_id"
        )
        params: list[Any] = []
        if facility_code:
            sql += " WHERE f.code=?"
            params.append(facility_code)
        sql += " ORDER BY f.code,s.shift_date DESC,s.shift_seq DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def batch_keys(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT batch_key,COUNT(*) AS shift_count,MIN(created_at) AS first_at,MAX(created_at) AS last_at,"
            "SUM(CASE WHEN issue_flags<>'' THEN 1 ELSE 0 END) AS flagged_shifts "
            "FROM ww_shift_registrations WHERE batch_key<>'' GROUP BY batch_key ORDER BY batch_key"
        ).fetchall()]
