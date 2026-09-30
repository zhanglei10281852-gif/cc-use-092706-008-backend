from __future__ import annotations

from app.database import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS water_facilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'm3',
    capacity_per_shift REAL NOT NULL CHECK(capacity_per_shift > 0),
    nominal_shift_hours REAL NOT NULL DEFAULT 8 CHECK(nominal_shift_hours BETWEEN 2 AND 12),
    continuity_tolerance_minutes INTEGER NOT NULL DEFAULT 60 CHECK(continuity_tolerance_minutes BETWEEN 0 AND 720),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS water_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    period_start TEXT,
    period_end TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    summary_json TEXT,
    closed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS water_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_key TEXT NOT NULL UNIQUE,
    batch_code TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS water_shift_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id INTEGER NOT NULL REFERENCES water_facilities(id) ON DELETE RESTRICT,
    batch_code TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('draft','confirmed','anomaly','anomaly_reviewed','corrected')),
    continuity_flag TEXT NOT NULL DEFAULT 'ok' CHECK(continuity_flag IN ('ok','gap')),
    continuity_note TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(facility_id, starts_at)
);
CREATE INDEX IF NOT EXISTS idx_water_entries_batch ON water_shift_entries(batch_code);
CREATE INDEX IF NOT EXISTS idx_water_entries_status ON water_shift_entries(status);
CREATE TABLE IF NOT EXISTS water_shift_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES water_shift_entries(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    readings_json TEXT NOT NULL,
    computed_json TEXT NOT NULL,
    formula_version TEXT NOT NULL,
    readings_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL DEFAULT '',
    status_snapshot TEXT NOT NULL,
    change_reason TEXT NOT NULL CHECK(change_reason IN ('initial','amend','rollback','correction','import')),
    based_on_version INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entry_id, version)
);
CREATE INDEX IF NOT EXISTS idx_water_versions_entry ON water_shift_versions(entry_id, version);
CREATE TABLE IF NOT EXISTS water_shift_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES water_shift_entries(id) ON DELETE CASCADE,
    version_ref INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('confirm','correct')),
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_reviews_entry ON water_shift_reviews(entry_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)
