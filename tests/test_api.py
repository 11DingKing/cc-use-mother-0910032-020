"""HTTP 接口端到端测试（标准库 urllib，起真实端口）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supplier_rating.api import create_server
from supplier_rating.models import CloseStatus


class APITest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @classmethod
    def tearDownClass(cls) -> None:
        pass

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _seed(self) -> None:
        for metric in [
            {"code": "delivery", "name": "交付准时率", "kind": "timeliness"},
            {"code": "quality", "name": "来料质量", "kind": "cpm",
             "unit": "PPM", "good_threshold": 100, "bad_threshold": 5000},
            {"code": "response", "name": "响应达标率", "kind": "ratio"},
        ]:
            status, _ = self.call("POST", "/metrics", metric)
            self.assertEqual(status, 201)

        status, _ = self.call("POST", "/rules", {
            "code": "R1", "metrics": ["delivery", "quality", "response"],
            "grade_cutoffs": [["A", 90], ["B", 80], ["C", 70], ["D", 0]],
        })
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/rules/R1/publish", {})[0], 200)

        status, _ = self.call("POST", "/weights", {
            "code": "W1", "weights": {"delivery": 0.5, "quality": 0.3, "response": 0.2},
        })
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/weights/W1/publish", {})[0], 200)

        for sid, supplier, values in [
            ("S1", "SUP1", {"delivery": 0.9, "quality": 1000, "response": 0.8}),
            ("S2", "SUP2", {"delivery": 0.7, "quality": 4000, "response": 0.9}),
        ]:
            status, _ = self.call("POST", "/snapshots", {
                "id": sid, "supplier_code": supplier, "period": "2026Q1",
                "source": "WMS/QMS/SRM", "values": values,
            })
            self.assertEqual(status, 201)

    def test_full_flow(self) -> None:
        self._seed()

        # 试算比较
        self.assertEqual(self.call("POST", "/weights", {
            "code": "W2", "weights": {"delivery": 0.2, "quality": 0.6, "response": 0.2},
        })[0], 201)
        for tid, label, wv in [("T1", "旧", "W1"), ("T2", "新", "W2")]:
            status, _ = self.call("POST", "/trials", {
                "id": tid, "label": label, "period": "2026Q1",
                "rule_version": "R1", "weight_version": wv,
                "snapshot_ids": ["S1", "S2"],
            })
            self.assertEqual(status, 201)
        status, comparison = self.call("POST", "/trials/compare", {"trial_ids": ["T1", "T2"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(comparison["rows"]), 2)

        # 封账
        status, batch = self.call("POST", "/batches", {
            "id": "B1", "period": "2026Q1", "rule_version": "R1",
            "weight_version": "W1", "snapshot_ids": ["S1", "S2"],
        })
        self.assertEqual(status, 201)

        # 重复封账冲突
        self.assertEqual(self.call("POST", "/batches", {
            "id": "B1", "period": "2026Q1", "rule_version": "R1",
            "weight_version": "W1", "snapshot_ids": ["S1", "S2"],
        })[0], 409)

        # 逐项解释
        status, detail = self.call("GET", "/batches/B1/explain?supplier=SUP1")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["lines"]), 3)
        self.assertIn("snapshot_hash", detail["calibration"])

        # 历史口径复算
        status, recomputed = self.call("GET", "/batches/B1/recompute")
        self.assertEqual(status, 200)
        self.assertTrue(all(r["score_match"] for r in recomputed["results"]))

        # 迟到数据更正
        self.assertEqual(self.call("POST", "/snapshots/S1/corrections", {
            "new_id": "S1-LATE", "reason": "late_data",
            "values": {"delivery": 0.98},
        })[0], 201)
        status, c1 = self.call("POST", "/batches/B1/corrections", {
            "new_batch_id": "B1-C1", "reason": "late_data",
            "corrected_snapshot_ids": ["S1-LATE", "S2"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(c1["revises"], "B1")

        # 申诉驳回
        status, appeal = self.call("POST", "/appeals", {
            "id": "A1", "batch_id": "B1-C1", "supplier_code": "SUP2",
            "metric_code": "quality", "reason": "不认可扣分",
        })
        self.assertEqual(status, 201)
        status, decision = self.call("POST", "/appeals/A1/decision", {
            "accepted": False, "decision_note": "证据不足",
        })
        self.assertEqual(status, 200)
        self.assertIsNone(decision["corrected_by_batch"])

        # 规则勘误
        status, _ = self.call("POST", "/rules/R1/corrections", {
            "new_code": "R2", "note": "PPM 归零阈值勘误",
            "metric_defs": [
                {"code": "delivery", "name": "交付准时率", "kind": "timeliness"},
                {"code": "quality", "name": "来料质量", "kind": "cpm",
                 "unit": "PPM", "good_threshold": 100, "bad_threshold": 3000},
                {"code": "response", "name": "响应达标率", "kind": "ratio"},
            ],
        })
        self.assertEqual(status, 201)
        status, c2 = self.call("POST", "/batches/B1-C1/corrections", {
            "new_batch_id": "B1-C2", "reason": "erratum", "rule_code": "R2",
        })
        self.assertEqual(status, 201)
        self.assertEqual(c2["rule_version"], "R2")

        # 原批次仍冻结且可解释
        status, original = self.call("GET", "/batches/B1")
        self.assertEqual(status, 200)
        self.assertEqual(original["status"], CloseStatus.CORRECTED.value)
        self.assertEqual(original["weight_version"], "W1")

    def test_bad_requests(self) -> None:
        self._seed()
        # 权重和不为 1
        status, body = self.call("POST", "/weights", {
            "code": "WBAD", "weights": {"delivery": 0.5},
        })
        self.assertEqual(status, 400)
        self.assertIn("权重", body["error"])
        # 不存在的对象
        self.assertEqual(self.call("GET", "/metrics/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
