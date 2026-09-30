"""审查规则层：只描述规则，不碰数据库与 HTTP。

- 主张阶段机
- 依据包条目可选择性、封存前置条件、失效判定
- 批量流转作业的状态机与重试筛选
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 主张阶段机：阶段不能跳跃，终态不可重新打开
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}

# 依据包只收集两类条目：公开来源事件、内部证据
ITEM_EVENT = "event"
ITEM_EVIDENCE = "evidence"
VALID_ITEM_TYPES = (ITEM_EVENT, ITEM_EVIDENCE)

# 条目状态
ITEM_PENDING = "pending"
BATCH_PENDING = "pending"
BATCH_DONE = "done"
BATCH_FAILED = "failed"
BATCH_BLOCKED = "blocked"
# 重试时继续处理的未完成状态
UNFINISHED_ITEM_STATES = (BATCH_PENDING, BATCH_FAILED, BATCH_BLOCKED)
FINISHED_ITEM_STATES = (BATCH_DONE,)


class RuleViolation(Exception):
    """规则层拒绝，携带机器可读 code。"""

    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ItemInput:
    item_type: str
    ref_id: int
    visibility: str | None = None
    digest: str | None = None

    def dedupe_key(self):
        return (self.item_type, self.ref_id)


@dataclass
class PackageDraft:
    status: str
    revision: int
    object_id: int
    items: list = field(default_factory=list)


def normalize_items(raw_items):
    """把请求里的条目归一成 ItemInput，并校验类型/重复。"""
    if not isinstance(raw_items, list) or not raw_items:
        raise RuleViolation("items_required", "依据包至少选择一个公开来源事件或内部证据")
    normalized, seen = [], set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise RuleViolation("invalid_item", "条目必须是对象")
        item_type = str(raw.get("item_type", "")).strip()
        if item_type not in VALID_ITEM_TYPES:
            raise RuleViolation("invalid_item_type", "item_type 只能是 event 或 evidence")
        try:
            ref_id = int(raw.get("ref_id"))
        except (TypeError, ValueError):
            raise RuleViolation("invalid_item_ref", "ref_id 必须是整数")
        if ref_id <= 0:
            raise RuleViolation("invalid_item_ref", "ref_id 必须为正整数")
        key = (item_type, ref_id)
        if key in seen:
            raise RuleViolation("duplicate_item", f"条目重复: {item_type}#{ref_id}")
        seen.add(key)
        normalized.append(ItemInput(item_type, ref_id, raw.get("visibility"), raw.get("digest")))
    return normalized


def validate_event_selectable(event_row, object_id):
    """审查员只能选取本藏品的公开来源事件。"""
    if event_row is None:
        raise RuleViolation("event_not_found", "来源事件不存在", 404)
    if event_row["object_id"] != object_id:
        raise RuleViolation("item_mismatch", "来源事件不属于该藏品", 409)
    if event_row["visibility"] != "public":
        raise RuleViolation("event_not_public", "依据包只能选取公开来源事件")


def validate_evidence_selectable(evidence_row, object_id):
    """审查员只能选取本藏品的内部证据。"""
    if evidence_row is None:
        raise RuleViolation("evidence_not_found", "证据不存在", 404)
    if evidence_row["object_id"] != object_id:
        raise RuleViolation("item_mismatch", "证据不属于该藏品", 409)
    if evidence_row["visibility"] != "internal":
        raise RuleViolation("evidence_not_internal", "依据包只能选取内部证据")


def assert_can_edit(draft: PackageDraft):
    if draft.status == "sealed":
        raise RuleViolation("package_sealed", "依据包已封存，需复审后才能调整", 409)
    if draft.status == "invalid":
        raise RuleViolation("package_invalid", "失效依据包不能直接修改，请复审", 409)


def assert_can_seal(draft: PackageDraft, object_version: int):
    """封存前置条件：草稿态、有条目、且封存时藏品版本与记录一致。"""
    if draft.status == "sealed":
        raise RuleViolation("package_sealed", "依据包已封存", 409)
    if draft.status == "invalid":
        raise RuleViolation("package_invalid", "失效依据包不能封存，请复审", 409)
    if not draft.items:
        raise RuleViolation("items_required", "没有任何依据条目，无法封存")
    if object_version <= 0:
        raise RuleViolation("invalid_version", "藏品版本非法")


def basis_is_valid(package_row, current_object_version):
    """失效判定规则。

    1. 状态必须是 sealed；
    2. 封存时记录的藏品版本必须等于当前版本（藏品/来源事件一变即失效）；
    3. 不存在失效记录（证据补进等直接失效原因）；
    4. 重算证据摘要必须与封存摘要一致（条目集被改动）。
    返回 (是否有效, 原因 code)。
    """
    if package_row is None:
        return False, "package_not_found"
    if package_row["status"] != "sealed":
        return False, f"package_{package_row['status']}"
    if package_row["sealed_object_version"] != current_object_version:
        return False, "object_version_changed"
    if package_row["invalidation_reason"]:
        return False, package_row["invalidation_reason"]
    return True, None


def assert_transition_allowed(current_status, new_status):
    allowed = CLAIM_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise RuleViolation(
            "invalid_transition",
            f"不能从 {current_status} 直接变更为 {new_status}",
            409,
        )


def unfinished_states():
    return UNFINISHED_ITEM_STATES
