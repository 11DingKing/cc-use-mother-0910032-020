"""零三方依赖的 HTTP 接口（标准库 http.server）。

服务状态保存在单进程内存中，适用于演示与契约验证；生产可替换 RatingService
的存储实现而不改变领域规则。

路由：
  POST /sources                              注册数据来源
  POST /metrics                              新建指标 / 追加勘误版本
  POST /weights                              创建权重方案草稿版本
  POST /weights/{wid}/publish                发布并冻结权重版本
  POST /batches                              创建季度批次
  POST /batches/{batch_id}/close             封账
  POST /records                              常规数据通道录入（封账前）
  POST /trials                               试算
  POST /compare                              多权重方案比较
  POST /publish                              封账后正式发布
  POST /publications/{id}/corrections/late-data     迟到数据更正
  POST /publications/{id}/corrections/exclusions    异常订单剔除更正
  POST /publications/{id}/corrections/errata        规则勘误更正
  GET  /publications/{id}/versions                 版本链
  GET  /publications/{id}/explain?version=&metric= 逐项解释
  GET  /publications/{id}/recompute?version=       历史口径复算校验
  POST /appeals                              供应商提出申诉
  POST /appeals/{id}/decision                申诉裁定（采纳自动生成更正版本）
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .models import OrderRecord
from .service import RatingError, RatingService

SERVICE = RatingService()


class _Handler(BaseHTTPRequestHandler):
    server_version = "SupplierRating/1.0"

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RatingError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise RatingError("请求体必须是 JSON 对象")
        return value

    def _records_from_body(self, body: dict, key: str = "records") -> list[OrderRecord]:
        return [
            OrderRecord(
                record_id=item["record_id"],
                batch_id=item["batch_id"],
                supplier_id=item["supplier_id"],
                metric_code=item["metric_code"],
                source_code=item["source_code"],
                occurred_at=item["occurred_at"],
                payload=item.get("payload", {}),
            )
            for item in body[key]
        ]

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802 - 标准库命名
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            m = re.fullmatch(r"/publications/([^/]+)/versions", parsed.path)
            if m:
                self._send(200, SERVICE.list_publication_versions(m.group(1)))
                return
            m = re.fullmatch(r"/publications/([^/]+)/explain", parsed.path)
            if m:
                version = int(query["version"]) if "version" in query else None
                self._send(200, SERVICE.explain(m.group(1), version, query.get("metric")))
                return
            m = re.fullmatch(r"/publications/([^/]+)/recompute", parsed.path)
            if m:
                version = int(query["version"]) if "version" in query else None
                self._send(200, SERVICE.recompute(m.group(1), version))
                return
            self._send(404, {"error": f"未知路径：{parsed.path}"})
        except RatingError as exc:
            self._send(422, {"error": str(exc)})

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._body()
            if parsed.path == "/sources":
                self._send(201, SERVICE.register_source(
                    body["code"], body["name"], body.get("description", "")))
            elif parsed.path == "/metrics":
                self._send(201, SERVICE.create_metric(
                    body["code"], body["name"], body["source_code"], body["kind"],
                    body.get("params", {}), body["formula_text"], body.get("erratum_note")))
            elif parsed.path == "/weights":
                self._send(201, SERVICE.create_weight_version(
                    body["scheme_code"], body["weights"], body["grade_bands"], body.get("note", "")))
            elif re.fullmatch(r"/weights/[^/]+/publish", parsed.path):
                wid = parsed.path.split("/")[2]
                self._send(200, SERVICE.publish_weight(wid))
            elif parsed.path == "/batches":
                self._send(201, SERVICE.create_batch(
                    body["batch_id"], body["period_start"], body["period_end"]))
            elif re.fullmatch(r"/batches/[^/]+/close", parsed.path):
                batch_id = parsed.path.split("/")[2]
                self._send(200, SERVICE.close_batch(batch_id))
            elif parsed.path == "/records":
                count = SERVICE.ingest_records(self._records_from_body(body))
                self._send(201, {"ingested": count})
            elif parsed.path == "/trials":
                self._send(200, SERVICE.trial(
                    body["batch_id"], body["supplier_id"], body["weight_version_id"]))
            elif parsed.path == "/compare":
                self._send(200, SERVICE.compare_trials(
                    body["batch_id"], body["supplier_id"], body["weight_version_ids"]))
            elif parsed.path == "/publish":
                self._send(201, SERVICE.publish(
                    body["batch_id"], body["supplier_id"], body["weight_version_id"]))
            elif re.fullmatch(r"/publications/[^/]+/corrections/late-data", parsed.path):
                pid = parsed.path.split("/")[2]
                self._send(201, SERVICE.correct_late_data(
                    pid, self._records_from_body(body), body["reason"]))
            elif re.fullmatch(r"/publications/[^/]+/corrections/exclusions", parsed.path):
                pid = parsed.path.split("/")[2]
                self._send(201, SERVICE.correct_order_exclusion(
                    pid, body["record_ids"], body["reason"], body["requested_by"]))
            elif re.fullmatch(r"/publications/[^/]+/corrections/errata", parsed.path):
                pid = parsed.path.split("/")[2]
                self._send(201, SERVICE.correct_erratum(pid, body["metric_code"], body.get("note", "")))
            elif parsed.path == "/appeals":
                self._send(201, SERVICE.file_appeal(
                    body["supplier_id"], body["publication_id"], body["reason"],
                    body.get("metric_code"), body.get("record_id")))
            elif re.fullmatch(r"/appeals/[^/]+/decision", parsed.path):
                aid = parsed.path.split("/")[2]
                self._send(200, SERVICE.decide_appeal(
                    aid, bool(body["accepted"]), body.get("decision_note", "")))
            else:
                self._send(404, {"error": f"未知路径：{parsed.path}"})
        except RatingError as exc:
            self._send(422, {"error": str(exc)})
        except KeyError as exc:
            self._send(422, {"error": f"缺少必填字段：{exc.args[0]}"})

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return


def _json_default(obj):
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"供应商评级服务监听 http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
