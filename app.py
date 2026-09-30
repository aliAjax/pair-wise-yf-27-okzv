"""HTTP 入口：博物馆藏品来源与返还审查系统。

规则见 rules.py，持久化见 store.py，页面见 web/index.html。
本文件只负责 HTTP 路由与参数解析。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from rules import BusinessError
from store import DEFAULT_DB, ProvenanceStore

# 向后兼容：旧测试从 app 导入。
__all__ = ["BusinessError", "ProvenanceStore"]


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/2.0"

    def _store(self):
        return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (DEFAULT_DB.parent / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET":
            return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body()
            return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""),
                                                        d.get("object_type", ""), d.get("current_holder", ""),
                                                        d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body()
            return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST":
                return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body()
                return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""),
                                                       d.get("date_end", ""), d.get("place", ""), d.get("description", ""),
                                                       d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body()
                return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""),
                                                             d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body()
                return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET":
                return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        # 依据包
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "packages" and method == "GET":
            return self._send(200, {"items": store.list_basis_packages(user, int(parts[2]))})
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "packages" and method == "POST":
            d = self._body()
            return self._send(201, store.create_basis_package(user, int(parts[2]), d.get("event_ids", []), d.get("evidence_ids", [])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "jobs" and method == "GET":
            return self._send(200, {"items": store.list_jobs(user, int(parts[2]))})
        if len(parts) == 3 and parts[:2] == ["api", "packages"] and method == "GET":
            return self._send(200, store.get_basis_package(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "packages"] and parts[3] == "seal" and method == "POST":
            return self._send(200, store.seal_basis_package(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "packages"] and parts[3] == "revalidate" and method == "POST":
            return self._send(201, store.revalidate_basis_package(user, int(parts[2])))
        # 任务
        if len(parts) == 3 and parts[:2] == ["api", "jobs"] and method == "GET":
            return self._send(200, store.get_job(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "retry" and method == "POST":
            return self._send(200, store.retry_job(user, int(parts[2])))
        # 主张流转
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body()
            return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""),
                                                          d.get("note", ""), d.get("basis_package_id")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try:
            self._dispatch(method)
        except BusinessError as exc:
            payload = {"error": {"code": exc.code, "message": exc.message}}
            if exc.affected:
                payload["error"]["affected"] = exc.affected
            self._send(exc.status, payload)
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    store = ProvenanceStore(args.db)
    store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
