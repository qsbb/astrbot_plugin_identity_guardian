"""Data models for identity guardian plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Role(str, Enum):
    """QQ 群角色枚举。"""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    UNKNOWN = "unknown"


class Relation(str, Enum):
    """用户与 bot 的关系枚举。"""

    OWNER = "owner"
    FRIENDLY = "friendly"
    NORMAL = "normal"
    UNKNOWN = "unknown"


class TriggerSource(str, Enum):
    """动作触发来源。"""

    LLM_AUTONOMOUS = "llm_autonomous"
    EXPLICIT_REQUEST = "explicit_request"
    SELF_SERVICE = "self_service"
    AUTOMATIC_MODERATION = "automatic_moderation"
    JOIN_AUDIT = "join_audit"


class PunishmentLevel(str, Enum):
    """处罚等级。"""

    NONE = "none"
    WARN = "warn"
    MUTE_SHORT = "mute_short"
    MUTE_LONG = "mute_long"
    DELETE = "delete"
    KICK = "kick"


class JoinVerdict(str, Enum):
    """入群审核裁决结果。"""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCERTAIN = "uncertain"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """当前事件的完整身份与关系上下文。

    所有有副作用的工具调用都必须基于此上下文进行授权判断。
    """

    bot_role: str
    requester_id: str
    requester_role: str
    requester_relation: str
    bot_id: str = ""
    target_id: str | None = None
    target_role: str | None = None
    target_relation: str | None = None
    group_id: str = ""
    platform_id: str = ""


@dataclass(slots=True)
class ActionDecision:
    """策略引擎的授权决策结果。"""

    allowed: bool
    reason: str = ""
    action: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False


@dataclass(slots=True)
class ModerationResult:
    """独立审核 LLM 的判断结果。"""

    is_violation: bool = False
    level: str = "none"
    reason: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class Punishment:
    """处罚决策。"""

    level: str = "none"
    mute_duration: int = 0
    delete_msg: bool = False
    kick: bool = False


@dataclass(slots=True)
class JoinDecision:
    """入群审核裁决。"""

    verdict: str = "uncertain"
    confidence: float = 0.0
    reason: str = ""
    evidence_summary: str = ""


@dataclass(slots=True)
class KnowledgeEvidence:
    """知识库检索证据。"""

    content: str = ""
    source: str = ""
    score: float = 0.0


@dataclass(slots=True)
class AuditEntry:
    """审计日志条目。"""

    ts: str
    platform_id: str
    group_id: str
    actor: str
    action: str
    target_user: str
    params: dict[str, Any] = field(default_factory=dict)
    llm_summary: str = ""
    result: str = ""
    error: str | None = None


@dataclass(slots=True)
class ConfirmEntry:
    """待确认操作条目。"""

    confirm_id: str
    action: str
    params: dict[str, Any]
    group_id: str
    target_user: str
    created_at: float = 0.0
    status: str = "pending"


def now_iso() -> str:
    """返回 ISO 格式时间戳。"""
    return datetime.now().astimezone().isoformat()
