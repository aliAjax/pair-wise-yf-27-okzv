"""持久化层：SQLite 存储与事务。

所有跨表一致性都在单事务内完成；规则判断委托给 rules.py。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import rules

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = rules.CLAIM_TRANSITIONS  # 向后兼容导出


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


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
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public','system'))
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
                    note TEXT NOT NULL, package_id INTEGER, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS basis_packages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','sealed','invalid')),
                    revision INTEGER NOT NULL DEFAULT 1,
                    sealed_by TEXT REFERENCES users(id), sealed_at TEXT,
                    sealed_object_version INTEGER,
                    evidence_summary TEXT, item_snapshot TEXT,
                    invalidation_reason TEXT, invalidated_at TEXT,
                    reopens_package_id INTEGER REFERENCES basis_packages(id),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS package_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER NOT NULL REFERENCES basis_packages(id),
                    item_type TEXT NOT NULL CHECK(item_type IN ('event','evidence')),
                    ref_id INTEGER NOT NULL,
                    added_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(package_id,item_type,ref_id)
                );
                CREATE TABLE IF NOT EXISTS batch_jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER NOT NULL REFERENCES basis_packages(id),
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES batch_jobs(id),
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    to_status TEXT NOT NULL, note TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','done','failed','blocked')),
                    last_error TEXT,
                    executed_by TEXT REFERENCES users(id), executed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_package ON package_items(package_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_package ON batch_jobs(package_id);
                CREATE INDEX IF NOT EXISTS idx_batch_items_job ON batch_items(job_id);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES('system','系统','system')"
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

    # ---------- 基础辅助 ----------

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

    # ---------- 藏品 / 来源 / 事件 / 证据 ----------

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
            self._invalidate_packages(conn, object_id, "object_version_changed",
                                      {"from": row["version"], "to": new_version, "trigger": "object.update"})
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

    def list_sources(self, user_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            return [dict(r) for r in conn.execute(
                "SELECT id,name,source_type,reference,created_at FROM sources ORDER BY id").fetchall()]

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
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
            self._invalidate_packages(conn, object_id, "object_version_changed",
                                      {"from": row["version"], "to": new_version, "trigger": "event.add",
                                       "event_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
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
            # 证据补进即审查依据变化，已封存依据包立即失效。
            self._invalidate_packages(conn, object_id, "evidence_changed",
                                      {"trigger": "evidence.upload", "evidence_id": cur.lastrowid, "sha256": digest})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    # ---------- 权利主张 ----------

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

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                try:
                    rules.assert_transition_allowed(claim["status"], new_status)
                except rules.RuleViolation as exc:
                    raise BusinessError(exc.message, exc.status, exc.code)
                self._apply_transition(conn, claim, new_status, note.strip(), user_id, None)
                result = {"claim_id": claim_id, "old_status": claim["status"], "status": new_status}
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _apply_transition(self, conn, claim, new_status, note, actor, package_id):
        """在当前事务内落一次主张流转（审查记录 + 快照 + 审计）。"""
        conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim["id"]))
        conn.execute(
            "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,package_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (claim["id"], actor, claim["status"], new_status, note, package_id, now()),
        )
        # 主张状态不属于审查依据，不推进藏品版本。
        self._snapshot(conn, claim["object_id"], actor)
        self._audit(conn, claim["object_id"], actor, "claim.transition",
                    {"claim_id": claim["id"], "from": claim["status"], "to": new_status, "package_id": package_id})

    # ---------- 依据包失效 ----------

    def _invalidate_packages(self, conn, object_id, reason, detail):
        """藏品/来源事件/证据变化后，该藏品所有已封存依据包立即失效，
        并挡住引用这些包、尚未执行完的主张流转项。"""
        rows = conn.execute(
            "SELECT id FROM basis_packages WHERE object_id=? AND status='sealed'",
            (object_id,),
        ).fetchall()
        stamp = now()
        affected = []
        for row in rows:
            package_id = row["id"]
            conn.execute(
                """UPDATE basis_packages
                   SET status='invalid', invalidation_reason=?, invalidated_at=?
                   WHERE id=? AND status='sealed'""",
                (reason, stamp, package_id),
            )
            conn.execute(
                """UPDATE batch_items
                   SET status='blocked',
                       last_error='依据包已失效: '||?,
                       executed_at=COALESCE(executed_at,?)
                   WHERE id IN (
                       SELECT bi.id FROM batch_items bi
                       JOIN batch_jobs bj ON bi.job_id=bj.id
                       WHERE bj.package_id=? AND bi.status IN ('pending','failed')
                   )""",
                (reason, stamp, package_id),
            )
            affected.append(package_id)
            self._audit(conn, object_id, "system", "package.invalidate",
                        {"package_id": package_id, "reason": reason, "detail": detail})
        return affected

    # ---------- 依据包 ----------

    def _package(self, conn, package_id):
        row = conn.execute("SELECT * FROM basis_packages WHERE id=?", (package_id,)).fetchone()
        if not row:
            raise BusinessError("依据包不存在", 404, "package_not_found")
        return row

    def _package_items(self, conn, package_id):
        return [dict(r) for r in conn.execute(
            "SELECT id,item_type,ref_id,added_by,created_at FROM package_items WHERE package_id=? ORDER BY id",
            (package_id,)).fetchall()]

    def _validate_item_refs(self, conn, object_id, items):
        for item in items:
            if item.item_type == rules.ITEM_EVENT:
                row = conn.execute("SELECT * FROM events WHERE id=?", (item.ref_id,)).fetchone()
                try:
                    rules.validate_event_selectable(row, object_id)
                except rules.RuleViolation as exc:
                    raise BusinessError(exc.message, exc.status, exc.code)
            else:
                row = conn.execute("SELECT * FROM evidence WHERE id=?", (item.ref_id,)).fetchone()
                try:
                    rules.validate_evidence_selectable(row, object_id)
                except rules.RuleViolation as exc:
                    raise BusinessError(exc.message, exc.status, exc.code)

    def _check_expected_revision(self, conn, package_id, expected_revision, current_revision):
        if expected_revision is None:
            raise BusinessError("缺少 expected_revision，无法检测并发修改", 422, "revision_required")
        if int(expected_revision) != current_revision:
            raise BusinessError(
                f"依据包已被他人修改（当前版本 {current_revision}，你依据的是 {expected_revision}），请刷新后重试",
                409, "package_revision_conflict")

    def create_package(self, user_id, object_id, raw_items):
        items = rules.normalize_items(raw_items)
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                self._object(conn, object_id)
                self._validate_item_refs(conn, object_id, items)
                stamp = now()
                cur = conn.execute(
                    """INSERT INTO basis_packages(object_id,status,revision,created_by,created_at)
                       VALUES(?,'draft',1,?,?)""",
                    (object_id, user_id, stamp),
                )
                package_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO package_items(package_id,item_type,ref_id,added_by,created_at) VALUES(?,?,?,?,?)",
                    [(package_id, i.item_type, i.ref_id, user_id, stamp) for i in items],
                )
                self._audit(conn, object_id, user_id, "package.create",
                            {"package_id": package_id, "items": [i.dedupe_key() for i in items]})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_package(user_id, package_id)

    def add_package_items(self, user_id, package_id, raw_items, expected_revision):
        items = rules.normalize_items(raw_items)
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                package = self._package(conn, package_id)
                draft = rules.PackageDraft(package["status"], package["revision"], package["object_id"],
                                           self._package_items(conn, package_id))
                try:
                    rules.assert_can_edit(draft)
                except rules.RuleViolation as exc:
                    raise BusinessError(exc.message, exc.status, exc.code)
                self._check_expected_revision(conn, package_id, expected_revision, package["revision"])
                self._validate_item_refs(conn, package["object_id"], items)
                existing = {(r["item_type"], r["ref_id"]) for r in conn.execute(
                    "SELECT item_type,ref_id FROM package_items WHERE package_id=?", (package_id,)).fetchall()}
                stamp = now()
                added = []
                for item in items:
                    key = item.dedupe_key()
                    if key in existing:
                        continue
                    conn.execute(
                        "INSERT INTO package_items(package_id,item_type,ref_id,added_by,created_at) VALUES(?,?,?,?,?)",
                        (package_id, item.item_type, item.ref_id, user_id, stamp),
                    )
                    added.append(key)
                cur = conn.execute(
                    "UPDATE basis_packages SET revision=revision+1 WHERE id=? AND revision=? AND status='draft'",
                    (package_id, package["revision"]),
                )
                if cur.rowcount == 0:
                    raise BusinessError("依据包状态已变化，补材料冲突", 409, "package_revision_conflict")
                self._audit(conn, package["object_id"], user_id, "package.add_items",
                            {"package_id": package_id, "added": added})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_package(user_id, package_id)

    def seal_package(self, user_id, package_id, expected_revision):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                package = self._package(conn, package_id)
                item_rows = self._package_items(conn, package_id)
                obj = self._object(conn, package["object_id"])
                draft = rules.PackageDraft(package["status"], package["revision"], package["object_id"], item_rows)
                try:
                    rules.assert_can_seal(draft, obj["version"])
                except rules.RuleViolation as exc:
                    raise BusinessError(exc.message, exc.status, exc.code)
                self._check_expected_revision(conn, package_id, expected_revision, package["revision"])
                # 封存前再次确认条目仍可选（公开事件/内部证据）。
                self._validate_item_refs(conn, package["object_id"],
                                         [rules.ItemInput(r["item_type"], r["ref_id"]) for r in item_rows])
                evidence_summary, item_snapshot = self._build_seal_snapshot(conn, item_rows)
                stamp = now()
                cur = conn.execute(
                    """UPDATE basis_packages
                       SET status='sealed', revision=revision+1, sealed_by=?, sealed_at=?,
                           sealed_object_version=?, evidence_summary=?, item_snapshot=?
                       WHERE id=? AND revision=? AND status='draft'""",
                    (user_id, stamp, obj["version"],
                     json.dumps(evidence_summary, ensure_ascii=False, sort_keys=True),
                     json.dumps(item_snapshot, ensure_ascii=False, sort_keys=True),
                     package_id, package["revision"]),
                )
                if cur.rowcount == 0:
                    raise BusinessError("依据包已被他人先封存或修改，请刷新后重试", 409, "package_revision_conflict")
                self._audit(conn, package["object_id"], user_id, "package.seal",
                            {"package_id": package_id, "object_version": obj["version"],
                             "evidence_count": len(evidence_summary)})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_package(user_id, package_id)

    def _build_seal_snapshot(self, conn, item_rows):
        evidence_summary, item_snapshot = [], []
        for r in item_rows:
            if r["item_type"] == rules.ITEM_EVIDENCE:
                ev = conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE id=?", (r["ref_id"],)).fetchone()
                if not ev:
                    raise BusinessError(f"证据 {r['ref_id']} 已不存在", 409, "evidence_missing")
                evidence_summary.append({"evidence_id": ev["id"], "filename": ev["filename"],
                                         "sha256": ev["sha256"], "size": ev["size"]})
                item_snapshot.append({"item_type": "evidence", **dict(ev)})
            else:
                ev = conn.execute("SELECT id,event_type,date_start,date_end,place,description,visibility FROM events WHERE id=?", (r["ref_id"],)).fetchone()
                if not ev:
                    raise BusinessError(f"来源事件 {r['ref_id']} 已不存在", 409, "event_missing")
                item_snapshot.append({"item_type": "event", **dict(ev)})
        return evidence_summary, item_snapshot

    def review_package(self, user_id, package_id):
        """复审：失效依据包不能复活，按原条目另起一个草稿包，由审查员确认后重新封存。"""
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                package = self._package(conn, package_id)
                if package["status"] != "invalid":
                    raise BusinessError("只有失效依据包需要复审", 409, "review_not_needed")
                item_rows = self._package_items(conn, package_id)
                stamp = now()
                cur = conn.execute(
                    """INSERT INTO basis_packages(object_id,status,revision,reopens_package_id,created_by,created_at)
                       VALUES(?,'draft',1,?,?,?)""",
                    (package["object_id"], package_id, user_id, stamp),
                )
                new_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO package_items(package_id,item_type,ref_id,added_by,created_at) VALUES(?,?,?,?,?)",
                    [(new_id, r["item_type"], r["ref_id"], user_id, stamp) for r in item_rows],
                )
                self._audit(conn, package["object_id"], user_id, "package.review",
                            {"old_package_id": package_id, "new_package_id": new_id,
                             "old_reason": package["invalidation_reason"]})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_package(user_id, new_id)

    def _basis_status(self, conn, package):
        """封存依据的现行有效性：返回 (ok, reason)。"""
        obj = self._object(conn, package["object_id"])
        ok, reason = rules.basis_is_valid(package, obj["version"])
        if not ok:
            return False, reason
        # 重算证据摘要，与封存摘要比对，防止证据条目变化后旧结论继续流转。
        sealed_summary = json.loads(package["evidence_summary"] or "[]")
        current, _ = self._build_seal_snapshot(conn, self._package_items(conn, package["id"]))
        # 条目仍可能引用已删除/改可见性的材料
        if {(e["evidence_id"], e["sha256"]) for e in current} != {(e["evidence_id"], e["sha256"]) for e in sealed_summary}:
            return False, "evidence_summary_changed"
        return True, None

    def get_package(self, user_id, package_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            package = self._package(conn, package_id)
            return self._package_view(conn, package)

    def _package_view(self, conn, package):
        items = self._package_items(conn, package["id"])
        detailed = []
        for r in items:
            if r["item_type"] == rules.ITEM_EVENT:
                row = conn.execute("SELECT id,event_type,date_start,date_end,place,description,visibility FROM events WHERE id=?", (r["ref_id"],)).fetchone()
            else:
                row = conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE id=?", (r["ref_id"],)).fetchone()
            detailed.append({**r, "detail": dict(row) if row else None,
                             "available": row is not None})
        affected = [dict(r) for r in conn.execute(
            """SELECT bi.id AS item_id,bi.claim_id,bi.to_status,bi.status,bi.last_error,bj.id AS job_id
               FROM batch_items bi JOIN batch_jobs bj ON bi.job_id=bj.id
               WHERE bj.package_id=? AND bi.status!='done' ORDER BY bi.id""",
            (package["id"],)).fetchall()]
        view = {
            "id": package["id"], "object_id": package["object_id"],
            "status": package["status"], "revision": package["revision"],
            "items": detailed,
            "sealed_by": package["sealed_by"], "sealed_at": package["sealed_at"],
            "sealed_object_version": package["sealed_object_version"],
            "evidence_summary": json.loads(package["evidence_summary"]) if package["evidence_summary"] else None,
            "invalidation_reason": package["invalidation_reason"],
            "invalidation_at": package["invalidated_at"],
            "reopens_package_id": package["reopens_package_id"],
            "created_by": package["created_by"], "created_at": package["created_at"],
            "affected_items": affected,
        }
        if package["status"] != "draft":
            obj = self._object(conn, package["object_id"])
            view["current_object_version"] = obj["version"]
            if package["status"] == "sealed":
                ok, reason = rules.basis_is_valid(package, obj["version"])
                if ok:
                    current, _ = self._build_seal_snapshot(conn, items)
                    sealed_summary = json.loads(package["evidence_summary"] or "[]")
                    if {(e["evidence_id"], e["sha256"]) for e in current} != {(e["evidence_id"], e["sha256"]) for e in sealed_summary}:
                        ok, reason = False, "evidence_summary_changed"
                view["basis_valid"] = ok
                view["invalid_reason"] = reason
            else:
                view["basis_valid"] = False
                view["invalid_reason"] = package["invalidation_reason"]
        return view

    def list_packages(self, user_id, object_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT * FROM basis_packages WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
            return [self._package_view(conn, r) for r in rows]

    # ---------- 主张流转批量作业（可失败、可续做、重启可查）----------

    def create_transition_job(self, user_id, package_id, transitions):
        transitions = transitions or []
        if not isinstance(transitions, list) or not transitions:
            raise BusinessError("至少提供一条主张流转", 422, "transitions_required")
        cleaned = []
        for t in transitions:
            if not isinstance(t, dict):
                raise BusinessError("流转项必须是对象", 422, "invalid_transition_item")
            try:
                claim_id = int(t.get("claim_id"))
            except (TypeError, ValueError):
                raise BusinessError("claim_id 必须是整数", 422, "invalid_claim_id")
            to_status = str(t.get("to_status", "")).strip()
            note = str(t.get("note", "")).strip()
            if to_status not in {"under_review", "negotiating", "resolved_return", "rejected"}:
                raise BusinessError(f"未知目标阶段: {to_status}", 422, "unknown_status")
            if len(note) < 5:
                raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
            cleaned.append((claim_id, to_status, note))
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                package = self._package(conn, package_id)
                obj = self._object(conn, package["object_id"])
                basis_ok, reason = rules.basis_is_valid(package, obj["version"])
                if basis_ok:
                    current, _ = self._build_seal_snapshot(conn, self._package_items(conn, package_id))
                    sealed_summary = json.loads(package["evidence_summary"] or "[]")
                    if {(e["evidence_id"], e["sha256"]) for e in current} != {(e["evidence_id"], e["sha256"]) for e in sealed_summary}:
                        basis_ok, reason = False, "evidence_summary_changed"
                if not basis_ok:
                    raise BusinessError(f"依据包不可用（{reason}），主张流转被挡住", 409, "basis_invalid")
                for claim_id, _, _ in cleaned:
                    claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                    if not claim:
                        raise BusinessError(f"权利主张 {claim_id} 不存在", 404, "claim_not_found")
                    if claim["object_id"] != package["object_id"]:
                        raise BusinessError(f"主张 {claim_id} 不属于依据包对应藏品", 409, "claim_mismatch")
                stamp = now()
                job = conn.execute(
                    """INSERT INTO batch_jobs(package_id,object_id,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (package_id, package["object_id"], user_id, stamp, stamp),
                )
                job_id = job.lastrowid
                conn.executemany(
                    "INSERT INTO batch_items(job_id,claim_id,to_status,note,created_at) VALUES(?,?,?,?,?)",
                    [(job_id, cid, ts, n, stamp) for cid, ts, n in cleaned],
                )
                self._audit(conn, package["object_id"], user_id, "job.create",
                            {"job_id": job_id, "package_id": package_id, "count": len(cleaned)})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.run_transition_job(user_id, job_id)

    def run_transition_job(self, user_id, job_id):
        """执行作业中所有未完成项；已完成的不动，失败/挡住的重试。"""
        def _run(conn):
            actor = self._user(conn, user_id, {"reviewer"})
            job = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise BusinessError("流转作业不存在", 404, "job_not_found")
            package = self._package(conn, job["package_id"])
            pending = conn.execute(
                "SELECT * FROM batch_items WHERE job_id=? AND status IN ({}) ORDER BY id".format(
                    ",".join("?" for _ in rules.unfinished_states())),
                (job_id, *rules.unfinished_states()),
            ).fetchall()
            stamp = now()
            # 依据失效：所有未完成项一律挡住，不动已完成项。
            basis_ok, reason = self._basis_status(conn, package)
            for item in pending:
                if not basis_ok:
                    conn.execute(
                        "UPDATE batch_items SET status='blocked',last_error=?,executed_at=? WHERE id=?",
                        (f"依据包已失效: {reason}", stamp, item["id"]),
                    )
                    continue
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (item["claim_id"],)).fetchone()
                if claim is None:
                    conn.execute(
                        "UPDATE batch_items SET status='failed',last_error=?,executed_at=? WHERE id=?",
                        ("权利主张不存在", stamp, item["id"]),
                    )
                    continue
                try:
                    rules.assert_transition_allowed(claim["status"], item["to_status"])
                except rules.RuleViolation as exc:
                    conn.execute(
                        "UPDATE batch_items SET status='failed',last_error=?,executed_by=?,executed_at=? WHERE id=?",
                        (exc.message, actor["id"], stamp, item["id"]),
                    )
                    continue
                self._apply_transition(conn, claim, item["to_status"], item["note"], actor["id"], package["id"])
                conn.execute(
                    "UPDATE batch_items SET status='done',last_error=NULL,executed_by=?,executed_at=? WHERE id=?",
                    (actor["id"], stamp, item["id"]),
                )
            remaining = conn.execute(
                "SELECT COUNT(*) FROM batch_items WHERE job_id=? AND status!='done'", (job_id,)).fetchone()[0]
            conn.execute("UPDATE batch_jobs SET updated_at=?, completed_at=? WHERE id=?",
                         (stamp, stamp if remaining == 0 else None, job_id))
            self._audit(conn, job["object_id"], actor["id"], "job.run",
                        {"job_id": job_id, "attempted": len(pending), "remaining": remaining,
                         "basis_valid": basis_ok})
            return self._job_view(conn, job_id)

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                view = _run(conn)
                conn.commit()
                return view
            except Exception:
                conn.rollback()
                raise

    def retry_transition_job(self, user_id, job_id):
        # 续做只挑未完成项；作业与进度都已持久化，重启服务后仍可重试。
        return self.run_transition_job(user_id, job_id)

    def _job_view(self, conn, job_id):
        job = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise BusinessError("流转作业不存在", 404, "job_not_found")
        items = [dict(r) for r in conn.execute(
            """SELECT bi.*, c.status AS claim_current_status, c.claimed_by
               FROM batch_items bi JOIN claims c ON bi.claim_id=c.id
               WHERE bi.job_id=? ORDER BY bi.id""", (job_id,)).fetchall()]
        counts = {"pending": 0, "done": 0, "failed": 0, "blocked": 0}
        for it in items:
            counts[it["status"]] += 1
        package = conn.execute("SELECT status,invalidation_reason FROM basis_packages WHERE id=?", (job["package_id"],)).fetchone()
        return {
            "id": job["id"], "package_id": job["package_id"], "object_id": job["object_id"],
            "created_by": job["created_by"], "created_at": job["created_at"],
            "updated_at": job["updated_at"], "completed_at": job["completed_at"],
            "package_status": package["status"],
            "package_invalidation_reason": package["invalidation_reason"],
            "items": items, "counts": counts,
            "finished": counts["done"] == len(items),
        }

    def get_job(self, user_id, job_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            return self._job_view(conn, job_id)

    def list_jobs(self, user_id, object_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id FROM batch_jobs WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
            return [self._job_view(conn, r["id"]) for r in rows]

    # ---------- 查询 ----------

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
            if user["role"] in ("staff", "reviewer"):
                result["packages"] = self.list_packages(user_id, object_id)
                result["jobs"] = [{"id": j["id"], "package_id": j["package_id"], "counts": j["counts"],
                                   "finished": j["finished"], "completed_at": j["completed_at"]}
                                  for j in self.list_jobs(user_id, object_id)]
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
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
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=? ORDER BY id DESC LIMIT 1", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}
