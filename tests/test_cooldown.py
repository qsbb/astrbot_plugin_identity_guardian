"""冷却与熔断测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.cooldown import CooldownService  # noqa: E402


def _make_config(**overrides):
    defaults = {
        "spam_threshold": 5,
        "action_cooldown_seconds": 60,
        "circuit_breaker_threshold": 10,
    }
    defaults.update(overrides)
    return Config(defaults)


def test_cooldown_basic():
    """操作冷却基本功能。"""
    svc = CooldownService(_make_config(action_cooldown_seconds=60))
    assert svc.is_on_cooldown("g1", "u1", "mute") is False
    svc.mark_action("g1", "u1", "mute")
    assert svc.is_on_cooldown("g1", "u1", "mute") is True


def test_cooldown_different_actions():
    """不同操作不互相影响。"""
    svc = CooldownService(_make_config(action_cooldown_seconds=60))
    svc.mark_action("g1", "u1", "mute")
    assert svc.is_on_cooldown("g1", "u1", "mute") is True
    assert svc.is_on_cooldown("g1", "u1", "kick") is False


def test_spam_detection():
    """刷屏检测。"""
    svc = CooldownService(_make_config(spam_threshold=3))
    for _ in range(3):
        assert svc.is_spamming("g1", "u1") is False
    assert svc.is_spamming("g1", "u1") is True


def test_spam_disabled():
    """刷屏检测关闭。"""
    svc = CooldownService(_make_config(spam_threshold=0))
    for _ in range(100):
        assert svc.is_spamming("g1", "u1") is False


def test_circuit_breaker():
    """熔断器触发。"""
    svc = CooldownService(_make_config(circuit_breaker_threshold=3))
    assert svc.check_breaker() is False
    svc.mark_action("g1", "u1", "mute")
    svc.mark_action("g1", "u2", "mute")
    svc.mark_action("g1", "u3", "mute")
    assert svc.check_breaker() is True


def test_circuit_breaker_reset():
    """熔断器重置。"""
    svc = CooldownService(_make_config(circuit_breaker_threshold=1))
    svc.mark_action("g1", "u1", "mute")
    assert svc.check_breaker() is True
    svc.trip_breaker()
    assert svc.check_breaker() is True
    svc.reset_breaker()
    assert svc.check_breaker() is False


def test_clear():
    """清空状态。"""
    svc = CooldownService(_make_config())
    svc.mark_action("g1", "u1", "mute")
    svc.clear()
    assert svc.is_on_cooldown("g1", "u1", "mute") is False
    assert svc.check_breaker() is False
