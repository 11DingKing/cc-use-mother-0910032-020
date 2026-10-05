"""供应商评级后端的领域回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supplier_rating import OrderRecord, RatingError, RatingService  # noqa: E402

BANDS = [
    {"grade": "A", "min_inclusive": 90},
    {"grade": "B", "min_inclusive": 80, "max_exclusive": 90},
    {"grade": "C", "min_inclusive": 70, "max_exclusive": 80},
    {"grade": "D", "min_inclusive": 0, "max_exclusive": 70},
]


def build_service() -> RatingService:
    svc = RatingService()
    svc.register_source("ERP", "ERP 订单系统")
    svc.register_source("QMS", "质量管理系统")
    svc.register_source("SRM", "供应商门户")
    svc.create_metric("delivery", "准时交付率", "ERP", "ratio",
                      {"flag_field": "on_time", "fail_label": "迟交"}, "交付公式")
    svc.create_metric("quality", "来料合格率", "QMS", "ratio",
                      {"flag_field": "passed", "fail_label": "不合格"}, "质量公式")
    svc.create_metric("response", "报价响应达标率", "SRM", "sla_ratio",
                      {"value_field": "response_hours", "sla_hours": 24}, "响应公式")
    svc.create_batch("2026Q3", "2026-07-01", "2026-09-30")
    return svc


def seed_records(svc: RatingService) -> None:
    svc.ingest_records([
        OrderRecord("D1", "2026Q3", "S001", "delivery", "ERP", "2026-07-05", {"on_time": True}),
        OrderRecord("D2", "2026Q3", "S001", "delivery", "ERP", "2026-08-11", {"on_time": False}),
        OrderRecord("D3", "2026Q3", "S001", "delivery", "ERP", "2026-09-02", {"on_time": True}),
        OrderRecord("Q1", "2026Q3", "S001", "quality", "QMS", "2026-07-20", {"passed": True}),
        OrderRecord("Q2", "2026Q3", "S001", "quality", "QMS", "2026-08-18", {"passed": False}),
        OrderRecord("R1", "2026Q3", "S001", "response", "SRM", "2026-07-08", {"response_hours": 10}),
        OrderRecord("R2", "2026Q3", "S001", "response", "SRM", "2026-09-01", {"response_hours": 30}),
    ])


class WeightAndTrialTest(unittest.TestCase):
    def test_weights_must_sum_to_one(self) -> None:
        svc = build_service()
        with self.assertRaises(RatingError):
            svc.create_weight_version("quarterly", {"delivery": 0.9}, BANDS)

    def test_trial_compare_changing_weights_does_not_mutate_anything(self) -> None:
        svc = build_service()
        seed_records(svc)
        w1 = svc.publish_weight(svc.create_weight_version(
            "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2}, BANDS))
        w2 = svc.create_weight_version(
            "quarterly", {"delivery": 0.3, "quality": 0.5, "response": 0.2}, BANDS)
        cmp = svc.compare_trials("2026Q3", "S001", [w1.wid, w2.wid])
        self.assertNotEqual(cmp["deltas"][0]["total_score_delta"], 0.0)
        self.assertEqual(cmp["plans"][0]["metrics"]["delivery"]["weight"], 0.5)
        self.assertEqual(cmp["plans"][1]["metrics"]["quality"]["weight"], 0.5)
        # 试算不落任何发布/快照
        self.assertEqual(svc.publications, {})

    def test_publish_requires_published_weight_and_closed_batch(self) -> None:
        svc = build_service()
        seed_records(svc)
        draft = svc.create_weight_version(
            "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2}, BANDS)
        with self.assertRaises(RatingError):
            svc.publish("2026Q3", "S001", draft.wid)  # 未封账
        svc.close_batch("2026Q3")
        with self.assertRaises(RatingError):
            svc.publish("2026Q3", "S001", draft.wid)  # 草稿权重
        svc.publish_weight(draft.wid)
        pv = svc.publish("2026Q3", "S001", draft.wid)
        self.assertEqual(pv.version, 1)
        with self.assertRaises(RatingError):
            svc.publish("2026Q3", "S001", draft.wid)  # 不得重复发布


class ClosureAndCorrectionTest(unittest.TestCase):
    def _published(self) -> tuple[RatingService, str]:
        svc = build_service()
        seed_records(svc)
        w1 = svc.publish_weight(svc.create_weight_version(
            "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2}, BANDS))
        svc.close_batch("2026Q3")
        pv = svc.publish("2026Q3", "S001", w1.wid)
        return svc, pv.publication_id

    def test_late_data_rejected_by_normal_channel(self) -> None:
        svc, _ = self._published()
        late = [OrderRecord("D4", "2026Q3", "S001", "delivery", "ERP", "2026-09-20", {"on_time": True})]
        with self.assertRaises(RatingError):
            svc.ingest_records(late)
        self.assertNotIn("D4", svc.records)

    def test_late_data_only_creates_correction_version(self) -> None:
        svc, pid = self._published()
        late = [OrderRecord("D4", "2026Q3", "S001", "delivery", "ERP", "2026-09-20", {"on_time": True})]
        v2 = svc.correct_late_data(pid, late, "接口故障延迟同步")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.correction["kind"], "late_data")
        # v1 的快照与分数保持原样
        v1 = svc.explain(pid, version=1)
        self.assertNotIn("D4", [i["record_id"] for i in v1["explanation"]["metrics"][0]["sample_items"]])

    def test_order_exclusion_marks_snapshot_but_keeps_raw_record(self) -> None:
        svc, pid = self._published()
        v2 = svc.correct_order_exclusion(pid, ["D2"], "客户临时改期", "qe-zhang")
        self.assertEqual(v2.correction["kind"], "order_exclusion")
        self.assertIn("D2", svc.records)  # 原始记录不删除
        snap = svc.snapshots[v2.snapshot_id]
        entry = next(e for e in snap.entries if e.record_id == "D2")
        self.assertTrue(entry.excluded)
        # 交付样本从 3 条变 2 条且全部准时
        delivery = next(m for m in v2.explanation["metrics"] if m["metric_code"] == "delivery")
        self.assertEqual(delivery["sample_size"], 2)
        self.assertEqual(delivery["score"], 100.0)

    def test_appeal_accepted_generates_correction_and_links_back(self) -> None:
        svc, pid = self._published()
        apl = svc.file_appeal("S001", pid, "D2 为客户改期", "delivery", "D2")
        svc.decide_appeal(apl.appeal_id, True, "核实成立")
        self.assertEqual(svc.appeals[apl.appeal_id].status, "accepted")
        self.assertEqual(svc.appeals[apl.appeal_id].correction_version, 2)
        versions = svc.list_publication_versions(pid)["versions"]
        self.assertEqual(versions[-1]["correction"]["kind"], "order_exclusion")
        self.assertIn("APL-0001", versions[-1]["correction"]["requested_by"])

    def test_appeal_rejected_changes_nothing(self) -> None:
        svc, pid = self._published()
        apl = svc.file_appeal("S001", pid, "不认 D2", "delivery", "D2")
        svc.decide_appeal(apl.appeal_id, False, "证据不足")
        self.assertEqual(len(svc.publications[pid].versions), 1)
        self.assertEqual(svc.appeals[apl.appeal_id].correction_version, None)

    def test_erratum_requires_new_spec_version_and_pins_it(self) -> None:
        svc, pid = self._published()
        with self.assertRaises(RatingError):
            svc.correct_erratum(pid, "response")  # 还没有勘误版本
        svc.create_metric("response", "报价响应达标率", "SRM", "sla_ratio",
                          {"value_field": "response_hours", "sla_hours": 48},
                          "响应公式v2", erratum_note="SLA 应为 48h")
        v2 = svc.correct_erratum(pid, "response")
        self.assertEqual(v2.spec_versions["response"], 2)
        self.assertEqual(v2.spec_versions["delivery"], 1)  # 其他指标口径不变
        # v1 仍钉住旧规则，30h 在 24h SLA 下不达标；v2 在 48h 下达标
        v1_resp = next(m for m in svc.explain(pid, 1)["explanation"]["metrics"]
                       if m["metric_code"] == "response")
        v2_resp = next(m for m in v2.explanation["metrics"] if m["metric_code"] == "response")
        self.assertEqual(v1_resp["score"], 50.0)
        self.assertEqual(v2_resp["score"], 100.0)


class ExplanationAndRecomputeTest(unittest.TestCase):
    def test_explain_lists_each_deduction(self) -> None:
        svc = build_service()
        seed_records(svc)
        w1 = svc.publish_weight(svc.create_weight_version(
            "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2}, BANDS))
        svc.close_batch("2026Q3")
        svc.publish("2026Q3", "S001", w1.wid)
        detail = svc.explain("PUB-2026Q3-S001", metric_code="delivery")
        items = detail["explanation"]["metrics"][0]["sample_items"]
        bad = [i for i in items if not i["compliant"]]
        self.assertEqual([i["record_id"] for i in bad], ["D2"])
        self.assertEqual(bad[0]["detail"], "迟交")

    def test_recompute_matches_every_historical_version(self) -> None:
        svc = build_service()
        seed_records(svc)
        w1 = svc.publish_weight(svc.create_weight_version(
            "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2}, BANDS))
        svc.close_batch("2026Q3")
        pid = svc.publish("2026Q3", "S001", w1.wid).publication_id
        svc.correct_late_data(
            pid, [OrderRecord("D4", "2026Q3", "S001", "delivery", "ERP", "2026-09-20", {"on_time": True})],
            "迟到")
        svc.correct_order_exclusion(pid, ["D2"], "客户改期", "qe")
        svc.create_metric("response", "报价响应达标率", "SRM", "sla_ratio",
                          {"value_field": "response_hours", "sla_hours": 48}, "v2", erratum_note="勘误")
        svc.correct_erratum(pid, "response")
        for v in range(1, 5):
            check = svc.recompute(pid, v)
            self.assertTrue(check["hash_match"], f"v{v} 复算哈希不一致")

    def test_weight_change_after_publish_cannot_alter_history(self) -> None:
        svc = build_service()
        seed_records(svc)
        w1 = svc.publish_weight(svc.create_weight_version(
            "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2}, BANDS))
        svc.close_batch("2026Q3")
        pid = svc.publish("2026Q3", "S001", w1.wid).publication_id
        v1_score = svc.explain(pid, 1)["explanation"]["total_score"]
        # 采购事后发布全新权重方案，历史版本仍钉住 w1，复算不变
        w2 = svc.create_weight_version(
            "quarterly", {"delivery": 0.1, "quality": 0.8, "response": 0.1}, BANDS, note="事后换权重")
        svc.publish_weight(w2.wid)
        self.assertEqual(svc.recompute(pid, 1)["replayed"]["total_score"], v1_score)
        self.assertEqual(svc.explain(pid, 1)["weight_version_id"], "quarterly-v1")


if __name__ == "__main__":
    unittest.main()
