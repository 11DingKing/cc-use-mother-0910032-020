"""评分计算引擎：纯函数、无副作用，结果完全由输入快照与规则版本决定。

可复算性保证：
score(snapshot, spec_versions, weight_version) 对相同三元组恒等，
历史版本持有的 spec_versions / snapshot_id / weight_version_id 永久保留，
因此任何历史口径都可以原样复算。
"""
from __future__ import annotations

from typing import Any

from .models import InputSnapshot, MetricSpec, OrderRecord, WeightVersion

ROUND_NDIGITS = 4


def _round(value: float) -> float:
    return round(value + 0.0, ROUND_NDIGITS)


def _grade(score: float, bands: tuple[dict[str, Any], ...]) -> str:
    """按从高到低的分数段匹配等级（min_inclusive <= score <= max_exclusive）。"""
    ordered = sorted(bands, key=lambda b: b["min_inclusive"], reverse=True)
    for band in ordered:
        upper = band.get("max_exclusive")
        if score >= band["min_inclusive"] and (upper is None or score < upper):
            return band["grade"]
    return ordered[-1]["grade"]


def compute_metric(
    spec: MetricSpec,
    records: list[OrderRecord],
) -> dict[str, Any]:
    """按单个指标规则版本计算得分与逐项依据。

    返回中的 sample_items 逐条列出是否达标，支撑“逐项解释扣分来源”。
    """
    items: list[dict[str, Any]] = []
    passed = 0
    if spec.kind == "ratio":
        flag_field = spec.params["flag_field"]
        noun = spec.params.get("noun", "记录")
        fail_label = spec.params.get("fail_label", "未达标")
        for rec in records:
            ok = bool(rec.payload.get(flag_field))
            passed += ok
            items.append(
                {
                    "record_id": rec.record_id,
                    "source_code": rec.source_code,
                    "occurred_at": rec.occurred_at,
                    "compliant": ok,
                    "detail": "达标" if ok else fail_label,
                    "raw": dict(rec.payload),
                }
            )
        formula = spec.formula_text
    elif spec.kind == "sla_ratio":
        value_field = spec.params["value_field"]
        sla = float(spec.params["sla_hours"])
        for rec in records:
            hours = float(rec.payload[value_field])
            ok = hours <= sla
            passed += ok
            items.append(
                {
                    "record_id": rec.record_id,
                    "source_code": rec.source_code,
                    "occurred_at": rec.occurred_at,
                    "compliant": ok,
                    "detail": f"{hours:g}h ≤ {sla:g}h" if ok else f"{hours:g}h > {sla:g}h",
                    "raw": dict(rec.payload),
                }
            )
        formula = spec.formula_text
    else:
        raise ValueError(f"未知指标类型：{spec.kind}")

    total = len(records)
    score = _round(100.0 * passed / total) if total else None
    return {
        "metric_code": spec.code,
        "metric_name": spec.name,
        "source_code": spec.source_code,
        "spec_version": spec.version,
        "spec_kind": spec.kind,
        "formula": formula,
        "erratum_note": spec.erratum_note,
        "sample_size": total,
        "compliant_count": passed,
        "score": score,  # 无样本时为 None，该指标不参与加权（而不是记 0 分）
        "sample_items": items,
    }


def score_supplier(
    records_by_metric: dict[str, list[OrderRecord]],
    specs: dict[str, MetricSpec],
    weight_version: WeightVersion,
    snapshot: InputSnapshot | None = None,
) -> dict[str, Any]:
    """汇总一个供应商在一个批次下的全部指标得分与等级。

    records_by_metric 必须已经与输入快照一致（剔除项不得传入）；
    snapshot 仅用于在解释中标注快照标识，不改变计算。
    """
    metric_results: list[dict[str, Any]] = []
    applied_weight_sum = 0.0
    weighted_sum = 0.0
    for code, weight in weight_version.weights.items():
        spec = specs.get(code)
        if spec is None:
            raise ValueError(f"指标 {code} 缺少已发布规则版本")
        result = compute_metric(spec, records_by_metric.get(code, []))
        result["weight"] = weight
        if result["score"] is None:
            result["weighted_contribution"] = None
            result["weight_note"] = "本批次无样本，权重不参与加权"
        else:
            contribution = _round(result["score"] * weight)
            result["weighted_contribution"] = contribution
            weighted_sum += contribution
            applied_weight_sum += weight
        metric_results.append(result)

    total = _round(weighted_sum / applied_weight_sum) if applied_weight_sum else None
    grade = _grade(total, weight_version.grade_bands) if total is not None else None
    return {
        "snapshot_id": snapshot.snapshot_id if snapshot else None,
        "weight_version_id": weight_version.wid,
        "scheme_code": weight_version.scheme_code,
        "weight_version_no": weight_version.version,
        "grade_bands": [dict(b) for b in weight_version.grade_bands],
        "metrics": metric_results,
        "applied_weight_sum": _round(applied_weight_sum),
        "total_score": total,
        "grade": grade,
    }
