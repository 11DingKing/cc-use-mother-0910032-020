"""供应商评级后端的领域回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supplier_rating.errors import Conflict, NotFound, ValidationFailed
from supplier_rating.models import (
    AppealStatus,
    CloseStatus,
    CorrectionReason,
    Metric,
    MetricKind,
)
from supplier_rating.store import RatingStore


def build_store() -> RatingStore:
    store = RatingStore()
    store.register_metric(Metric("delivery", "交付准时率", MetricKind.TIMELINESS))
    store.register_metric(
        Metric("quality", "来料质量", MetricKind.COUNT_PER_MILLION,
               unit="PPM", good_threshold=100, bad_threshold=5000)
    )
    store.register_metric(Metric("response", "响应达标率", MetricKind.RATIO))
    store.create_rule(
        "R1", ["delivery", "quality", "response"],
        [("A", 90), ("B", 80), ("C", 70), ("D", 0)],
    )
    store.publish_rule("R1")
    store.create_weights("W1", {"delivery": 0.5, "quality": 0.3, "response": 0.2})
    store.publish_weights("W1")
    store.ingest_snapshot("S1", "SUP1", "2026Q1", "WMS",
                          {"delivery": 0.90, "quality": 1000.0, "response": 0.80})
    store.ingest_snapshot("S2", "SUP2", "2026Q1", "WMS",
                          {"delivery": 0.70, "quality": 4000.0, "response": 0.90})
    return store


class ScoringTest(unittest.TestCase):
    def test_weighted_score_and_grade(self) -> None:
        store = build_store()
        batch = store.close_batch("B1", "2026Q1", "R1", "W1", ["S1", "S2"])
        s1 = batch.results["SUP1"]
        # delivery: 0.9*50=45; quality: (5000-1000)/4900≈0.8163*30≈24.49;
        # response: 0.8*20=16
        self.assertAlmostEqual(s1.total_score, 85.4898, places=2)
        self.assertEqual(s1.grade, "B")
        s2 = batch.results["SUP2"]
        # delivery 35; quality (1000/4900)*30≈6.12; response 18
        self.assertAlmostEqual(s2.total_score, 59.1224, places=2)
        self.assertEqual(s2.grade, "D")

    def test_invalid_ratio_and_weights(self) -> None:
        store = RatingStore()
        store.register_metric(Metric("delivery", "交付准时率", MetricKind.RATIO))
        with self.assertRaises(ValidationFailed):
            store.create_weights("W", {"delivery": 0.9})
        store.create_weights("W", {"delivery": 1.0})
        store.publish_weights("W")
        store.create_rule("R", ["delivery"], [("A", 60), ("D", 0)])
        store.publish_rule("R")
        store.ingest_snapshot("SX", "X", "2026Q1", "WMS", {"delivery": 1.2})
        with self.assertRaises(ValidationFailed):
            store.close_batch("BX", "2026Q1", "R", "W", ["SX"])


class ImmutabilityTest(unittest.TestCase):
    def test_published_rule_and_weights_are_frozen(self) -> None:
        store = build_store()
        with self.assertRaises(Conflict):
            store.publish_rule("R1")
        with self.assertRaises(Conflict):
            store.publish_weights("W1")

    def test_closed_batch_cannot_change_and_new_weights_keep_history(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S1"])
        # 采购部门换权重
        store.create_weights("W2", {"delivery": 0.2, "quality": 0.6, "response": 0.2})
        store.publish_weights("W2")
        # 历史批次仍绑定 W1，复算一致
        check = store.recompute("B1", "SUP1")["results"][0]
        self.assertTrue(check["score_match"])
        self.assertTrue(check["grade_match"])
        self.assertEqual(check["sealed_score"], check["recomputed_score"])

    def test_snapshot_cannot_be_overwritten(self) -> None:
        store = build_store()
        with self.assertRaises(Conflict):
            store.ingest_snapshot("S1", "SUP1", "2026Q1", "WMS",
                                  {"delivery": 0.99, "quality": 1.0, "response": 1.0})


class CorrectionTest(unittest.TestCase):
    def test_late_data_creates_versioned_correction_chain(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S1", "S2"])
        original = store.batches["B1"].results["SUP1"].total_score

        store.correct_snapshot(
            "S1-LATE", "S1", CorrectionReason.LATE_DATA,
            values={"delivery": 0.98},
        )
        new_batch = store.correct_batch(
            "B1-C1", "B1", CorrectionReason.LATE_DATA,
            corrected_snapshot_ids=["S1-LATE", "S2"],
        )
        self.assertEqual(new_batch.revises, "B1")
        self.assertEqual(store.batches["B1"].status, CloseStatus.CORRECTED)
        self.assertEqual(new_batch.rule_version, "R1")
        self.assertEqual(new_batch.weight_version, "W1")
        self.assertGreater(new_batch.results["SUP1"].total_score, original)
        # 原批次结果原样保留
        self.assertEqual(store.batches["B1"].results["SUP1"].total_score, original)

    def test_abnormal_order_exclusion_records_orders(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S2"])
        store.correct_snapshot(
            "S2-EX", "S2", CorrectionReason.ABNORMAL_EXCLUSION,
            values={"delivery": 0.85}, exclude_orders=["PO-1", "PO-2"],
        )
        batch = store.correct_batch(
            "B1-C1", "B1", CorrectionReason.ABNORMAL_EXCLUSION,
            corrected_snapshot_ids=["S2-EX"],
        )
        snap_id = batch.snapshot_ids[0]
        self.assertEqual(store.snapshots[snap_id].excluded_orders, ["PO-1", "PO-2"])
        with self.assertRaises(ValidationFailed):
            # 非派生快照不能用于更正
            store.correct_batch(
                "B1-CX", "B1-C1", CorrectionReason.LATE_DATA,
                corrected_snapshot_ids=["S2"],
            )

    def test_appeal_accepted_must_produce_correction_batch(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S1", "S2"])
        store.open_appeal("A1", "B1", "SUP2", "quality", reason="PPM 误计")
        with self.assertRaises(ValidationFailed):
            store.decide_appeal("A1", accepted=True, decision_note="采纳")
        store.correct_snapshot(
            "S2-APL", "S2", CorrectionReason.APPEAL_ACCEPTED,
            values={"quality": 800.0},
        )
        appeal = store.decide_appeal(
            "A1", accepted=True, decision_note="证据充分",
            corrected_snapshot_id="S2-APL", new_batch_id="B1-C1",
        )
        self.assertEqual(appeal.status, AppealStatus.ACCEPTED)
        self.assertEqual(appeal.corrected_by_batch, "B1-C1")
        self.assertEqual(store.batches["B1-C1"].revises, "B1")
        with self.assertRaises(Conflict):
            store.decide_appeal("A1", accepted=False)

    def test_rejected_appeal_changes_nothing(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S1"])
        store.open_appeal("A2", "B1", "SUP1", "delivery", reason="不认可")
        store.decide_appeal("A2", accepted=False, decision_note="证据不足")
        self.assertEqual(store.batches["B1"].status, CloseStatus.CLOSED)
        self.assertIsNone(store.appeals["A2"].corrected_by_batch)

    def test_erratum_rule_version_and_batch(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S2"])
        before = store.batches["B1"].results["SUP2"].total_score

        errata = [
            Metric("delivery", "交付准时率", MetricKind.TIMELINESS),
            Metric("quality", "来料质量", MetricKind.COUNT_PER_MILLION,
                   unit="PPM", good_threshold=100, bad_threshold=3000),
            Metric("response", "响应达标率", MetricKind.RATIO),
        ]
        rule = store.correct_rule(
            "R2", "R1", CorrectionReason.ERRATUM,
            note="归零阈值勘误 5000->3000", metric_defs=errata,
        )
        self.assertEqual(rule.correction_of, "R1")
        self.assertTrue(rule.published)
        with self.assertRaises(ValidationFailed):
            # 数据更正原因不能改规则
            store.correct_batch(
                "B1-BAD", "B1", CorrectionReason.LATE_DATA, rule_code="R2",
            )
        batch = store.correct_batch(
            "B1-C1", "B1", CorrectionReason.ERRATUM, rule_code="R2",
        )
        # 阈值更严，4000 PPM 在新口径下归零，分数下降
        self.assertLess(batch.results["SUP2"].total_score, before)
        # 原批次仍可按旧口径复算
        check = store.recompute("B1", "SUP2")["results"][0]
        self.assertTrue(check["score_match"])

    def test_data_correction_rejects_erratum_reason(self) -> None:
        store = build_store()
        with self.assertRaises(ValidationFailed):
            store.correct_snapshot("S1-E", "S1", CorrectionReason.ERRATUM)

    def test_correction_chain_across_multiple_rounds(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S1", "S2"])
        store.correct_snapshot("S1-A", "S1", CorrectionReason.LATE_DATA,
                               values={"delivery": 0.95})
        store.correct_batch("B1-C1", "B1", CorrectionReason.LATE_DATA,
                            corrected_snapshot_ids=["S1-A", "S2"])
        # 第二轮异常剔除，S1 沿用 S1-A，S2 使用派生自 S2 的新快照
        store.correct_snapshot("S2-B", "S2", CorrectionReason.ABNORMAL_EXCLUSION,
                               values={"delivery": 0.85}, exclude_orders=["PO-9"])
        store.correct_batch("B1-C2", "B1-C1", CorrectionReason.ABNORMAL_EXCLUSION,
                            corrected_snapshot_ids=["S1-A", "S2-B"])
        self.assertEqual(store.batches["B1-C2"].revises, "B1-C1")
        self.assertEqual(
            store.batches["B1-C2"].results["SUP1"].total_score,
            store.batches["B1-C1"].results["SUP1"].total_score,
        )


class TrialTest(unittest.TestCase):
    def test_trial_compare_does_not_affect_closed_data(self) -> None:
        store = build_store()
        store.create_weights("WDRAFT", {"delivery": 0.3, "quality": 0.5, "response": 0.2})
        t1 = store.create_trial("T1", "旧方案", "2026Q1", "R1", "W1", ["S1", "S2"])
        t2 = store.create_trial("T2", "新方案", "2026Q1", "R1", "WDRAFT", ["S1", "S2"])
        comparison = store.compare_trials(["T1", "T2"])
        rows = {r["supplier_code"]: r for r in comparison["rows"]}
        self.assertTrue(rows["SUP1"]["grade_changed"] or rows["SUP1"]["score_delta"] > 0)
        self.assertEqual(set(rows["SUP2"]["schemes"]), {"T1", "T2"})
        # 试算不产生批次
        self.assertNotIn("T1", store.batches)

    def test_close_requires_published_versions(self) -> None:
        store = RatingStore()
        store.register_metric(Metric("delivery", "交付准时率", MetricKind.RATIO))
        store.create_rule("RD", ["delivery"], [("A", 60), ("D", 0)])
        store.create_weights("WD", {"delivery": 1.0})
        store.ingest_snapshot("S", "X", "2026Q1", "WMS", {"delivery": 0.9})
        with self.assertRaises(Conflict):
            store.close_batch("B", "2026Q1", "RD", "WD", ["S"])


class ExplainTest(unittest.TestCase):
    def test_explain_lists_every_deduction(self) -> None:
        store = build_store()
        store.close_batch("B1", "2026Q1", "R1", "W1", ["S1"])
        detail = store.explain_score("B1", "SUP1")
        self.assertEqual(len(detail["lines"]), 3)
        delivery = next(line for line in detail["lines"]
                        if line["metric_code"] == "delivery")
        self.assertIn("扣分", delivery["explanation"])
        self.assertEqual(detail["calibration"]["rule_version"], "R1")
        self.assertEqual(detail["calibration"]["weight_version"], "W1")
        self.assertIsNotNone(detail["calibration"]["snapshot_hash"])
        # 快照哈希防篡改
        snap = store.snapshots["S1"]
        snap.values["delivery"] = 0.999
        self.assertNotEqual(snap.compute_hash(), detail["calibration"]["snapshot_hash"])

    def test_get_nonexistent(self) -> None:
        store = build_store()
        with self.assertRaises(NotFound):
            store.explain_score("NOPE", "SUP1")


if __name__ == "__main__":
    unittest.main()
