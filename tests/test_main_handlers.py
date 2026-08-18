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
import json
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


def test_proactive_authorization_is_exact_allowlisted_private_target():
    plugin = plugin_instance()
    plugin.config = main.Config(
        {
            "proactive_delivery_targets": [
                "aiocqhttp:FriendMessage:owner-1",
                "telegram:PrivateMessage:owner-2",
            ]
        }
    )
    plugin._stopped = False

    contract = plugin.proactive_delivery_authorization_contract()
    assert contract["name"] == "identity.proactive_authorization"
    assert contract["version"] == "1.0"
    assert contract["cross_platform_inheritance"] is False

    allowed = plugin.authorize_proactive_delivery("aiocqhttp:FriendMessage:owner-1")
    assert allowed["authorized"] is True
    assert allowed["channel"] == "private"
    assert allowed["owner_confirmed"] is True

    assert (
        plugin.authorize_proactive_delivery("aiocqhttp:FriendMessage:someone-else")[
            "reason"
        ]
        == "target_not_authorized"
    )
    assert (
        plugin.authorize_proactive_delivery("aiocqhttp:GroupMessage:owner-1")["reason"]
        == "private_target_required"
    )
    assert (
        plugin.authorize_proactive_delivery("telegram:ChannelMessage:owner-2")["reason"]
        == "private_target_required"
    )
    for invalid_target in (
        ":PrivateMessage:",
        "telegram:FriendRequest:owner-2",
        "telegram:IndirectMessage:owner-2",
        "telegram:PrivateMessage:",
    ):
        assert (
            plugin.authorize_proactive_delivery(invalid_target)["reason"]
            == "private_target_required"
        )

    plugin.config = main.Config(
        {"enabled": False, "proactive_delivery_targets": ["x:PrivateMessage:y"]}
    )
    assert (
        plugin.authorize_proactive_delivery("x:PrivateMessage:y")["reason"]
        == "plugin_disabled"
    )


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

    result = asyncio.run(
        plugin._approve_pending_action(ApprovalEvent("200"), confirm_id)
    )

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


def test_notify_only_notifies_even_when_answer_is_correct():
    """notify_only 不自动放行时，高置信度正确结论也必须交给人工。"""
    plugin = plugin_instance()
    plugin.config = SimpleNamespace(
        join_audit_mode="notify_only",
        audit_notify_targets=["aiocqhttp:GroupMessage:100"],
    )
    decision = SimpleNamespace(verdict="correct", confidence=0.95, reason="回答正确")

    class StubJoinAudit:
        async def handle_request(self, event, raw):
            return decision

        @staticmethod
        def should_auto_approve(result):
            return False

    plugin.join_audit = StubJoinAudit()
    plugin._ensure_llm_caller = lambda: None
    notified = []

    async def record_notification(event, raw, result):
        notified.append((event, raw, result))

    plugin._notify_audit_targets = record_notification
    plugin.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    event = ApprovalEvent()
    raw = {"request_type": "group", "group_id": "100", "user_id": "9"}

    asyncio.run(plugin._handle_request(event, raw))

    assert notified == [(event, raw, decision)]


async def _collect_async_generator(generator):
    return [item async for item in generator]


# ----------------------------------------------------- S2 管理员变更事件


@pytest.mark.parametrize("sub_type", ["set", "unset"])
def test_group_admin_notice_clears_identity_cache(sub_type):
    """OneBot V11 管理员变更事件是 notice_type=group_admin（set/unset）。"""
    plugin = plugin_instance()
    cleared = []
    plugin.identity = SimpleNamespace(clear_cache=lambda: cleared.append("clear"))
    plugin.logger = SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None
    )
    raw = {
        "notice_type": "group_admin",
        "sub_type": sub_type,
        "group_id": "100",
        "user_id": "9",
    }

    asyncio.run(plugin._handle_notice(ApprovalEvent(), raw))

    assert cleared == ["clear"]


def test_legacy_notify_group_admin_change_still_clears_cache():
    """旧的 notify/group_admin_change 匹配作为冗余兼容分支保留。"""
    plugin = plugin_instance()
    cleared = []
    plugin.identity = SimpleNamespace(clear_cache=lambda: cleared.append("clear"))
    plugin.logger = SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None
    )
    raw = {
        "notice_type": "notify",
        "sub_type": "group_admin_change",
        "group_id": "100",
        "user_id": "9",
    }

    asyncio.run(plugin._handle_notice(ApprovalEvent(), raw))

    assert cleared == ["clear"]


# ----------------------------------------------------- S5 二次确认事务语义


def _armed_approval_plugin():
    """构造一个全部校验可通过、只剩平台执行待桩接的审批场景。"""
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
            allowed=True, action="kick_member", params={"user_id": "9"}
        )
    )
    return plugin, confirm_id


def test_confirm_claim_has_single_winner_and_release_allows_retry():
    service = main.ConfirmService()
    confirm_id = service.create("kick_member", {"user_id": "9"}, "100", "9")

    first = service.claim(confirm_id)
    second = service.claim(confirm_id)
    assert first is not None
    assert second is None

    # 失败释放后可再次占位；成功 finish 后记录删除、不可再占位。
    assert service.release(confirm_id) is not None
    assert service.get(confirm_id).status == "pending"
    assert service.claim(confirm_id) is not None
    assert service.finish(confirm_id) is not None
    assert service.get(confirm_id) is None
    assert service.claim(confirm_id) is None

    # 占位中的条目仍可被拒绝（管理员明确撤销）。
    third = service.create("kick_member", {"user_id": "9"}, "100", "9")
    assert service.claim(third) is not None
    assert service.reject(third) is not None
    assert service.get(third) is None


def test_approval_failure_releases_entry_and_reports_real_reason():
    """平台失败后不再显示「已批准」，确认单保留可重试。"""
    plugin, confirm_id = _armed_approval_plugin()

    async def failing_execute(event, decision, target_id):
        return "执行失败：OneBot 连接超时", False

    plugin._execute_action_result = failing_execute

    result = asyncio.run(plugin._approve_pending_action(ApprovalEvent(), confirm_id))

    assert "已批准" not in result
    assert "执行失败：OneBot 连接超时" in result
    entry = plugin.confirm.get(confirm_id)
    assert entry is not None
    assert entry.status == "pending"

    # 排除故障后重试成功，记录才被删除。
    async def ok_execute(event, decision, target_id):
        return "已执行 kick_member。", True

    plugin._execute_action_result = ok_execute

    retry = asyncio.run(plugin._approve_pending_action(ApprovalEvent(), confirm_id))

    assert "已批准 kick_member" in retry
    assert "已执行 kick_member" in retry
    assert plugin.confirm.get(confirm_id) is None


def test_concurrent_approval_executes_platform_action_only_once():
    """并发审批同一确认单：claim 只有一个赢家，平台动作只执行一次。"""
    plugin, confirm_id = _armed_approval_plugin()
    executions = []

    async def recording_execute(event, decision, target_id):
        await asyncio.sleep(0)  # 让另一个协程有机会进入 claim 竞争
        executions.append(decision.action)
        return "已执行 kick_member。", True

    plugin._execute_action_result = recording_execute

    async def run_both():
        return await asyncio.gather(
            plugin._approve_pending_action(ApprovalEvent(), confirm_id),
            plugin._approve_pending_action(ApprovalEvent(), confirm_id),
        )

    results = asyncio.run(run_both())

    assert executions == ["kick_member"]
    assert any("已批准 kick_member" in item for item in results)
    assert any("已被处理" in item for item in results)
    assert plugin.confirm.get(confirm_id) is None


# ----------------------------------------------------- 入群申请事件驱动推送


class RequestEvent:
    """最小 request 事件桩。"""

    def __init__(self, platform="aiocqhttp"):
        self._platform = platform

    def get_platform_name(self):
        return self._platform


def _review_result(outcome, request=None, decision=None):
    return SimpleNamespace(outcome=outcome, request=request, decision=decision)


def _push_wired_plugin(
    *, outcome="pending_review", push_group_ids=("300",), style="formatted"
):
    """组装 _handle_request 事件链路桩：runtime 返回固定 outcome。"""
    plugin = plugin_instance()
    plugin._stopped = False
    plugin.context = None
    plugin.config = SimpleNamespace(enable_api_guard=False, join_audit_mode="off")
    plugin.cooldown = SimpleNamespace(check_breaker=lambda: False)
    plugin.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    plugin._ensure_llm_caller = lambda: None
    request = SimpleNamespace(
        request_id="r1",
        platform_id="qq-main",
        group_id="100",
        user_id="200",
        nickname="小明",
    )
    decision = SimpleNamespace(verdict="correct", confidence=0.9, reason="答案正确")
    result = _review_result(outcome, request, decision)

    class StubRuntime:
        async def handle_event(self, event, raw):
            return result

    plugin.join_review = StubRuntime()
    plugin.join_review_store = SimpleNamespace(
        get_group_config=lambda platform_id, group_id: _async_value(
            SimpleNamespace(
                push_group_ids=list(push_group_ids),
                push_style=style,
                include_answer=True,
            )
        )
    )
    return plugin, request, decision


async def _async_value(value):
    return value


def test_pending_review_triggers_push_with_persona_llm_caller():
    """pending_review：按群配置推送，人格按申请所属群 UMO 取，decision 透传，natural 走带人格 LLM。"""
    plugin, request, decision = _push_wired_plugin(style="natural")
    persona_calls = []
    llm_calls = []
    push_calls = []

    async def persona(umo):
        persona_calls.append(umo)
        return "人格 prompt"

    async def push_llm(prompt, system_prompt=""):
        llm_calls.append((prompt, system_prompt))
        return "文案"

    class StubPush:
        async def push_for_request(
            self, context, req, config, llm_caller, logger, push_decision=None
        ):
            assert req is request
            assert config.push_style == "natural"
            assert push_decision is decision
            push_calls.append(config.push_group_ids)
            assert await llm_caller("p") == "文案"
            return ["300"], [], []

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = push_llm
    plugin.request_push = StubPush()

    asyncio.run(plugin._handle_request(RequestEvent(), {"request_type": "group"}))

    assert persona_calls == ["qq-main:GroupMessage:100"]
    assert llm_calls == [("p", "人格 prompt")]
    assert push_calls == [["300"]]


def test_non_pending_outcomes_do_not_push():
    """auto_approved / ignored / left_on_platform 不触发推送。"""
    for outcome in ("auto_approved", "ignored", "left_on_platform"):
        plugin, _, _ = _push_wired_plugin(outcome=outcome)

        class StubPush:
            async def push_for_request(self, *args):
                raise AssertionError("不应推送")

        plugin.request_push = StubPush()
        asyncio.run(plugin._handle_request(RequestEvent(), {"request_type": "group"}))


def test_empty_push_groups_still_push_with_source_group_fallback():
    """推送群留空不再静默：交给推送服务回退到申请所属群。"""
    plugin, _, _ = _push_wired_plugin(push_group_ids=())
    push_calls = []

    async def persona(umo):
        return ""

    class StubPush:
        async def push_for_request(
            self, context, req, config, llm_caller, logger, push_decision=None
        ):
            push_calls.append(list(config.push_group_ids))
            return ["100"], [], []

    plugin._get_push_persona_prompt = persona
    plugin.request_push = StubPush()
    asyncio.run(plugin._handle_request(RequestEvent(), {"request_type": "group"}))

    assert push_calls == [[]]


def test_guard_stop_skips_push():
    """紧急停止时不推送，也不影响审核主流程。"""
    plugin, _, _ = _push_wired_plugin()
    plugin._stopped = True

    class StubPush:
        async def push_for_request(self, *args):
            raise AssertionError("不应推送")

    plugin.request_push = StubPush()
    asyncio.run(plugin._handle_request(RequestEvent(), {"request_type": "group"}))


def test_push_failure_does_not_break_review_flow():
    """推送服务抛异常只记日志，不向事件处理传播。"""
    plugin, _, _ = _push_wired_plugin()

    async def persona(umo):
        return ""

    class FailingPush:
        async def push_for_request(self, *args):
            raise RuntimeError("send pipeline down")

    plugin._get_push_persona_prompt = persona
    plugin.request_push = FailingPush()
    # 不抛异常即通过
    asyncio.run(plugin._handle_request(RequestEvent(), {"request_type": "group"}))


# ----------------------------------------------------- 引用回复审批


class ReplyChain:
    """MessageChain 桩：记录纯文本链内容。"""

    def __init__(self, chain=None):
        self.chain = list(chain or [])


class ReplyPlain:
    def __init__(self, text=""):
        self.text = text


# _send_group_reply 延迟导入 astrbot.api 的 MessageChain / Plain，
# conftest 注册的桩模块缺少这两个符号，这里补齐。
sys.modules["astrbot.api.event"].MessageChain = ReplyChain
sys.modules["astrbot.api.message_components"].Plain = ReplyPlain


class GroupMessageEvent:
    """最小群消息事件桩。"""

    def __init__(self, platform_id="qq-main", self_id="999"):
        self._platform_id = platform_id
        self._self_id = self_id
        self.stopped = False

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return self._platform_id

    def get_self_id(self):
        return self._self_id

    def stop_event(self):
        self.stopped = True


class ReplyContext:
    """记录 AstrBot send_message 的上下文桩。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


def _reply_raw(quoted="1001", text="同意", group_id="300", user_id="500"):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "message": [
            {"type": "reply", "data": {"id": quoted}},
            {"type": "text", "data": {"text": text}},
        ],
    }
    if quoted is None:
        raw["message"] = [{"type": "text", "data": {"text": text}}]
    return raw


def _reply_wired_plugin(
    *,
    role="admin",
    llm_output='{"decision":"approve"}',
    request_status="pending",
    owner_users=(),
    tracked=True,
):
    """组装 _handle_push_reply 链路桩。返回 (plugin, request, 观察字典)。"""
    plugin = plugin_instance()
    plugin._stopped = False
    plugin.config = SimpleNamespace(enabled=True, owner_users=list(owner_users))
    plugin.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    plugin.context = ReplyContext()
    request = SimpleNamespace(
        request_id="r1",
        platform_id="qq-main",
        group_id="100",
        user_id="200",
        status=request_status,
        platform_error="",
    )
    observed = {"find": [], "llm": [], "process": []}

    async def find_ref(platform_id, group_id, message_id):
        observed["find"].append((platform_id, group_id, message_id))
        return request if tracked else None

    plugin.join_review_store = SimpleNamespace(find_request_by_push_ref=find_ref)

    async def get_role(event, group_id, user_id):
        return role

    plugin.identity = SimpleNamespace(get_role=get_role)

    async def audit_llm(prompt):
        observed["llm"].append(prompt)
        return llm_output

    plugin._call_audit_llm = audit_llm

    class StubRuntime:
        async def process_request(self, context, request_id, *, approve, reason=""):
            observed["process"].append((request_id, approve, reason))
            return SimpleNamespace(
                status="approved" if approve else "rejected", platform_error=""
            )

    plugin.join_review = StubRuntime()
    return plugin, request, observed


def _run_reply(plugin, raw=None, event=None):
    raw = raw if raw is not None else _reply_raw()
    event = event or GroupMessageEvent()
    return asyncio.run(plugin._handle_push_reply(event, raw))


def _sent_texts(plugin):
    return [chain.chain[0].text for _, chain in plugin.context.sent]


def test_reply_without_quote_is_ignored():
    """普通群消息（无引用段）不进入审批链路。"""
    plugin, _, observed = _reply_wired_plugin()

    _run_reply(plugin, _reply_raw(quoted=None))

    assert observed["find"] == []
    assert plugin.context.sent == []


def test_reply_to_untracked_message_is_ignored():
    """引用的是未追踪消息：静默忽略。"""
    plugin, _, observed = _reply_wired_plugin(tracked=False)

    _run_reply(plugin)

    assert observed["find"] == [("qq-main", "300", "1001")]
    assert observed["llm"] == []
    assert plugin.context.sent == []


def test_reply_from_plain_member_is_silent():
    """普通成员引用回复：无权限，静默不处理。"""
    plugin, _, observed = _reply_wired_plugin(role="member")

    _run_reply(plugin)

    assert observed["llm"] == []
    assert observed["process"] == []
    assert plugin.context.sent == []


def test_owner_user_bypasses_role_check():
    """bot 主人即使不是群管理也可审批。"""
    plugin, _, observed = _reply_wired_plugin(role="member", owner_users=("500",))

    _run_reply(plugin)

    assert observed["process"] == [("r1", True, "")]


def test_reply_for_processed_request_gets_notice():
    """申请已终态：回复「该申请已被处理」，不再走 LLM 判断。"""
    plugin, _, observed = _reply_wired_plugin(request_status="approved")

    _run_reply(plugin)

    assert observed["llm"] == []
    assert observed["process"] == []
    assert [umo for umo, _ in plugin.context.sent] == ["qq-main:GroupMessage:300"]
    assert _sent_texts(plugin) == ["该申请已被处理。"]


@pytest.mark.parametrize(
    "llm_output",
    ['{"decision":"unclear"}', "看不懂的回复", '{"decision":"approve"', "{}"],
)
def test_unclear_or_malformed_judgement_is_silent(llm_output):
    """LLM 判断含糊或输出无法解析：静默，不打扰群。"""
    plugin, _, observed = _reply_wired_plugin(llm_output=llm_output)

    _run_reply(plugin)

    assert observed["llm"]  # 确实调用了 LLM 判断
    assert observed["process"] == []
    assert plugin.context.sent == []


def test_approve_reply_processes_and_confirms():
    """同意：走 process_request 并在群里确认结果。"""
    plugin, _, observed = _reply_wired_plugin(llm_output='{"decision":"approve"}')

    _run_reply(plugin)

    assert observed["process"] == [("r1", True, "")]
    assert _sent_texts(plugin) == ["已同意 QQ 200 的入群申请。"]


def test_reject_reply_processes_with_fixed_reason():
    """拒绝：带固定文案 reason 走 process_request。"""
    plugin, _, observed = _reply_wired_plugin(llm_output='{"decision":"reject"}')

    _run_reply(plugin)

    assert observed["process"] == [("r1", False, "管理员群内拒绝")]
    assert _sent_texts(plugin) == ["已拒绝 QQ 200 的入群申请。"]


def test_not_actionable_reply_reports_processed():
    """审批竞争失败：回复已被其他管理员处理或已过期。"""
    # 与 main.py 包内导入保持同一模块对象，保证 except 能捕获。
    from astrbot_plugin_identity_guardian.core.join_review_store import (
        RequestNotActionable,
    )

    plugin, _, _ = _reply_wired_plugin()

    class BusyRuntime:
        async def process_request(self, context, request_id, *, approve, reason=""):
            raise RequestNotActionable("busy")

    plugin.join_review = BusyRuntime()
    _run_reply(plugin)

    assert _sent_texts(plugin) == ["该申请已被其他管理员处理或已过期。"]


def test_platform_failure_reply_reports_reason():
    """平台失败：回复失败原因并引导到管理页。"""
    plugin, _, _ = _reply_wired_plugin()

    class FailingRuntime:
        async def process_request(self, context, request_id, *, approve, reason=""):
            return SimpleNamespace(
                status="platform_error", platform_error="permission_denied"
            )

    plugin.join_review = FailingRuntime()
    _run_reply(plugin)

    assert _sent_texts(plugin) == ["处理失败：permission_denied，请到管理页处理。"]


def test_concurrent_replies_have_single_winner():
    """两个管理员同时回复：process_request 的一次性语义保证只有一个生效。"""
    from astrbot_plugin_identity_guardian.core.join_review_store import (
        RequestNotActionable,
    )

    plugin, _, observed = _reply_wired_plugin()
    state = {"won": False}

    async def process_request(context, request_id, *, approve, reason=""):
        await asyncio.sleep(0)  # 让出事件循环，制造交错
        observed["process"].append((request_id, approve, reason))
        if state["won"]:
            raise RequestNotActionable("busy")
        state["won"] = True
        return SimpleNamespace(status="approved", platform_error="")

    plugin.join_review = SimpleNamespace(process_request=process_request)

    async def both():
        await asyncio.gather(
            plugin._handle_push_reply(GroupMessageEvent(), _reply_raw(user_id="500")),
            plugin._handle_push_reply(GroupMessageEvent(), _reply_raw(user_id="501")),
        )

    # 第二位回复者也需要权限：role=admin 对所有人生效
    asyncio.run(both())

    assert observed["process"] == [("r1", True, "")] * 2
    texts = sorted(_sent_texts(plugin))
    assert texts == ["已同意 QQ 200 的入群申请。", "该申请已被其他管理员处理或已过期。"]


def test_on_event_dispatches_group_message_to_push_reply():
    """on_event 把 group message 事件分发到 _handle_push_reply。"""
    plugin, _, observed = _reply_wired_plugin()
    event = GroupMessageEvent()
    event.message_obj = SimpleNamespace(raw_message=_reply_raw())

    main.IdentityGuardianPlugin._current_instance = plugin
    try:
        asyncio.run(plugin.on_event(event))
    finally:
        main.IdentityGuardianPlugin._current_instance = None

    assert observed["process"] == [("r1", True, "")]
    assert _sent_texts(plugin) == ["已同意 QQ 200 的入群申请。"]


def test_result_reply_consumes_event_and_stops_main_conversation():
    """发送审批结果回复前 stop_event：引用回复不再进入主对话 LLM。"""
    plugin, _, _ = _reply_wired_plugin(llm_output='{"decision":"approve"}')
    event = GroupMessageEvent()

    _run_reply(plugin, event=event)

    assert event.stopped is True
    assert _sent_texts(plugin) == ["已同意 QQ 200 的入群申请。"]


def test_unclear_judgement_does_not_consume_event():
    """含糊回复静默放行：不消费事件，也不发送结果回复。"""
    plugin, _, observed = _reply_wired_plugin(llm_output='{"decision":"unclear"}')
    event = GroupMessageEvent()

    _run_reply(plugin, event=event)

    assert observed["llm"]  # 确实走了 LLM 判断
    assert event.stopped is False
    assert plugin.context.sent == []


def test_bot_own_reply_echo_is_ignored():
    """bot 自己引用推送消息的回显直接忽略，不进入审批链路。"""
    plugin, _, observed = _reply_wired_plugin()
    event = GroupMessageEvent(self_id="999")

    _run_reply(plugin, _reply_raw(user_id="999"), event)

    assert observed["find"] == []
    assert observed["llm"] == []
    assert plugin.context.sent == []
    assert event.stopped is False


def _preview_wired_plugin():
    """组装 _simulate_push_preview 所需的最小插件桩。"""
    plugin = plugin_instance()
    plugin.config = SimpleNamespace(push_llm_provider="push-llm")
    plugin.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    return plugin


def _preview_config(style="natural"):
    return SimpleNamespace(push_style=style, include_answer=True)


def _preview_decision():
    return SimpleNamespace(verdict="uncertain", confidence=0.3, reason="不像")


def test_get_recent_group_contexts_filters_and_caps_at_limit():
    """会话历史是 JSON 字符串；只留 role/content 均为字符串的消息，上限 10 条。"""
    plugin = _preview_wired_plugin()
    history = [
        *[{"role": "user", "content": f"消息{i}"} for i in range(12)],
        {"role": "user", "content": [{"type": "text", "text": "多模态"}]},
        {"role": "user"},
        "不是字典",
    ]
    conversation = SimpleNamespace(history=json.dumps(history))
    plugin.context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=lambda umo: _async_value("cid-1"),
            get_conversation=lambda umo, cid: _async_value(conversation),
        )
    )

    contexts = asyncio.run(plugin._get_recent_group_contexts("qq:GroupMessage:100"))

    assert [c["content"] for c in contexts] == [f"消息{i}" for i in range(2, 12)]


def test_get_recent_group_contexts_silently_empty_without_conversation():
    """无会话/无 conversation_manager 时静默返回空列表。"""
    plugin = _preview_wired_plugin()
    plugin.context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=lambda umo: _async_value(None),
        )
    )
    assert asyncio.run(plugin._get_recent_group_contexts("qq:GroupMessage:100")) == []

    plugin.context = SimpleNamespace()
    assert asyncio.run(plugin._get_recent_group_contexts("qq:GroupMessage:100")) == []


def test_simulate_push_preview_natural_uses_persona_and_contexts():
    """natural 预览：人格 system prompt 与近期群消息 contexts 透传给 push LLM。"""
    plugin = _preview_wired_plugin()
    history = [{"role": "user", "content": "刚聊的话题"}]
    conversation = SimpleNamespace(history=json.dumps(history), persona_id="p")
    plugin.context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=lambda umo: _async_value("cid-1"),
            get_conversation=lambda umo, cid: _async_value(conversation),
        )
    )
    persona_calls, llm_calls = [], []

    async def persona(umo):
        persona_calls.append(umo)
        return "人格 prompt"

    async def push_llm(prompt, system_prompt="", contexts=None):
        llm_calls.append((prompt, system_prompt, contexts))
        return "人格化预览文案"

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = push_llm

    preview = asyncio.run(
        plugin._simulate_push_preview(
            platform_id="qq-main",
            group_id="100",
            question="口令？",
            answer="小河",
            config=_preview_config(),
            decision=_preview_decision(),
            source_group_name="申请群",
        )
    )

    assert preview["style"] == "natural"
    assert preview["text"] == "人格化预览文案"
    assert preview["opinion_source"] == "llm"
    assert preview["persona_used"] is True
    assert preview["provider"] == "push-llm"
    assert preview["contexts_used"] == 1
    assert persona_calls == ["qq-main:GroupMessage:100"]
    prompt, system_prompt, contexts = llm_calls[0]
    assert "口令？" in prompt and "小河" in prompt
    assert system_prompt == "人格 prompt"
    assert contexts == [{"role": "user", "content": "刚聊的话题"}]


def test_simulate_push_preview_formatted_uses_llm_opinion():
    """formatted 样式预览同样调 push LLM 拿一句话看法（只调一次）。"""
    plugin = _preview_wired_plugin()
    plugin.context = SimpleNamespace(conversation_manager=None)
    llm_calls = []

    async def persona(umo):
        return ""

    async def push_llm(prompt, system_prompt="", contexts=None):
        llm_calls.append((prompt, system_prompt, contexts))
        return "答案靠谱，正是群内暗号"

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = push_llm

    preview = asyncio.run(
        plugin._simulate_push_preview(
            platform_id="qq-main",
            group_id="100",
            question="口令？",
            answer="小河",
            config=_preview_config(style="formatted"),
            decision=_preview_decision(),
            source_group_name="申请群",
        )
    )

    assert preview["style"] == "formatted"
    assert preview["opinion_source"] == "llm"
    assert "看法：答案靠谱，正是群内暗号" in preview["text"]
    assert "模拟用户" in preview["text"]
    assert "回复『同意』" in preview["text"]
    assert len(llm_calls) == 1
    prompt, _, _ = llm_calls[0]
    assert "口令？" in prompt and "小河" in prompt


def test_simulate_push_preview_formatted_llm_empty_falls_back_to_decision():
    """formatted 样式 LLM 看法为空：回退自动审核结论行并标注 decision。"""
    plugin = _preview_wired_plugin()
    plugin.context = SimpleNamespace(conversation_manager=None)

    async def persona(umo):
        return ""

    async def push_llm(prompt, system_prompt="", contexts=None):
        return ""

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = push_llm

    preview = asyncio.run(
        plugin._simulate_push_preview(
            platform_id="qq-main",
            group_id="100",
            question="口令？",
            answer="小河",
            config=_preview_config(style="formatted"),
            decision=_preview_decision(),
            source_group_name="申请群",
        )
    )

    assert preview["style"] == "formatted"
    assert preview["opinion_source"] == "decision"
    assert "看法：自动审核无法确定" in preview["text"]


def test_production_push_formatted_also_calls_push_llm_for_opinion(tmp_path):
    """生产推送路径：formatted 样式也经真实渲染调 push LLM，看法进入文案。"""
    plugin = _preview_wired_plugin()
    plugin._stopped = False
    plugin.context = None
    plugin.config.enable_api_guard = False
    plugin.cooldown = SimpleNamespace(check_breaker=lambda: False)

    store = main.JoinReviewStore(tmp_path)
    asyncio.run(
        store.upsert_group_config(
            platform_id="qq-main",
            group_id="100",
            push_group_ids=["300"],
            push_style="formatted",
            include_answer=True,
        )
    )
    plugin.join_review_store = store
    service = main.RequestPushService(store, main.OneBotClient())
    request = SimpleNamespace(
        request_id="r1",
        platform_id="qq-main",
        group_id="100",
        user_id="200",
        nickname="小明",
        level="16",
        question="口令？",
        answer="溪流",
    )
    decision = SimpleNamespace(verdict="uncertain", confidence=0.3, reason="不像")
    llm_calls, sent = [], []

    async def persona(umo):
        return "人格 prompt"

    async def push_llm(prompt, system_prompt="", contexts=None):
        llm_calls.append((prompt, system_prompt))
        return "答案看着靠谱"

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = push_llm

    class RenderOnlyPush:
        """只借真实渲染路径、不真正发送的推送桩。"""

        async def push_for_request(
            self, context, req, config, llm_caller, logger, push_decision=None
        ):
            text = await service.render_message(
                req, config, "申请群", llm_caller, push_decision
            )
            sent.append(text)
            return ["300"], [], []

    plugin.request_push = RenderOnlyPush()
    asyncio.run(plugin._push_join_request_review(request, decision))

    assert len(llm_calls) == 1
    prompt, system_prompt = llm_calls[0]
    assert "口令？" in prompt and "溪流" in prompt
    # caller 闭包带人格：看法生成也用同一人格 system prompt
    assert system_prompt == "人格 prompt"
    assert len(sent) == 1
    assert "看法：答案看着靠谱" in sent[0]
    assert "回复『同意』" in sent[0]


def test_simulate_push_preview_natural_failure_marks_fallback():
    """natural LLM 返回空：回退格式化模板并标注 natural_fallback_formatted。"""
    plugin = _preview_wired_plugin()
    plugin.context = SimpleNamespace(conversation_manager=None)

    async def persona(umo):
        return ""

    async def push_llm(prompt, system_prompt="", contexts=None):
        return ""

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = push_llm

    preview = asyncio.run(
        plugin._simulate_push_preview(
            platform_id="qq-main",
            group_id="100",
            question="口令？",
            answer="小河",
            config=_preview_config(),
            decision=_preview_decision(),
            source_group_name="申请群",
        )
    )

    assert preview["style"] == "natural_fallback_formatted"
    # natural 回退后不重试看法生成：看法直接取自动审核结论
    assert preview["opinion_source"] == "decision"
    assert "入群申请待审核" in preview["text"]


# ----------------------------------------------------- 审批结果回复人格化


def _wire_result_reply_llm(plugin, *, persona="人格 prompt", text=""):
    """给回复链路接上人格/结果文案 LLM 桩，返回 (persona_calls, llm_calls)。"""
    persona_calls, llm_calls = [], []

    async def get_persona(umo):
        persona_calls.append(umo)
        return persona

    async def push_llm(prompt, system_prompt="", contexts=None):
        llm_calls.append((prompt, system_prompt))
        return text

    plugin._get_push_persona_prompt = get_persona
    plugin._call_push_llm = push_llm
    return persona_calls, llm_calls


def test_result_reply_prompt_covers_four_outcomes():
    """结果回复 prompt：四种 outcome 都有描述，含申请人事实与字数限制。"""
    cases = {
        "approved": "已同意",
        "rejected": "已拒绝",
        "already_processed": "已被处理",
        "failed": "处理失败",
    }
    for outcome, desc in cases.items():
        prompt = main.build_result_reply_prompt(outcome, "小明", "200", detail="细节")
        assert desc in prompt
        assert "小明" in prompt and "200" in prompt and "细节" in prompt
        assert "100 字" in prompt
        assert "所在群" not in prompt
    # 未知 outcome 按 failed 处理；提供群名时带所在群行
    assert "处理失败" in main.build_result_reply_prompt("weird", "小明", "200")
    prompt = main.build_result_reply_prompt(
        "approved", "小明", "200", group_name="测试群"
    )
    assert "所在群：测试群" in prompt


def test_result_reply_approved_uses_persona_llm():
    """同意：结果回复由 push LLM 按人设生成，人格按推送群 UMO 取。"""
    plugin, _, observed = _reply_wired_plugin(llm_output='{"decision":"approve"}')
    persona_calls, llm_calls = _wire_result_reply_llm(plugin, text="好嘞，放他进来了。")

    _run_reply(plugin)

    assert observed["process"] == [("r1", True, "")]
    assert _sent_texts(plugin) == ["好嘞，放他进来了。"]
    assert persona_calls == ["qq-main:GroupMessage:300"]
    (prompt, system_prompt), *rest = llm_calls
    assert rest == []
    assert system_prompt == "人格 prompt"
    assert "已同意" in prompt and "200" in prompt


def test_result_reply_rejected_uses_persona_llm():
    """拒绝：结果回复走 LLM 人格文案。"""
    plugin, _, observed = _reply_wired_plugin(llm_output='{"decision":"reject"}')
    _, llm_calls = _wire_result_reply_llm(plugin, text="行，那我拒绝了。")

    _run_reply(plugin, _reply_raw(text="不要"))

    assert observed["process"] == [("r1", False, "管理员群内拒绝")]
    assert _sent_texts(plugin) == ["行，那我拒绝了。"]
    ((prompt, _),) = llm_calls
    assert "已拒绝" in prompt


def test_result_reply_already_processed_uses_persona_llm():
    """已处理：终态回复同样人格化。"""
    plugin, _, observed = _reply_wired_plugin(request_status="approved")
    _, llm_calls = _wire_result_reply_llm(plugin, text="这个早就处理过啦。")

    _run_reply(plugin)

    assert observed["process"] == []
    assert _sent_texts(plugin) == ["这个早就处理过啦。"]
    ((prompt, _),) = llm_calls
    assert "已被处理" in prompt


def test_result_reply_platform_failure_uses_persona_llm_with_detail():
    """平台失败：LLM 文案，prompt 带失败细节。"""
    plugin, _, _ = _reply_wired_plugin()
    _, llm_calls = _wire_result_reply_llm(
        plugin, text="哎呀，平台不放行，去管理页看看吧。"
    )

    class FailRuntime:
        async def process_request(self, context, request_id, *, approve, reason=""):
            return SimpleNamespace(
                status="platform_error", platform_error="平台拒绝了操作"
            )

    plugin.join_review = FailRuntime()
    _run_reply(plugin)

    assert _sent_texts(plugin) == ["哎呀，平台不放行，去管理页看看吧。"]
    ((prompt, _),) = llm_calls
    assert "处理失败" in prompt
    assert "平台拒绝了操作" in prompt


@pytest.mark.parametrize("llm_result", ["", "   ", None])
def test_result_reply_falls_back_to_fixed_text_when_llm_empty(llm_result):
    """LLM 返回空：回退现有固定文案。"""
    plugin, _, _ = _reply_wired_plugin()
    _wire_result_reply_llm(plugin, text=llm_result)

    _run_reply(plugin)

    assert _sent_texts(plugin) == ["已同意 QQ 200 的入群申请。"]


def test_result_reply_falls_back_when_llm_raises():
    """LLM 调用抛异常：回退固定文案，不影响审批结果。"""
    plugin, _, observed = _reply_wired_plugin()

    async def persona(umo):
        return "人格 prompt"

    async def failing_llm(prompt, system_prompt="", contexts=None):
        raise RuntimeError("llm down")

    plugin._get_push_persona_prompt = persona
    plugin._call_push_llm = failing_llm

    _run_reply(plugin)

    assert observed["process"] == [("r1", True, "")]
    assert _sent_texts(plugin) == ["已同意 QQ 200 的入群申请。"]


# ----------------------------------------------------- 结果回复预览（_render_result_reply 抽取）


def test_render_result_reply_returns_llm_text_without_fallback():
    """_render_result_reply：人格/caller 透传，LLM 非空时 fallback=False 不发送。"""
    plugin = _preview_wired_plugin()
    persona_calls, llm_calls = _wire_result_reply_llm(plugin, text="好，放他进来了。")

    rendered = asyncio.run(
        plugin._render_result_reply(
            "approved",
            "qq-main:GroupMessage:300",
            "模拟用户",
            "（模拟）",
            fallback="已同意 QQ （模拟） 的入群申请。",
        )
    )

    assert rendered == {"text": "好，放他进来了。", "fallback": False}
    assert persona_calls == ["qq-main:GroupMessage:300"]
    ((prompt, system_prompt),) = llm_calls
    assert system_prompt == "人格 prompt"
    assert "已同意" in prompt
    assert "模拟用户" in prompt and "（模拟）" in prompt


def test_render_result_reply_marks_fallback_when_llm_empty():
    """LLM 返回空：回退固定文案并标注 fallback=True。"""
    plugin = _preview_wired_plugin()
    _wire_result_reply_llm(plugin, text="")

    rendered = asyncio.run(
        plugin._render_result_reply(
            "rejected",
            "qq-main:GroupMessage:300",
            "模拟用户",
            "（模拟）",
            detail="",
            fallback="已拒绝 QQ （模拟） 的入群申请。",
        )
    )

    assert rendered == {"text": "已拒绝 QQ （模拟） 的入群申请。", "fallback": True}


def test_send_result_reply_reuses_render_path():
    """_send_result_reply 复用 _render_result_reply：同一实现不漂移。"""
    plugin, _, _ = _reply_wired_plugin()
    render_calls = []

    async def spy_render(outcome, umo, nickname, user_id, detail="", fallback=""):
        render_calls.append((outcome, umo, nickname, user_id, detail, fallback))
        return {"text": "人格化结果文案", "fallback": False}

    plugin._render_result_reply = spy_render
    request = SimpleNamespace(nickname="小明", user_id="200")

    asyncio.run(
        plugin._send_result_reply(
            "qq-main:GroupMessage:300",
            "approved",
            request,
            detail="",
            fallback="已同意 QQ 200 的入群申请。",
        )
    )

    assert render_calls == [
        (
            "approved",
            "qq-main:GroupMessage:300",
            "小明",
            "200",
            "",
            "已同意 QQ 200 的入群申请。",
        )
    ]
    assert _sent_texts(plugin) == ["人格化结果文案"]


def test_simulate_result_reply_preview_returns_two_outcomes():
    """模拟预览：approved/rejected 两结局，占位申请人与目标群人格。"""
    plugin = _preview_wired_plugin()
    persona_calls, llm_calls = _wire_result_reply_llm(plugin, text="人设结果回复")

    preview = asyncio.run(
        plugin._simulate_result_reply_preview(platform_id="qq-main", group_id="100")
    )

    assert set(preview) == {"approved", "rejected"}
    assert preview["approved"] == {"text": "人设结果回复", "fallback": False}
    assert preview["rejected"] == {"text": "人设结果回复", "fallback": False}
    assert persona_calls == ["qq-main:GroupMessage:100"] * 2
    prompts = [prompt for prompt, _ in llm_calls]
    assert len(prompts) == 2
    assert "已同意" in prompts[0] and "已拒绝" in prompts[1]
    assert all("模拟用户" in prompt for prompt in prompts)


def test_simulate_result_reply_preview_marks_fallback_per_outcome():
    """LLM 空：两结局各自回退固定文案并标注。"""
    plugin = _preview_wired_plugin()
    _wire_result_reply_llm(plugin, text="")

    preview = asyncio.run(
        plugin._simulate_result_reply_preview(platform_id="qq-main", group_id="100")
    )

    assert preview["approved"]["fallback"] is True
    assert preview["approved"]["text"] == "已同意 QQ （模拟） 的入群申请。"
    assert preview["rejected"]["fallback"] is True
    assert preview["rejected"]["text"] == "已拒绝 QQ （模拟） 的入群申请。"


# ----------------------------------------------------- Page 全局设置


def test_list_llm_providers_parses_meta():
    """_list_llm_providers：取 meta().id/model 组标签，过滤无 id 项。"""
    plugin = _preview_wired_plugin()

    class StubProvider:
        def __init__(self, pid, model=""):
            self._id, self._model = pid, model

        def meta(self):
            return SimpleNamespace(id=self._id, model=self._model)

    plugin.context = SimpleNamespace(
        get_all_providers=lambda: [
            StubProvider("gpt-a", "gpt-4o"),
            StubProvider("gpt-b"),
            StubProvider("", "no-id"),
        ]
    )

    assert plugin._list_llm_providers() == [
        {"id": "gpt-a", "label": "gpt-a（gpt-4o）"},
        {"id": "gpt-b", "label": "gpt-b"},
    ]

    # 框架无 get_all_providers / 列举抛异常：静默返回空列表
    plugin.context = SimpleNamespace()
    assert plugin._list_llm_providers() == []

    def boom():
        raise RuntimeError("provider manager down")

    plugin.context = SimpleNamespace(get_all_providers=boom)
    assert plugin._list_llm_providers() == []


def test_update_review_settings_commits_and_rebuilds_config():
    """写回成功：原子提交后 Config 就地重建，运行态立即生效。"""
    plugin = _preview_wired_plugin()
    plugin.config = main.Config(
        {"audit_llm_provider": "", "enable_active_learner_recall": False}
    )
    saved = []

    class Native:
        async def save_config_async(self, changes):
            saved.append(dict(changes))
            return True

    plugin._native_config = Native()

    result = asyncio.run(
        plugin._update_review_settings(
            audit_llm_provider="gpt-a", enable_active_learner_recall=True
        )
    )

    assert result == {"ok": True}
    assert saved == [
        {"audit_llm_provider": "gpt-a", "enable_active_learner_recall": True}
    ]
    # 同一 Config 对象属性读取即新值
    assert plugin.config.audit_llm_provider == "gpt-a"
    assert plugin.config.enable_active_learner_recall is True


def test_update_review_settings_failure_keeps_config():
    """写回失败不落盘不重建：superseded / 异常 / 不可写三种失败。"""
    plugin = _preview_wired_plugin()
    plugin.config = main.Config({"audit_llm_provider": "old"})

    class Superseded:
        async def save_config_async(self, changes):
            return False

    plugin._native_config = Superseded()
    result = asyncio.run(
        plugin._update_review_settings(
            audit_llm_provider="gpt-a", enable_active_learner_recall=True
        )
    )
    assert result["ok"] is False and result["error"] == "config_save_superseded"
    assert plugin.config.audit_llm_provider == "old"

    class Boom:
        async def save_config_async(self, changes):
            raise RuntimeError("disk full")

    plugin._native_config = Boom()
    result = asyncio.run(
        plugin._update_review_settings(
            audit_llm_provider="gpt-a", enable_active_learner_recall=True
        )
    )
    assert result["ok"] is False and result["error"] == "config_save_failed"
    assert plugin.config.audit_llm_provider == "old"

    plugin._native_config = None
    result = asyncio.run(
        plugin._update_review_settings(
            audit_llm_provider="gpt-a", enable_active_learner_recall=False
        )
    )
    assert result == {"ok": False, "error": "config_unavailable"}
