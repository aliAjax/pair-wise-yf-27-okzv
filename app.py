"""HTTP 入口层：只做协议解析、路由与静态页面服务。

业务规则在 rules.py，持久化与事务在 store.py，页面在 web/。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from store import BusinessError, DEFAULT_DB, ProvenanceStore

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.1"

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

    def _static(self, name):
        # 页面资源单独维护，入口只负责按白名单提供静态文件。
        allowed = {"index.html", "review.html", "app.js", "review.js"}
        if name not in allowed:
            raise BusinessError("页面不存在", 404, "not_found")
        path = (WEB_DIR / name).resolve()
        if WEB_DIR not in path.parents or not path.is_file():
            raise BusinessError("页面不存在", 404, "not_found")
        body = path.read_bytes()
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET":
            if path == "/":
                return self._static("index.html")
            if path == "/review":
                return self._static("review.html")
            if path in {"/static/app.js", "/static/review.js"}:
                return self._static("app.js" if path.endswith("app.js") else "review.js")
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()

        if parts == ["api", "objects"] and method == "GET":
            return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body()
            return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "GET":
            return self._send(200, {"items": store.list_sources(user)})
        if parts == ["api", "sources"] and method == "POST":
            d = self._body()
            return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))

        # /api/objects/{id}/...
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            try:
                object_id = int(parts[2])
            except ValueError:
                raise BusinessError("藏品 id 必须是整数", 400, "invalid_path")
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST":
                return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body()
                return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body()
                return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body()
                return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET":
                return self._send(200, store.history_detail(user, object_id, int(parts[4])))
            if len(parts) == 4 and parts[3] == "packages" and method == "GET":
                return self._send(200, {"items": store.list_packages(user, object_id)})
            if len(parts) == 4 and parts[3] == "packages" and method == "POST":
                d = self._body()
                return self._send(201, store.create_package(user, object_id, d.get("items", [])))
            if len(parts) == 4 and parts[3] == "jobs" and method == "GET":
                return self._send(200, {"items": store.list_jobs(user, object_id)})

        # /api/packages/{id}/...
        if len(parts) >= 3 and parts[:2] == ["api", "packages"]:
            package_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_package(user, package_id))
            if len(parts) == 4 and parts[3] == "items" and method == "POST":
                d = self._body()
                return self._send(200, store.add_package_items(user, package_id, d.get("items", []), d.get("expected_revision")))
            if len(parts) == 4 and parts[3] == "seal" and method == "POST":
                d = self._body()
                return self._send(200, store.seal_package(user, package_id, d.get("expected_revision")))
            if len(parts) == 4 and parts[3] == "review" and method == "POST":
                return self._send(201, store.review_package(user, package_id))
            if len(parts) == 4 and parts[3] == "jobs" and method == "POST":
                d = self._body()
                return self._send(201, store.create_transition_job(user, package_id, d.get("transitions", [])))

        # /api/jobs/{id} 与 /api/jobs/{id}/retry
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            job_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_job(user, job_id))
            if len(parts) == 4 and parts[3] == "retry" and method == "POST":
                return self._send(200, store.retry_transition_job(user, job_id))

        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body()
            return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:  # noqa: BLE001
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
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}（审查工作台 /review）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
