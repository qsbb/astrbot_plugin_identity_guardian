"""配置读取与类型化访问。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger


def _coerce_config(config: Any) -> dict[str, Any]:
    """将多种形态的 config 归一化为 dict。"""
    if isinstance(config, dict):
        return dict(config)
    items = getattr(config, "items", None)
    if callable(items):
        try:
            return dict(items())
        except Exception:
            return {}
    getter = getattr(config, "get", None)
    if callable(getter):
        result: dict[str, Any] = {}
        for key in _DEFAULTS:
            try:
                value = getter(key)
            except Exception:
                continue
            if value is not None:
                result[key] = value
        return result
    return {}


_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "owner_users": [],
    "friendly_users": [],
    "protected_users": [],
    "allow_playful_mute_protected": False,
    "playful_mute_max_seconds": 60,
    "max_mute_seconds": 1800,
    "confirm_mute_threshold": 3600,
    "auto_confirm_threshold": "mute_short",
    "blacklist_users": [],
    "auto_moderate": False,
    "moderation_rules": [],
    "spam_threshold": 5,
    "enable_api_guard": True,
    "join_audit_mode": "off",
    "join_questions": [],
    "join_approve_threshold": 0.9,
    "enable_active_learner_recall": False,
    "active_learner_scope": "group",
    "manual_threshold": 0.6,
    "audit_llm_provider": "",
    "audit_notify_targets": [],
    "confirm_notify_targets": [],
    "pending_ttl_hours": 24,
    "cross_group_violation": False,
    "enable_set_admin_revoke": False,
    "welcome_bot_speak": False,
    "welcome_template": "",
    "identity_refresh_interval": 1800,
    "action_cooldown_seconds": 60,
    "circuit_breaker_threshold": 10,
    "log_level": "INFO",
}


def _parse_list(value: Any) -> list[Any]:
    """将配置值解析为 list。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _parse_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    return result


def _parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


class Config:
    """类型化配置访问器。"""

    def __init__(self, config: Any) -> None:
        raw = _coerce_config(config)
        # 合并默认值
        self._raw: dict[str, Any] = {**_DEFAULTS, **raw}
        # 对 list 类型做正确解析
        for key in (
            "owner_users",
            "friendly_users",
            "protected_users",
            "blacklist_users",
            "moderation_rules",
            "join_questions",
            "audit_notify_targets",
            "confirm_notify_targets",
        ):
            self._raw[key] = _parse_list(self._raw.get(key))

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    @property
    def enabled(self) -> bool:
        return _parse_bool(self._raw.get("enabled"), True)

    @property
    def owner_users(self) -> list[str]:
        return [str(x) for x in self._raw.get("owner_users", [])]

    @property
    def friendly_users(self) -> list[str]:
        return [str(x) for x in self._raw.get("friendly_users", [])]

    @property
    def protected_users(self) -> list[str]:
        return [str(x) for x in self._raw.get("protected_users", [])]

    @property
    def allow_playful_mute_protected(self) -> bool:
        return _parse_bool(self._raw.get("allow_playful_mute_protected"), False)

    @property
    def playful_mute_max_seconds(self) -> int:
        return _parse_int(self._raw.get("playful_mute_max_seconds"), 60, minimum=1)

    @property
    def max_mute_seconds(self) -> int:
        return _parse_int(self._raw.get("max_mute_seconds"), 1800, minimum=0)

    @property
    def confirm_mute_threshold(self) -> int:
        return _parse_int(self._raw.get("confirm_mute_threshold"), 3600, minimum=0)

    @property
    def auto_confirm_threshold(self) -> str:
        return str(self._raw.get("auto_confirm_threshold", "mute_short"))

    @property
    def blacklist_users(self) -> list[str]:
        return [str(x) for x in self._raw.get("blacklist_users", [])]

    @property
    def auto_moderate(self) -> bool:
        return _parse_bool(self._raw.get("auto_moderate"), False)

    @property
    def moderation_rules(self) -> list[str]:
        return [str(x) for x in self._raw.get("moderation_rules", [])]

    @property
    def spam_threshold(self) -> int:
        return _parse_int(self._raw.get("spam_threshold"), 5, minimum=0)

    @property
    def enable_api_guard(self) -> bool:
        return _parse_bool(self._raw.get("enable_api_guard"), True)

    @property
    def join_audit_mode(self) -> str:
        return str(self._raw.get("join_audit_mode", "off"))

    @property
    def join_questions(self) -> list[dict[str, Any]]:
        """入群问答配置，解析为 [{question, answers}] 列表。

        支持两种配置格式，WebUI 友好：
        1. 字符串格式（推荐）：``问题|答案1,答案2``
           例如 ``"1+1=?|2,二"``
           若不含 ``|``，整体视为答案（问题留空，匹配任意问题）
        2. 对象格式（兼容旧配置）：``{"question": "...", "answers": [...]}``
        """
        raw = self._raw.get("join_questions", [])
        parsed: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                # 兼容旧的对象格式
                parsed.append(
                    {
                        "question": str(item.get("question", "")),
                        "answers": [str(a) for a in item.get("answers", [])],
                    }
                )
            elif isinstance(item, str):
                item = item.strip()
                if not item:
                    continue
                if "|" in item:
                    q, a = item.split("|", 1)
                    question = q.strip()
                    answers = [x.strip() for x in a.split(",") if x.strip()]
                else:
                    # 不含分隔符，整体视为答案
                    question = ""
                    answers = [item]
                parsed.append({"question": question, "answers": answers})
        return parsed

    @property
    def join_approve_threshold(self) -> float:
        return _parse_float(self._raw.get("join_approve_threshold"), 0.9)

    @property
    def enable_active_learner_recall(self) -> bool:
        return _parse_bool(self._raw.get("enable_active_learner_recall"), False)

    @property
    def active_learner_scope(self) -> str:
        return str(self._raw.get("active_learner_scope", "group"))

    @property
    def manual_threshold(self) -> float:
        return _parse_float(self._raw.get("manual_threshold"), 0.6)

    @property
    def audit_llm_provider(self) -> str:
        return str(self._raw.get("audit_llm_provider", ""))

    @property
    def audit_notify_targets(self) -> list[str]:
        return [str(x) for x in self._raw.get("audit_notify_targets", [])]

    @property
    def confirm_notify_targets(self) -> list[str]:
        return [str(x) for x in self._raw.get("confirm_notify_targets", [])]

    @property
    def pending_ttl_hours(self) -> int:
        return _parse_int(self._raw.get("pending_ttl_hours"), 24, minimum=1)

    @property
    def cross_group_violation(self) -> bool:
        return _parse_bool(self._raw.get("cross_group_violation"), False)

    @property
    def enable_set_admin_revoke(self) -> bool:
        return _parse_bool(self._raw.get("enable_set_admin_revoke"), False)

    @property
    def welcome_bot_speak(self) -> bool:
        return _parse_bool(self._raw.get("welcome_bot_speak"), False)

    @property
    def welcome_template(self) -> str:
        return str(self._raw.get("welcome_template", ""))

    @property
    def identity_refresh_interval(self) -> int:
        return _parse_int(self._raw.get("identity_refresh_interval"), 1800, minimum=60)

    @property
    def action_cooldown_seconds(self) -> int:
        return _parse_int(self._raw.get("action_cooldown_seconds"), 60, minimum=0)

    @property
    def circuit_breaker_threshold(self) -> int:
        return _parse_int(self._raw.get("circuit_breaker_threshold"), 10, minimum=1)

    @property
    def log_level(self) -> str:
        return str(self._raw.get("log_level", "INFO"))

    def is_protected(self, user_id: str) -> bool:
        """判断用户是否在强保护列表中。"""
        return str(user_id) in self.protected_users

    def is_owner(self, user_id: str) -> bool:
        """判断用户是否是 bot 主人。"""
        return str(user_id) in self.owner_users

    def is_friendly(self, user_id: str) -> bool:
        """判断用户是否是友好用户（主人或额外友好列表）。"""
        return str(user_id) in self.owner_users or str(user_id) in self.friendly_users

    def is_blacklisted(self, user_id: str) -> bool:
        """判断用户是否在黑名单中。"""
        return str(user_id) in self.blacklist_users

    def apply_log_level(self) -> None:
        """应用日志级别配置。"""
        level_str = self.log_level.upper()
        try:
            import logging

            level = getattr(logging, level_str, logging.INFO)
            logger.setLevel(level)
        except Exception:
            pass
