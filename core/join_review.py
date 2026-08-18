"""Per-platform, per-group join-review runtime orchestration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .audit import AutoAuditResult, JoinAuditService
from .group_discovery import get_aiocqhttp_bot, get_bot_group_role
from .join_notification import JoinNotificationService, NotificationResult
from .join_review_store import (
    JoinRequest,
    JoinReviewStore,
    ValidationError,
    normalize_platform_id,
    normalize_qq_id,
)
from .models import JoinDecision, JoinVerdict
from .onebot import OneBotClient

MAX_EVENT_TEXT = 2048
MAX_NICKNAME = 128
MAX_LEVEL = 64


class GuardBlockedError(RuntimeError):
    """紧急停止/熔断护栏拦截了入群申请处理。"""


@dataclass(frozen=True, slots=True)
class ParsedJoinRequest:
    platform_id: str
    group_id: str
    user_id: str
    nickname: str
    level: str
    question: str
    answer: str
    sub_type: str
    flag: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class JoinReviewResult:
    outcome: str
    decision: JoinDecision | None = None
    auto_audit: AutoAuditResult | None = None
    request: JoinRequest | None = None
    notification: NotificationResult | None = None


def _bounded_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:maximum]


def _event_platform_id(event: Any, raw: dict[str, Any]) -> str:
    getter = getattr(event, "get_platform_id", None)
    if callable(getter):
        try:
            value = getter()
        except (RuntimeError, TypeError, ValueError):
            value = ""
        if value:
            return normalize_platform_id(value)
    value = raw.get("platform_id")
    if value:
        return normalize_platform_id(value)
    raise ValidationError("platform_id_required")


def parse_join_request(event: Any, raw: dict[str, Any]) -> ParsedJoinRequest:
    """Parse only allowlisted event fields; absent non-standard fields stay unknown."""
    if not isinstance(raw, dict):
        raise ValidationError("invalid_request_event")
    platform_id = _event_platform_id(event, raw)
    group_id = normalize_qq_id(raw.get("group_id"))
    user_id = normalize_qq_id(raw.get("user_id"), "user_id")
    flag = _bounded_text(raw.get("flag"), 4096)
    if not flag:
        raise ValidationError("invalid_flag")
    sub_type = _bounded_text(raw.get("sub_type") or "add", 32)

    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    nickname = _bounded_text(
        raw.get("nickname") or sender.get("nickname"), MAX_NICKNAME
    )
    level = _bounded_text(raw.get("level") or sender.get("level"), MAX_LEVEL)
    comment = _bounded_text(raw.get("comment"), MAX_EVENT_TEXT)
    question = _bounded_text(raw.get("question"), MAX_EVENT_TEXT)
    if not question:
        question = JoinAuditService.extract_question(None, comment)
    answer = _bounded_text(raw.get("answer"), MAX_EVENT_TEXT) or comment
    _, _, _, _, parsed_answer = JoinAuditService.parse_request(
        None, {"comment": comment}
    )
    if parsed_answer:
        answer = parsed_answer
    return ParsedJoinRequest(
        platform_id=platform_id,
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
        level=level,
        question=_bounded_text(question, MAX_EVENT_TEXT),
        answer=_bounded_text(answer, MAX_EVENT_TEXT),
        sub_type=sub_type,
        flag=flag,
    )


def resolve_presets(config: Any) -> list[Any] | None:
    """按群问答预设优先；该群未配置时返回 None，由 audit 回退全局配置。"""
    return list(config.join_questions) or None


class JoinReviewRuntime:
    def __init__(
        self,
        audit: JoinAuditService,
        onebot: OneBotClient,
        store: JoinReviewStore,
        notification: JoinNotificationService | None = None,
        guard: Callable[[], bool] | None = None,
    ) -> None:
        self.audit = audit
        self.onebot = onebot
        self.store = store
        self.notification = notification or JoinNotificationService(store, onebot)
        # 紧急停止/熔断护栏谓词：返回 False 时拒绝处理入群申请。
        self.guard = guard

    @staticmethod
    def _request_id(parsed: ParsedJoinRequest) -> str:
        digest = hashlib.sha256(
            "\0".join(
                (
                    parsed.platform_id,
                    parsed.group_id,
                    parsed.user_id,
                    parsed.sub_type,
                    parsed.flag,
                )
            ).encode("utf-8")
        ).hexdigest()
        return f"jr_{digest}"

    async def _store_request(self, parsed: ParsedJoinRequest) -> JoinRequest:
        request_id = self._request_id(parsed)
        existing = await self.store.get_request(request_id, expire=False)
        if existing is not None:
            return existing
        try:
            return await self.store.add_request(
                request_id=request_id,
                platform_id=parsed.platform_id,
                group_id=parsed.group_id,
                user_id=parsed.user_id,
                nickname=parsed.nickname,
                level=parsed.level,
                question=parsed.question,
                answer=parsed.answer,
                sub_type=parsed.sub_type,
                flag=parsed.flag,
            )
        except ValidationError as exc:
            if str(exc) != "duplicate_request_id":
                raise
            duplicate = await self.store.get_request(request_id, expire=False)
            if duplicate is None:
                raise
            return duplicate

    async def handle_event(self, event: Any, raw: dict[str, Any]) -> JoinReviewResult:
        parsed = parse_join_request(event, raw)
        config = await self.store.get_group_config(parsed.platform_id, parsed.group_id)
        if not config.auto_audit_enabled and not config.review_send_enabled:
            return JoinReviewResult("ignored")

        audit_result: AutoAuditResult | None = None
        if config.auto_audit_enabled:
            # 按群问答预设优先；该群未配置时传 None，由 audit 回退全局 join_questions。
            try:
                audit_result = await self.audit.execute_auto_audit(
                    event, raw, configured_questions=resolve_presets(config)
                )
            except Exception:
                audit_result = AutoAuditResult(
                    decision=JoinDecision(
                        verdict=JoinVerdict.UNAVAILABLE.value,
                        confidence=0.0,
                        reason="自动审核异常",
                    ),
                    platform_error="auto_audit_error",
                )
            if audit_result.platform_approved:
                return JoinReviewResult(
                    "auto_approved",
                    decision=audit_result.decision,
                    auto_audit=audit_result,
                )
            if not config.review_send_enabled:
                return JoinReviewResult(
                    "left_on_platform",
                    decision=audit_result.decision,
                    auto_audit=audit_result,
                )

        request = await self._store_request(parsed)
        notification = await self.notification.notify(event.bot, request, config)
        return JoinReviewResult(
            "pending_review",
            decision=audit_result.decision if audit_result else None,
            auto_audit=audit_result,
            request=request,
            notification=notification,
        )

    async def process_request(
        self,
        context: Any,
        request_id: str,
        *,
        approve: bool,
        reason: str = "",
    ) -> JoinRequest:
        """Approve or reject once, after re-checking the Bot's current group role."""
        if not isinstance(approve, bool):
            raise ValidationError("invalid_approve")
        if self.guard is not None and not self.guard():
            raise GuardBlockedError("guard_blocked")

        async def platform_action(request: JoinRequest) -> tuple[bool, str]:
            bot = get_aiocqhttp_bot(context, request.platform_id)
            if bot is None:
                return False, "platform_unavailable"
            role = await get_bot_group_role(self.onebot, bot, request.group_id)
            if role not in {"owner", "admin"}:
                return False, "permission_denied"
            return await self.onebot.set_group_add_request_for_bot(
                bot,
                request.flag,
                request.sub_type,
                approve=approve,
                reason=_bounded_text(reason, 256) if not approve else "",
            )

        return await self.store.process_request(
            request_id,
            status="approved" if approve else "rejected",
            platform_action=platform_action,
        )


__all__ = [
    "GuardBlockedError",
    "JoinReviewResult",
    "JoinReviewRuntime",
    "ParsedJoinRequest",
    "parse_join_request",
    "resolve_presets",
]
