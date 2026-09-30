from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.database import get_connection
from app.water.calculations import compute_balance
from app.water.service import WaterLedgerService

PASSWORD = "Water!23456"


def login(client, username: str) -> dict:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD, "client_label": "tests"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture()
def users(client, admin) -> dict:
    def make(username: str, roles: list[str]) -> dict:
        created = client.post(
            "/api/users",
            headers=admin["headers"],
            json={"username": username, "password": PASSWORD, "display_name": username, "role_codes": roles},
        )
        assert created.status_code == 201, created.text
        return login(client, username)

    return {
        "operator": make("water.ops", ["water-operator"]),
        "reviewer": make("water.rev", ["water-reviewer"]),
        "auditor": make("water.aud", ["auditor"]),
        "nobody": make("water.noauth", []),
    }


def make_facilities(client, headers: dict, count: int = 5) -> None:
    for index in range(1, count + 1):
        response = client.post(
            "/api/water/facilities",
            headers=headers,
            json={
                "code": f"RW-{index}",
                "name": f"第{index}再生水处理系统",
                "unit": "m3",
                "capacity_per_shift": 5000,
                "nominal_shift_hours": 8,
                "continuity_tolerance_minutes": 60,
            },
        )
        assert response.status_code == 201, response.text


def shift_payload(facility: str, batch: str, starts: str, ends: str, readings: dict, key: str, **extra) -> dict:
    payload = {
        "facility_code": facility,
        "batch_code": batch,
        "starts_at": starts,
        "ends_at": ends,
        "readings": readings,
        "unit": "m3",
        "idempotency_key": key,
    }
    payload.update(extra)
    return payload


# ------------------------------------------------------------------ 纯计算


def test_compute_point_interval_and_anomaly_bounds():
    point = compute_balance({"influent": 1000, "effluent": 800, "return_flow": 150}, 5000)
    assert point["quality"] == "point"
    assert point["loss"] == {"lo": 50.0, "hi": 50.0}
    assert point["return_rate"] == {"lo": 0.15, "hi": 0.15}

    missing_return = compute_balance({"influent": 1000, "effluent": 800, "return_flow": None}, 5000)
    assert missing_return["quality"] == "interval"
    assert missing_return["missing"] == ["return_flow"]
    assert missing_return["bounds"]["return_flow"] == {"lo": 0.0, "hi": 200.0}
    assert missing_return["loss"] == {"lo": 0.0, "hi": 200.0}
    assert missing_return["return_rate"] == {"lo": 0.0, "hi": 0.2}

    missing_influent = compute_balance({"influent": None, "effluent": 800, "return_flow": 100}, 5000)
    assert missing_influent["bounds"]["influent"]["lo"] == 900.0
    assert missing_influent["bounds"]["influent"]["hi"] == 5000.0
    assert missing_influent["loss"] == {"lo": 0.0, "hi": 4100.0}
    assert missing_influent["return_rate"]["lo"] == pytest.approx(0.02)
    assert missing_influent["return_rate"]["hi"] == pytest.approx(0.111111)

    anomaly = compute_balance({"influent": 500, "effluent": 600, "return_flow": 0}, 5000)
    assert "effluent_exceeds_influent" in anomaly["violations"]
    assert "balance_negative_loss" in anomaly["violations"]
    # 异常读数不得被静默修正：原始值原样保留
    assert anomaly["bounds"]["effluent"] == {"lo": 600.0, "hi": 600.0}

    infeasible = compute_balance({"influent": None, "effluent": 800, "return_flow": 100}, 500)
    assert "infeasible_physical_bounds" in infeasible["violations"]


# ------------------------------------------------------------------ 登记校验


def test_register_unit_validation_and_partial_missing_interval(client, admin, users):
    make_facilities(client, users["operator"])
    payload = shift_payload(
        "RW-1", "B-DAY", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 150}, "reg-rw1-001",
    )
    created = client.post("/api/water/shifts", headers=users["operator"], json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "confirmed"
    assert body["computed"]["loss"] == {"lo": 50.0, "hi": 50.0}
    assert body["computed"]["return_rate"]["lo"] == 0.15
    assert body["current_version"] == 1

    wrong_unit = shift_payload(
        "RW-2", "B-DAY", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 150}, "reg-rw2-unit", unit="L",
    )
    rejected = client.post("/api/water/shifts", headers=users["operator"], json=wrong_unit)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["context"]["facility_unit"] == "m3"

    missing = shift_payload(
        "RW-2", "B-DAY", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1200, "effluent": 900, "return_flow": None}, "reg-rw2-partial",
    )
    partial = client.post("/api/water/shifts", headers=users["operator"], json=missing)
    assert partial.status_code == 201, partial.text
    result = partial.json()
    assert result["computed"]["quality"] == "interval"
    assert result["computed"]["bounds"]["return_flow"] == {"lo": 0.0, "hi": 300.0}
    assert result["computed"]["loss"] == {"lo": 0.0, "hi": 300.0}


def test_cross_day_night_shift_and_continuity_gap(client, users):
    make_facilities(client, users["operator"])
    night = shift_payload(
        "RW-1", "B-CROSS", "2026-09-29T22:00:00+00:00", "2026-09-30T06:00:00+00:00",
        {"influent": 900, "effluent": 700, "return_flow": 100}, "cross-night",
    )
    response = client.post("/api/water/shifts", headers=users["operator"], json=night)
    assert response.status_code == 201, response.text
    assert response.json()["continuity_flag"] == "ok"

    morning = shift_payload(
        "RW-1", "B-CROSS", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 100}, "cross-morning",
    )
    response = client.post("/api/water/shifts", headers=users["operator"], json=morning)
    assert response.status_code == 201
    morning_body = response.json()
    assert morning_body["continuity_flag"] == "ok"

    # 晚班晚到 3 小时，超过 60 分钟容差
    late = shift_payload(
        "RW-1", "B-CROSS", "2026-09-30T17:00:00+00:00", "2026-10-01T01:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 100}, "cross-late",
    )
    response = client.post("/api/water/shifts", headers=users["operator"], json=late)
    assert response.status_code == 201
    body = response.json()
    assert body["continuity_flag"] == "gap"
    assert "容差 60" in body["continuity_note"]
    # 相邻的早班连续性也应被重新计算并标记缺口
    morning_detail = client.get(f"/api/water/shifts/{morning_body['id']}", headers=users["operator"])
    assert morning_detail.json()["continuity_flag"] == "gap"

    bad_duration = shift_payload(
        "RW-2", "B-CROSS", "2026-09-30T06:00:00+00:00", "2026-09-30T09:30:00+00:00",
        {"influent": 100, "effluent": 8, "return_flow": 1}, "cross-bad-duration",
    )
    response = client.post("/api/water/shifts", headers=users["operator"], json=bad_duration)
    assert response.status_code == 422
    assert "班次时长" in response.json()["error"]["message"]


def test_duplicate_shift_and_idempotent_replay(client, users):
    make_facilities(client, users["operator"])
    payload = shift_payload(
        "RW-1", "B-DUP", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 150}, "dup-001",
    )
    first = client.post("/api/water/shifts", headers=users["operator"], json=payload)
    assert first.status_code == 201
    entry_id = first.json()["id"]

    replay = client.post("/api/water/shifts", headers=users["operator"], json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == entry_id
    assert replay.json().get("replayed") is True

    different = dict(payload)
    different["readings"] = {"influent": 1001, "effluent": 800, "return_flow": 150}
    clash = client.post("/api/water/shifts", headers=users["operator"], json=different)
    assert clash.status_code == 409
    assert "同一幂等键" in clash.json()["error"]["message"]

    # 同班次走登记接口重复创建（换幂等键）应被拒绝，必须走补录
    payload["idempotency_key"] = "dup-002"
    again = client.post("/api/water/shifts", headers=users["operator"], json=payload)
    assert again.status_code == 409
    assert "补录接口" in again.json()["error"]["message"]


# ---------------------------------------------------------- 补录版本与回滚


def test_amend_creates_immutable_versions_with_diff(client, users):
    make_facilities(client, users["operator"])
    payload = shift_payload(
        "RW-1", "B-VER", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 150}, "amend-base",
    )
    entry = client.post("/api/water/shifts", headers=users["operator"], json=payload).json()
    entry_id = entry["id"]

    amended = client.post(
        f"/api/water/shifts/{entry_id}/amend",
        headers=users["operator"],
        json={
            "reason": "仪表补录：回流流量计读数修正",
            "readings": {"influent": 1000, "effluent": 800, "return_flow": 180},
            "unit": "m3",
            "idempotency_key": "amend-fix-1",
        },
    )
    assert amended.status_code == 200, amended.text
    body = amended.json()
    assert body["current_version"] == 2
    assert body["diff"]["changed_metrics"] == ["return_flow"]
    assert body["diff"]["metrics"]["return_flow"] == {"label": "回流", "before": 150.0, "after": 180.0, "delta": 30.0}
    assert body["diff"]["before_loss"] == {"lo": 50.0, "hi": 50.0}
    assert body["diff"]["after_loss"] == {"lo": 20.0, "hi": 20.0}

    # 旧版本仍然完整可取，未被覆盖
    v1 = client.get(f"/api/water/shifts/{entry_id}/versions/1", headers=users["operator"])
    assert v1.status_code == 200
    assert v1.json()["readings"]["return_flow"] == 150.0
    assert v1.json()["change_reason"] == "initial"
    v2 = client.get(f"/api/water/shifts/{entry_id}/versions/2", headers=users["operator"])
    assert v2.json()["change_reason"] == "amend"
    assert v2.json()["based_on_version"] == 1

    diff = client.get(f"/api/water/shifts/{entry_id}/diff?from_version=1&to_version=2", headers=users["operator"])
    assert diff.json()["changed_metrics"] == ["return_flow"]

    # 相同补录重复提交：重放而非新增版本
    replay = client.post(
        f"/api/water/shifts/{entry_id}/amend",
        headers=users["operator"],
        json={
            "reason": "仪表补录：回流流量计读数修正",
            "readings": {"influent": 1000, "effluent": 800, "return_flow": 180},
            "unit": "m3",
            "idempotency_key": "amend-fix-1",
        },
    )
    assert replay.json()["current_version"] == 2
    assert replay.json().get("replayed") is True

    # SQLite 层确认版本行不可覆盖地追加
    connection = get_connection()
    count = connection.execute(
        "SELECT COUNT(*) FROM water_shift_versions WHERE entry_id=?", (entry_id,),
    ).fetchone()[0]
    assert count == 2


def test_rollback_restores_readings_as_new_revision(client, users):
    make_facilities(client, users["operator"])
    payload = shift_payload(
        "RW-1", "B-ROLL", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 150}, "roll-base",
    )
    entry = client.post("/api/water/shifts", headers=users["operator"], json=payload).json()
    entry_id = entry["id"]
    client.post(
        f"/api/water/shifts/{entry_id}/amend",
        headers=users["operator"],
        json={"reason": "补录", "readings": {"influent": 1000, "effluent": 820, "return_flow": 150},
              "unit": "m3", "idempotency_key": "roll-amend-1"},
    )

    forbidden = client.post(
        f"/api/water/shifts/{entry_id}/rollback",
        headers=users["operator"],
        json={"target_version": 1, "reason": "补录有误恢复原值", "idempotency_key": "roll-rb-1"},
    )
    assert forbidden.status_code == 403

    rolled = client.post(
        f"/api/water/shifts/{entry_id}/rollback",
        headers=users["reviewer"],
        json={"target_version": 1, "reason": "补录有误恢复原值", "idempotency_key": "roll-rb-1"},
    )
    assert rolled.status_code == 200, rolled.text
    body = rolled.json()
    assert body["current_version"] == 3
    assert body["readings"] == {"influent": 1000.0, "effluent": 800.0, "return_flow": 150.0}
    assert body["status"] == "confirmed"
    v3 = body["versions"][-1]
    assert v3["change_reason"] == "rollback"
    assert v3["based_on_version"] == 1
    assert body["diff"]["restored_from_version"] == 1

    # v2 仍然存在，回滚产生的是追加修订而非删除
    assert [version["version"] for version in body["versions"]] == [1, 2, 3]

    connection = get_connection()
    reasons = [
        row[0]
        for row in connection.execute(
            "SELECT change_reason FROM water_shift_versions WHERE entry_id=? ORDER BY version", (entry_id,),
        ).fetchall()
    ]
    assert reasons == ["initial", "amend", "rollback"]


# ---------------------------------------------------------- 异常待复核流程


def test_anomaly_goes_to_review_queue_and_review_decisions(client, users):
    make_facilities(client, users["operator"])
    anomaly_payload = shift_payload(
        "RW-1", "B-ANOM", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 500, "effluent": 600, "return_flow": 0}, "anom-1",
    )
    created = client.post("/api/water/shifts", headers=users["operator"], json=anomaly_payload)
    assert created.status_code == 201
    entry = created.json()
    assert entry["status"] == "anomaly"
    assert "effluent_exceeds_influent" in entry["computed"]["violations"]
    # 读数原样保留，没有被静默修改
    assert entry["readings"]["effluent"] == 600.0

    # 运维员不能自行复核
    denied = client.post(
        f"/api/water/shifts/{entry['id']}/review",
        headers=users["operator"],
        json={"decision": "confirm", "note": "确认", "idempotency_key": "rev-001"},
    )
    assert denied.status_code == 403

    confirmed = client.post(
        f"/api/water/shifts/{entry['id']}/review",
        headers=users["reviewer"],
        json={"decision": "confirm", "note": "现场复核仪表确实显示 600", "idempotency_key": "rev-001"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "anomaly_reviewed"
    assert confirmed.json()["reviews"][0]["decision"] == "confirm"

    # 第二个异常：复核员选择纠正，形成 correction 新版本
    other = shift_payload(
        "RW-2", "B-ANOM", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 500, "effluent": 520, "return_flow": 0}, "anom-2",
    )
    second = client.post("/api/water/shifts", headers=users["operator"], json=other).json()
    corrected = client.post(
        f"/api/water/shifts/{second['id']}/review",
        headers=users["reviewer"],
        json={
            "decision": "correct",
            "note": "超录 100",
            "corrected_readings": {"influent": 600, "effluent": 520, "return_flow": 30},
            "unit": "m3",
            "idempotency_key": "rev-002",
        },
    )
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["status"] == "corrected"
    assert body["current_version"] == 2
    assert body["versions"][-1]["change_reason"] == "correction"

    # 纠正缺少读数应被拒绝
    third = shift_payload(
        "RW-3", "B-ANOM", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 500, "effluent": 520, "return_flow": 0}, "anom-3",
    )
    third_entry = client.post("/api/water/shifts", headers=users["operator"], json=third).json()
    missing = client.post(
        f"/api/water/shifts/{third_entry['id']}/review",
        headers=users["reviewer"],
        json={"decision": "correct", "note": "", "idempotency_key": "rev-003"},
    )
    assert missing.status_code == 422


# ---------------------------------------------------------- 批次边界与汇总


def _entry(client, headers, facility: str, batch: str, index: int, day: str, readings: dict) -> dict:
    response = client.post(
        "/api/water/shifts",
        headers=headers,
        json=shift_payload(
            facility, batch, f"{day}T06:00:00+00:00", f"{day}T14:00:00+00:00",
            readings, f"imp-{batch}-{facility}-{index}",
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_batch_boundary_cross_facility_summary_and_missing_facilities(client, users):
    make_facilities(client, users["operator"])
    _entry(client, users["operator"], "RW-1", "B-SEP", 1, "2026-09-30",
           {"influent": 1000, "effluent": 800, "return_flow": 150})
    _entry(client, users["operator"], "RW-2", "B-SEP", 1, "2026-09-30",
           {"influent": 2000, "effluent": None, "return_flow": 400})

    summary = client.get("/api/water/batches/B-SEP/summary", headers=users["operator"])
    assert summary.status_code == 200
    body = summary.json()
    assert body["entry_count"] == 2
    assert body["registered_facilities"] == ["RW-1", "RW-2"]
    assert body["missing_active_facilities"] == ["RW-3", "RW-4", "RW-5"]
    assert body["summary"]["quality"] == "interval"
    assert body["summary"]["influent"] == {"lo": 3000.0, "hi": 3000.0}
    assert body["summary"]["effluent"]["lo"] == 800.0
    assert body["summary"]["effluent"]["hi"] == 2400.0

    # 另一个批次的数据不得混入
    _entry(client, users["operator"], "RW-1", "B-OCT", 1, "2026-10-01",
           {"influent": 9999, "effluent": 1, "return_flow": 1})
    other = client.get("/api/water/batches/B-OCT/summary", headers=users["operator"]).json()
    assert other["entry_count"] == 1
    september = client.get("/api/water/batches/B-SEP/summary", headers=users["operator"]).json()
    assert september["entry_count"] == 2


def test_batch_import_is_idempotent_and_close_blocks_mutation(client, users):
    make_facilities(client, users["operator"])
    entries = [
        {
            "facility_code": f"RW-{index}",
            "starts_at": "2026-09-30T06:00:00+00:00",
            "ends_at": "2026-09-30T14:00:00+00:00",
            "readings": {"influent": 1000 + index * 100, "effluent": 800 + index * 50,
                         "return_flow": None if index == 5 else 100 + index * 10},
            "unit": "m3",
        }
        for index in range(1, 6)
    ]
    payload = {"batch_code": "B-IMPORT", "import_key": "import-file-001", "entries": entries}
    first = client.post("/api/water/imports", headers=users["operator"], json=payload)
    assert first.status_code == 201, first.text
    assert len(first.json()["imported"]) == 5

    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM water_shift_entries WHERE batch_code='B-IMPORT'").fetchone()[0] == 5

    repeat = client.post("/api/water/imports", headers=users["operator"], json=payload)
    assert repeat.status_code == 201
    assert repeat.json()["replayed"] is True
    assert repeat.json()["imported"] == first.json()["imported"]
    assert connection.execute("SELECT COUNT(*) FROM water_shift_entries WHERE batch_code='B-IMPORT'").fetchone()[0] == 5
    assert connection.execute("SELECT COUNT(*) FROM water_imports").fetchone()[0] == 1

    tampered = dict(payload)
    tampered["entries"] = [dict(entries[0])]
    clash = client.post("/api/water/imports", headers=users["operator"], json=tampered)
    assert clash.status_code == 409

    summary = client.get("/api/water/batches/B-IMPORT/summary", headers=users["reviewer"]).json()
    assert summary["entry_count"] == 5
    assert summary["missing_active_facilities"] == []
    assert summary["summary"]["quality"] == "interval"  # RW-5 回流缺测

    # 关账后补录被拒绝
    first_entry_id = client.get(
        "/api/water/shifts?batch_code=B-IMPORT&facility_code=RW-1", headers=users["reviewer"],
    ).json()["items"][0]["id"]
    closed = client.post(
        "/api/water/batches/B-IMPORT/close", headers=users["reviewer"], json={"note": "月度关账"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    mutate = client.post(
        f"/api/water/shifts/{first_entry_id}/amend",
        headers=users["operator"],
        json={"reason": "关账后尝试补录", "readings": {"influent": 1, "effluent": 1, "return_flow": 1},
              "unit": "m3", "idempotency_key": "after-close-1"},
    )
    assert mutate.status_code == 409

    close_again = client.post(
        "/api/water/batches/B-IMPORT/close", headers=users["reviewer"], json={"note": "重复关账"},
    )
    assert close_again.status_code == 409


def test_batch_close_blocked_while_anomaly_pending(client, users):
    make_facilities(client, users["operator"])
    _entry(client, users["operator"], "RW-1", "B-BLOCK", 1, "2026-09-30",
           {"influent": 500, "effluent": 600, "return_flow": 0})
    blocked = client.post("/api/water/batches/B-BLOCK/close", headers=users["reviewer"], json={"note": ""})
    assert blocked.status_code == 409
    assert "待复核" in blocked.json()["error"]["message"]


# ------------------------------------------------------------------ 权限审计


def test_permissions_and_audit_trail(client, admin, users):
    make_facilities(client, users["operator"])

    no_token = client.post(
        "/api/water/shifts",
        json=shift_payload("RW-1", "B-AUD", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 1, "effluent": 1, "return_flow": 1}, "aud-001"),
    )
    assert no_token.status_code == 401

    no_perm = client.post(
        "/api/water/shifts",
        headers=users["nobody"],
        json=shift_payload("RW-1", "B-AUD", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 1, "effluent": 1, "return_flow": 1}, "aud-001"),
    )
    assert no_perm.status_code == 403

    all_missing = client.post(
        "/api/water/shifts",
        headers=users["operator"],
        json=shift_payload("RW-1", "B-AUD", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": None, "effluent": None, "return_flow": None}, "aud-empty"),
    )
    assert all_missing.status_code == 422

    entry = client.post(
        "/api/water/shifts",
        headers=users["operator"],
        json=shift_payload("RW-1", "B-AUD", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 1000, "effluent": 800, "return_flow": 150}, "aud-001"),
    ).json()
    client.post(
        f"/api/water/shifts/{entry['id']}/amend",
        headers=users["operator"],
        json={"reason": "补录读数", "readings": {"influent": 1000, "effluent": 790, "return_flow": 150},
              "unit": "m3", "idempotency_key": "aud-002"},
    )

    # 审计角色只读：可以看台账与审计流，不能写
    read = client.get("/api/water/batches/B-AUD/summary", headers=users["auditor"])
    assert read.status_code == 200
    write = client.post(
        "/api/water/facilities",
        headers=users["auditor"],
        json={"code": "RW-X", "name": "越权设施", "capacity_per_shift": 10},
    )
    assert write.status_code == 403

    events = client.get("/api/audit?resource_type=water_shift_entry", headers=admin["headers"])
    assert events.status_code == 200
    actions = {item["action"] for item in events.json()["data"]}
    assert {"water.shift.register", "water.shift.amend"} <= actions
    register_event = next(item for item in events.json()["data"] if item["action"] == "water.shift.register")
    assert register_event["actor_name"] == "water.ops"
    assert register_event["outcome"] == "success"

    facility_events = client.get("/api/audit?action=water.facility.create", headers=admin["headers"])
    assert facility_events.json()["total"] == 5

    denied_events = client.get("/api/audit?action=water.access_denied", headers=admin["headers"])
    assert denied_events.status_code == 200
    denied_body = denied_events.json()
    assert denied_body["total"] >= 1
    denied_item = next(
        item for item in denied_body["data"]
        if item["actor_name"] == "water.noauth"
        and json.loads(item["metadata_json"])["required_permission"] == "water.write"
    )
    assert denied_item["outcome"] == "denied"
    assert denied_item["actor_name"] == "water.noauth"


def test_batch_explicit_create_rejects_duplicate(client, users):
    make_facilities(client, users["operator"], count=1)
    first = client.post(
        "/api/water/batches", headers=users["operator"],
        json={"batch_code": "B-MANUAL", "note": "人工建批"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/water/batches", headers=users["operator"],
        json={"batch_code": "B-MANUAL", "note": "重复建批"},
    )
    assert second.status_code == 409


# ---------------------------------------------------------- 服务层：冻结时钟


def test_service_rollback_with_frozen_clock(client, users):
    make_facilities(client, users["operator"])
    clock = FrozenClock(datetime(2026, 9, 30, 14, 0, tzinfo=UTC))
    service = WaterLedgerService(get_connection(), clock)
    payload = shift_payload(
        "RW-1", "B-SVC", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
        {"influent": 1000, "effluent": 800, "return_flow": 150}, "svc-001",
    )
    from app.services.audit import AuditContext

    entry = service.register(payload, AuditContext(None, "tester"))
    clock.advance(hours=1)
    amended = service.amend(
        entry["id"],
        {"reason": "补录", "readings": {"influent": 1000, "effluent": 800, "return_flow": 160},
         "unit": "m3", "idempotency_key": "svc-002"},
        AuditContext(None, "tester"),
    )
    assert amended["current_version"] == 2
    assert amended["versions"][1]["created_at"] > amended["versions"][0]["created_at"]


def test_amend_blocked_while_anomaly_pending_then_allowed_after_review(client, users):
    make_facilities(client, users["operator"])
    entry = client.post(
        "/api/water/shifts", headers=users["operator"],
        json=shift_payload("RW-1", "B-EDGE", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 500, "effluent": 600, "return_flow": 0}, "edge-anom-001"),
    ).json()
    assert entry["status"] == "anomaly"

    blocked = client.post(
        f"/api/water/shifts/{entry['id']}/amend", headers=users["operator"],
        json={"reason": "运维员试图绕过复核直接改数", "readings": {"influent": 700, "effluent": 600, "return_flow": 10},
              "unit": "m3", "idempotency_key": "edge-amend-001"},
    )
    assert blocked.status_code == 409

    client.post(
        f"/api/water/shifts/{entry['id']}/review", headers=users["reviewer"],
        json={"decision": "confirm", "note": "维持原值", "idempotency_key": "edge-rev-001"},
    )
    allowed = client.post(
        f"/api/water/shifts/{entry['id']}/amend", headers=users["operator"],
        json={"reason": "下一班次前常规补录", "readings": {"influent": 700, "effluent": 600, "return_flow": 10},
              "unit": "m3", "idempotency_key": "edge-amend-002"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "confirmed"
    assert allowed.json()["current_version"] == 2


def test_idempotency_key_cannot_span_shift_amends(client, users):
    make_facilities(client, users["operator"])
    first = client.post(
        "/api/water/shifts", headers=users["operator"],
        json=shift_payload("RW-1", "B-EDGE2", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 1000, "effluent": 800, "return_flow": 150}, "span-reg-001"),
    ).json()
    second = client.post(
        "/api/water/shifts", headers=users["operator"],
        json=shift_payload("RW-2", "B-EDGE2", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 1000, "effluent": 800, "return_flow": 150}, "span-reg-002"),
    ).json()
    client.post(
        f"/api/water/shifts/{first['id']}/amend", headers=users["operator"],
        json={"reason": "班次一补录", "readings": {"influent": 1000, "effluent": 810, "return_flow": 150},
              "unit": "m3", "idempotency_key": "span-amend-001"},
    )
    cross = client.post(
        f"/api/water/shifts/{second['id']}/amend", headers=users["operator"],
        json={"reason": "班次二误用同一补录键", "readings": {"influent": 1000, "effluent": 820, "return_flow": 150},
              "unit": "m3", "idempotency_key": "span-amend-001"},
    )
    assert cross.status_code == 409


def test_timezone_offsets_normalize_to_utc_for_continuity(client, users):
    make_facilities(client, users["operator"])
    first = client.post(
        "/api/water/shifts", headers=users["operator"],
        json=shift_payload("RW-1", "B-TZ", "2026-09-30T14:00:00+08:00", "2026-09-30T22:00:00+08:00",
                           {"influent": 1000, "effluent": 800, "return_flow": 100}, "tz-reg-001"),
    )
    assert first.status_code == 201, first.text
    # 同一物理时刻（06:00Z）用 +00:00 登记，归一化后应视为同一班次而拒绝
    duplicate = client.post(
        "/api/water/shifts", headers=users["operator"],
        json=shift_payload("RW-1", "B-TZ", "2026-09-30T06:00:00+00:00", "2026-09-30T14:00:00+00:00",
                           {"influent": 1000, "effluent": 800, "return_flow": 100}, "tz-reg-002"),
    )
    assert duplicate.status_code == 409
    assert "补录接口" in duplicate.json()["error"]["message"]
