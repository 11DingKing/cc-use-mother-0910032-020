"""端到端演示：一个季度供应商评级的完整生命周期。

运行：python3 tools/demo_rating.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supplier_rating import OrderRecord, RatingService  # noqa: E402


def j(title: str, value) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=lambda o: vars(o)))


def main() -> None:
    svc = RatingService()

    # 1) 数据来源
    svc.register_source("ERP", "ERP 订单系统", "交付时间与订单状态")
    svc.register_source("QMS", "质量管理系统", "来料检验结果")
    svc.register_source("SRM", "供应商门户", "询报价响应时长")

    # 2) 评分指标
    svc.create_metric("delivery", "准时交付率", "ERP", "ratio",
                      {"flag_field": "on_time", "noun": "订单", "fail_label": "迟交"},
                      "准时交付率 = 准时订单数 / 订单总数 × 100")
    svc.create_metric("quality", "来料合格率", "QMS", "ratio",
                      {"flag_field": "passed", "noun": "检验批", "fail_label": "检验不合格"},
                      "来料合格率 = 合格批次数 / 检验批次总数 × 100")
    svc.create_metric("response", "报价响应达标率", "SRM", "sla_ratio",
                      {"value_field": "response_hours", "sla_hours": 24},
                      "报价响应达标率 = 24h 内响应次数 / 报价请求总数 × 100")

    # 3) 两套权重方案（采购部门换权重 => 新版本，旧版本保留）
    w1 = svc.create_weight_version(
        "quarterly", {"delivery": 0.5, "quality": 0.3, "response": 0.2},
        [{"grade": "A", "min_inclusive": 90}, {"grade": "B", "min_inclusive": 80, "max_exclusive": 90},
         {"grade": "C", "min_inclusive": 70, "max_exclusive": 80}, {"grade": "D", "min_inclusive": 0, "max_exclusive": 70}],
        note="初始权重：交付优先")
    svc.publish_weight(w1.wid)
    w2 = svc.create_weight_version(
        "quarterly", {"delivery": 0.3, "quality": 0.5, "response": 0.2},
        [{"grade": "A", "min_inclusive": 90}, {"grade": "B", "min_inclusive": 80, "max_exclusive": 90},
         {"grade": "C", "min_inclusive": 70, "max_exclusive": 80}, {"grade": "D", "min_inclusive": 0, "max_exclusive": 70}],
        note="调整权重：质量优先")
    # 草稿也可试算；正式发布前再发布

    # 4) 季度批次与数据录入
    svc.create_batch("2026Q3", "2026-07-01", "2026-09-30")
    rows = [
        OrderRecord("D1", "2026Q3", "S001", "delivery", "ERP", "2026-07-05", {"on_time": True}),
        OrderRecord("D2", "2026Q3", "S001", "delivery", "ERP", "2026-08-11", {"on_time": False}),
        OrderRecord("D3", "2026Q3", "S001", "delivery", "ERP", "2026-09-02", {"on_time": True}),
        OrderRecord("Q1", "2026Q3", "S001", "quality", "QMS", "2026-07-20", {"passed": True}),
        OrderRecord("Q2", "2026Q3", "S001", "quality", "QMS", "2026-08-18", {"passed": True}),
        OrderRecord("Q3", "2026Q3", "S001", "quality", "QMS", "2026-09-15", {"passed": True}),
        OrderRecord("R1", "2026Q3", "S001", "response", "SRM", "2026-07-08", {"response_hours": 10}),
        OrderRecord("R2", "2026Q3", "S001", "response", "SRM", "2026-09-01", {"response_hours": 30}),
    ]
    svc.ingest_records(rows)

    # 5) 试算并比较两套方案
    comparison = svc.compare_trials("2026Q3", "S001", [w1.wid, w2.wid])
    j("试算比较（换权重前后）", comparison["deltas"])

    # 6) 封账并按 w1 正式发布（固定输入快照与规则版本）
    svc.close_batch("2026Q3")
    v1 = svc.publish("2026Q3", "S001", w1.wid)
    j("正式发布 v1（总分/等级）", {"version": v1.version, "total_score": v1.explanation["total_score"],
                              "grade": v1.explanation["grade"], "snapshot": v1.snapshot_id})

    # 7) 迟到数据：封账后常规通道被拒，只能走更正版本
    late = [OrderRecord("D4", "2026Q3", "S001", "delivery", "ERP", "2026-09-28", {"on_time": True})]
    try:
        svc.ingest_records(late)
    except Exception as exc:
        j("封账后常规录入被拒", {"error": str(exc)})
    v2 = svc.correct_late_data(v1.publication_id, late, reason="ERP 接口故障，交付数据延迟 5 天同步")
    j("更正版本 v2（迟到数据补录）", {"version": v2.version, "total_score": v2.explanation["total_score"],
                                "grade": v2.explanation["grade"], "correction": v2.correction})

    # 8) 供应商申诉：D2 迟交是客户临时改期，属异常订单 -> 采纳后生成更正版本
    apl = svc.file_appeal("S001", v1.publication_id, "D2 迟交系客户临时改期，非供应商责任",
                          metric_code="delivery", record_id="D2")
    svc.decide_appeal(apl.appeal_id, accepted=True, decision_note="核实客户改期邮件，申诉成立")
    j("申诉案件", vars(svc.appeals[apl.appeal_id]))

    # 9) 规则勘误：响应 SLA 从 24h 更正为 48h（指标 v2），仅影响更正版本
    svc.create_metric("response", "报价响应达标率", "SRM", "sla_ratio",
                      {"value_field": "response_hours", "sla_hours": 48},
                      "报价响应达标率 = 48h 内响应次数 / 报价请求总数 × 100",
                      erratum_note="勘误：框架协议约定 SLA 为 48h，原 24h 配置错误")
    v4 = svc.correct_erratum(v1.publication_id, "response", note="按框架协议勘误")
    j("更正版本 v4（规则勘误）", {"version": v4.version, "total_score": v4.explanation["total_score"],
                             "grade": v4.explanation["grade"], "correction": v4.correction})

    # 10) 版本链：历史等级全部原样保留
    j("版本链", svc.list_publication_versions(v1.publication_id)["versions"])

    # 11) 逐项解释（供应商视角：每条扣分来源）
    explain = svc.explain(v1.publication_id, version=1, metric_code="delivery")
    j("v1 交付指标逐项解释", explain["explanation"]["metrics"][0])

    # 12) 每一版按其历史口径复算，哈希必须全部一致
    checks = [svc.recompute(v1.publication_id, v)["hash_match"]
              for v in range(1, len(svc.publications[v1.publication_id].versions) + 1)]
    j("全部历史版本按原口径复算", {"hash_match_per_version": checks, "all_match": all(checks)})


if __name__ == "__main__":
    main()
