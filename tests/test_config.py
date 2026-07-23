"""Config 解析测试。"""

import sys
from pathlib import Path

# 将插件根目录加入 sys.path 以便直接导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402


def test_defaults():
    """默认值正确。"""
    cfg = Config({})
    assert cfg.enabled is True
    assert cfg.owner_users == []
    assert cfg.protected_users == []
    assert cfg.max_mute_seconds == 1800
    assert cfg.join_audit_mode == "off"
    assert cfg.auto_moderate is False
    assert cfg.enable_api_guard is True


def test_list_parsing():
    """list 类型配置项正确解析。"""
    cfg = Config(
        {
            "owner_users": ["111", "222"],
            "protected_users": ["333"],
            "moderation_rules": ["spam.*", "ad.*"],
        }
    )
    assert cfg.owner_users == ["111", "222"]
    assert cfg.protected_users == ["333"]
    assert cfg.moderation_rules == ["spam.*", "ad.*"]


def test_list_from_string():
    """字符串逗号分隔自动解析为 list。"""
    cfg = Config({"owner_users": "111, 222,333"})
    assert cfg.owner_users == ["111", "222", "333"]


def test_bool_parsing():
    """bool 类型正确解析。"""
    cfg = Config({"enabled": "false", "auto_moderate": "true"})
    assert cfg.enabled is False
    assert cfg.auto_moderate is True


def test_int_parsing():
    """int 类型正确解析。"""
    cfg = Config({"max_mute_seconds": "600", "spam_threshold": 3})
    assert cfg.max_mute_seconds == 600
    assert cfg.spam_threshold == 3


def test_float_parsing():
    """float 类型正确解析。"""
    cfg = Config({"join_approve_threshold": "0.85", "manual_threshold": 0.5})
    assert cfg.join_approve_threshold == 0.85
    assert cfg.manual_threshold == 0.5


def test_helper_methods():
    """辅助判断方法正确。"""
    cfg = Config({"owner_users": ["111"], "protected_users": ["222"]})
    assert cfg.is_owner("111") is True
    assert cfg.is_owner("222") is False
    assert cfg.is_protected("222") is True
    assert cfg.is_protected("111") is False
    assert cfg.is_friendly("111") is True


def test_coerce_from_object():
    """从非 dict 对象（有 get 方法）正确读取。"""

    class MockConfig:
        def __init__(self, data):
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

    mock = MockConfig({"owner_users": ["999"], "max_mute_seconds": 100})
    cfg = Config(mock)
    assert cfg.owner_users == ["999"]
    assert cfg.max_mute_seconds == 100
