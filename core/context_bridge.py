"""跨会话上下文读取授权。

本模块只裁定“言”能否把其他作用域的近期上下文用于当前一轮请求，不读取
近期缓存，也不执行发送或任何平台动作。私聊内容进入群聊必须由当前群消息
逐轮明示同意；同意不缓存、不继承，解析不确定时一律拒绝。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

CONTEXT_BRIDGE_AUTH_CONTRACT_NAME = "identity.context_bridge_authorization"
CONTEXT_BRIDGE_AUTH_CONTRACT_VERSION = "1.0"

SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"

MODE_NONE = "none"
MODE_PRIVATE_READ_ONLY = "private_read_only"
MODE_GROUP_SELF_READ_ONLY = "group_self_read_only"
MODE_TOPIC_ONLY = "topic_only"
MODE_DETAILS = "details"

PRIVATE_READ_ONLY_MAX_CHARS = 1200
GROUP_SELF_READ_ONLY_MAX_CHARS = 600
# 只续话题不授权披露任何私聊正文；消费者只能使用当前群消息已写明的话题锚点。
TOPIC_ONLY_MAX_CHARS = 0
DETAILS_MAX_CHARS = 600

_VALID_SCOPES = frozenset({SCOPE_PRIVATE, SCOPE_GROUP})

# 保守处理否定、撤回、假设、转述和规则讨论。即使同一句里还出现授权短语，
# 也不能在不确定时把它解释成真实授权。
_NEGATION_RE = re.compile(
    r"(?:不要|别(?:再)?|不准|不许|禁止|不能|不可以|不愿意|不同意|"
    r"没(?:有)?同意|未同意|不授权|取消(?:授权|同意)?|撤回(?:授权|同意)?)"
)
_NON_CONSENT_CONTEXT_RE = re.compile(
    r"(?:如果|假如|假设|比如|例如|举例|测试|提示词|规则|关键词|"
    r"什么叫|什么意思|是否|能不能|可不可以|要不要|为什么)"
)

_PRIVATE_REF = r"(?:我(?:们)?|咱们)?(?:刚才|之前|前面|上次)?(?:在)?私聊(?:里|中)?"
_GROUP_REF = r"(?:这个|本|当前)?群(?:里|中|聊)?"
_DETAIL_NOUN = r"(?:说过的|说的|聊过的|聊的|提过的|提到的)?(?:内容|细节|原话|消息|记录|那件事|那个|[^，。！？!?]{1,30})?"
_PUBLISH_VERB = r"(?:带到|发到|贴到|转到|搬到|公开到|分享到|公布到|讲到|说到)"

_DETAILS_PATTERNS = (
    re.compile(
        rf"(?:我)?(?:明确)?(?:同意|允许|授权|可以)(?:你)?(?:把|将)?"
        rf"{_PRIVATE_REF}{_DETAIL_NOUN}{_PUBLISH_VERB}{_GROUP_REF}"
    ),
    re.compile(
        rf"(?:请|麻烦)?(?:你)?(?:把|将){_PRIVATE_REF}{_DETAIL_NOUN}"
        rf"{_PUBLISH_VERB}{_GROUP_REF}"
    ),
    re.compile(
        rf"(?:我)?(?:明确)?(?:同意|允许|授权|可以)(?:你)?(?:在)?{_GROUP_REF}"
        rf"(?:公开|分享|公布|复述|转述|说|讲){_PRIVATE_REF}{_DETAIL_NOUN}"
    ),
)

_TOPIC_PATTERNS = (
    re.compile(
        rf"(?:我)?(?:同意|允许|可以)?(?:你)?(?:在)?{_GROUP_REF}?"
        rf"(?:接着|继续)(?:聊|说|谈){_PRIVATE_REF}(?:的)?(?:话题|那件事|那个)?"
    ),
    re.compile(
        rf"(?:我)?(?:同意|允许|可以)(?:你)?(?:在)?{_GROUP_REF}?"
        rf"(?:提|聊|说|谈)(?:一下)?{_PRIVATE_REF}(?:的)?(?:话题|那件事|那个)"
    ),
    re.compile(
        rf"(?:接着|继续){_PRIVATE_REF}(?:的)?(?:话题|那件事|那个)?(?:聊|说|谈)"
    ),
)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).casefold()[:2000]


def _component_type(component: Any) -> str:
    if isinstance(component, dict):
        return str(component.get("type") or "").casefold()
    return type(component).__name__.casefold()


def _component_text(component: Any) -> str:
    if isinstance(component, dict):
        data = component.get("data")
        if isinstance(data, dict):
            return str(data.get("text") or "")
        return str(component.get("text") or "")
    return str(getattr(component, "text", "") or "")


def extract_current_plain_text(event: Any) -> str:
    """只读取当前事件自己的 Plain/Text 段，避免引用正文伪造本轮同意。"""
    message_obj = getattr(event, "message_obj", None)
    chain = getattr(message_obj, "message", None) if message_obj is not None else None
    if isinstance(chain, (list, tuple)):
        parts = [
            _component_text(component)
            for component in chain
            if _component_type(component) in {"plain", "text"}
        ]
        # 消息链存在时绝不回退到 get_message_str；后者可能拼入 Reply 引用正文。
        return "".join(parts).strip()

    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""
    return str(getattr(event, "message_str", "") or "").strip()


def event_scope(event: Any) -> str:
    """从当前事件判断目标作用域；无法确认时返回空串。"""
    group_getter = getattr(event, "get_group_id", None)
    if callable(group_getter):
        try:
            return SCOPE_GROUP if str(group_getter() or "").strip() else SCOPE_PRIVATE
        except Exception:
            return ""

    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None and hasattr(message_obj, "group_id"):
        return (
            SCOPE_GROUP
            if str(getattr(message_obj, "group_id", "") or "").strip()
            else SCOPE_PRIVATE
        )

    umo = str(getattr(event, "unified_msg_origin", "") or "")
    parts = umo.split(":", 2)
    if len(parts) == 3:
        message_type = parts[1].casefold()
        if message_type == "groupmessage":
            return SCOPE_GROUP
        if message_type in {"friendmessage", "privatemessage", "directmessage"}:
            return SCOPE_PRIVATE
    return ""


def classify_private_to_group_consent(text: str) -> tuple[str, str]:
    """返回 ``(mode, reason)``；仅识别当前消息中的明确、非否定同意。"""
    normalized = _normalize_text(text)
    if not normalized:
        return MODE_NONE, "current_group_consent_missing"
    if _NEGATION_RE.search(normalized):
        return MODE_NONE, "current_group_consent_negated"
    if _NON_CONSENT_CONTEXT_RE.search(normalized):
        return MODE_NONE, "current_group_consent_ambiguous"
    if any(pattern.search(normalized) for pattern in _DETAILS_PATTERNS):
        return MODE_DETAILS, "explicit_private_details_consent"
    if any(pattern.search(normalized) for pattern in _TOPIC_PATTERNS):
        return MODE_TOPIC_ONLY, "explicit_private_topic_consent"
    return MODE_NONE, "current_group_consent_missing"


def decision(
    authorized: bool,
    reason: str,
    *,
    mode: str = MODE_NONE,
    explicit: bool = False,
    max_chars: int = 0,
) -> dict[str, object]:
    """构造固定六字段结果，防止身份或消息内容意外越过契约。"""
    return {
        "version": CONTEXT_BRIDGE_AUTH_CONTRACT_VERSION,
        "authorized": bool(authorized),
        "reason": str(reason),
        "mode": str(mode),
        "explicit": bool(explicit),
        "max_chars": max(0, int(max_chars)),
    }


def authorize(event: Any, source_scope: str, target_scope: str) -> dict[str, object]:
    """按作用域方向与当前事件逐轮裁定只读上下文桥接。"""
    source = str(source_scope or "").strip().casefold()
    target = str(target_scope or "").strip().casefold()
    if source not in _VALID_SCOPES or target not in _VALID_SCOPES:
        return decision(False, "unsupported_scope")

    actual_target = event_scope(event)
    if not actual_target or actual_target != target:
        return decision(False, "current_event_scope_mismatch")

    if source == SCOPE_PRIVATE and target == SCOPE_PRIVATE:
        return decision(
            True,
            "private_to_private_read_only",
            mode=MODE_PRIVATE_READ_ONLY,
            max_chars=PRIVATE_READ_ONLY_MAX_CHARS,
        )

    if source == SCOPE_GROUP and target == SCOPE_PRIVATE:
        return decision(
            True,
            "group_to_private_self_only",
            mode=MODE_GROUP_SELF_READ_ONLY,
            max_chars=GROUP_SELF_READ_ONLY_MAX_CHARS,
        )

    if source == SCOPE_GROUP and target == SCOPE_GROUP:
        return decision(False, "group_to_group_denied")

    # private -> group：只信当前群事件自己的正文，不接受调用方传入文本，也不记忆同意。
    mode, reason = classify_private_to_group_consent(
        extract_current_plain_text(event)
    )
    if mode == MODE_TOPIC_ONLY:
        return decision(
            True,
            reason,
            mode=mode,
            explicit=True,
            max_chars=TOPIC_ONLY_MAX_CHARS,
        )
    if mode == MODE_DETAILS:
        return decision(
            True,
            reason,
            mode=mode,
            explicit=True,
            max_chars=DETAILS_MAX_CHARS,
        )
    return decision(False, reason)
