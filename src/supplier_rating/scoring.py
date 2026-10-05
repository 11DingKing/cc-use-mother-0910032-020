"""确定性评分引擎。

评分是纯函数：给定规则版本、权重版本和数据快照，输出逐项明细与总分。
不读取任何可变状态，因此同一份历史输入在任何时候复算结果完全一致。
"""
from __future__ import annotations

from .errors import ValidationFailed
from .models import (
    DataSourceSnapshot,
    Metric,
    MetricKind,
    RuleVersion,
    ScoreLine,
    SupplierScore,
    WeightVersion,
)


def _normalize(metric: Metric, value: float) -> tuple[float, str]:
    """把原始指标值归一化到 0~1，并返回口径解释。"""
    if metric.kind in (MetricKind.RATIO, MetricKind.TIMELINESS):
        if not 0.0 <= value <= 1.0:
            raise ValidationFailed(
                f"指标 {metric.code} 的值 {value} 超出 [0,1] 比率范围"
            )
        kind_label = "准时率" if metric.kind == MetricKind.TIMELINESS else "达标比率"
        return value, f"{kind_label} {value:.2%} 直接作为得分率"

    if metric.kind == MetricKind.COUNT_PER_MILLION:
        good = metric.good_threshold
        bad = metric.bad_threshold
        if good is None or bad is None or good >= bad:
            raise ValidationFailed(
                f"CPM 指标 {metric.code} 须配置 good_threshold < bad_threshold"
            )
        if value <= good:
            normalized = 1.0
        elif value >= bad:
            normalized = 0.0
        else:
            normalized = (bad - value) / (bad - good)
        return normalized, (
            f"每百万 {value:g}{(' ' + metric.unit) if metric.unit else ''}，"
            f"满分阈值 ≤ {good:g}、归零阈值 ≥ {bad:g}，线性插值得分率 {normalized:.4f}"
        )

    raise ValidationFailed(f"暂不支持的指标类型：{metric.kind}")


def score_supplier(
    rule: RuleVersion,
    weights: WeightVersion,
    snapshot: DataSourceSnapshot,
) -> SupplierScore:
    """按指定规则版本与权重版本计算单个供应商得分。"""
    metric_by_code = {m.code: m for m in rule.metrics}

    missing_metrics = set(weights.weights) - set(metric_by_code)
    if missing_metrics:
        raise ValidationFailed(
            f"权重引用了规则 {rule.code} 中不存在的指标：{sorted(missing_metrics)}"
        )
    missing_weights = set(metric_by_code) - set(weights.weights)
    if missing_weights:
        raise ValidationFailed(
            f"权重版本 {weights.code} 缺少指标权重：{sorted(missing_weights)}"
        )

    total = 0.0
    total_w = 0.0
    lines: list[ScoreLine] = []
    for metric in rule.metrics:
        w = weights.weights[metric.code]
        if w < 0:
            raise ValidationFailed(f"指标 {metric.code} 权重不能为负")
        if metric.code not in snapshot.values:
            raise ValidationFailed(
                f"快照 {snapshot.id} 缺少指标 {metric.code} 的输入值"
            )
        raw = float(snapshot.values[metric.code])
        normalized, why = _normalize(metric, raw)
        weighted = normalized * w * 100.0
        total += weighted
        total_w += w
        lost = w * 100.0 - weighted
        lines.append(
            ScoreLine(
                metric_code=metric.code,
                metric_name=metric.name,
                raw_value=raw,
                normalized=round(normalized, 6),
                weight=w,
                weighted_score=round(weighted, 4),
                contribution=round(weighted, 4),
                explanation=(
                    f"{why}；权重 {w:.2%}，满分 {w * 100:.2f}，"
                    f"实得 {weighted:.2f}，扣分 {lost:.2f}"
                ),
            )
        )

    if abs(total_w - 1.0) > 1e-9:
        raise ValidationFailed(
            f"权重版本 {weights.code} 权重合计为 {total_w}，必须等于 1"
        )

    total = round(total, 4)
    return SupplierScore(
        supplier_code=snapshot.supplier_code,
        total_score=total,
        grade=rule.grade_for(total),
        lines=lines,
        rule_version=rule.code,
        weight_version=weights.code,
        snapshot_id=snapshot.id,
    )


def score_many(
    rule: RuleVersion,
    weights: WeightVersion,
    snapshots: list[DataSourceSnapshot],
) -> dict[str, SupplierScore]:
    """批量评分；同一供应商出现多个快照时以数据来源排序后的最后一个为准
    （快照编号本身携带版本序，由仓储层保证只传入当次口径选定的快照）。"""
    results: dict[str, SupplierScore] = {}
    for snapshot in snapshots:
        results[snapshot.supplier_code] = score_supplier(rule, weights, snapshot)
    return results
