"""持久化层：SQLite 实现的藏品来源与返还审查存储。

规则判定见 rules.py，HTTP 入口见 app.py。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from rules import (
    BusinessError,
    check_event_input,
    check_evidence_input,
    check_package_validity,
    evidence_digest,
    first_writer_wins_update,
    validate_transition,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_basis_packages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','sealed','invalid')),
                    selected_event_ids TEXT NOT NULL DEFAULT '[]',
                    selected_evidence_ids TEXT NOT NULL DEFAULT '[]',
                    sealed_object_version INTEGER,
                    sealed_event_ids TEXT,
                    sealed_evidence_digest TEXT,
                    sealed_by TEXT REFERENCES users(id),
                    sealed_at TEXT,
                    invalid_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL CHECK(job_type IN ('seal','transition')),
                    claim_id INTEGER REFERENCES claims(id),
                    package_id INTEGER REFERENCES review_basis_packages(id),
                    object_id INTEGER REFERENCES objects(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','in_progress','completed','failed','blocked')),
                    steps TEXT NOT NULL DEFAULT '[]',
                    request TEXT NOT NULL DEFAULT '{}',
                    result TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    # ------------------------------------------------------------------ 基础辅助

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _claim(self, conn, claim_id):
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise BusinessError("权利主张不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    # ------------------------------------------------------------------ 藏品 / 来源 / 证据 / 主张

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            # 藏品变化 → 已封存依据包立即失效。
            self._invalidate_packages(conn, object_id, user_id)
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        ok, err = check_event_input(event_type, date_start, date_end, place, description)
        if not ok:
            raise BusinessError(err, 422, "invalid_event")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            # 来源事件变化 → 已封存依据包立即失效。
            self._invalidate_packages(conn, object_id, user_id)
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        ok, err, content = check_evidence_input(filename, content_b64, visibility)
        if not ok:
            raise BusinessError(err, 422, "invalid_evidence")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            # 证据变化 → 已封存依据包立即失效。
            self._invalidate_packages(conn, object_id, user_id)
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    # ------------------------------------------------------------------ 依据包

    def _package_or_404(self, conn, package_id):
        row = conn.execute("SELECT * FROM review_basis_packages WHERE id=?", (package_id,)).fetchone()
        if not row:
            raise BusinessError("依据包不存在", 404, "not_found")
        return row

    def _current_event_ids(self, conn, object_id):
        return [r["id"] for r in conn.execute("SELECT id FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()]

    def _current_evidence(self, conn, object_id):
        return [dict(r) for r in conn.execute(
            "SELECT id,sha256 FROM evidence WHERE object_id=? ORDER BY id", (object_id,)
        ).fetchall()]

    def _package_validity(self, conn, package):
        """计算依据包当前是否有效及受影响项。"""
        obj = self._object(conn, package["object_id"])
        sealed_evidence = json.loads(package["sealed_evidence_digest"] or "[]")
        valid, affected = check_package_validity(
            sealed_object_version=package["sealed_object_version"],
            sealed_event_ids=json.loads(package["sealed_event_ids"] or "[]"),
            sealed_evidence_digest=sealed_evidence,
            current_object_version=obj["version"],
            current_event_ids=self._current_event_ids(conn, package["object_id"]),
            current_evidence=self._current_evidence(conn, package["object_id"]),
        )
        return valid, affected, obj

    def _invalidate_packages(self, conn, object_id, actor):
        """藏品、来源事件或证据变化后，使该藏品下所有已封存依据包立即失效。"""
        rows = conn.execute(
            "SELECT * FROM review_basis_packages WHERE object_id=? AND status='sealed' ORDER BY id",
            (object_id,),
        ).fetchall()
        for pkg in rows:
            valid, affected, _ = self._package_validity(conn, pkg)
            if valid:
                continue
            conn.execute(
                "UPDATE review_basis_packages SET status='invalid', invalid_reason=?, updated_at=? WHERE id=?",
                (json.dumps(affected, ensure_ascii=False), now(), pkg["id"]),
            )
            self._audit(conn, object_id, actor, "basis_package.invalidate",
                        {"package_id": pkg["id"], "affected": affected})

    def create_basis_package(self, user_id, claim_id, event_ids, evidence_ids):
        """审查员选取公开来源事件和内部证据组成依据包（草稿）。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            claim = self._claim(conn, claim_id)
            object_id = claim["object_id"]
            self._object(conn, object_id)
            event_ids = sorted({int(x) for x in (event_ids or [])})
            evidence_ids = sorted({int(x) for x in (evidence_ids or [])})
            # 公开来源事件：必须存在、属于该藏品且为 public。
            for eid in event_ids:
                ev = conn.execute("SELECT * FROM events WHERE id=? AND object_id=?", (eid, object_id)).fetchone()
                if not ev:
                    raise BusinessError(f"来源事件 {eid} 不存在或不属于该藏品", 404, "event_not_found")
                if ev["visibility"] != "public":
                    raise BusinessError(f"来源事件 {eid} 不是公开事件，不能作为公开来源依据", 422, "event_not_public")
            for eid in evidence_ids:
                if not conn.execute("SELECT 1 FROM evidence WHERE id=? AND object_id=?", (eid, object_id)).fetchone():
                    raise BusinessError(f"证据 {eid} 不存在或不属于该藏品", 404, "evidence_not_found")
            cur = conn.execute(
                """INSERT INTO review_basis_packages
                   (claim_id,object_id,status,selected_event_ids,selected_evidence_ids,created_by,created_at,updated_at)
                   VALUES(?,?, 'draft', ?,?,?,?,?)""",
                (claim_id, object_id, json.dumps(event_ids), json.dumps(evidence_ids), user_id, now(), now()),
            )
            self._audit(conn, object_id, user_id, "basis_package.create",
                        {"package_id": cur.lastrowid, "claim_id": claim_id,
                         "event_ids": event_ids, "evidence_ids": evidence_ids})
            return self.get_basis_package(user_id, cur.lastrowid, _conn=conn)

    def seal_basis_package(self, user_id, package_id):
        """封存依据包：记下藏品版本与证据摘要。

        两个人同时封存时先写入者生效，后到者返回冲突（乐观锁，WHERE 带 version）。
        封存失败保留未完成项并落库为 job，可重试。
        """
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            package = self._package_or_404(conn, package_id)
            if package["status"] == "invalid":
                raise BusinessError("依据包已失效，请复审后重新建包封存", 409, "package_invalid")
            # 建一个封存任务跟踪进度，失败后保留未完成项。
            job = self._create_job("seal", package=package, user_id=user_id,
                                   request={"package_id": package_id},
                                   steps=[{"name": "validate", "status": "done"},
                                          {"name": "lock", "status": "pending"},
                                          {"name": "finalize", "status": "pending"}])
            return self._run_seal_job(conn, job, user_id)

    def _run_seal_job(self, conn, job, user_id):
        package = self._package_or_404(conn, job["package_id"])
        steps = json.loads(job["steps"])
        try:
            # lock：乐观并发更新，先写入者生效。
            if steps[1]["status"] != "done":
                obj = self._object(conn, package["object_id"])
                sealed_evidence = evidence_digest(
                    conn.execute("SELECT id,sha256 FROM evidence WHERE object_id=? ORDER BY id", (package["object_id"],)).fetchall()
                )
                ok = first_writer_wins_update(
                    conn, "review_basis_packages",
                    "status='sealed', sealed_object_version=?, sealed_event_ids=?, sealed_evidence_digest=?, sealed_by=?, sealed_at=?, version=version+1, updated_at=?",
                    "id=? AND version=? AND status='draft'",
                    (obj["version"], json.dumps(self._current_event_ids(conn, package["object_id"])),
                     json.dumps(sealed_evidence), user_id, now(), now(), package["id"], package["version"]),
                )
                if not ok:
                    raise BusinessError("封存冲突：依据包已被他人先封存或修改", 409, "conflict")
                steps[1]["status"] = "done"
                self._update_job_steps(conn, job["id"], steps)
            # finalize：审计。
            if steps[2]["status"] != "done":
                self._audit(conn, package["object_id"], user_id, "basis_package.seal",
                            {"package_id": package["id"], "object_version": package["version"]})
                steps[2]["status"] = "done"
                self._update_job_steps(conn, job["id"], steps)
            self._complete_job(conn, job["id"], {"package_id": package["id"], "status": "sealed"})
        except BusinessError as exc:
            unfinished = [s["name"] for s in steps if s["status"] != "done"]
            conn.rollback()  # 释放主事务写锁，避免与独立连接的失败记录死锁
            self._fail_job(job["id"], exc, unfinished)
            raise
        return self.get_job(user_id, job["id"], _conn=conn)

    def revalidate_basis_package(self, user_id, package_id):
        """复审：依据当前藏品/事件/证据重新建一个草稿包，可调整后再封存。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            old = self._package_or_404(conn, package_id)
            claim = self._claim(conn, old["claim_id"])
            # 复审草稿默认带上当前全部公开来源事件与证据。
            event_ids = self._current_event_ids(conn, old["object_id"])
            evidence_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM evidence WHERE object_id=? ORDER BY id", (old["object_id"],)).fetchall()]
            new_pkg = self.create_basis_package(user_id, claim["id"], event_ids, evidence_ids)
            self._audit(conn, old["object_id"], user_id, "basis_package.revalidate",
                        {"old_package_id": old["id"], "new_package_id": new_pkg["id"]})
            return new_pkg

    def get_basis_package(self, user_id, package_id, _conn=None):
        own = _conn is None
        with (self.connect() if own else _conn) as conn:
            self._user(conn, user_id)
            package = self._package_or_404(conn, package_id)
            result = dict(package)
            result["selected_event_ids"] = json.loads(package["selected_event_ids"])
            result["selected_evidence_ids"] = json.loads(package["selected_evidence_ids"])
            result["sealed_event_ids"] = json.loads(package["sealed_event_ids"] or "[]")
            result["sealed_evidence_digest"] = json.loads(package["sealed_evidence_digest"] or "[]")
            if package["status"] == "sealed":
                valid, affected, obj = self._package_validity(conn, package)
                result["is_valid"] = valid
                result["affected_items"] = affected
            elif package["status"] == "invalid":
                obj = self._object(conn, package["object_id"])
                result["is_valid"] = False
                result["affected_items"] = json.loads(package["invalid_reason"] or "[]")
            else:
                obj = self._object(conn, package["object_id"])
                result["is_valid"] = None
                result["affected_items"] = []
            result["current_object_version"] = obj["version"]
            result["current_event_ids"] = self._current_event_ids(conn, package["object_id"])
            result["current_evidence"] = self._current_evidence(conn, package["object_id"])
            return result

    def list_basis_packages(self, user_id, claim_id):
        with self.connect() as conn:
            self._user(conn, user_id)
            self._claim(conn, claim_id)
            rows = conn.execute(
                "SELECT * FROM review_basis_packages WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["selected_event_ids"] = json.loads(r["selected_event_ids"])
                item["selected_evidence_ids"] = json.loads(r["selected_evidence_ids"])
                if r["status"] == "sealed":
                    valid, affected, _ = self._package_validity(conn, r)
                    item["is_valid"] = valid
                    item["affected_items"] = affected
                else:
                    item["is_valid"] = None
                    item["affected_items"] = json.loads(r["invalid_reason"] or "[]")
                out.append(item)
            return out

    # ------------------------------------------------------------------ 任务（失败保留 / 重试）

    def _create_job(self, job_type, package=None, claim=None, user_id=None, request=None, steps=None):
        """建任务并独立提交，确保后续即使主事务回滚，任务记录仍保留。"""
        now_ts = now()
        package = dict(package) if package is not None else None
        claim = dict(claim) if claim is not None else None
        conn = self.connect()
        try:
            cur = conn.execute(
                """INSERT INTO review_jobs(job_type,claim_id,package_id,object_id,status,steps,request,attempts,created_at,updated_at)
                   VALUES(?,?,?,?, 'in_progress', ?, ?, 1, ?, ?)""",
                (job_type,
                 (package or {}).get("claim_id") if package else (claim or {}).get("id"),
                 (package or {}).get("id"),
                 (package or {}).get("object_id") if package else (claim or {}).get("object_id"),
                 json.dumps(steps or [], ensure_ascii=False),
                 json.dumps(request or {}, ensure_ascii=False),
                 now_ts, now_ts),
            )
            conn.commit()
            job_id = cur.lastrowid
        finally:
            conn.close()
        return {
            "id": job_id, "job_type": job_type,
            "claim_id": (package or {}).get("claim_id") if package else (claim or {}).get("id"),
            "package_id": (package or {}).get("id"),
            "object_id": (package or {}).get("object_id") if package else (claim or {}).get("object_id"),
            "status": "in_progress", "steps": json.dumps(steps or [], ensure_ascii=False),
            "request": json.dumps(request or {}, ensure_ascii=False), "attempts": 1,
        }

    def _update_job_steps(self, conn, job_id, steps):
        conn.execute("UPDATE review_jobs SET steps=?, updated_at=? WHERE id=?",
                     (json.dumps(steps, ensure_ascii=False), now(), job_id))

    def _complete_job(self, conn, job_id, result):
        conn.execute(
            "UPDATE review_jobs SET status='completed', result=?, updated_at=?, completed_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), now(), now(), job_id),
        )

    def _fail_job(self, job_id, exc, unfinished):
        """失败记录独立提交，重启后仍可查、可重试。"""
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE review_jobs SET status=?, error=?, updated_at=? WHERE id=?",
                ("blocked" if exc.code == "basis_package_invalid" else "failed",
                 json.dumps({"code": exc.code, "message": exc.message, "affected": exc.affected,
                             "unfinished": unfinished}, ensure_ascii=False),
                 now(), job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_job(self, user_id, job_id, _conn=None):
        own = _conn is None
        with (self.connect() if own else _conn) as conn:
            self._user(conn, user_id)
            row = conn.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise BusinessError("任务不存在", 404, "not_found")
            return self._job_to_dict(row)

    def _job_to_dict(self, row):
        d = dict(row)
        d["steps"] = json.loads(row["steps"])
        d["request"] = json.loads(row["request"])
        d["result"] = json.loads(row["result"]) if row["result"] else None
        d["error"] = json.loads(row["error"]) if row["error"] else None
        return d

    def list_jobs(self, user_id, claim_id):
        with self.connect() as conn:
            self._user(conn, user_id)
            self._claim(conn, claim_id)
            rows = conn.execute(
                "SELECT * FROM review_jobs WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
            return [self._job_to_dict(r) for r in rows]

    def retry_job(self, user_id, job_id):
        """重试：只续做未完成部分，已完成步骤跳过。进度落库，重启后仍可查。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            row = conn.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise BusinessError("任务不存在", 404, "not_found")
            if row["status"] == "completed":
                return self._job_to_dict(row)
            conn.execute("UPDATE review_jobs SET attempts=attempts+1, status='in_progress', updated_at=? WHERE id=?",
                         (now(), job_id))
            job = conn.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone()
            if job["job_type"] == "seal":
                return self._run_seal_job(conn, job, user_id)
            return self._run_transition_job(conn, job, user_id)

    # ------------------------------------------------------------------ 主张流转

    def transition_claim(self, user_id, claim_id, new_status, note, basis_package_id=None):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            claim = self._claim(conn, claim_id)
            ok, err = validate_transition(claim["status"], new_status)
            if not ok:
                raise BusinessError(err, 409, "invalid_transition")
            # 关联依据包：若指定或该主张已有封存/失效包，则流转前必须校验包仍有效。
            package = None
            if basis_package_id:
                package = self._package_or_404(conn, basis_package_id)
                if package["claim_id"] != claim_id:
                    raise BusinessError("依据包不属于该主张", 422, "package_claim_mismatch")
            else:
                package = conn.execute(
                    "SELECT * FROM review_basis_packages WHERE claim_id=? AND status IN ('sealed','invalid') ORDER BY id DESC LIMIT 1",
                    (claim_id,)).fetchone()
            # 建流转任务跟踪进度，失败保留未完成项。
            job = self._create_job(
                "transition", claim=claim, user_id=user_id,
                request={"claim_id": claim_id, "to": new_status, "note": note.strip(),
                         "basis_package_id": basis_package_id},
                steps=[{"name": "validate", "status": "done"},
                       {"name": "basis_check", "status": "pending" if package else "done"},
                       {"name": "apply", "status": "pending"}])
            return self._run_transition_job(conn, job, user_id, package_override=package)

    def _run_transition_job(self, conn, job, user_id, package_override=None):
        req = json.loads(job["request"])
        claim_id = req["claim_id"]
        new_status = req["to"]
        note = req["note"]
        steps = json.loads(job["steps"])
        package = package_override
        try:
            claim = self._claim(conn, claim_id)
            # 幂等：若已到目标阶段，直接完成。
            if claim["status"] == new_status:
                for i, s in enumerate(steps):
                    steps[i]["status"] = "done"
                self._update_job_steps(conn, job["id"], steps)
                self._complete_job(conn, job["id"], {"claim_id": claim_id, "status": new_status, "idempotent": True})
                return self.get_job(user_id, job["id"], _conn=conn)

            # basis_check：校验依据包有效性；失效则挡住未执行流转并列出受影响项。
            if steps[1]["status"] != "done":
                if package is None and req.get("basis_package_id"):
                    package = self._package_or_404(conn, req["basis_package_id"])
                if package is None:
                    package = conn.execute(
                        "SELECT * FROM review_basis_packages WHERE claim_id=? AND status IN ('sealed','invalid') ORDER BY id DESC LIMIT 1",
                        (claim_id,)).fetchone()
                if package is not None:
                    if package["status"] == "invalid":
                        raise BusinessError("依据包已失效，未执行的主张流转被挡住", 409, "basis_package_invalid",
                                            affected=json.loads(package["invalid_reason"] or "[]"))
                    if package["status"] == "sealed":
                        valid, affected, _ = self._package_validity(conn, package)
                        if not valid:
                            raise BusinessError("依据包已失效，未执行的主张流转被挡住", 409, "basis_package_invalid",
                                                affected=affected)
                    # draft：尚未封存，不挡住流转
                steps[1]["status"] = "done"
                self._update_job_steps(conn, job["id"], steps)

            # apply：写入新阶段。
            if steps[2]["status"] != "done":
                claim = self._claim(conn, claim_id)
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note, now()),
                )
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition",
                            {"claim_id": claim_id, "from": claim["status"], "to": new_status,
                             "basis_package_id": package["id"] if package else None})
                # 流转改变藏品 → 已封存依据包失效。
                self._invalidate_packages(conn, claim["object_id"], user_id)
                steps[2]["status"] = "done"
                self._update_job_steps(conn, job["id"], steps)
                self._complete_job(conn, job["id"], {"claim_id": claim_id, "old_status": claim["status"],
                                                     "status": new_status, "object_version": next_version})
        except BusinessError as exc:
            unfinished = [s["name"] for s in steps if s["status"] != "done"]
            conn.rollback()  # 释放主事务写锁，避免与独立连接的失败记录死锁
            self._fail_job(job["id"], exc, unfinished)
            raise
        return self.get_job(user_id, job["id"], _conn=conn)

    # ------------------------------------------------------------------ 查询视图

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}
