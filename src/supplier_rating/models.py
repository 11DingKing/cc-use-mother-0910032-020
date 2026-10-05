"""供应商季度评级的领域模型。

所有写操作产出的对象均为不可变 dataclass（批次/申诉等有状态对象除外），
正式发布与更正版本一经生成不得修改，只能追加新版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DataSource:
    """评分数据来源（如 WMS 出库系统、QMS 质检系统）。"""

    code: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class MetricSpec:
    """评分指标规则版本。

    kind 目前支持：
    - ratio：布尔标志达标率，params: flag_field / noun / fail_label
    - sla_ratio：数值相对 SLA 的达标率，params: value_field / sla_hours
    同一 code 的规则勘误通过递增 version 表达，旧版本永久保留。
    """

    code: str
    name: str
    source_code: str
    version: int
    kind: str
    params: dict[str, Any]
    formula_text: str
    erratum_note: str | None = None


@dataclass(frozen=True, eq=False)
class WeightVersion:
    """权重方案版本。draft 可试算，published 才能用于正式发布，发布后冻结。"""

    wid: str
    scheme_code: str
    version: int
    weights: dict[str, float]
    grade_bands: tuple[dict[str, Any], ...]
    status: str  # draft / published
    note: str = ""


@dataclass(frozen=True)
class OrderRecord:
    """一条原始业务记录（订单交付、来料检验、响应记录）。"""

    record_id: str
    batch_id: str
    supplier_id: str
    metric_code: str
    source_code: str
    occurred_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SnapshotEntry:
    """快照中的一条记录及其剔除状态。"""

    record_id: str
    excluded: bool = False
    exclude_reason: str | None = None


@dataclass(frozen=True)
class InputSnapshot:
    """正式发布/更正版本固定下来的输入集合，按 record_id 排序并哈希。"""

    snapshot_id: str
    batch_id: str
    supplier_id: str
    entries: tuple[SnapshotEntry, ...]
    parent_snapshot_id: str | None
    content_hash: str
    origin: str = "publish"  # publish / late_data / order_exclusion / appeal_accepted


@dataclass
class Batch:
    """季度封账批次。closed 后常规数据通道关闭，迟到数据只能走更正版本。"""

    batch_id: str
    period_start: str
    period_end: str
    status: str = "open"  # open / closed
    closed_seq: int | None = None


@dataclass
class PublicationVersion:
    """正式评级的一个版本。v1 为首发，其后全部为更正版本，逐版链式保留。"""

    publication_id: str
    version: int
    batch_id: str
    supplier_id: str
    weight_version_id: str
    spec_versions: dict[str, int]
    snapshot_id: str
    explanation: dict[str, Any]
    content_hash: str
    seq: int
    correction: dict[str, Any] | None = None  # 非空即为更正版本


@dataclass
class Publication:
    """一次正式发布及其全部版本链。"""

    publication_id: str
    batch_id: str
    supplier_id: str
    versions: list[PublicationVersion] = field(default_factory=list)


@dataclass
class Appeal:
    """供应商申诉案件。采纳后生成 appeal_accepted 更正版本。"""

    appeal_id: str
    publication_id: str
    supplier_id: str
    reason: str
    metric_code: str | None
    record_id: str | None
    status: str = "filed"  # filed / accepted / rejected
    decision_note: str | None = None
    correction_version: int | None = None
