"""班次水量的纯计算函数：连续性校验、循环率与损耗区间。

所有体积统一为立方米（m³），升（L）由服务层在进入本模块前换算。
区间用包含 ``low`` / ``high`` 的字典表示，``high=None`` 表示上界无界。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

METRICS = ("influent", "effluent", "return_flow")
METRIC_LABELS = {"influent": "进水", "effluent": "出水", "return_flow": "回流"}

TOLERANCE_ABS = 1e-4
TOLERANCE_REL = 1e-6

FLAG_LABELS = {
    "carried_start": "接班读数缺测，沿用上一班次末读数",
    "carried_end": "交班读数缺测，锚定下一班次首读数",
    "continuity_gap": "与上一班次末读数不一致",
    "time_gap": "与上一班次存在时间空档",
    "negative_delta": "表计差值为负",
    "missing_open": "读数缺测且无法由相邻班次推断",
    "rejected": "读数已被复核驳回，按缺测处理",
}


def to_cubic_meters(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    return value / 1000.0 if unit == "L" else value


def continuity_ok(previous: float, current: float) -> bool:
    return abs(previous - current) <= max(TOLERANCE_REL * max(abs(previous), abs(current), 1.0), TOLERANCE_ABS)


def is_adjacent(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """判断两条班次快照在班次序列上是否相邻（含跨日 3 班 → 次日 1 班）。"""
    first_date = first.get("shift_date")
    second_date = second.get("shift_date")
    if first_date is None or second_date is None:
        return False
    first_seq = int(first["shift_seq"])
    second_seq = int(second["shift_seq"])
    if second_seq == first_seq + 1 and second_date == first_date:
        return True
    if first_seq == 3 and second_seq == 1:
        expected = date.fromisoformat(first_date) + timedelta(days=1)
        return second_date == expected.isoformat()
    return False


def resolve_point_volumes(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按时间顺序解析同一表计在各班次的体积与标记。

    chain 元素：{"shift_id", "shift_date", "shift_seq", "start", "end",
    "review_status"}，start/end 为立方米计数器读数，缺测为 None。
    只有逻辑相邻的班次才允许锚定缺测边界与比对连续性，推断一律显式打标，
    绝不静默改写原始读数。
    """
    results: list[dict[str, Any]] = []
    count = len(chain)
    for index, item in enumerate(chain):
        raw_start = item.get("start")
        raw_end = item.get("end")
        start, end = raw_start, raw_end
        flags: list[str] = []
        prev_item = chain[index - 1] if index > 0 else None
        next_item = chain[index + 1] if index + 1 < count else None
        previous_end = prev_item.get("end") if prev_item is not None and is_adjacent(prev_item, item) else None
        next_start = next_item.get("start") if next_item is not None and is_adjacent(item, next_item) else None

        if start is None and previous_end is not None:
            start = previous_end
            flags.append("carried_start")
        if end is None and next_start is not None:
            end = next_start
            flags.append("carried_end")
        if raw_start is not None and previous_end is not None and not continuity_ok(previous_end, raw_start):
            flags.append("continuity_gap")

        review = item.get("review_status", "ok")
        volume: float | None = None
        status = "measured"
        if review == "rejected":
            flags.append("rejected")
            status = "missing"
        elif start is None or end is None:
            flags.append("missing_open")
            status = "missing"
        elif end < start and not continuity_ok(end, start):
            flags.append("negative_delta")
            if review == "resolved":
                # 复核确认（如换表归零），按登记差值参与计算。
                volume = end - start
                status = "measured"
            else:
                status = "abnormal"
        else:
            volume = end - start
            if any(flag in flags for flag in ("carried_start", "carried_end")):
                status = "carried"

        results.append({
            "shift_id": item.get("shift_id"),
            "start_m3": start,
            "end_m3": end,
            "volume_m3": volume,
            "status": status,
            "flags": flags,
            "review_status": review,
        })
    return results


def aggregate_volumes(per_shift: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把某指标各班次体积汇总为区间。

    已知体积之和给出下界；只要存在缺测/异常班次，上界即无界（high=None），
    避免输出虚假的精确值。复核确认的负差值会拉低下界。
    """
    values = [item["volume_m3"] for item in per_shift]
    known = [value for value in values if value is not None]
    if len(known) == len(values):
        total = sum(known)
        return {"low": total, "high": total, "point": total, "bounded": True,
                "known_shifts": len(known), "missing_shifts": 0}
    low = sum(known)
    return {"low": low, "high": None, "point": None, "bounded": False,
            "known_shifts": len(known), "missing_shifts": len(values) - len(known)}


def recycle_interval(influent: dict[str, Any], returned: dict[str, Any]) -> dict[str, Any]:
    """循环率 = 回流 / 进水，按区间传播。"""
    point = None
    if influent.get("point") not in (None, 0) and returned.get("point") is not None:
        point = returned["point"] / influent["point"]
    if not influent["bounded"] or not returned["bounded"]:
        denominator_high = influent["high"]
        low = returned["low"] / denominator_high if denominator_high else 0.0
        return {"low": low, "high": None, "point": point, "bounded": False}
    low = returned["low"] / influent["high"] if influent["high"] else 0.0
    high = returned["high"] / influent["low"] if influent["low"] else None
    return {"low": low, "high": high, "point": point, "bounded": high is not None}


def loss_interval(influent: dict[str, Any], effluent: dict[str, Any], returned: dict[str, Any]) -> dict[str, Any]:
    """损耗 = 进水 - 出水 - 回流，按独立区间做最坏情况组合。"""
    point = None
    if all(metric.get("point") is not None for metric in (influent, effluent, returned)):
        point = influent["point"] - effluent["point"] - returned["point"]
    bounded = influent["bounded"] and effluent["bounded"] and returned["bounded"]
    if bounded:
        return {
            "low": influent["low"] - effluent["high"] - returned["high"],
            "high": influent["high"] - effluent["low"] - returned["low"],
            "point": point,
            "bounded": True,
        }
    low = None if (effluent["high"] is None or returned["high"] is None) else influent["low"] - effluent["high"] - returned["high"]
    high = None if influent["high"] is None else influent["high"] - effluent["low"] - returned["low"]
    return {"low": low, "high": high, "point": point, "bounded": False}
