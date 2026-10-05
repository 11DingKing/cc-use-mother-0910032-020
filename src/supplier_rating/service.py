"""应用服务层：维护指标、数据来源、权重版本、封账批次、发布与申诉。

核心纪律：
- 权重方案分 draft/published；正式发布只接受 published，并钉住 weight_version_id。
- 发布时对输入记录做不可变快照（含 content_hash），并钉住每项指标规则版本。
- 批次封账后常规通道拒收记录；迟到数据、异常订单剔除、申诉采纳、规则勘误
  只能在既有发布上“追加更正版本”，绝不修改历史版本。
- 复算 = 用版本钉住的快照/规则/权重重新跑纯函数引擎，与留存结果比对哈希。
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from .models import (
    Appeal,
    Batch,
    DataSource,
    InputSnapshot,
    MetricSpec,
    OrderRecord,
    Publication,
    PublicationVersion,
    SnapshotEntry,
    WeightVersion,
)
from .scoring import score_supplier


class RatingError(ValueError):
    """业务规则冲突（输入不合法、状态不允许等）。"""


def canonical_hash(payload: Any) -> str:
    """对任意可 JSON 化对象计算稳定哈希，用于快照与评分结果固化。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RatingService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.sources: dict[str, DataSource] = {}
        self.specs: dict[str, dict[int, MetricSpec]] = {}
        self.weights: dict[str, WeightVersion] = {}
        self._scheme_next: dict[str, int] = {}
        self.batches: dict[str, Batch] = {}
        self.records: dict[str, OrderRecord] = {}  # record_id 全局唯一不可变
        self.snapshots: dict[str, InputSnapshot] = {}
        self.publications: dict[str, Publication] = {}
        self.appeals: dict[str, Appeal] = {}
        self._batch_seq: dict[str, int] = {}
        self._appeal_seq = 0

    # ---------- 数据来源 ----------
    def register_source(self, code: str, name: str, description: str = "") -> DataSource:
        with self._lock:
            if code in self.sources:
                raise RatingError(f"数据来源已存在：{code}")
            source = DataSource(code=code, name=name, description=description)
            self.sources[code] = source
            return source

    # ---------- 评分指标（含规则勘误版本） ----------
    def create_metric(
        self,
        code: str,
        name: str,
        source_code: str,
        kind: str,
        params: dict[str, Any],
        formula_text: str,
        erratum_note: str | None = None,
    ) -> MetricSpec:
        """新建指标或为既有指标追加勘误版本；历史版本永久保留。"""
        with self._lock:
            if source_code not in self.sources:
                raise RatingError(f"未知数据来源：{source_code}")
            if kind not in ("ratio", "sla_ratio"):
                raise RatingError(f"未知指标类型：{kind}")
            version = max(self.specs.get(code, {}), default=0) + 1
            spec = MetricSpec(
                code=code,
                name=name,
                source_code=source_code,
                version=version,
                kind=kind,
                params=dict(params),
                formula_text=formula_text,
                erratum_note=erratum_note,
            )
            self.specs.setdefault(code, {})[version] = spec
            return spec

    def _latest_specs(self) -> dict[str, MetricSpec]:
        return {code: versions[max(versions)] for code, versions in self.specs.items()}

    def _pinned_specs(self, spec_versions: dict[str, int]) -> dict[str, MetricSpec]:
        pinned: dict[str, MetricSpec] = {}
        for code, ver in spec_versions.items():
            if code not in self.specs or ver not in self.specs[code]:
                raise RatingError(f"指标规则版本已缺失，无法复算：{code}@v{ver}")
            pinned[code] = self.specs[code][ver]
        return pinned

    # ---------- 权重方案版本 ----------
    def create_weight_version(
        self, scheme_code: str, weights: dict[str, float], grade_bands: list[dict[str, Any]], note: str = ""
    ) -> WeightVersion:
        """创建权重方案的新草稿版本。换权重 = 新版本，从不改动旧版本。"""
        with self._lock:
            unknown = set(weights) - set(self.specs)
            if unknown:
                raise RatingError("权重引用了未知指标：" + "、".join(sorted(unknown)))
            total = round(sum(weights.values()), 6)
            if abs(total - 1.0) > 1e-6:
                raise RatingError(f"权重之和必须为 1，当前为 {total}")
            if not grade_bands:
                raise RatingError("等级分数段不能为空")
            version = self._scheme_next.get(scheme_code, 0) + 1
            self._scheme_next[scheme_code] = version
            bands = tuple(
                dict(grade=b["grade"], min_inclusive=float(b["min_inclusive"]), max_exclusive=b.get("max_exclusive"))
                for b in sorted(grade_bands, key=lambda x: float(x["min_inclusive"]), reverse=True)
            )
            wv = WeightVersion(
                wid=f"{scheme_code}-v{version}",
                scheme_code=scheme_code,
                version=version,
                weights=dict(weights),
                grade_bands=bands,
                status="draft",
                note=note,
            )
            self.weights[wv.wid] = wv
            return wv

    def publish_weight(self, wid: str | WeightVersion) -> WeightVersion:
        with self._lock:
            wid = wid.wid if isinstance(wid, WeightVersion) else wid
            wv = self.weights.get(wid)
            if wv is None:
                raise RatingError(f"未知权重版本：{wid}")
            if wv.status == "published":
                raise RatingError("权重版本已发布且冻结")
            self.weights[wid] = WeightVersion(
                wid=wv.wid, scheme_code=wv.scheme_code, version=wv.version,
                weights=wv.weights, grade_bands=wv.grade_bands,
                status="published", note=wv.note,
            )
            return self.weights[wid]

    # ---------- 封账批次与数据通道 ----------
    def create_batch(self, batch_id: str, period_start: str, period_end: str) -> Batch:
        with self._lock:
            if batch_id in self.batches:
                raise RatingError(f"批次已存在：{batch_id}")
            if period_start > period_end:
                raise RatingError("批次起止日期不合法")
            batch = Batch(batch_id=batch_id, period_start=period_start, period_end=period_end)
            self.batches[batch_id] = batch
            return batch

    def close_batch(self, batch_id: str) -> Batch:
        with self._lock:
            batch = self._require_batch(batch_id)
            if batch.status == "closed":
                raise RatingError("批次已封账")
            batch.status = "closed"
            return batch

    def ingest_records(self, records: list[OrderRecord]) -> int:
        """常规数据通道：仅封账前接受。记录一经录入不可变、不可删除。"""
        with self._lock:
            for rec in records:
                if rec.record_id in self.records:
                    raise RatingError(f"记录编号重复：{rec.record_id}")
                batch = self._require_batch(rec.batch_id)
                if batch.status == "closed":
                    raise RatingError(
                        f"批次 {rec.batch_id} 已封账，记录 {rec.record_id} 属迟到数据，"
                        "只能通过更正版本（late_data）补录"
                    )
                if not (batch.period_start <= rec.occurred_at[:10] <= batch.period_end):
                    raise RatingError(f"记录 {rec.record_id} 发生时间超出批次区间")
                if rec.metric_code not in self.specs:
                    raise RatingError(f"记录 {rec.record_id} 引用未知指标：{rec.metric_code}")
            for rec in records:
                self.records[rec.record_id] = rec
            return len(records)

    def _batch_supplier_records(self, batch_id: str, supplier_id: str) -> list[OrderRecord]:
        return [
            rec for rec in self.records.values()
            if rec.batch_id == batch_id and rec.supplier_id == supplier_id
        ]

    # ---------- 试算与方案比较 ----------
    def trial(self, batch_id: str, supplier_id: str, weight_version_id: str) -> dict[str, Any]:
        """用当前全部记录 + 最新规则 + 指定权重方案试算，不产生任何固化结果。"""
        with self._lock:
            self._require_batch(batch_id)
            wv = self._require_weight(weight_version_id)
            records = self._batch_supplier_records(batch_id, supplier_id)
            by_metric = self._group_records(records)
            return score_supplier(by_metric, self._latest_specs(), wv)

    def compare_trials(self, batch_id: str, supplier_id: str, weight_version_ids: list[str]) -> dict[str, Any]:
        """同一批数据上比较多个权重方案（可含草稿），输出总分/等级差异。"""
        with self._lock:
            if len(weight_version_ids) < 2:
                raise RatingError("方案比较至少需要两个权重版本")
            results = [self.trial(batch_id, supplier_id, wid) for wid in weight_version_ids]
            base = results[0]
            plans: list[dict[str, Any]] = []
            for res in results:
                plans.append({
                    "weight_version_id": res["weight_version_id"],
                    "total_score": res["total_score"],
                    "grade": res["grade"],
                    "metrics": {
                        m["metric_code"]: {"score": m["score"], "weight": m["weight"],
                                           "weighted_contribution": m["weighted_contribution"]}
                        for m in res["metrics"]
                    },
                })
            deltas = []
            for plan in plans[1:]:
                deltas.append({
                    "against": base["weight_version_id"],
                    "compare": plan["weight_version_id"],
                    "total_score_delta": _sub(plan["total_score"], base["total_score"]),
                    "grade_changed": plan["grade"] != base["grade"],
                    "base_grade": base["grade"],
                    "compare_grade": plan["grade"],
                })
            return {"batch_id": batch_id, "supplier_id": supplier_id, "plans": plans, "deltas": deltas}

    # ---------- 正式发布 ----------
    def publish(self, batch_id: str, supplier_id: str, weight_version_id: str) -> PublicationVersion:
        with self._lock:
            batch = self._require_batch(batch_id)
            if batch.status != "closed":
                raise RatingError("批次封账后才能正式发布")
            existing = [
                p for p in self.publications.values()
                if p.batch_id == batch_id and p.supplier_id == supplier_id
            ]
            if existing:
                raise RatingError(
                    f"供应商 {supplier_id} 在批次 {batch_id} 已发布（{existing[0].publication_id}），"
                    "更换口径不得重新发布，只能追加更正版本"
                )
            wv = self._require_weight(weight_version_id)
            if wv.status != "published":
                raise RatingError("正式发布只能使用已发布（published）的权重版本")
            records = self._batch_supplier_records(batch_id, supplier_id)
            snapshot = self._make_snapshot(batch_id, supplier_id, sorted(r.record_id for r in records),
                                           excluded={}, parent=None, origin="publish")
            spec_versions = {code: spec.version for code, spec in self._latest_specs().items()}
            publication_id = f"PUB-{batch_id}-{supplier_id}"
            publication = Publication(publication_id=publication_id, batch_id=batch_id, supplier_id=supplier_id)
            self.publications[publication_id] = publication
            return self._append_version(publication, snapshot, wv, spec_versions, correction=None)

    # ---------- 更正版本 ----------
    def correct_late_data(self, publication_id: str, records: list[OrderRecord], reason: str) -> PublicationVersion:
        """封账后迟到数据：补录为不可变记录，并生成 late_data 更正版本。"""
        with self._lock:
            publication = self._require_publication(publication_id)
            batch = self._require_batch(publication.batch_id)
            if batch.status != "closed":
                raise RatingError("批次尚未封账，迟到数据应走常规录入通道")
            known = {r.record_id for r in self._batch_supplier_records(publication.batch_id, publication.supplier_id)}
            new_ids: list[str] = []
            for rec in records:
                if rec.record_id in self.records or rec.record_id in known:
                    raise RatingError(f"迟到记录编号重复：{rec.record_id}")
                if rec.batch_id != publication.batch_id or rec.supplier_id != publication.supplier_id:
                    raise RatingError(f"迟到记录 {rec.record_id} 与发布归属不一致")
                if not (batch.period_start <= rec.occurred_at[:10] <= batch.period_end):
                    raise RatingError(f"迟到记录 {rec.record_id} 发生时间超出批次区间")
                if rec.metric_code not in self.specs:
                    raise RatingError(f"迟到记录 {rec.record_id} 引用未知指标：{rec.metric_code}")
                new_ids.append(rec.record_id)
            for rec in records:
                self.records[rec.record_id] = rec  # 补录后同样不可变
            parent = self._latest_snapshot(publication)
            snapshot = self._make_snapshot(
                publication.batch_id, publication.supplier_id,
                sorted({e.record_id for e in parent.entries} | set(new_ids)),
                excluded={e.record_id: e.exclude_reason for e in parent.entries if e.excluded},
                parent=parent.snapshot_id, origin="late_data",
            )
            last = publication.versions[-1]
            correction = {"kind": "late_data", "reason": reason,
                          "added_record_ids": sorted(new_ids), "base_version": last.version}
            return self._append_version(publication, snapshot, self.weights[last.weight_version_id],
                                        dict(last.spec_versions), correction)

    def correct_order_exclusion(
        self, publication_id: str, record_ids: list[str], reason: str, requested_by: str
    ) -> PublicationVersion:
        """异常订单剔除：只在快照层标记 excluded，原始记录保留，生成更正版本。"""
        with self._lock:
            publication = self._require_publication(publication_id)
            parent = self._latest_snapshot(publication)
            index = {e.record_id: e for e in parent.entries}
            for rid in record_ids:
                if rid not in index:
                    raise RatingError(f"记录不在发布输入范围内，无法剔除：{rid}")
                if index[rid].excluded:
                    raise RatingError(f"记录已被剔除：{rid}")
            excluded = {e.record_id: e.exclude_reason for e in parent.entries if e.excluded}
            for rid in record_ids:
                excluded[rid] = reason
            snapshot = self._make_snapshot(
                publication.batch_id, publication.supplier_id,
                sorted(index), excluded=excluded,
                parent=parent.snapshot_id, origin="order_exclusion",
            )
            last = publication.versions[-1]
            correction = {"kind": "order_exclusion", "reason": reason, "requested_by": requested_by,
                          "excluded_record_ids": sorted(record_ids), "base_version": last.version}
            return self._append_version(publication, snapshot, self.weights[last.weight_version_id],
                                        dict(last.spec_versions), correction)

    def correct_erratum(self, publication_id: str, metric_code: str, note: str = "") -> PublicationVersion:
        """规则勘误：以该指标的最新勘误版本重算；输入快照不变，钉住新版本。"""
        with self._lock:
            publication = self._require_publication(publication_id)
            if metric_code not in self.specs:
                raise RatingError(f"未知指标：{metric_code}")
            last = publication.versions[-1]
            pinned_ver = last.spec_versions.get(metric_code)
            latest_ver = max(self.specs[metric_code])
            if pinned_ver is None:
                raise RatingError(f"原发布未覆盖指标 {metric_code}，不能以勘误形式追加")
            if latest_ver <= pinned_ver:
                raise RatingError(f"指标 {metric_code} 没有比 v{pinned_ver} 更新的勘误版本")
            spec_versions = dict(last.spec_versions)
            spec_versions[metric_code] = latest_ver
            snapshot = self.snapshots[last.snapshot_id]  # 输入原样固定
            correction = {"kind": "rule_erratum", "metric_code": metric_code,
                          "from_spec_version": pinned_ver, "to_spec_version": latest_ver,
                          "erratum_note": self.specs[metric_code][latest_ver].erratum_note or note,
                          "base_version": last.version}
            return self._append_version(publication, snapshot, self.weights[last.weight_version_id],
                                        spec_versions, correction)

    # ---------- 申诉 ----------
    def file_appeal(
        self, supplier_id: str, publication_id: str, reason: str,
        metric_code: str | None = None, record_id: str | None = None,
    ) -> Appeal:
        with self._lock:
            publication = self._require_publication(publication_id)
            if supplier_id != publication.supplier_id:
                raise RatingError("只能对本供应商的评级提出申诉")
            self._appeal_seq += 1
            appeal = Appeal(
                appeal_id=f"APL-{self._appeal_seq:04d}",
                publication_id=publication_id,
                supplier_id=supplier_id,
                reason=reason,
                metric_code=metric_code,
                record_id=record_id,
            )
            self.appeals[appeal.appeal_id] = appeal
            return appeal

    def decide_appeal(self, appeal_id: str, accepted: bool, decision_note: str) -> Appeal:
        with self._lock:
            appeal = self.appeals.get(appeal_id)
            if appeal is None:
                raise RatingError(f"未知申诉：{appeal_id}")
            if appeal.status != "filed":
                raise RatingError("申诉已作出决定")
            if accepted:
                if not appeal.record_id:
                    raise RatingError("采纳申诉必须指明受影响的记录，以便形成可追溯的输入更正")
                publication = self._require_publication(appeal.publication_id)
                version = self.correct_order_exclusion(
                    appeal.publication_id, [appeal.record_id],
                    reason=f"申诉 {appeal_id} 采纳：{appeal.reason}",
                    requested_by=f"appeal:{appeal_id}",
                )
                appeal.status = "accepted"
                appeal.decision_note = decision_note
                appeal.correction_version = version.version
            else:
                appeal.status = "rejected"
                appeal.decision_note = decision_note
            return appeal

    # ---------- 解释与历史口径复算 ----------
    def explain(self, publication_id: str, version: int | None = None,
                metric_code: str | None = None) -> dict[str, Any]:
        """返回某版本的逐项得分解释，可只看单个指标的每条扣分依据。"""
        with self._lock:
            publication = self._require_publication(publication_id)
            pv = self._version(publication, version)
            explanation = pv.explanation
            if metric_code:
                metrics = [m for m in explanation["metrics"] if m["metric_code"] == metric_code]
                if not metrics:
                    raise RatingError(f"该版本不含指标：{metric_code}")
                body = dict(explanation)
                body["metrics"] = metrics
            else:
                body = explanation
            return {
                "publication_id": publication_id,
                "version": pv.version,
                "is_correction": pv.correction is not None,
                "correction": pv.correction,
                "weight_version_id": pv.weight_version_id,
                "spec_versions": pv.spec_versions,
                "snapshot_id": pv.snapshot_id,
                "explanation": body,
            }

    def recompute(self, publication_id: str, version: int | None = None) -> dict[str, Any]:
        """按历史版本钉住的快照、规则版本、权重复算，并校验留存哈希。"""
        with self._lock:
            publication = self._require_publication(publication_id)
            pv = self._version(publication, version)
            snapshot = self.snapshots.get(pv.snapshot_id)
            if snapshot is None:
                raise RatingError("历史输入快照缺失，无法保证按原口径复算")
            records = [self.records[e.record_id] for e in snapshot.entries if not e.excluded]
            by_metric = self._group_records(records)
            wv = self.weights.get(pv.weight_version_id)
            if wv is None:
                raise RatingError("历史权重版本缺失，无法保证按原口径复算")
            replayed = score_supplier(by_metric, self._pinned_specs(pv.spec_versions), wv, snapshot)
        replayed_hash = canonical_hash(replayed)
        return {
            "publication_id": publication_id,
            "version": pv.version,
            "stored_hash": pv.content_hash,
            "recomputed_hash": replayed_hash,
            "hash_match": replayed_hash == pv.content_hash,
            "replayed": replayed,
        }

    def list_publication_versions(self, publication_id: str) -> dict[str, Any]:
        with self._lock:
            publication = self._require_publication(publication_id)
            return {
                "publication_id": publication_id,
                "batch_id": publication.batch_id,
                "supplier_id": publication.supplier_id,
                "versions": [
                    {
                        "version": pv.version,
                        "seq": pv.seq,
                        "weight_version_id": pv.weight_version_id,
                        "snapshot_id": pv.snapshot_id,
                        "spec_versions": pv.spec_versions,
                        "total_score": pv.explanation["total_score"],
                        "grade": pv.explanation["grade"],
                        "is_correction": pv.correction is not None,
                        "correction": pv.correction,
                    }
                    for pv in publication.versions
                ],
            }

    # ---------- 内部辅助 ----------
    def _append_version(
        self, publication: Publication, snapshot: InputSnapshot,
        wv: WeightVersion, spec_versions: dict[str, int],
        correction: dict[str, Any] | None,
    ) -> PublicationVersion:
        records = [self.records[e.record_id] for e in snapshot.entries if not e.excluded]
        by_metric = self._group_records(records)
        explanation = score_supplier(by_metric, self._pinned_specs(spec_versions), wv, snapshot)
        seq = self._batch_seq.get(publication.batch_id, 0) + 1
        self._batch_seq[publication.batch_id] = seq
        pv = PublicationVersion(
            publication_id=publication.publication_id,
            version=len(publication.versions) + 1,
            batch_id=publication.batch_id,
            supplier_id=publication.supplier_id,
            weight_version_id=wv.wid,
            spec_versions=dict(spec_versions),
            snapshot_id=snapshot.snapshot_id,
            explanation=explanation,
            content_hash=canonical_hash(explanation),
            seq=seq,
            correction=correction,
        )
        publication.versions.append(pv)
        return pv

    def _make_snapshot(
        self, batch_id: str, supplier_id: str, record_ids: list[str],
        excluded: dict[str, str], parent: str | None, origin: str,
    ) -> InputSnapshot:
        entries = tuple(
            SnapshotEntry(record_id=rid, excluded=rid in excluded,
                          exclude_reason=excluded.get(rid))
            for rid in record_ids
        )
        content = [(e.record_id, e.excluded, e.exclude_reason) for e in entries]
        snapshot_id = "SNAP-" + canonical_hash((batch_id, supplier_id, content))[:16]
        snapshot = InputSnapshot(
            snapshot_id=snapshot_id,
            batch_id=batch_id,
            supplier_id=supplier_id,
            entries=entries,
            parent_snapshot_id=parent,
            content_hash=canonical_hash(content),
            origin=origin,
        )
        self.snapshots[snapshot_id] = snapshot
        return snapshot

    @staticmethod
    def _group_records(records: list[OrderRecord]) -> dict[str, list[OrderRecord]]:
        grouped: dict[str, list[OrderRecord]] = {}
        for rec in records:
            grouped.setdefault(rec.metric_code, []).append(rec)
        return grouped

    def _require_batch(self, batch_id: str) -> Batch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise RatingError(f"未知批次：{batch_id}")
        return batch

    def _require_weight(self, wid: str) -> WeightVersion:
        wv = self.weights.get(wid)
        if wv is None:
            raise RatingError(f"未知权重版本：{wid}")
        return wv

    def _require_publication(self, publication_id: str) -> Publication:
        publication = self.publications.get(publication_id)
        if publication is None:
            raise RatingError(f"未知发布：{publication_id}")
        return publication

    @staticmethod
    def _version(publication: Publication, version: int | None) -> PublicationVersion:
        if not publication.versions:
            raise RatingError("发布尚无任何版本")
        if version is None:
            return publication.versions[-1]
        for pv in publication.versions:
            if pv.version == version:
                return pv
        raise RatingError(f"发布不存在版本 v{version}")

    def _latest_snapshot(self, publication: Publication) -> InputSnapshot:
        return self.snapshots[publication.versions[-1].snapshot_id]


def _sub(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(a - b, 4)
