"""内容审核测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.moderation import ModerationService  # noqa: E402
from core.models import PunishmentLevel  # noqa: E402


def _make_config(**overrides):
    defaults = {
        "moderation_rules": ["加我微信.*", "免费领.*"],
        "auto_moderate": False,
        "manual_threshold": 0.6,
        "auto_confirm_threshold": "mute_short",
        "max_mute_seconds": 1800,
    }
    defaults.update(overrides)
    return Config(defaults)


def test_rule_match():
    """关键词规则命中。"""
    svc = ModerationService(_make_config())
    result = svc.check_rules("加我微信123456")
    assert result.is_violation is True
    assert result.level == PunishmentLevel.MUTE_SHORT.value
    assert result.confidence == 1.0


def test_rule_no_match():
    """关键词规则未命中。"""
    svc = ModerationService(_make_config())
    result = svc.check_rules("今天天气真好")
    assert result.is_violation is False


def test_rule_case_insensitive():
    """关键词规则不区分大小写。"""
    svc = ModerationService(_make_config(moderation_rules=["spam"]))
    result = svc.check_rules("SPAM message")
    assert result.is_violation is True


def test_determine_punishment_none():
    """无违规 — 无处罚。"""
    svc = ModerationService(_make_config())
    from core.models import ModerationResult

    result = ModerationResult(is_violation=False)
    punishment = svc.determine_punishment(result)
    assert punishment.level == PunishmentLevel.NONE.value


def test_determine_punishment_mute():
    """短禁言处罚。"""
    svc = ModerationService(_make_config())
    from core.models import ModerationResult

    result = ModerationResult(
        is_violation=True,
        level=PunishmentLevel.MUTE_SHORT.value,
        confidence=0.9,
    )
    punishment = svc.determine_punishment(result)
    assert punishment.level == PunishmentLevel.MUTE_SHORT.value
    assert punishment.mute_duration == 300


def test_determine_punishment_exceeds_auto():
    """超过自动执行上限 — 降级为警告。"""
    svc = ModerationService(_make_config(auto_confirm_threshold="warn"))
    from core.models import ModerationResult

    result = ModerationResult(
        is_violation=True,
        level=PunishmentLevel.MUTE_LONG.value,
        confidence=0.9,
    )
    punishment = svc.determine_punishment(result)
    assert punishment.level == PunishmentLevel.WARN.value


def test_llm_parse_result():
    """LLM 返回解析。"""
    svc = ModerationService(_make_config())
    result = svc._parse_llm_result(
        '{"is_violation": true, "level": "mute_short", "reason": "广告", "confidence": 0.85}'
    )
    assert result.is_violation is True
    assert result.level == "mute_short"
    assert result.confidence == 0.85
