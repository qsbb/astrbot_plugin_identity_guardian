"""main.py 事件处理器的形参错位防护与版本一致性回归测试。

覆盖三个真实故障：
1. 热重载残留 partial 套娃时，event 形参会收到插件实例本身，
   表现为 ``'IdentityGuardianPlugin' object has no attribute 'get_platform_name'``。
2. ``_unwrap_registry_handlers`` 曾误用 ``registry.handlers`` 与 ``handler.full_name``
   两个上游不存在的字段名，导致整个防护静默失效。
3. ``metadata.yaml`` 的 version 与 ``__init__.__version__`` 漂移，
   使远端版本号读取不到新版本。
"""

import functools
import importlib.util
import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT.parent))


class RegisteringCommandable:
    """模拟 AstrBot 指令组装饰器：command_group 返回带 .command 的对象。"""

    def command(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


def _install_command_group_stub():
    """conftest 的 command_group 桩返回裸函数，缺少 .command，这里补齐。"""
    from astrbot.api.event import filter as api_filter

    def command_group(*args, **kwargs):
        def decorator(func):
            return RegisteringCommandable()

        return decorator

    api_filter.command_group = command_group


def _load_main():
    """以包形式导入 main，使其内部相对导入可用。"""
    _install_command_group_stub()
    if "astrbot_plugin_identity_guardian.main" in sys.modules:
        del sys.modules["astrbot_plugin_identity_guardian.main"]
    spec = importlib.util.find_spec("astrbot_plugin_identity_guardian.main")
    if spec is None:  # pragma: no cover - 环境异常
        pytest.skip("无法定位 main 模块")
    return importlib.import_module("astrbot_plugin_identity_guardian.main")


main = _load_main()


class FakeEvent:
    """最小 event 桩：只需具备 get_platform_name 以通过鸭子类型识别。"""

    def __init__(self, platform="aiocqhttp"):
        self._platform = platform

    def get_platform_name(self):
        return self._platform


class FakeRequest:
    """最小 ProviderRequest 桩。"""

    def __init__(self):
        self.system_prompt = ""


def plugin_instance():
    """不走 __init__ 造一个实例，避免拉起全部服务依赖。"""
    return main.IdentityGuardianPlugin.__new__(main.IdentityGuardianPlugin)


# ----------------------------------------------------------- 形参错位识别


def test_resolve_event_picks_event_from_first_position():
    event = FakeEvent()
    assert main._resolve_event(event) is event


def test_resolve_event_skips_plugin_instance_and_finds_shifted_event():
    """partial 套娃后 event 形参拿到插件实例，真正的 event 被挤进 args。"""
    plugin = plugin_instance()
    event = FakeEvent()

    # 模拟 raw(旧实例, 新实例, event) 的错位调用
    assert main._resolve_event(plugin, plugin, event) is event


def test_resolve_event_returns_none_when_no_event_present():
    plugin = plugin_instance()
    assert main._resolve_event(plugin, None, "not-an-event") is None


def test_resolve_event_ignores_objects_without_platform_accessor():
    assert main._resolve_event(object(), SimpleNamespace()) is None


def test_resolve_llm_request_skips_event_and_plugin():
    plugin = plugin_instance()
    event = FakeEvent()
    req = FakeRequest()

    assert main._resolve_llm_request(plugin, event, req) is req


def test_resolve_llm_request_returns_none_without_request_like_object():
    plugin = plugin_instance()
    assert main._resolve_llm_request(plugin, FakeEvent()) is None


# ------------------------------------------------ registry partial 拆解


class FakeHandler:
    """按上游 StarHandlerMetadata 的真实字段名构造。"""

    def __init__(self, handler, full_name, module_path=""):
        self.handler = handler
        self.handler_full_name = full_name
        self.handler_module_path = module_path


class FakeRegistry:
    """上游用 _handlers 存列表，而不是 handlers。"""

    def __init__(self, handlers):
        self._handlers = handlers

    def __iter__(self):
        return iter(self._handlers)


def raw_handler(self, event):  # pragma: no cover - 仅作为拆解目标
    return event


def test_unwrap_reads_upstream_private_handlers_field():
    """必须能读到 _handlers；旧实现只认 handlers，导致防护从未生效。"""
    plugin = plugin_instance()
    plugin.logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None
    )
    stale = functools.partial(raw_handler, "旧实例")
    handler = FakeHandler(
        stale,
        "data.plugins.astrbot_plugin_identity_guardian.main_on_event",
    )
    registry = FakeRegistry([handler])

    plugin._apply_unwrap(registry)

    assert handler.handler is raw_handler


def test_unwrap_matches_by_handler_full_name(monkeypatch):
    """旧实现误用 full_name 取到空串，本插件 handler 全部匹配不上。"""
    plugin = plugin_instance()
    plugin.logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None
    )
    ours = FakeHandler(
        functools.partial(raw_handler, "旧实例"),
        "data.plugins.astrbot_plugin_identity_guardian.main_on_event",
    )
    foreign = functools.partial(raw_handler, "别人的实例")
    other = FakeHandler(foreign, "data.plugins.some_other_plugin.main_on_event")

    plugin._apply_unwrap(FakeRegistry([ours, other]))

    assert ours.handler is raw_handler
    # 不能碰其他插件的 handler
    assert other.handler is foreign


def test_unwrap_collapses_nested_partials():
    """套娃可能不止一层，必须一路剥到原始函数。"""
    plugin = plugin_instance()
    plugin.logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None
    )
    nested = functools.partial(functools.partial(raw_handler, "旧实例"), "更旧实例")
    handler = FakeHandler(
        nested,
        "data.plugins.astrbot_plugin_identity_guardian.main_on_event",
    )

    plugin._apply_unwrap(FakeRegistry([handler]))

    assert handler.handler is raw_handler


def test_unwrap_matches_by_module_path_when_full_name_missing():
    plugin = plugin_instance()
    plugin.logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None
    )
    handler = FakeHandler(
        functools.partial(raw_handler, "旧实例"),
        "",
        module_path="data.plugins.astrbot_plugin_identity_guardian.main",
    )

    plugin._apply_unwrap(FakeRegistry([handler]))

    assert handler.handler is raw_handler


def test_unwrap_leaves_plain_function_untouched():
    plugin = plugin_instance()
    plugin.logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None
    )
    handler = FakeHandler(
        raw_handler,
        "data.plugins.astrbot_plugin_identity_guardian.main_on_event",
    )

    plugin._apply_unwrap(FakeRegistry([handler]))

    assert handler.handler is raw_handler


# ----------------------------------------------------------- 版本一致性


def test_metadata_version_matches_package_version():
    """metadata.yaml 是远端版本号的唯一来源，漂移会导致更新检查失效。"""
    import re

    metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)", metadata, re.MULTILINE)
    assert match, "metadata.yaml 缺少 version 字段"

    init_text = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert init_match, "__init__.py 缺少 __version__"

    assert match.group(1) == init_match.group(1)


# ----------------------------------------------------------- 管理命令安全


def test_sensitive_idg_commands_require_admin_permission():
    tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
    sensitive = {
        "idg_status",
        "idg_stop",
        "idg_resume",
        "idg_reset_breaker",
        "idg_refresh",
        "idg_approve",
        "idg_reject",
    }
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in sensitive:
            continue
        found.add(node.name)
        names = [ast.unparse(item) for item in node.decorator_list]
        assert any("permission_type" in name and "ADMIN" in name for name in names)
    assert found == sensitive


class ApprovalEvent:
    def __init__(self, group_id="100"):
        self.group_id = group_id

    def get_group_id(self):
        return self.group_id

    @staticmethod
    def plain_result(text):
        return text


def test_approval_is_bound_to_original_group_and_keeps_pending_entry():
    plugin = plugin_instance()
    plugin.confirm = main.ConfirmService()
    confirm_id = plugin.confirm.create("kick_member", {"user_id": "9"}, "100", "9")

    result = asyncio.run(plugin._approve_pending_action(ApprovalEvent("200"), confirm_id))

    assert "创建它的群聊" in result
    assert plugin.confirm.get(confirm_id) is not None


def test_approval_rechecks_live_policy_before_consuming(monkeypatch):
    plugin = plugin_instance()
    plugin.confirm = main.ConfirmService()
    confirm_id = plugin.confirm.create("kick_member", {"user_id": "9"}, "100", "9")
    plugin._stopped = False
    plugin.config = SimpleNamespace(enable_api_guard=False)
    plugin.cooldown = SimpleNamespace(check_breaker=lambda: False)

    async def get_actor(event, target_id):
        return SimpleNamespace(group_id="100")

    plugin._get_actor = get_actor
    plugin.policy = SimpleNamespace(
        evaluate=lambda *args: main.ActionDecision(
            allowed=False, action="kick_member", reason="权限已变化"
        )
    )
    monkeypatch.setattr(plugin, "_execute_action", lambda *args: None)

    result = asyncio.run(plugin._approve_pending_action(ApprovalEvent(), confirm_id))

    assert "重新校验未通过" in result
    assert plugin.confirm.get(confirm_id) is not None


def test_rejection_is_bound_to_original_group():
    plugin = plugin_instance()
    plugin.confirm = main.ConfirmService()
    confirm_id = plugin.confirm.create("kick_member", {"user_id": "9"}, "100", "9")
    main.IdentityGuardianPlugin._current_instance = plugin
    try:
        output = asyncio.run(
            _collect_async_generator(
                plugin.idg_reject(ApprovalEvent("200"), confirm_id)
            )
        )
    finally:
        main.IdentityGuardianPlugin._current_instance = None

    assert "创建它的群聊" in output[0]
    assert plugin.confirm.get(confirm_id) is not None


async def _collect_async_generator(generator):
    return [item async for item in generator]
