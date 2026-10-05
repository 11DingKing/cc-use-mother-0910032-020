"""HTTP 接口：标准库 http.server 实现的 JSON API。

路由概览
--------
POST   /metrics                              登记指标
GET    /metrics /metrics/{code}              查询指标
POST   /rules                                创建规则版本（草稿）
POST   /rules/{code}/publish                 发布规则（冻结）
POST   /rules/{code}/corrections             规则勘误 -> 新版本
GET    /rules/{code}
POST   /weights                              创建权重版本
POST   /weights/{code}/publish               发布权重（冻结）
POST   /snapshots                            录入数据来源快照
POST   /snapshots/{id}/finalize              快照定稿
POST   /snapshots/{id}/corrections           迟到数据/异常剔除/申诉订正 -> 新快照
POST   /trials                               创建试算方案
GET    /trials/{id}
POST   /trials/compare                       比较多个试算方案
POST   /batches                              正式封账（固定输入与规则）
GET    /batches/{id}
POST   /batches/{id}/corrections             封账更正 -> 新批次
POST   /appeals                              针对已封账批次申诉
POST   /appeals/{id}/decision                申诉裁决（采纳须驱动更正批次）
GET    /batches/{id}/explain?supplier=CODE   逐项得分解释
GET    /batches/{id}/recompute?supplier=CODE 按历史口径复算校验
"""
from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import Conflict, NotFound, RatingError, ValidationFailed
from .models import CorrectionReason, Metric, MetricKind
from .store import RatingStore


def _cutoffs(payload: list) -> list[tuple[str, float]]:
    return [(str(row[0]), float(row[1])) for row in payload]


class RatingAPIHandler(BaseHTTPRequestHandler):
    store: RatingStore = None  # 由 create_server 注入（类属性）
    lock = threading.Lock()

    server_version = "SupplierRating/0.1"

    # -------------------------------------------------------- 基础工具

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationFailed(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self, name: str, default: str | None = None) -> str | None:
        query = parse_qs(urlparse(self.path).query)
        values = query.get(name)
        return values[0] if values else default

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认日志
        return

    # -------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            with self.lock:
                for pattern, verbs, handler in ROUTES:
                    match = pattern.fullmatch(path)
                    if match and method in verbs:
                        payload = self._read_json() if method == "POST" else {}
                        handler(self, payload, **match.groupdict())
                        return
            self._send(HTTPStatus.NOT_FOUND, {"error": f"无此接口：{method} {path}"})
        except ValidationFailed as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Conflict as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except NotFound as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except RatingError as exc:
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})

    # -------------------------------------------------------- 处理函数

    # -- 指标 --

    def h_metric_create(self, payload: dict) -> None:
        kind = MetricKind(payload.get("kind", "ratio"))
        metric = Metric(
            code=payload["code"],
            name=payload["name"],
            kind=kind,
            unit=payload.get("unit", ""),
            description=payload.get("description", ""),
            good_threshold=payload.get("good_threshold"),
            bad_threshold=payload.get("bad_threshold"),
        )
        self.store.register_metric(metric)
        self._send(HTTPStatus.CREATED, metric.to_dict())

    def h_metric_list(self, payload: dict) -> None:
        self._send(
            HTTPStatus.OK,
            {"metrics": [m.to_dict() for m in self.store.metrics.values()]},
        )

    def h_metric_get(self, payload: dict, code: str) -> None:
        self._send(HTTPStatus.OK, self.store.get_metric(code).to_dict())

    # -- 规则 --

    def h_rule_create(self, payload: dict) -> None:
        rule = self.store.create_rule(
            code=payload["code"],
            metric_codes=payload["metrics"],
            grade_cutoffs=_cutoffs(payload["grade_cutoffs"]),
        )
        self._send(HTTPStatus.CREATED, rule.to_dict())

    def h_rule_get(self, payload: dict, code: str) -> None:
        self._send(HTTPStatus.OK, self.store._get_rule(code).to_dict())

    def h_rule_publish(self, payload: dict, code: str) -> None:
        self._send(HTTPStatus.OK, self.store.publish_rule(code).to_dict())

    def h_rule_correct(self, payload: dict, code: str) -> None:
        metric_defs = None
        if payload.get("metric_defs") is not None:
            metric_defs = [
                Metric(
                    code=m["code"],
                    name=m["name"],
                    kind=MetricKind(m.get("kind", "ratio")),
                    unit=m.get("unit", ""),
                    description=m.get("description", ""),
                    good_threshold=m.get("good_threshold"),
                    bad_threshold=m.get("bad_threshold"),
                )
                for m in payload["metric_defs"]
            ]
        rule = self.store.correct_rule(
            new_code=payload["new_code"],
            base_code=code,
            reason=CorrectionReason.ERRATUM,
            note=payload.get("note", ""),
            metric_codes=payload.get("metrics"),
            metric_defs=metric_defs,
            grade_cutoffs=_cutoffs(payload["grade_cutoffs"])
            if payload.get("grade_cutoffs") is not None
            else None,
        )
        self._send(HTTPStatus.CREATED, rule.to_dict())

    # -- 权重 --

    def h_weight_create(self, payload: dict) -> None:
        weights = self.store.create_weights(
            code=payload["code"],
            weights={k: float(v) for k, v in payload["weights"].items()},
            note=payload.get("note", ""),
        )
        self._send(HTTPStatus.CREATED, weights.to_dict())

    def h_weight_publish(self, payload: dict, code: str) -> None:
        self._send(HTTPStatus.OK, self.store.publish_weights(code).to_dict())

    # -- 数据快照 --

    def h_snapshot_ingest(self, payload: dict) -> None:
        snapshot = self.store.ingest_snapshot(
            snapshot_id=payload["id"],
            supplier_code=payload["supplier_code"],
            period=payload["period"],
            source=payload["source"],
            values={k: float(v) for k, v in payload["values"].items()},
            finalized=bool(payload.get("finalized", True)),
            note=payload.get("note", ""),
        )
        self._send(HTTPStatus.CREATED, snapshot.to_dict())

    def h_snapshot_finalize(self, payload: dict, sid: str) -> None:
        self._send(HTTPStatus.OK, self.store.finalize_snapshot(sid).to_dict())

    def h_snapshot_correct(self, payload: dict, sid: str) -> None:
        snapshot = self.store.correct_snapshot(
            new_id=payload["new_id"],
            base_id=sid,
            reason=CorrectionReason(payload["reason"]),
            values={k: float(v) for k, v in payload.get("values", {}).items()} or None,
            exclude_orders=payload.get("exclude_orders"),
            note=payload.get("note", ""),
        )
        self._send(HTTPStatus.CREATED, snapshot.to_dict())

    # -- 试算 --

    def h_trial_create(self, payload: dict) -> None:
        trial = self.store.create_trial(
            trial_id=payload["id"],
            label=payload.get("label", payload["id"]),
            period=payload["period"],
            rule_code=payload["rule_version"],
            weight_code=payload["weight_version"],
            snapshot_ids=payload["snapshot_ids"],
        )
        self._send(HTTPStatus.CREATED, trial.to_dict())

    def h_trial_get(self, payload: dict, tid: str) -> None:
        self._send(HTTPStatus.OK, self.store.trials[tid].to_dict())

    def h_trial_compare(self, payload: dict) -> None:
        self._send(HTTPStatus.OK, self.store.compare_trials(payload["trial_ids"]))

    # -- 封账批次 --

    def h_batch_close(self, payload: dict) -> None:
        batch = self.store.close_batch(
            batch_id=payload["id"],
            period=payload["period"],
            rule_code=payload["rule_version"],
            weight_code=payload["weight_version"],
            snapshot_ids=payload["snapshot_ids"],
        )
        self._send(HTTPStatus.CREATED, batch.to_dict())

    def h_batch_get(self, payload: dict, bid: str) -> None:
        self._send(HTTPStatus.OK, self.store._get_batch(bid).to_dict())

    def h_batch_correct(self, payload: dict, bid: str) -> None:
        reason = CorrectionReason(payload["reason"])
        batch = self.store.correct_batch(
            new_batch_id=payload["new_batch_id"],
            base_batch_id=bid,
            reason=reason,
            corrected_snapshot_ids=payload.get("corrected_snapshot_ids"),
            rule_code=payload.get("rule_code"),
            note=payload.get("note", ""),
        )
        self._send(HTTPStatus.CREATED, batch.to_dict())

    def h_batch_explain(self, payload: dict, bid: str) -> None:
        supplier = self._query("supplier")
        if not supplier:
            raise ValidationFailed("缺少 supplier 查询参数")
        self._send(HTTPStatus.OK, self.store.explain_score(bid, supplier))

    def h_batch_recompute(self, payload: dict, bid: str) -> None:
        supplier = self._query("supplier")
        self._send(HTTPStatus.OK, self.store.recompute(bid, supplier))

    # -- 申诉 --

    def h_appeal_open(self, payload: dict) -> None:
        appeal = self.store.open_appeal(
            appeal_id=payload["id"],
            batch_id=payload["batch_id"],
            supplier_code=payload["supplier_code"],
            metric_code=payload["metric_code"],
            reason=payload["reason"],
            evidence=payload.get("evidence", ""),
        )
        self._send(HTTPStatus.CREATED, appeal.to_dict())

    def h_appeal_decide(self, payload: dict, aid: str) -> None:
        appeal = self.store.decide_appeal(
            appeal_id=aid,
            accepted=bool(payload["accepted"]),
            decision_note=payload.get("decision_note", ""),
            corrected_snapshot_id=payload.get("corrected_snapshot_id"),
            new_batch_id=payload.get("new_batch_id"),
        )
        self._send(HTTPStatus.OK, appeal.to_dict())


Handler = Callable[..., None]

ROUTES: list[tuple[re.Pattern[str], set[str], Handler]] = [
    (re.compile(r"/metrics"), {"POST"}, RatingAPIHandler.h_metric_create),
    (re.compile(r"/metrics"), {"GET"}, RatingAPIHandler.h_metric_list),
    (re.compile(r"/metrics/(?P<code>[^/]+)"), {"GET"}, RatingAPIHandler.h_metric_get),
    (re.compile(r"/rules"), {"POST"}, RatingAPIHandler.h_rule_create),
    (re.compile(r"/rules/(?P<code>[^/]+)/publish"), {"POST"}, RatingAPIHandler.h_rule_publish),
    (re.compile(r"/rules/(?P<code>[^/]+)/corrections"), {"POST"}, RatingAPIHandler.h_rule_correct),
    (re.compile(r"/rules/(?P<code>[^/]+)"), {"GET"}, RatingAPIHandler.h_rule_get),
    (re.compile(r"/weights"), {"POST"}, RatingAPIHandler.h_weight_create),
    (re.compile(r"/weights/(?P<code>[^/]+)/publish"), {"POST"}, RatingAPIHandler.h_weight_publish),
    (re.compile(r"/snapshots"), {"POST"}, RatingAPIHandler.h_snapshot_ingest),
    (re.compile(r"/snapshots/(?P<sid>[^/]+)/finalize"), {"POST"}, RatingAPIHandler.h_snapshot_finalize),
    (re.compile(r"/snapshots/(?P<sid>[^/]+)/corrections"), {"POST"}, RatingAPIHandler.h_snapshot_correct),
    (re.compile(r"/trials/compare"), {"POST"}, RatingAPIHandler.h_trial_compare),
    (re.compile(r"/trials"), {"POST"}, RatingAPIHandler.h_trial_create),
    (re.compile(r"/trials/(?P<tid>[^/]+)"), {"GET"}, RatingAPIHandler.h_trial_get),
    (re.compile(r"/batches"), {"POST"}, RatingAPIHandler.h_batch_close),
    (re.compile(r"/batches/(?P<bid>[^/]+)/corrections"), {"POST"}, RatingAPIHandler.h_batch_correct),
    (re.compile(r"/batches/(?P<bid>[^/]+)/explain"), {"GET"}, RatingAPIHandler.h_batch_explain),
    (re.compile(r"/batches/(?P<bid>[^/]+)/recompute"), {"GET"}, RatingAPIHandler.h_batch_recompute),
    (re.compile(r"/batches/(?P<bid>[^/]+)"), {"GET"}, RatingAPIHandler.h_batch_get),
    (re.compile(r"/appeals"), {"POST"}, RatingAPIHandler.h_appeal_open),
    (re.compile(r"/appeals/(?P<aid>[^/]+)/decision"), {"POST"}, RatingAPIHandler.h_appeal_decide),
]


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    store = RatingStore()

    handler = type("BoundRatingAPIHandler", (RatingAPIHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    server.store = store  # 方便测试/外层取用
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="供应商季度评级后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = create_server(args.host, args.port)
    print(f"供应商评级后端已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
