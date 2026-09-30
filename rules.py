"""审查领域规则：依据包、版本校验、流转约束。

本模块只包含纯领域规则，不依赖 SQLite 或 HTTP，便于持久化层和页面层共用同一套判定。
"""
from __future__ import annotations

from datetime import date

# 权利主张阶段流转状态机。
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}

# 依据包状态：draft 未封存、sealed 已封存、invalid 已失效。
PACKAGE_STATUSES = {"draft", "sealed", "invalid"}

# 任务（封存 / 流转）状态。
JOB_STATUSES = {"pending", "in_progress", "completed", "failed", "blocked"}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", affected=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        # 失效或冲突时列出的受影响项。
        self.affected = affected or []


def validate_transition(current_status, new_status):
    """校验主张阶段流转是否合法，返回 (ok, error_message)。"""
    if new_status not in CLAIM_TRANSITIONS:
        return False, f"未知的审查阶段 {new_status}"
    if new_status not in CLAIM_TRANSITIONS.get(current_status, set()):
        return False, f"不能从 {current_status} 直接变更为 {new_status}"
    return True, None


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def check_event_input(event_type, date_start, date_end, place, description):
    """校验来源事件录入，返回 (ok, error_message)。"""
    if not (event_type or "").strip() or not (description or "").strip() or not (place or "").strip():
        return False, "事件类型、地点和说明不能为空"
    start = parse_date(date_start)
    if start is None:
        return False, "事件日期必须是 YYYY-MM-DD"
    end = parse_date(date_end) if date_end else start
    if end is None:
        return False, "事件日期必须是 YYYY-MM-DD"
    if end < start:
        return False, "事件结束日期不能早于开始日期"
    return True, None


def check_evidence_input(filename, content_b64, visibility):
    """校验证据录入，返回 (ok, error_message, content_or_None)。"""
    import base64
    import binascii

    if not (filename or "").strip():
        return False, "文件名不能为空", None
    if visibility not in {"public", "internal"}:
        return False, "visibility 必须是 public 或 internal", None
    try:
        content = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return False, "content_b64 不是合法 Base64", None
    return True, None, content


def evidence_digest(evidence_rows):
    """生成证据摘要：[(id, sha256), ...]，按 id 排序。"""
    return sorted(({"id": e["id"], "sha256": e["sha256"]} for e in evidence_rows), key=lambda x: x["id"])


def check_package_validity(*, sealed_object_version, sealed_event_ids, sealed_evidence_digest,
                          current_object_version, current_event_ids, current_evidence):
    """校验已封存依据包是否仍有效。

    封存时记下藏品版本、来源事件集合与证据摘要；此后藏品、来源事件或证据发生任何变化，
    都判定依据包失效，并逐项列出受影响对象。

    返回 (valid: bool, affected: list[dict])。
    """
    affected = []
    if current_object_version != sealed_object_version:
        affected.append({
            "type": "object",
            "id": None,
            "message": f"藏品版本由 {sealed_object_version} 变为 {current_object_version}",
        })

    sealed_events = set(sealed_event_ids or [])
    current_events = set(current_event_ids or [])
    for eid in sorted(current_events - sealed_events):
        affected.append({"type": "event", "id": eid, "message": "新增来源事件"})
    for eid in sorted(sealed_events - current_events):
        affected.append({"type": "event", "id": eid, "message": "来源事件已删除"})

    sealed_ev = {e["id"]: e["sha256"] for e in (sealed_evidence_digest or [])}
    current_ev = {e["id"]: e["sha256"] for e in current_evidence}
    for eid in sorted(set(current_ev) - set(sealed_ev)):
        affected.append({"type": "evidence", "id": eid, "message": "新增证据"})
    for eid in sorted(set(sealed_ev) - set(current_ev)):
        affected.append({"type": "evidence", "id": eid, "message": "证据已删除"})
    for eid in sorted(set(sealed_ev) & set(current_ev)):
        if sealed_ev[eid] != current_ev[eid]:
            affected.append({"type": "evidence", "id": eid, "message": "证据内容摘要变化"})

    return (len(affected) == 0), affected


def first_writer_wins_update(conn, table, assignments, where_sql, params):
    """乐观并发更新：先写入者生效。

    调用方在 WHERE 中带上版本号；若影响行数为 0，说明已被他人先写入，返回 False。
    """
    cur = conn.execute(f"UPDATE {table} SET {assignments} WHERE {where_sql}", params)
    return cur.rowcount > 0
