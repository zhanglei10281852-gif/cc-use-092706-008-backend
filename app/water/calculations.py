"""再生水班次水量平衡计算（纯函数，便于追溯与单测）。

质量平衡关系：进水 = 出水 + 回流 + 损耗
- 循环率（回流率）= 回流 / 进水
- 仪表部分缺测时不做点估计，而是在物理可行多面体上求极值给出区间。
- 异常读数不在计算层静默修正，只标记违反的物理约束。

可行多面体（I=进水, E=出水, R=回流, C=设施班次能力）：
    I >= 0, E >= 0, R >= 0, I <= C, E <= I, R <= I, E + R <= I
极值通过枚举活跃约束交点（多面体顶点）求得。
"""

from __future__ import annotations

import itertools
from typing import Any

INFLUENT = "influent"
EFFLUENT = "effluent"
RETURN_FLOW = "return_flow"

METRICS = (INFLUENT, EFFLUENT, RETURN_FLOW)

METRIC_LABELS = {
    INFLUENT: "进水",
    EFFLUENT: "出水",
    RETURN_FLOW: "回流",
}

VIOLATION_MESSAGES = {
    "negative_reading": "存在负读数",
    "above_capacity": "进水超过设施班次设计能力",
    "effluent_exceeds_influent": "出水大于进水",
    "return_exceeds_influent": "回流大于进水",
    "balance_negative_loss": "进水不足以平衡出水与回流",
    "zero_influent": "进水为零但存在回流，循环率不可定义",
    "infeasible_physical_bounds": "已知读数在物理约束下无解（如出水/回流超过设施能力）",
}

FORMULA_VERSION = "water-balance-v1"
_EPS = 1e-7

# 约束写作 a_I*I + a_E*E + a_R*R <= b
_BASE_CONSTRAINTS: list[tuple[tuple[float, float, float], float]] = [
    ((-1.0, 0.0, 0.0), 0.0),    # I >= 0
    ((0.0, -1.0, 0.0), 0.0),    # E >= 0
    ((0.0, 0.0, -1.0), 0.0),    # R >= 0
    ((1.0, 0.0, 0.0), 0.0),     # I <= C （右端在运行时替换为能力）
    ((-1.0, 1.0, 0.0), 0.0),    # E <= I
    ((-1.0, 0.0, 1.0), 0.0),    # R <= I
    ((-1.0, 1.0, 1.0), 0.0),    # E + R <= I
]
_VAR_INDEX = {INFLUENT: 0, EFFLUENT: 1, RETURN_FLOW: 2}


def _violations(present: dict[str, float], capacity: float) -> list[str]:
    flags: list[str] = []
    if any(value < -_EPS for value in present.values()):
        flags.append("negative_reading")
    influent = present.get(INFLUENT)
    if influent is not None and influent > capacity + _EPS:
        flags.append("above_capacity")
    effluent = present.get(EFFLUENT)
    return_flow = present.get(RETURN_FLOW)
    if influent is not None and effluent is not None and effluent > influent + _EPS:
        flags.append("effluent_exceeds_influent")
    if influent is not None and return_flow is not None and return_flow > influent + _EPS:
        flags.append("return_exceeds_influent")
    if None not in (influent, effluent, return_flow) and influent - effluent - return_flow < -_EPS:
        flags.append("balance_negative_loss")
    if influent is not None and abs(influent) <= _EPS and (return_flow or 0) > _EPS:
        flags.append("zero_influent")
    return flags


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """对 n<=3 的小矩阵做高斯消元；奇异返回 None。"""
    n = len(matrix)
    work = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(work[r][col]))
        if abs(work[pivot][col]) < 1e-9:
            return None
        work[col], work[pivot] = work[pivot], work[col]
        pivot_value = work[col][col]
        work[col] = [value / pivot_value for value in work[col]]
        for row in range(n):
            if row == col:
                continue
            factor = work[row][col]
            if abs(factor) < 1e-12:
                continue
            work[row] = [a - factor * b for a, b in zip(work[row], work[col])]
    return [work[i][n] for i in range(n)]


def _feasible_vertices(present: dict[str, float], capacity: float) -> list[dict[str, float]]:
    """枚举物理可行多面体在自由变量空间中的全部顶点。"""
    free = [metric for metric in METRICS if metric not in present]
    fixed_indexes = {_VAR_INDEX[metric]: value for metric, value in present.items()}

    constraints: list[tuple[dict[int, float], float]] = []
    for coefficients, bound in _BASE_CONSTRAINTS:
        row = list(coefficients)
        right = capacity if bound == 0.0 and coefficients == (1.0, 0.0, 0.0) else bound
        for index, value in fixed_indexes.items():
            right -= row[index] * value
        free_row = {
            free_index: row[_VAR_INDEX[metric]]
            for free_index, metric in enumerate(free)
            if abs(row[_VAR_INDEX[metric]]) > 1e-12
        }
        constraints.append((free_row, right))

    points: list[dict[str, float]] = []
    seen: set[tuple[float, ...]] = set()
    for combo in itertools.combinations(range(len(constraints)), len(free)):
        matrix: list[list[float]] = []
        rhs: list[float] = []
        for constraint_index in combo:
            row, right = constraints[constraint_index]
            matrix.append([row.get(i, 0.0) for i in range(len(free))])
            rhs.append(right)
        solution = _solve_linear(matrix, rhs)
        if solution is None:
            continue
        candidate = {metric: float(solution[i]) for i, metric in enumerate(free)}
        merged = {**present, **candidate}
        if not _satisfies(merged, capacity):
            continue
        key = tuple(round(candidate[metric], 9) for metric in free)
        if key in seen:
            continue
        seen.add(key)
        points.append(merged)
    return points


def _satisfies(point: dict[str, float], capacity: float) -> bool:
    i, e, r = point[INFLUENT], point[EFFLUENT], point[RETURN_FLOW]
    return (
        i >= -_EPS and e >= -_EPS and r >= -_EPS
        and i <= capacity + _EPS
        and e <= i + _EPS
        and r <= i + _EPS
        and e + r <= i + _EPS
    )


def _independent_bounds(present: dict[str, float], capacity: float) -> dict[str, tuple[float, float]]:
    bounds: dict[str, tuple[float, float]] = {}
    for metric in METRICS:
        if metric in present:
            value = present[metric]
            bounds[metric] = (value, value)
        elif metric == INFLUENT:
            lower = max(0.0, present.get(EFFLUENT, 0.0) + present.get(RETURN_FLOW, 0.0))
            bounds[metric] = (lower, capacity)
        else:
            bounds[metric] = (0.0, capacity)
    return bounds


def _rounded_bounds(value: tuple[float, float]) -> dict[str, float | None]:
    def clean(number: float) -> float:
        rounded = round(number, 4)
        return 0.0 if rounded == 0 else rounded

    return {"lo": clean(value[0]), "hi": clean(value[1])}


def compute_balance(readings: dict[str, float | None], capacity_per_shift: float) -> dict[str, Any]:
    """根据三项仪表读数计算可追溯的损耗区间与循环率区间。

    readings 缺少某个键或值为 None 表示该仪表缺测。
    返回结构固定包含公式版本、使用假设、各项区间、缺测清单与物理违例标记。
    """
    present = {metric: float(readings[metric]) for metric in METRICS if readings.get(metric) is not None}
    missing = [metric for metric in METRICS if metric not in present]
    violations = _violations(present, capacity_per_shift)

    if not violations and present:
        vertices = _feasible_vertices(present, capacity_per_shift)
        if not vertices:
            violations.append("infeasible_physical_bounds")

    if violations or not present:
        # 异常或完全缺测：保留原始点值，不做任何"修正"，区间仅按独立界给出。
        bounds = _independent_bounds(present, capacity_per_shift)
        loss = (
            bounds[INFLUENT][0] - bounds[EFFLUENT][1] - bounds[RETURN_FLOW][1],
            bounds[INFLUENT][1] - bounds[EFFLUENT][0] - bounds[RETURN_FLOW][0],
        )
        rate_values = [
            bounds[RETURN_FLOW][0] / bounds[INFLUENT][1],
            bounds[RETURN_FLOW][1] / max(bounds[INFLUENT][0], _EPS),
        ] if bounds[INFLUENT][1] > _EPS else []
        rate: tuple[float, float] | None = (min(rate_values), max(rate_values)) if rate_values else None
    else:
        vertices = _feasible_vertices(present, capacity_per_shift)
        bounds = {
            metric: (
                min(point[metric] for point in vertices),
                max(point[metric] for point in vertices),
            )
            for metric in METRICS
        }
        loss = (
            min(point[INFLUENT] - point[EFFLUENT] - point[RETURN_FLOW] for point in vertices),
            max(point[INFLUENT] - point[EFFLUENT] - point[RETURN_FLOW] for point in vertices),
        )
        rate_points = [point[RETURN_FLOW] / point[INFLUENT] for point in vertices if point[INFLUENT] > _EPS]
        rate = (min(rate_points), max(rate_points)) if rate_points else None

    point_like = not missing and not violations
    if not present:
        quality = "uncomputable"
    elif point_like:
        quality = "point"
    else:
        quality = "interval"

    return {
        "formula_version": FORMULA_VERSION,
        "quality": quality,
        "missing": missing,
        "violations": violations,
        "bounds": {metric: _rounded_bounds(bounds[metric]) for metric in METRICS},
        "loss": _rounded_bounds(loss),
        "return_rate": (
            {"lo": round(rate[0], 6), "hi": round(rate[1], 6)}
            if rate is not None
            else {"lo": None, "hi": None}
        ),
        "assumptions": {
            "balance": "进水 = 出水 + 回流 + 损耗",
            "missing_upper_bound": "缺测项上界取设施班次设计能力，损耗非负",
            "capacity_per_shift": capacity_per_shift,
        },
    }


def combine_intervals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """跨设施/跨班次区间求和；循环率按水量加权（Σ回流/Σ进水），不平均各班次比率。"""
    totals = {metric: {"lo": 0.0, "hi": 0.0} for metric in (*METRICS, "loss")}
    rate_lo_pairs: list[tuple[float, float]] = []
    rate_hi_pairs: list[tuple[float, float]] = []
    all_point = True
    for row in rows:
        computed = row["computed"]
        bounds = computed["bounds"]
        for metric in METRICS:
            totals[metric]["lo"] += bounds[metric]["lo"]
            totals[metric]["hi"] += bounds[metric]["hi"]
        totals["loss"]["lo"] += computed["loss"]["lo"]
        totals["loss"]["hi"] += computed["loss"]["hi"]
        if computed["quality"] != "point":
            all_point = False
        if bounds[INFLUENT]["hi"] > _EPS:
            rate_lo_pairs.append((bounds[RETURN_FLOW]["lo"], bounds[INFLUENT]["hi"]))
        if bounds[INFLUENT]["lo"] > _EPS:
            rate_hi_pairs.append((bounds[RETURN_FLOW]["hi"], bounds[INFLUENT]["lo"]))

    def round_bounds(block: dict[str, float]) -> dict[str, float]:
        return {"lo": round(block["lo"], 4), "hi": round(block["hi"], 4)}

    rate: dict[str, Any] = {"lo": None, "hi": None}
    if rate_lo_pairs:
        rate["lo"] = round(sum(n for n, _ in rate_lo_pairs) / sum(d for _, d in rate_lo_pairs), 6)
    if rate_hi_pairs:
        rate["hi"] = round(sum(n for n, _ in rate_hi_pairs) / sum(d for _, d in rate_hi_pairs), 6)
    if all_point and rate["lo"] is not None:
        rate["point"] = rate["lo"]

    return {
        "formula_version": FORMULA_VERSION,
        "quality": "point" if all_point else "interval",
        **{metric: round_bounds(totals[metric]) for metric in (*METRICS, "loss")},
        "return_rate": rate,
        "facility_entries": len(rows),
    }
