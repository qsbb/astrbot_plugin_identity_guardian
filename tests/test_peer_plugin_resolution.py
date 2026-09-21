"""回归：序 解析其他插件实例时必须兼容 AstrBot 官方 API。

背景：AstrBot 4.x 没有 ``get_star_instance``；官方入口是
``get_registered_star(name)``，运行实例挂在 ``StarMetadata.star_cls``。
本测试锁死该回退，避免联动再次静默失效。
"""

from __future__ import annotations

import types

import astrbot_plugin_identity_guardian.main as _module


def _plugin_cls():
    for name in dir(_module):
        candidate = getattr(_module, name)
        if isinstance(candidate, type) and hasattr(candidate, "_resolve_peer_plugin"):
            return candidate
    raise AssertionError("找不到插件类")


class _StarMeta:
    def __init__(self, instance):
        self.star_cls = instance


class _OfficialOnlyContext:
    """只有官方 API 的上下文（没有 get_star_instance）。"""

    def __init__(self, registry):
        self._registry = registry

    def get_registered_star(self, name):
        return self._registry.get(name)


def _plugin(context):
    plugin = _plugin_cls().__new__(_plugin_cls())
    plugin.context = context
    return plugin


def test_peer_lookup_via_official_registry():
    peer = types.SimpleNamespace(name="peer")
    plugin = _plugin(_OfficialOnlyContext({"peer-plugin": _StarMeta(peer)}))
    assert getattr(plugin, "_resolve_peer_plugin")("peer-plugin") is peer


def test_peer_lookup_skips_class_values():
    class NotAnInstance:
        pass

    plugin = _plugin(_OfficialOnlyContext({"peer-plugin": _StarMeta(NotAnInstance)}))
    assert getattr(plugin, "_resolve_peer_plugin")("peer-plugin") is None


def test_peer_lookup_accepts_legacy_shortcut():
    peer = types.SimpleNamespace(name="peer")

    class _LegacyContext(_OfficialOnlyContext):
        def get_star_instance(self, name):
            return peer

    plugin = _plugin(_LegacyContext({}))
    assert getattr(plugin, "_resolve_peer_plugin")("peer-plugin") is peer


def test_peer_lookup_missing_registry_returns_empty():
    plugin = _plugin(_OfficialOnlyContext({}))
    assert getattr(plugin, "_resolve_peer_plugin")("peer-plugin") is None
