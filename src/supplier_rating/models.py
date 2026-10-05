"""领域模型：指标、规则版本、权重版本、数据快照、封账批次、申诉、试算。

设计要点
--------
* 正式规则版本（RuleVersion）一经发布即不可变；迟到数据、异常订单剔除、
  申诉采纳、规则勘误都不能改写已发布版本，只能基于其生成更正版本
  （correction_of 指向前版，reason 记录更正原因类型）。
* 权重版本（WeightVersion）同样不可变，更换权重产生新版本，历史批次仍
  绑定旧权重，因此历史等级不会随之变化。
* 封账批次（CloseBatch）固定 rule_version 与数据快照编号；更正批次通过
  revises 串联，形成可追溯的更正链。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 枚举


class MetricKind(str, Enum):
    RATIO = "ratio"          # 比率型：得分 = 比率 × 满分
    COUNT_PER_MILLION = "cpm"  # 每百万缺陷数，越低越好
    TIMELINESS = "timeliness"  # 准时率型（含容忍期），等同比率语义但口径独立
    CUSTOM = "custom"        # 预留自定义口径


class VersionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class CloseStatus(str, Enum):
    TRIAL = "trial"          # 试算，未封账
    CLOSED = "closed"        # 已封账
    CORRECTED = "corrected"  # 已被后续更正批次替代


class AppealStatus(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class CorrectionReason(str, Enum):
    LATE_DATA = "late_data"              # 迟到数据补录
    ABNORMAL_EXCLUSION = "abnormal_exclusion"  # 异常订单剔除
    APPEAL_ACCEPTED = "appeal_accepted"  # 申诉采纳
    ERRATUM = "erratum"                  # 规则勘误


# ---------------------------------------------------------------- 指标与规则


@dataclass(frozen=True)
class Metric:
    """评分指标定义。"""

    code: str
    name: str
    kind: MetricKind
    unit: str = ""
    description: str = ""
    # CPM 型指标的满分阈值与归零阈值：value <= good 得满分，>= bad 得 0 分
    good_threshold: float | None = None
    bad_threshold: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "kind": self.kind.value,
            "unit": self.unit,
            "description": self.description,
            "good_threshold": self.good_threshold,
            "bad_threshold": self.bad_threshold,
        }


@dataclass
class WeightVersion:
    """权重版本。发布后不可变；更换权重必须新建版本。"""

    code: str
    weights: dict[str, float]          # metric_code -> 权重（合计须为 1）
    status: VersionStatus = VersionStatus.DRAFT
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "weights": dict(self.weights),
            "status": self.status.value,
            "note": self.note,
        }


@dataclass
class RuleVersion:
    """评分规则版本：指标集合 + 等级分档。

    published=True 后冻结。更正版本通过 correction_of 引用前版。
    """

    code: str
    metrics: list[Metric]
    grade_cutoffs: list[tuple[str, float]]  # [(等级, 下限分)]，从高到低
    published: bool = False
    correction_of: str | None = None
    correction_reason: CorrectionReason | None = None
    correction_note: str = ""
    published_at: str | None = None

    @property
    def metric_codes(self) -> list[str]:
        return [m.code for m in self.metrics]

    def grade_for(self, score: float) -> str:
        for grade, lower in self.grade_cutoffs:
            if score >= lower:
                return grade
        return self.grade_cutoffs[-1][0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "metrics": [m.to_dict() for m in self.metrics],
            "grade_cutoffs": [[g, c] for g, c in self.grade_cutoffs],
            "published": self.published,
            "correction_of": self.correction_of,
            "correction_reason": self.correction_reason.value if self.correction_reason else None,
            "correction_note": self.correction_note,
            "published_at": self.published_at,
        }


# ---------------------------------------------------------------- 数据快照


@dataclass
class DataSourceSnapshot:
    """数据来源快照：某供应商某季度的原始指标输入。

    finalized 后内容冻结。迟到数据不能回填本快照，必须创建 successor。
    excluded_orders 记录本快照剔除的异常订单（异常订单从分母/分子中移除，
    剔除动作同样不可原地撤销，只能以新快照表达）。
    """

    id: str
    supplier_code: str
    period: str  # 例如 2026Q1
    source: str  # 数据来源系统标识，如 WMS / QMS / SRM
    values: dict[str, float]
    excluded_orders: list[str] = field(default_factory=list)
    finalized: bool = False
    successor_of: str | None = None
    successor_reason: CorrectionReason | None = None
    note: str = ""
    content_hash: str | None = None

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "supplier_code": self.supplier_code,
                "period": self.period,
                "source": self.source,
                "values": self.values,
                "excluded_orders": sorted(self.excluded_orders),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "supplier_code": self.supplier_code,
            "period": self.period,
            "source": self.source,
            "values": dict(self.values),
            "excluded_orders": list(self.excluded_orders),
            "finalized": self.finalized,
            "successor_of": self.successor_of,
            "successor_reason": self.successor_reason.value if self.successor_reason else None,
            "note": self.note,
            "content_hash": self.content_hash,
        }


# ---------------------------------------------------------------- 评分结果与批次


@dataclass
class ScoreLine:
    """逐项得分明细，用于扣分来源解释。"""

    metric_code: str
    metric_name: str
    raw_value: float
    normalized: float          # 0~1 的归一化得分
    weight: float
    weighted_score: float      # normalized * weight * 100
    contribution: float        # 对总分的贡献（与 weighted_score 相同，保留字段便于扩展）
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass
class SupplierScore:
    supplier_code: str
    total_score: float
    grade: str
    lines: list[ScoreLine]
    rule_version: str
    weight_version: str
    snapshot_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier_code": self.supplier_code,
            "total_score": self.total_score,
            "grade": self.grade,
            "rule_version": self.rule_version,
            "weight_version": self.weight_version,
            "snapshot_id": self.snapshot_id,
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass
class CloseBatch:
    """封账批次：固定规则版本、权重版本与一组数据快照。

    已封账批次及其结果永久不可变；更正只能生成 revises 指向本批次的新批次。
    """

    id: str
    period: str
    rule_version: str
    weight_version: str
    snapshot_ids: list[str]
    status: CloseStatus
    created_at: str
    closed_at: str | None = None
    results: dict[str, SupplierScore] = field(default_factory=dict)
    revises: str | None = None  # 更正链：指向被本批次更正的前批次

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "period": self.period,
            "rule_version": self.rule_version,
            "weight_version": self.weight_version,
            "snapshot_ids": list(self.snapshot_ids),
            "status": self.status.value,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "revises": self.revises,
            "results": {code: score.to_dict() for code, score in self.results.items()},
        }


# ---------------------------------------------------------------- 申诉


@dataclass
class Appeal:
    """供应商申诉案件。

    申诉针对某封账批次中的某项指标扣分；只有 CLOSED 批次才能被申诉。
    申诉采纳后不直接改分，而是驱动生成更正版本/快照与更正批次。
    """

    id: str
    batch_id: str
    supplier_code: str
    metric_code: str
    reason: str
    evidence: str = ""
    status: AppealStatus = AppealStatus.OPEN
    created_at: str = ""
    decided_at: str | None = None
    decision_note: str = ""
    corrected_by_batch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "supplier_code": self.supplier_code,
            "metric_code": self.metric_code,
            "reason": self.reason,
            "evidence": self.evidence,
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "corrected_by_batch": self.corrected_by_batch,
        }


@dataclass
class Trial:
    """试算方案：可与其他试算并存、比较，不影响任何正式数据。"""

    id: str
    label: str
    period: str
    rule_version: str
    weight_version: str
    snapshot_ids: list[str]
    created_at: str
    results: dict[str, SupplierScore] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "period": self.period,
            "rule_version": self.rule_version,
            "weight_version": self.weight_version,
            "snapshot_ids": list(self.snapshot_ids),
            "created_at": self.created_at,
            "results": {code: score.to_dict() for code, score in self.results.items()},
        }
