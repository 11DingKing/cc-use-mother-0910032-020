"""端到端业务演示：走通 发布 -> 封账 -> 换权重不影响历史 ->
迟到数据更正 -> 异常订单剔除 -> 申诉采纳 -> 规则勘误 -> 历史复算 全链路。

运行：python3 examples/demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supplier_rating.models import CorrectionReason
from supplier_rating.store import RatingStore


def show(title: str, value) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    store = RatingStore()

    # 1) 指标登记：交付准时率、质量（每百万缺陷）、响应达标率
    from supplier_rating.models import Metric, MetricKind

    store.register_metric(Metric("delivery", "交付准时率", MetricKind.TIMELINESS))
    store.register_metric(
        Metric("quality", "来料质量", MetricKind.COUNT_PER_MILLION,
               unit="PPM", good_threshold=100, bad_threshold=5000)
    )
    store.register_metric(Metric("response", "响应达标率", MetricKind.RATIO))

    # 2) 规则 v1 与权重 v1（交付 0.5 / 质量 0.3 / 响应 0.2）
    store.create_rule(
        "RULE-Q1-V1", ["delivery", "quality", "response"],
        [("A", 90), ("B", 80), ("C", 70), ("D", 0)],
    )
    store.publish_rule("RULE-Q1-V1")
    store.create_weights(
        "W-Q1-V1", {"delivery": 0.5, "quality": 0.3, "response": 0.2},
        note="季度初始权重",
    )
    store.publish_weights("W-Q1-V1")

    # 3) 数据来源快照（WMS/QMS/SRM），定稿后不可改
    store.ingest_snapshot("SNAP-S1-Q1", "SUP-001", "2026Q1", "WMS/QMS/SRM",
                          {"delivery": 0.92, "quality": 800.0, "response": 0.75})
    store.ingest_snapshot("SNAP-S2-Q1", "SUP-002", "2026Q1", "WMS/QMS/SRM",
                          {"delivery": 0.80, "quality": 3000.0, "response": 0.90})

    # 4) 试算：旧权重方案 vs 采购部门拟换的新权重方案
    store.create_weights("W-Q1-DRAFT2", {"delivery": 0.3, "quality": 0.5, "response": 0.2},
                         note="拟调整：质量优先")
    store.create_trial("TRIAL-OLD", "初始权重", "2026Q1",
                       "RULE-Q1-V1", "W-Q1-V1", ["SNAP-S1-Q1", "SNAP-S2-Q1"])
    store.create_trial("TRIAL-NEW", "质量优先方案", "2026Q1",
                       "RULE-Q1-V1", "W-Q1-DRAFT2", ["SNAP-S1-Q1", "SNAP-S2-Q1"])
    show("试算方案比较", store.compare_trials(["TRIAL-OLD", "TRIAL-NEW"]))

    # 5) 正式封账：固定规则、权重与输入
    batch = store.close_batch(
        "BATCH-2026Q1", "2026Q1", "RULE-Q1-V1", "W-Q1-V1",
        ["SNAP-S1-Q1", "SNAP-S2-Q1"],
    )
    show("封账结果（SUP-001）", store.explain_score("BATCH-2026Q1", "SUP-001"))

    # 6) 采购部门更换权重并发布 v2 —— 历史批次仍绑定 W-Q1-V1，等级不变
    store.publish_weights("W-Q1-DRAFT2")
    show("换权重后历史复算仍一致", store.recompute("BATCH-2026Q1", "SUP-001"))

    # 7) 迟到数据补录：SUP-001 的交付数据迟到，生成快照更正版 + 更正批次
    store.correct_snapshot(
        "SNAP-S1-Q1-LATE", "SNAP-S1-Q1", CorrectionReason.LATE_DATA,
        values={"delivery": 0.96}, note="3 月最后一批到货数据迟到补录",
    )
    store.correct_batch(
        "BATCH-2026Q1-C1", "BATCH-2026Q1", CorrectionReason.LATE_DATA,
        corrected_snapshot_ids=["SNAP-S1-Q1-LATE", "SNAP-S2-Q1"],
    )

    # 8) 异常订单剔除：SUP-002 一笔因客户临时改单导致的异常交付单剔除
    store.correct_snapshot(
        "SNAP-S2-Q1-EX", "SNAP-S2-Q1", CorrectionReason.ABNORMAL_EXCLUSION,
        values={"delivery": 0.86},
        exclude_orders=["PO-88012"],
        note="PO-88012 客户临时变更交期，认定为异常订单并剔除",
    )
    store.correct_batch(
        "BATCH-2026Q1-C2", "BATCH-2026Q1-C1", CorrectionReason.ABNORMAL_EXCLUSION,
        corrected_snapshot_ids=["SNAP-S1-Q1-LATE", "SNAP-S2-Q1-EX"],
    )

    # 9) 供应商申诉：SUP-002 不认可质量扣分，申诉采纳 -> 数据订正 -> 更正批次
    store.open_appeal(
        "APL-001", "BATCH-2026Q1-C2", "SUP-002", "quality",
        reason="缺陷批中两批已在入库前退货，不应计入 PPM",
        evidence="退货单 RT-556/R T-557",
    )
    store.correct_snapshot(
        "SNAP-S2-Q1-APL", "SNAP-S2-Q1-EX", CorrectionReason.APPEAL_ACCEPTED,
        values={"quality": 1200.0}, note="申诉 APL-001 采纳，剔除退货批后重算 PPM",
    )
    store.decide_appeal(
        "APL-001", accepted=True, decision_note="退货批证据充分，予以采纳",
        corrected_snapshot_id="SNAP-S2-Q1-APL",
        new_batch_id="BATCH-2026Q1-C3",
    )
    show("申诉采纳后的更正批次（SUP-002）",
         store.explain_score("BATCH-2026Q1-C3", "SUP-002"))

    # 10) 规则勘误：质量 PPM 归零阈值配置错误，发布勘误规则并更正批次
    errata_metrics = [
        Metric("delivery", "交付准时率", MetricKind.TIMELINESS),
        Metric("quality", "来料质量", MetricKind.COUNT_PER_MILLION,
               unit="PPM", good_threshold=100, bad_threshold=3000),
        Metric("response", "响应达标率", MetricKind.RATIO),
    ]
    store.correct_rule(
        "RULE-Q1-V2", "RULE-Q1-V1", CorrectionReason.ERRATUM,
        note="质量 PPM 归零阈值由 5000 更正为 3000",
        metric_defs=errata_metrics,
        grade_cutoffs=[("A", 90), ("B", 80), ("C", 70), ("D", 0)],
    )
    store.correct_batch(
        "BATCH-2026Q1-C4", "BATCH-2026Q1-C3", CorrectionReason.ERRATUM,
        rule_code="RULE-Q1-V2",
        note="按 RULE-Q1-V2 勘误口径重算",
    )

    # 11) 按历史口径复算最早批次：结果必须与当初封账完全一致
    show("最早批次按历史口径复算", store.recompute("BATCH-2026Q1"))
    print("\n演示完成：原始批次 BATCH-2026Q1 保持冻结，"
          "更正链 C1(迟到) -> C2(异常剔除) -> C3(申诉) -> C4(勘误) 全程可追溯。")


if __name__ == "__main__":
    main()
