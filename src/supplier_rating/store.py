"""应用服务与仓储：维护指标、规则版本、权重版本、数据快照、封账批次、
申诉案件与试算方案，强制版本不可变与更正流程。

存储默认内存态，可选 JSON 文件持久化；所有写操作集中在此处，
领域对象本身不被外部直接改写。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import Conflict, NotFound, ValidationFailed
from .models import (
    Appeal,
    AppealStatus,
    CloseBatch,
    CloseStatus,
    CorrectionReason,
    DataSourceSnapshot,
    Metric,
    MetricKind,
    RuleVersion,
    SupplierScore,
    Trial,
    VersionStatus,
    WeightVersion,
)
from .scoring import score_many, score_supplier


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RatingStore:
    """集中式仓储与领域服务。"""

    def __init__(self) -> None:
        self.metrics: dict[str, Metric] = {}
        self.rules: dict[str, RuleVersion] = {}
        self.weights: dict[str, WeightVersion] = {}
        self.snapshots: dict[str, DataSourceSnapshot] = {}
        self.batches: dict[str, CloseBatch] = {}
        self.appeals: dict[str, Appeal] = {}
        self.trials: dict[str, Trial] = {}

    # ------------------------------------------------------------ 指标

    def register_metric(self, metric: Metric) -> Metric:
        if metric.code in self.metrics:
            raise Conflict(f"指标 {metric.code} 已存在")
        if metric.kind == MetricKind.COUNT_PER_MILLION:
            if metric.good_threshold is None or metric.bad_threshold is None:
                raise ValidationFailed(f"CPM 指标 {metric.code} 必须配置阈值")
        self.metrics[metric.code] = metric
        return metric

    def get_metric(self, code: str) -> Metric:
        try:
            return self.metrics[code]
        except KeyError:
            raise NotFound(f"指标不存在：{code}") from None

    # ------------------------------------------------------------ 规则版本

    def create_rule(
        self,
        code: str,
        metric_codes: list[str],
        grade_cutoffs: list[tuple[str, float]],
    ) -> RuleVersion:
        if code in self.rules:
            raise Conflict(f"规则版本 {code} 已存在")
        if not metric_codes:
            raise ValidationFailed("规则版本至少包含一个指标")
        metrics = [self.get_metric(c) for c in metric_codes]
        self._validate_cutoffs(grade_cutoffs)
        rule = RuleVersion(
            code=code, metrics=metrics, grade_cutoffs=list(grade_cutoffs)
        )
        self.rules[code] = rule
        return rule

    @staticmethod
    def _validate_cutoffs(cutoffs: list[tuple[str, float]]) -> None:
        if not cutoffs:
            raise ValidationFailed("至少需要一个等级分档")
        grades = [g for g, _ in cutoffs]
        if len(grades) != len(set(grades)):
            raise ValidationFailed("等级名称不能重复")
        lowers = [c for _, c in cutoffs]
        if lowers != sorted(lowers, reverse=True):
            raise ValidationFailed("等级分档必须按分数下限从高到低排列")
        if not (0.0 <= lowers[-1] <= 100.0 and 0.0 <= lowers[0] <= 100.0):
            raise ValidationFailed("分数下限必须位于 0~100")

    def publish_rule(self, code: str) -> RuleVersion:
        rule = self._get_rule(code)
        if rule.published:
            raise Conflict(f"规则版本 {code} 已发布，不可重复发布或修改")
        rule.published = True
        rule.published_at = _now()
        return rule

    def correct_rule(
        self,
        new_code: str,
        base_code: str,
        reason: CorrectionReason,
        note: str,
        metric_codes: list[str] | None = None,
        grade_cutoffs: list[tuple[str, float]] | None = None,
        metric_defs: list[Metric] | None = None,
    ) -> RuleVersion:
        """基于已发布规则生成勘误/更正规则版本。

        迟到数据与异常订单剔除属于数据更正，不应改动规则；规则本身的
        错误（如分档、阈值、指标口径有误）只能以 erratum 形式发布新版。
        metric_defs 可给出勘误后的指标定义（如 CPM 阈值修正），按 code
        覆盖基础版本中的同名指标，使每个规则版本自带完整口径快照。
        """
        base = self._get_rule(base_code)
        if not base.published:
            raise Conflict("只能基于已发布规则创建更正版本")
        if reason != CorrectionReason.ERRATUM:
            raise ValidationFailed(
                "规则版本仅接受规则勘误（erratum）；数据问题请生成数据快照更正版"
            )
        if new_code in self.rules:
            raise Conflict(f"规则版本 {new_code} 已存在")
        if metric_defs is not None and metric_codes is not None:
            raise ValidationFailed("metric_defs 与 metric_codes 不能同时指定")
        if metric_defs is not None:
            metrics = list(metric_defs)
        else:
            codes = metric_codes if metric_codes is not None else base.metric_codes
            base_by_code = {m.code: m for m in base.metrics}
            metrics = []
            for code in codes:
                metrics.append(self.metrics.get(code) or base_by_code[code])
        if not metrics:
            raise ValidationFailed("规则版本至少包含一个指标")
        cutoffs = list(grade_cutoffs) if grade_cutoffs is not None else list(base.grade_cutoffs)
        self._validate_cutoffs(cutoffs)
        for metric in metrics:
            if metric.kind == MetricKind.COUNT_PER_MILLION:
                if metric.good_threshold is None or metric.bad_threshold is None:
                    raise ValidationFailed(f"CPM 指标 {metric.code} 必须配置阈值")
        rule = RuleVersion(
            code=new_code,
            metrics=metrics,
            grade_cutoffs=cutoffs,
            published=True,
            correction_of=base.code,
            correction_reason=reason,
            correction_note=note,
            published_at=_now(),
        )
        self.rules[new_code] = rule
        return rule

    def _get_rule(self, code: str) -> RuleVersion:
        try:
            return self.rules[code]
        except KeyError:
            raise NotFound(f"规则版本不存在：{code}") from None

    # ------------------------------------------------------------ 权重版本

    def create_weights(
        self, code: str, weights: dict[str, float], note: str = ""
    ) -> WeightVersion:
        if code in self.weights:
            raise Conflict(f"权重版本 {code} 已存在")
        for metric_code in weights:
            self.get_metric(metric_code)
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-9:
            raise ValidationFailed(f"权重合计必须等于 1，当前为 {total}")
        if any(w < 0 for w in weights.values()):
            raise ValidationFailed("权重不能为负")
        version = WeightVersion(code=code, weights=dict(weights), note=note)
        self.weights[code] = version
        return version

    def publish_weights(self, code: str) -> WeightVersion:
        version = self._get_weights(code)
        if version.status == VersionStatus.PUBLISHED:
            raise Conflict(f"权重版本 {code} 已发布，不可修改")
        version.status = VersionStatus.PUBLISHED
        return version

    def _get_weights(self, code: str) -> WeightVersion:
        try:
            return self.weights[code]
        except KeyError:
            raise NotFound(f"权重版本不存在：{code}") from None

    # ------------------------------------------------------------ 数据快照

    def ingest_snapshot(
        self,
        snapshot_id: str,
        supplier_code: str,
        period: str,
        source: str,
        values: dict[str, float],
        finalized: bool = True,
        note: str = "",
    ) -> DataSourceSnapshot:
        if snapshot_id in self.snapshots:
            raise Conflict(f"数据快照 {snapshot_id} 已存在，原始数据不可覆盖")
        for metric_code in values:
            self.get_metric(metric_code)
        snapshot = DataSourceSnapshot(
            id=snapshot_id,
            supplier_code=supplier_code,
            period=period,
            source=source,
            values=dict(values),
            finalized=finalized,
            note=note,
        )
        if finalized:
            snapshot.content_hash = snapshot.compute_hash()
        self.snapshots[snapshot_id] = snapshot
        return snapshot

    def finalize_snapshot(self, snapshot_id: str) -> DataSourceSnapshot:
        snapshot = self._get_snapshot(snapshot_id)
        if snapshot.finalized:
            raise Conflict(f"快照 {snapshot_id} 已定稿，不可修改")
        snapshot.finalized = True
        snapshot.content_hash = snapshot.compute_hash()
        return snapshot

    def correct_snapshot(
        self,
        new_id: str,
        base_id: str,
        reason: CorrectionReason,
        values: dict[str, float] | None = None,
        exclude_orders: list[str] | None = None,
        note: str = "",
    ) -> DataSourceSnapshot:
        """生成数据快照更正版。

        * late_data：迟到数据补录，合并/覆盖原始指标值；
        * abnormal_exclusion：异常订单剔除，重算后的值由调用方给出，
          同时登记被剔除的异常订单号；
        * appeal_accepted：申诉采纳带来的数据订正。
        规则勘误不得通过数据更正表达。
        """
        base = self._get_snapshot(base_id)
        if not base.finalized:
            raise Conflict("只能基于已定稿快照创建更正版")
        if reason == CorrectionReason.ERRATUM:
            raise ValidationFailed("规则勘误应生成新的规则版本，而非数据快照")
        if reason not in (
            CorrectionReason.LATE_DATA,
            CorrectionReason.ABNORMAL_EXCLUSION,
            CorrectionReason.APPEAL_ACCEPTED,
        ):
            raise ValidationFailed(f"不支持的数据更正原因：{reason.value}")
        if new_id in self.snapshots:
            raise Conflict(f"快照 {new_id} 已存在")

        merged = dict(base.values)
        if values:
            merged.update(values)
        excluded = list(base.excluded_orders)
        if exclude_orders:
            added = set(exclude_orders) - set(excluded)
            excluded.extend(sorted(added))

        snapshot = DataSourceSnapshot(
            id=new_id,
            supplier_code=base.supplier_code,
            period=base.period,
            source=base.source,
            values=merged,
            excluded_orders=excluded,
            finalized=True,
            successor_of=base.id,
            successor_reason=reason,
            note=note,
        )
        snapshot.content_hash = snapshot.compute_hash()
        self.snapshots[new_id] = snapshot
        return snapshot

    def _get_snapshot(self, snapshot_id: str) -> DataSourceSnapshot:
        try:
            return self.snapshots[snapshot_id]
        except KeyError:
            raise NotFound(f"数据快照不存在：{snapshot_id}") from None

    # ------------------------------------------------------------ 试算

    def create_trial(
        self,
        trial_id: str,
        label: str,
        period: str,
        rule_code: str,
        weight_code: str,
        snapshot_ids: list[str],
    ) -> Trial:
        if trial_id in self.trials:
            raise Conflict(f"试算方案 {trial_id} 已存在")
        rule, weights, snapshots = self._prepare_inputs(
            rule_code, weight_code, snapshot_ids, require_published=False
        )
        trial = Trial(
            id=trial_id,
            label=label,
            period=period,
            rule_version=rule.code,
            weight_version=weights.code,
            snapshot_ids=list(snapshot_ids),
            created_at=_now(),
            results=score_many(rule, weights, snapshots),
        )
        self.trials[trial_id] = trial
        return trial

    def compare_trials(self, trial_ids: list[str]) -> dict[str, Any]:
        """并列比较多个试算方案，输出供应商总分/等级差异。"""
        trials = []
        for tid in trial_ids:
            if tid not in self.trials:
                raise NotFound(f"试算方案不存在：{tid}")
            trials.append(self.trials[tid])
        suppliers = sorted({s for t in trials for s in t.results})
        rows: list[dict[str, Any]] = []
        for supplier in suppliers:
            row: dict[str, Any] = {"supplier_code": supplier, "schemes": {}}
            for trial in trials:
                result = trial.results.get(supplier)
                row["schemes"][trial.id] = (
                    None
                    if result is None
                    else {
                        "label": trial.label,
                        "total_score": result.total_score,
                        "grade": result.grade,
                    }
                )
            present = [
                row["schemes"][t.id] for t in trials if row["schemes"][t.id]
            ]
            if len(present) > 1:
                scores = [p["total_score"] for p in present]
                row["score_delta"] = round(max(scores) - min(scores), 4)
                row["grade_changed"] = len({p["grade"] for p in present}) > 1
            rows.append(row)
        return {
            "schemes": [
                {"id": t.id, "label": t.label, "rule_version": t.rule_version,
                 "weight_version": t.weight_version}
                for t in trials
            ],
            "rows": rows,
        }

    # ------------------------------------------------------------ 封账批次

    def close_batch(
        self,
        batch_id: str,
        period: str,
        rule_code: str,
        weight_code: str,
        snapshot_ids: list[str],
    ) -> CloseBatch:
        """正式封账：固定规则版本、权重版本与输入快照。

        规则与权重必须已发布，快照必须已定稿；封账后结果永久冻结。
        """
        if batch_id in self.batches:
            raise Conflict(f"批次 {batch_id} 已存在")
        rule, weights, snapshots = self._prepare_inputs(
            rule_code, weight_code, snapshot_ids, require_published=True
        )
        periods = {s.period for s in snapshots}
        if periods != {period}:
            raise ValidationFailed(f"快照季度与批次季度不一致：{sorted(periods)}")
        batch = CloseBatch(
            id=batch_id,
            period=period,
            rule_version=rule.code,
            weight_version=weights.code,
            snapshot_ids=list(snapshot_ids),
            status=CloseStatus.CLOSED,
            created_at=_now(),
            closed_at=_now(),
            results=score_many(rule, weights, snapshots),
        )
        self.batches[batch_id] = batch
        return batch

    def correct_batch(
        self,
        new_batch_id: str,
        base_batch_id: str,
        reason: CorrectionReason,
        corrected_snapshot_ids: list[str] | None = None,
        rule_code: str | None = None,
        note: str = "",
    ) -> CloseBatch:
        """对已封账批次生成更正批次。

        四种更正来源：
        * late_data / abnormal_exclusion / appeal_accepted：使用数据快照更正版，
          规则与权重沿用原批次（口径不变）；
        * erratum：使用勘误规则版本，快照沿用原批次。
        原批次保持不变并标记为 CORRECTED，更正批次重新封账。
        """
        base = self._get_batch(base_batch_id)
        if base.status != CloseStatus.CLOSED:
            raise Conflict("只能对在效的已封账批次发起更正")
        if new_batch_id in self.batches:
            raise Conflict(f"批次 {new_batch_id} 已存在")

        weight_code = base.weight_version
        if reason == CorrectionReason.ERRATUM:
            if not rule_code:
                raise ValidationFailed("规则勘误必须指定新的规则版本")
            new_rule = self._get_rule(rule_code)
            if not new_rule.published or new_rule.correction_of != base.rule_version:
                raise ValidationFailed(
                    f"规则 {rule_code} 必须是基于 {base.rule_version} 的已发布勘误版"
                )
            snapshot_ids = list(base.snapshot_ids)
        else:
            if not corrected_snapshot_ids:
                raise ValidationFailed("数据更正必须提供更正后的快照编号")
            self._validate_corrected_snapshots(base, corrected_snapshot_ids, reason)
            rule_code = base.rule_version
            snapshot_ids = list(corrected_snapshot_ids)

        rule, weights, snapshots = self._prepare_inputs(
            rule_code, weight_code, snapshot_ids, require_published=True
        )
        new_batch = CloseBatch(
            id=new_batch_id,
            period=base.period,
            rule_version=rule.code,
            weight_version=weights.code,
            snapshot_ids=snapshot_ids,
            status=CloseStatus.CLOSED,
            created_at=_now(),
            closed_at=_now(),
            results=score_many(rule, weights, snapshots),
            revises=base.id,
        )
        base.status = CloseStatus.CORRECTED
        self.batches[new_batch_id] = new_batch
        return new_batch

    def _validate_corrected_snapshots(
        self,
        base: CloseBatch,
        corrected_ids: list[str],
        reason: CorrectionReason,
    ) -> None:
        if len(corrected_ids) != len(base.snapshot_ids):
            raise ValidationFailed(
                "更正快照数量必须与原批次一致（每家供应商一份）"
            )
        for new_id, old_id in zip(corrected_ids, base.snapshot_ids):
            snap = self._get_snapshot(new_id)
            old = self._get_snapshot(old_id)
            if new_id == old_id:
                continue  # 本批次更正不涉及该供应商，沿用原快照
            chain_ok = snap.successor_of == old_id or self._descends_from(snap, old_id)
            if not chain_ok:
                raise ValidationFailed(
                    f"快照 {new_id} 不是 {old_id} 的更正版"
                )
            if snap.successor_reason != reason and not self._chain_has_reason(snap, reason):
                raise ValidationFailed(
                    f"快照 {new_id} 的更正原因与 {reason.value} 不符"
                )
            if snap.supplier_code != old.supplier_code:
                raise ValidationFailed("更正快照供应商不匹配")

    def _descends_from(
        self, snap: DataSourceSnapshot, ancestor_id: str
    ) -> bool:
        current = snap
        seen: set[str] = set()
        while current.successor_of and current.id not in seen:
            seen.add(current.id)
            if current.successor_of == ancestor_id:
                return True
            current = self._get_snapshot(current.successor_of)
        return False

    def _chain_has_reason(
        self, snap: DataSourceSnapshot, reason: CorrectionReason
    ) -> bool:
        current = snap
        seen: set[str] = set()
        while current.successor_of and current.id not in seen:
            seen.add(current.id)
            if current.successor_reason == reason:
                return True
            current = self._get_snapshot(current.successor_of)
        return False

    def _get_batch(self, batch_id: str) -> CloseBatch:
        try:
            return self.batches[batch_id]
        except KeyError:
            raise NotFound(f"封账批次不存在：{batch_id}") from None

    def _latest_batch(self, batch_id: str) -> CloseBatch:
        """沿 revises 链找到当前在效（最新）的批次。"""
        current = self._get_batch(batch_id)
        successors = {b.revises: b for b in self.batches.values() if b.revises}
        while current.id in successors:
            current = successors[current.id]
        return current

    # ------------------------------------------------------------ 申诉

    def open_appeal(
        self,
        appeal_id: str,
        batch_id: str,
        supplier_code: str,
        metric_code: str,
        reason: str,
        evidence: str = "",
    ) -> Appeal:
        batch = self._get_batch(batch_id)
        if batch.status != CloseStatus.CLOSED:
            raise Conflict("只能针对当前在效的已封账批次提出申诉（更正请针对最新批次）")
        if supplier_code not in batch.results:
            raise ValidationFailed(f"批次中无供应商 {supplier_code} 的结果")
        self.get_metric(metric_code)
        if appeal_id in self.appeals:
            raise Conflict(f"申诉案件 {appeal_id} 已存在")
        appeal = Appeal(
            id=appeal_id,
            batch_id=batch_id,
            supplier_code=supplier_code,
            metric_code=metric_code,
            reason=reason,
            evidence=evidence,
            created_at=_now(),
        )
        self.appeals[appeal_id] = appeal
        return appeal

    def decide_appeal(
        self,
        appeal_id: str,
        accepted: bool,
        decision_note: str = "",
        corrected_snapshot_id: str | None = None,
        new_batch_id: str | None = None,
    ) -> Appeal:
        """裁决申诉。

        采纳申诉不会原地改分：必须携带已登记的更正快照并立即生成更正批次，
        分数变化只通过更正批次体现；驳回则记录驳回结论。
        """
        appeal = self._get_appeal(appeal_id)
        if appeal.status != AppealStatus.OPEN:
            raise Conflict("申诉已裁决，不能重复处理")

        corrected_batch_id: str | None = None
        if accepted:
            if not corrected_snapshot_id or not new_batch_id:
                raise ValidationFailed(
                    "采纳申诉必须提供更正快照编号与更正批次编号"
                )
            # 申诉期间原批次可能已被其他更正替代，更正须挂在最新在效批次上
            target_batch = self._latest_batch(self._get_batch(appeal.batch_id).id)
            old_snapshot = next(
                sid for sid in target_batch.snapshot_ids
                if self._get_snapshot(sid).supplier_code == appeal.supplier_code
            )
            index = target_batch.snapshot_ids.index(old_snapshot)
            corrected_ids = list(target_batch.snapshot_ids)
            corrected_ids[index] = corrected_snapshot_id
            snap = self._get_snapshot(corrected_snapshot_id)
            if not self._descends_from(snap, old_snapshot):
                raise ValidationFailed("申诉更正快照必须派生自在效批次快照")
            # 全部校验通过后才落状态、生成更正批次
            new_batch = self.correct_batch(
                new_batch_id,
                target_batch.id,
                CorrectionReason.APPEAL_ACCEPTED,
                corrected_snapshot_ids=corrected_ids,
                note=f"申诉 {appeal.id} 采纳：{decision_note}",
            )
            corrected_batch_id = new_batch.id

        appeal.status = AppealStatus.ACCEPTED if accepted else AppealStatus.REJECTED
        appeal.decided_at = _now()
        appeal.decision_note = decision_note
        appeal.corrected_by_batch = corrected_batch_id
        return appeal

    def _get_appeal(self, appeal_id: str) -> Appeal:
        try:
            return self.appeals[appeal_id]
        except KeyError:
            raise NotFound(f"申诉案件不存在：{appeal_id}") from None

    # ------------------------------------------------------------ 解释与复算

    def explain_score(
        self, batch_id: str, supplier_code: str
    ) -> dict[str, Any]:
        """逐项解释某批次中某供应商的得分与扣分来源。"""
        batch = self._get_batch(batch_id)
        result = batch.results.get(supplier_code)
        if result is None:
            raise NotFound(f"批次 {batch_id} 中无供应商 {supplier_code} 的结果")
        snapshot = self._get_snapshot(result.snapshot_id)
        return {
            "batch_id": batch.id,
            "batch_status": batch.status.value,
            "period": batch.period,
            "supplier_code": supplier_code,
            "total_score": result.total_score,
            "grade": result.grade,
            "calibration": {
                "rule_version": batch.rule_version,
                "weight_version": batch.weight_version,
                "snapshot_id": snapshot.id,
                "snapshot_source": snapshot.source,
                "snapshot_hash": snapshot.content_hash,
                "excluded_orders": list(snapshot.excluded_orders),
            },
            "lines": [line.to_dict() for line in result.lines],
            "revises": batch.revises,
        }

    def recompute(
        self, batch_id: str, supplier_code: str | None = None
    ) -> dict[str, Any]:
        """按批次固定的历史口径重新计算，验证与封账结果逐分一致。

        即使采购部门后来更换了权重或发布了勘误规则，本方法仍读取批次
        绑定的旧版本，输出复算值与封账值的逐行对比。
        """
        batch = self._get_batch(batch_id)
        rule = self._get_rule(batch.rule_version)
        weights = self._get_weights(batch.weight_version)
        targets = (
            [supplier_code] if supplier_code else list(batch.results.keys())
        )
        snapshot_by_supplier = {
            self._get_snapshot(sid).supplier_code: sid
            for sid in batch.snapshot_ids
        }
        recomputed: list[dict[str, Any]] = []
        for code in targets:
            sealed = batch.results.get(code)
            if sealed is None:
                raise NotFound(f"批次 {batch_id} 中无供应商 {code} 的结果")
            snapshot = self._get_snapshot(snapshot_by_supplier[code])
            fresh: SupplierScore = score_supplier(rule, weights, snapshot)
            line_match = [
                {
                    "metric_code": a.metric_code,
                    "sealed": a.weighted_score,
                    "recomputed": b.weighted_score,
                    "match": abs(a.weighted_score - b.weighted_score) < 1e-6,
                }
                for a, b in zip(sealed.lines, fresh.lines)
            ]
            recomputed.append({
                "supplier_code": code,
                "sealed_score": sealed.total_score,
                "recomputed_score": fresh.total_score,
                "sealed_grade": sealed.grade,
                "recomputed_grade": fresh.grade,
                "score_match": abs(sealed.total_score - fresh.total_score) < 1e-6,
                "grade_match": sealed.grade == fresh.grade,
                "lines": line_match,
            })
        return {
            "batch_id": batch.id,
            "rule_version": rule.code,
            "weight_version": weights.code,
            "results": recomputed,
        }

    # ------------------------------------------------------------ 内部

    def _prepare_inputs(
        self,
        rule_code: str,
        weight_code: str,
        snapshot_ids: list[str],
        require_published: bool,
    ):
        rule = self._get_rule(rule_code)
        weights = self._get_weights(weight_code)
        if require_published:
            if not rule.published:
                raise Conflict(f"规则版本 {rule_code} 尚未发布，不能用于正式封账")
            if weights.status != VersionStatus.PUBLISHED:
                raise Conflict(f"权重版本 {weight_code} 尚未发布，不能用于正式封账")
        if not snapshot_ids:
            raise ValidationFailed("至少需要一个数据快照")
        snapshots = [self._get_snapshot(sid) for sid in snapshot_ids]
        for snapshot in snapshots:
            if not snapshot.finalized:
                raise Conflict(f"快照 {snapshot.id} 尚未定稿，不能用于评分")
        suppliers = [s.supplier_code for s in snapshots]
        if len(suppliers) != len(set(suppliers)):
            raise ValidationFailed("同一供应商在一次评分中只能对应一份快照")
        return rule, weights, snapshots

    # ------------------------------------------------------------ 持久化

    def export_state(self) -> dict[str, Any]:
        """导出完整状态（含不可变历史），用于 JSON 持久化。"""
        return {
            "metrics": {c: m.to_dict() for c, m in self.metrics.items()},
            "rules": {c: r.to_dict() for c, r in self.rules.items()},
            "weights": {c: w.to_dict() for c, w in self.weights.items()},
            "snapshots": {c: s.to_dict() for c, s in self.snapshots.items()},
            "batches": {c: b.to_dict() for c, b in self.batches.items()},
            "appeals": {c: a.to_dict() for c, a in self.appeals.items()},
            "trials": {c: t.to_dict() for c, t in self.trials.items()},
        }

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.export_state(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
