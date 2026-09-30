from __future__ import annotations

import pytest

POINTS = [
    {"code": "IN-1", "metric": "influent", "unit": "m3"},
    {"code": "OUT-1", "metric": "effluent", "unit": "m3"},
    {"code": "RT-1", "metric": "return_flow", "unit": "m3"},
]


def auth(client, username="admin", password="Admin!23456"):
    client.post("/api/auth/bootstrap", json={"username": username, "password": password, "client_label": "tests"})
    token = client.post("/api/auth/login", json={"username": username, "password": password, "client_label": "tests"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def make_facility(client, headers, code="plant-1", name="一号再生水系统", points=None):
    r = client.post("/api/water/facilities", headers=headers, json={"code": code, "name": name})
    assert r.status_code == 201, r.text
    r = client.put(f"/api/water/facilities/{code}/meter-points", headers=headers,
                   json={"points": points or POINTS})
    assert r.status_code == 200, r.text


def reading(code, start, end, unit="m3", *, start_at=None, end_at=None):
    item = {"point_code": code, "start_value": start, "end_value": end, "unit": unit}
    if start_at:
        item["start_observed_at"] = start_at
    if end_at:
        item["end_observed_at"] = end_at
    return item


def shift_entry(code="plant-1", date="2026-09-29", seq=1, start="2026-09-29T00:00:00Z",
                end="2026-09-29T08:00:00Z", readings=None):
    return {"facility_code": code, "shift_date": date, "shift_seq": seq,
            "starts_at": start, "ends_at": end, "readings": readings or []}


def register(client, headers, entry, key=None, expected=201):
    payload = dict(entry)
    if key:
        payload["idempotency_key"] = key
    r = client.post("/api/water/shifts", headers=headers, json=payload)
    assert r.status_code == expected, r.text
    return r.json() if r.status_code < 300 else r


# ---------- 登记、单位校验、时间连续性 -------------------------------------

def test_unit_validation_and_litre_conversion(client):
    headers = auth(client)
    make_facility(client, headers, points=[
        {"code": "OUT-L", "metric": "effluent", "unit": "L"},
        {"code": "IN-1", "metric": "influent", "unit": "m3"},
    ])
    # 单位不匹配被拒绝（而不是静默换算）。
    bad = shift_entry(readings=[reading("OUT-L", 80000, 110000, "m3")])
    r = client.post("/api/water/shifts", headers=headers, json=bad)
    assert r.status_code == 422
    assert r.json()["error"]["context"]["expected_unit"] == "L"
    # 未知表计同样拒绝。
    bad = shift_entry(readings=[reading("GHOST", 1, 2, "m3")])
    assert client.post("/api/water/shifts", headers=headers, json=bad).status_code == 422
    # 升按 1000:1 换算为立方米参与计算。
    entry = shift_entry(readings=[
        reading("OUT-L", 80000, 110000, "L"),
        reading("IN-1", 100, 130, "m3"),
    ])
    detail = register(client, headers, entry)
    out = next(row for row in detail["readings"] if row["point_code"] == "OUT-L")
    assert out["start_m3"] == 80.0 and out["end_m3"] == 110.0 and out["volume_m3"] == 30.0


def test_time_continuity_flags_and_cross_day_shift(client):
    headers = auth(client)
    make_facility(client, headers)
    # 直接登记次日 1 班（此前无任何班次）：首个班次不打连续性标记。
    first = shift_entry(date="2026-09-30", seq=1, start="2026-09-30T00:00:00Z", end="2026-09-30T08:00:00Z",
                        readings=[reading("IN-1", 250, 300)])
    detail = register(client, headers, first)
    assert detail["issue_flags"] == []
    # 中间缺 2 班直接登记 3 班：序号不连续。
    third = shift_entry(date="2026-09-30", seq=3, start="2026-09-30T16:00:00Z", end="2026-09-30T23:59:59Z",
                        readings=[reading("IN-1", 360, 400)])
    detail = register(client, headers, third)
    assert "sequence_gap" in detail["issue_flags"]
    # 跨日：登记 9-29 三班延伸至次日零点、再接 9-30 一班，时间严格衔接，无 time_gap。
    make_facility(client, headers, code="plant-2", name="二号")
    late = shift_entry(code="plant-2", date="2026-09-29", seq=3,
                       start="2026-09-29T16:00:00Z", end="2026-09-30T00:00:00Z",
                       readings=[reading("IN-1", 190, 250)])
    detail = register(client, headers, late)
    assert "time_gap" not in detail["issue_flags"]
    nextday = shift_entry(code="plant-2", date="2026-09-30", seq=1,
                          start="2026-09-30T00:00:00Z", end="2026-09-30T08:00:00Z",
                          readings=[reading("IN-1", 250, 300)])
    detail = register(client, headers, nextday)
    assert detail["issue_flags"] == []
    # 存在 2 秒空档则标记 time_gap。
    gapped = shift_entry(code="plant-2", date="2026-09-30", seq=2,
                         start="2026-09-30T08:00:02Z", end="2026-09-30T16:00:00Z",
                         readings=[reading("IN-1", 300, 360)])
    detail = register(client, headers, gapped)
    assert "time_gap" in detail["issue_flags"]


# ---------- 部分缺测 --------------------------------------------------------

def test_partial_missing_anchor_and_unbounded_interval(client):
    headers = auth(client)
    make_facility(client, headers)
    payload = {
        "batch_key": "batch-missing-01",
        "entries": [
            shift_entry(seq=1, readings=[
                reading("IN-1", 100, 140), reading("OUT-1", 80, 110), reading("RT-1", 10, 16)]),
            shift_entry(seq=2, start="2026-09-29T08:00:00Z", end="2026-09-29T16:00:00Z", readings=[
                reading("IN-1", 140, 190), reading("RT-1", 16, 24),
                # 出水接班读数缺测，沿用上一班末读数锚定。
                reading("OUT-1", None, 145)]),
            # 第三班整表出水缺登记（readings 中不含 OUT-1）。
            shift_entry(seq=3, start="2026-09-29T16:00:00Z", end="2026-09-30T00:00:00Z",
                        readings=[reading("IN-1", 190, 250), reading("RT-1", 24, 30)]),
        ],
    }
    r = client.post("/api/water/batches/import", headers=headers, json=payload)
    assert r.status_code == 201, r.text

    third = client.get("/api/water/shifts/3", headers=headers).json()
    assert "OUT-1" in third["missing_points"]
    # 单班次视角：本班出水未知，下界为 0、上界无界。
    assert third["volumes"]["effluent"]["bounded"] is False
    assert third["volumes"]["effluent"]["high"] is None
    assert third["volumes"]["effluent"]["low"] == 0.0
    # 进水、回流齐全，循环率仍可精确计算；损耗无法给点值。
    assert third["recycle_rate"]["point"] == pytest.approx(0.1)
    assert third["loss"]["point"] is None and third["loss"]["bounded"] is False

    # 回看二班：接班缺测沿用上一班末读数 110 → 体积 35，且显式标记。
    second = client.get("/api/water/shifts/2", headers=headers).json()
    out = next(row for row in second["readings"] if row["point_code"] == "OUT-1")
    assert out["volume_m3"] == 35.0 and out["volume_status"] == "carried"
    assert "carried_start" in out["issue_flags"]

    # 批次累计下界：班1 的 30 + 班2 锚定的 35 + 班3 的 0 = 65，上界无界。
    summary = client.get("/api/water/batches/batch-missing-01/summary", headers=headers).json()
    effluent = summary["totals"]["volumes"]["effluent"]
    assert effluent["low"] == pytest.approx(65.0) and effluent["high"] is None
    assert effluent["missing_shifts"] == 1


# ---------- 补录版本与差异摘要 ---------------------------------------------

def test_revision_creates_immutable_version_and_diff(client):
    headers = auth(client)
    make_facility(client, headers)
    detail = register(client, headers, shift_entry(readings=[
        reading("IN-1", 100, 140), reading("OUT-1", 80, 110), reading("RT-1", 10, 16),
    ]), key="reg-key-0001")
    shift_id = detail["id"]

    # 幂等键重放不产生新数据。
    again = register(client, headers, shift_entry(readings=[
        reading("IN-1", 100, 140), reading("OUT-1", 80, 110), reading("RT-1", 10, 16),
    ]), key="reg-key-0001")
    assert again["id"] == shift_id

    # 补录出水交班读数（仪表读数补登）。
    r = client.post(f"/api/water/shifts/{shift_id}/revisions", headers=headers, json={
        "reason": "出水仪表交班读数补录",
        "readings": [reading("OUT-1", 80, 112)],
    })
    assert r.status_code == 201, r.text
    revised = r.json()
    assert revised["current_version"] == 2
    # 未补录的表计原样继承到新版本快照。
    inherited = {row["point_code"]: row for row in revised["readings"]}
    assert inherited["IN-1"]["end_value"] == 140
    assert inherited["IN-1"]["inherited_from_version"] == 1
    assert inherited["OUT-1"]["end_value"] == 112

    # 差异摘要给出前后读数与指标变化。
    diff = client.get(f"/api/water/shifts/{shift_id}/diff?from_version=1&to_version=2", headers=headers).json()
    assert len(diff["reading_changes"]) == 1
    change = diff["reading_changes"][0]
    assert change["point_code"] == "OUT-1" and change["before"]["end_value"] == 110 and change["after"]["end_value"] == 112
    assert diff["metric_summary"]["before"]["loss"]["point"] == pytest.approx(4.0)
    assert diff["metric_summary"]["after"]["loss"]["point"] == pytest.approx(2.0)

    # 历史版本行在 SQLite 中不可变。
    from app.database import get_connection
    conn = get_connection()
    old_rows = conn.execute(
        "SELECT end_value FROM ww_reading_versions WHERE registration_id=? AND version=1 AND point_id="
        "(SELECT id FROM ww_meter_points WHERE code='OUT-1')", (shift_id,)).fetchall()
    assert [row[0] for row in old_rows] == [110.0]
    versions = conn.execute(
        "SELECT version FROM ww_shift_versions WHERE registration_id=? ORDER BY version", (shift_id,)).fetchall()
    assert [row[0] for row in versions] == [1, 2]

    # 事件链留痕。
    events = client.get(f"/api/water/shifts/{shift_id}", headers=headers).json()["events"]
    assert [(e["action"], e["from_version"], e["to_version"]) for e in events] == [
        ("register", None, 1), ("revise", 1, 2)]

    # 重复提交班次被拒绝，引导走补录接口。
    dup = client.post("/api/water/shifts", headers=headers, json=shift_entry(readings=[
        reading("IN-1", 100, 140), reading("OUT-1", 80, 110), reading("RT-1", 10, 16)]))
    assert dup.status_code == 409

    # 与当前版本完全相同的补录不产生新版本。
    same = client.post(f"/api/water/shifts/{shift_id}/revisions", headers=headers, json={
        "reason": "无实质变化", "readings": [reading("OUT-1", 80, 112)]})
    assert same.status_code == 422
    assert client.get(f"/api/water/shifts/{shift_id}", headers=headers).json()["current_version"] == 2


# ---------- 版本回滚 --------------------------------------------------------

def test_rollback_restores_pointer_and_history_stays_viewable(client):
    headers = auth(client)
    make_facility(client, headers)
    detail = register(client, headers, shift_entry(readings=[
        reading("IN-1", 100, 140), reading("OUT-1", 80, 110), reading("RT-1", 10, 16)]))
    shift_id = detail["id"]
    client.post(f"/api/water/shifts/{shift_id}/revisions", headers=headers, json={
        "reason": "第一次补录", "readings": [reading("OUT-1", 80, 112)]})
    client.post(f"/api/water/shifts/{shift_id}/revisions", headers=headers, json={
        "reason": "第二次补录", "readings": [reading("RT-1", 10, 18)]})

    r = client.post(f"/api/water/shifts/{shift_id}/rollback", headers=headers,
                    json={"version": 1, "reason": "补录依据不足"})
    assert r.status_code == 200, r.text
    assert r.json()["current_version"] == 1

    # 回滚后默认视图即 v1，指标恢复。
    current = client.get(f"/api/water/shifts/{shift_id}", headers=headers).json()
    assert current["loss"]["point"] == pytest.approx(4.0)
    # v2/v3 仍可查看。
    assert client.get(f"/api/water/shifts/{shift_id}?version=2", headers=headers).json()["viewing_version"] == 2
    assert client.get(f"/api/water/shifts/{shift_id}?version=3", headers=headers).json()["viewing_version"] == 3
    # 回滚到不存在版本/当前版本分别报错。
    assert client.post(f"/api/water/shifts/{shift_id}/rollback", headers=headers,
                       json={"version": 9, "reason": "x" * 2}).status_code == 404
    assert client.post(f"/api/water/shifts/{shift_id}/rollback", headers=headers,
                       json={"version": 1, "reason": "重复回滚" }).status_code == 409
    # 回滚后再补录：基于当前 v1 生成 v4（不覆盖 v2/v3）。
    r = client.post(f"/api/water/shifts/{shift_id}/revisions", headers=headers, json={
        "reason": "回滚后重新核定", "readings": [reading("OUT-1", 80, 108)]})
    assert r.status_code == 201
    assert r.json()["current_version"] == 4
    versions = [v["version"] for v in r.json()["versions"]]
    assert versions == [1, 2, 3, 4]
    # 事件链包含 rollback。
    actions = [e["action"] for e in client.get(f"/api/water/shifts/{shift_id}", headers=headers).json()["events"]]
    assert actions == ["register", "revise", "revise", "rollback", "revise"]


# ---------- 异常读数待复核 --------------------------------------------------

def test_abnormal_reading_goes_to_pending_not_silently_fixed(client):
    headers = auth(client)
    make_facility(client, headers)
    register(client, headers, shift_entry(seq=1, readings=[
        reading("IN-1", 100, 140), reading("OUT-1", 80, 110), reading("RT-1", 10, 16)]))
    # 二班进水计数器倒退 → 异常，进入待复核；体积不参与精确值。
    detail = register(client, headers, shift_entry(
        seq=2, start="2026-09-29T08:00:00Z", end="2026-09-29T16:00:00Z",
        readings=[
            reading("IN-1", 140, 135), reading("OUT-1", 110, 145), reading("RT-1", 16, 24)]))
    assert detail["pending_readings"] == 1
    abnormal = next(row for row in detail["readings"] if row["point_code"] == "IN-1")
    assert abnormal["review_status"] == "pending"
    assert "negative_delta" in abnormal["issue_flags"]
    assert abnormal["volume_status"] == "abnormal"
    # 待复核列表可查。
    pending = client.get("/api/water/reviews/pending", headers=headers).json()["items"]
    assert len(pending) == 1 and pending[0]["point_code"] == "IN-1"
    reading_id = abnormal["reading_id"]

    # 无复核权限的账号不能处理（见权限测试），管理员确认换表归零后按登记差值计。
    r = client.post(f"/api/water/reviews/{reading_id}/resolution", headers=headers,
                    json={"action": "confirm", "resolution": "已核实为换表归零"})
    assert r.status_code == 200 and r.json()["review_status"] == "resolved"
    confirmed = client.get("/api/water/shifts/2", headers=headers).json()
    row = next(x for x in confirmed["readings"] if x["point_code"] == "IN-1")
    assert row["volume_m3"] == -5.0 and row["review_status"] == "resolved"

    # 另一条负值读数：驳回后按缺测处理，区间变为无界。
    detail = register(client, headers, shift_entry(
        seq=3, start="2026-09-29T16:00:00Z", end="2026-09-30T00:00:00Z",
        readings=[
            reading("IN-1", -10, 60), reading("OUT-1", 145, 185), reading("RT-1", 24, 30)]))
    bad = next(row for row in detail["readings"] if row["point_code"] == "IN-1")
    assert "negative_value" in bad["issue_flags"] and bad["review_status"] == "pending"
    client.post(f"/api/water/reviews/{bad['reading_id']}/resolution", headers=headers,
                json={"action": "reject", "resolution": "读数无效，等待重新抄录"})
    after = client.get("/api/water/shifts/3", headers=headers).json()
    inflow = after["volumes"]["influent"]
    assert inflow["bounded"] is False and inflow["high"] is None
    # 已处理的读数不能重复处理。
    assert client.post(f"/api/water/reviews/{bad['reading_id']}/resolution", headers=headers,
                       json={"action": "confirm", "resolution": "再次处理"}).status_code == 409


# ---------- 跨设施批次：边界、幂等、区间 ------------------------------------

def _batch_payload():
    def entry(plant, date, seq, start, end, vals):
        return {"facility_code": plant, "shift_date": date, "shift_seq": seq,
                "starts_at": start, "ends_at": end,
                "readings": [reading(k, *v) for k, v in vals.items()]}

    return {
        "batch_key": "batch-2026-0929-day",
        "reason": "五套系统 9 月 29 日全天读数导入",
        "entries": [
            entry("plant-1", "2026-09-29", 1, "2026-09-29T00:00:00Z", "2026-09-29T08:00:00Z",
                  {"IN-1": (100, 140), "OUT-1": (80, 110), "RT-1": (10, 16)}),
            entry("plant-1", "2026-09-29", 2, "2026-09-29T08:00:00Z", "2026-09-29T16:00:00Z",
                  {"IN-1": (140, 190), "OUT-1": (110, 145), "RT-1": (16, 24)}),
            entry("plant-2", "2026-09-29", 1, "2026-09-29T00:00:00Z", "2026-09-29T08:00:00Z",
                  {"IN-1": (200, 260), "OUT-1": (160, 200), "RT-1": (20, 32)}),
        ],
    }


def test_batch_import_idempotent_and_summary_scoped_to_boundary(client):
    headers = auth(client)
    make_facility(client, headers, "plant-1", "一号")
    make_facility(client, headers, "plant-2", "二号")
    payload = _batch_payload()

    r = client.post("/api/water/batches/import", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["replayed"] is False and len(first["shifts"]) == 3

    # 重复导入：重放同一结果，不产生重复班次。
    r = client.post("/api/water/batches/import", headers=headers, json=payload)
    assert r.status_code == 201 and r.json()["replayed"] is True
    shifts = client.get("/api/water/shifts?limit=500", headers=headers).json()["items"]
    assert len(shifts) == 3

    # 同一批次号但内容不同 → 冲突。
    tampered = _batch_payload()
    tampered["entries"][0]["readings"][0]["end_value"] = 999
    assert client.post("/api/water/batches/import", headers=headers, json=tampered).status_code == 409

    summary = client.get("/api/water/batches/batch-2026-0929-day/summary", headers=headers).json()
    assert summary["boundary_respected"] is True
    assert summary["shift_count"] == 3 and summary["facility_count"] == 2
    # plant-1 进水 90 + plant-2 进水 60 = 150；回流 14 + 12 = 26；出水 65 + 40 = 105。
    totals = summary["totals"]
    assert totals["volumes"]["influent"]["point"] == pytest.approx(150.0)
    assert totals["volumes"]["return_flow"]["point"] == pytest.approx(26.0)
    assert totals["volumes"]["effluent"]["point"] == pytest.approx(105.0)
    assert totals["recycle_rate"]["point"] == pytest.approx(26.0 / 150.0)
    assert totals["loss"]["point"] == pytest.approx(150.0 - 105.0 - 26.0)
    assert {f["facility_code"] for f in summary["facilities"]} == {"plant-1", "plant-2"}

    # 在批次外再登记一班次：汇总不得把它计入该批次。
    extra = shift_entry(code="plant-1", date="2026-09-30", seq=1,
                        start="2026-09-30T00:00:00Z", end="2026-09-30T08:00:00Z",
                        readings=[reading("IN-1", 900, 999)])
    register(client, headers, extra)
    summary = client.get("/api/water/batches/batch-2026-0929-day/summary", headers=headers).json()
    assert summary["shift_count"] == 3
    assert summary["totals"]["volumes"]["influent"]["point"] == pytest.approx(150.0)


def test_batch_summary_intervals_with_partial_missing(client):
    headers = auth(client)
    make_facility(client, headers, "plant-1", "一号")
    make_facility(client, headers, "plant-2", "二号")
    payload = _batch_payload()
    # plant-2 的出水交班读数缺测，且该批次没有相邻班次可锚定（plant-2 只有一班）。
    payload["entries"][2]["readings"][1] = reading("OUT-1", 160, None)
    client.post("/api/water/batches/import", headers=headers, json=payload)
    summary = client.get("/api/water/batches/batch-2026-0929-day/summary", headers=headers).json()
    totals = summary["totals"]
    assert totals["volumes"]["effluent"]["bounded"] is False
    assert totals["volumes"]["effluent"]["low"] == pytest.approx(65.0)  # 仅 plant-1
    assert totals["volumes"]["effluent"]["high"] is None
    # 损耗 = 150 - 出水 - 26：出水 ∈ [65,∞)，故损耗 ∈ (-∞, 59]，下界无界、上界 59。
    loss = totals["loss"]
    assert loss["bounded"] is False and loss["low"] is None
    assert loss["high"] == pytest.approx(59.0)
    # 回流/进水有界，循环率仍可给点值与下界。
    assert totals["recycle_rate"]["point"] == pytest.approx(26.0 / 150.0)


# ---------- 权限与审计 ------------------------------------------------------

def test_permission_enforcement_and_denied_audit(client):
    # 未认证。
    assert client.get("/api/water/facilities").status_code == 401
    admin_headers = auth(client)
    make_facility(client, admin_headers)
    # 创建一个无任何角色的普通账号。
    r = client.post("/api/users", headers=admin_headers, json={
        "username": "clerk01", "password": "Clerk!12345", "display_name": "无权限办事员"})
    assert r.status_code == 201, r.text
    login = client.post("/api/auth/login", json={
        "username": "clerk01", "password": "Clerk!12345", "client_label": "tests"})
    clerk_headers = {"Authorization": f"Bearer {login.json()['token']}"}

    assert client.get("/api/water/facilities", headers=clerk_headers).status_code == 403
    assert client.post("/api/water/shifts", headers=clerk_headers, json=shift_entry()).status_code == 403
    # 拒绝事件进入审计。
    events = client.get("/api/audit?resource_type=water&outcome=denied", headers=admin_headers).json()
    denied = [item for item in events["data"] if item["action"].startswith("water.access_denied")]
    assert len(denied) >= 2
    assert any("water.write" in e["action"] for e in denied)

    # 仅授予 water.read 的角色：可读不可写、不可复核。
    client.post("/api/roles", headers=admin_headers, json={
        "code": "water-viewer", "name": "再生水查看员", "permission_codes": ["water.read"]})
    # 给办事员替换角色。
    me = client.get("/api/auth/me", headers=clerk_headers).json()
    r = client.put(f"/api/users/{me['user_id']}/roles", headers=admin_headers,
                   json={"role_codes": ["water-viewer"]})
    assert r.status_code == 200, r.text
    assert client.get("/api/water/facilities", headers=clerk_headers).status_code == 200
    assert client.post("/api/water/shifts", headers=clerk_headers, json=shift_entry()).status_code == 403
    assert client.get("/api/water/reviews/pending", headers=clerk_headers).status_code == 403
