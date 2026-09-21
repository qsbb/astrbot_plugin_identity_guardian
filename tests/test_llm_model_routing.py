"""序 LLM 调用链的核路由接入测试。

三层链的优先级：本地显式 provider → 核统一模型路由（fast）→ AstrBot 原生
``get_using_provider()``。只有核路由命中的那一层附带核配的 ``model``。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT.parent))

CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.1",
    "read_only": True,
    "capabilities": ("resolve", "status"),
}


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


# ------------------------------------------------------------------ 测试替身


class Provider:
    """最小 provider 桩：记录每次 text_chat 关键字参数。"""

    def __init__(self, text="llm-ok", accept_model=True):
        self.text = text
        self.accept_model = accept_model
        self.calls: list[dict] = []

    async def text_chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self.accept_model and kwargs.get("model"):
            # 模拟老版本 provider：不认识 model 关键字参数。
            raise TypeError("unexpected keyword argument 'model'")
        return SimpleNamespace(completion_text=self.text)


class LegacyProvider:
    """老版本 provider：text_chat 形参里根本没有 model。"""

    def __init__(self, text="legacy-ok"):
        self.text = text
        self.calls: list[dict] = []

    async def text_chat(self, prompt=None, system_prompt="", contexts=None):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "contexts": contexts}
        )
        return SimpleNamespace(completion_text=self.text)


class LegacyProviderRequest:
    """没有 model 字段的旧版 ProviderRequest。"""

    def __init__(self, prompt=None, system_prompt="", contexts=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = list(contexts or [])


class ProviderRequest:
    """AstrBot 4.x ProviderRequest 桩：自带 model 字段。"""

    def __init__(self, prompt=None, system_prompt="", contexts=None, model=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = list(contexts or [])
        self.model = model


class Router:
    """核的 resolve_model_route 桩。"""

    def __init__(self, route, contract=None):
        self.route = route
        self.contract = CONTRACT if contract is None else contract
        self.kinds: list[str] = []

    def series_model_router_contract(self):
        return self.contract

    def resolve_model_route(self, kind, **_kwargs):
        self.kinds.append(kind)
        return {**self.route, "kind": kind}


UNUSED_PROVIDER = Provider("unused")


def core_route(**overrides):
    route = {
        "source": "core",
        "available": True,
        "provider_id": "core-fast",
        "model": "fast-mini",
    }
    route.update(overrides)
    return route


class Context:
    """同时模拟 AstrBot Context 与核插件注册表。"""

    def __init__(self, *, providers=None, native=None, router=None):
        self.providers = dict(providers or {})
        self.native = native
        self.router = router

    def get_provider_by_id(self, provider_id):
        return self.providers.get(str(provider_id or "").strip())

    def get_using_provider(self):
        return self.native

    def get_star_instance(self, plugin_name):
        if plugin_name != "astrbot_plugin_update_manager":
            return None
        return self.router


class OfficialOnlyContext(Context):
    """只实现官方 Context API（``get_registered_star``）的环境。

    AstrBot 4.x 没有 ``get_star_instance``；运行实例挂在 ``get_registered_star``
    返回的 ``StarMetadata.star_cls`` 上。
    """

    get_star_instance = None

    def __init__(self, *, providers=None, native=None, router=None):
        super().__init__(providers=providers, native=native, router=router)
        self.metadata = SimpleNamespace(star_cls=router) if router is not None else None

    def get_registered_star(self, _plugin_name):
        return self.metadata


def make_plugin(context):
    """不走 __init__ 造实例，避免拉起全部服务依赖。"""
    plugin = main.IdentityGuardianPlugin.__new__(main.IdentityGuardianPlugin)
    plugin.context = context
    plugin.config = SimpleNamespace(audit_llm_provider="", push_llm_provider="")
    plugin.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    return plugin


def ask(plugin, provider_id="", *, contexts=None):
    return asyncio.run(plugin._request_llm("问题", "", provider_id, contexts=contexts))


@pytest.fixture(autouse=True)
def _stub_provider_request(monkeypatch):
    """用可控桩替换 conftest 的 ProviderRequest MagicMock。"""
    import astrbot.api.provider as provider_module

    monkeypatch.setattr(
        provider_module, "ProviderRequest", ProviderRequest, raising=False
    )


# --------------------------------------------------------------- 三层链优先级


def test_core_route_provider_wins_over_native():
    core = Provider("core-ok")
    native = Provider("native-ok")
    router = Router(core_route())
    plugin = make_plugin(
        Context(providers={"core-fast": core}, native=native, router=router)
    )

    assert ask(plugin) == "core-ok"

    assert router.kinds == ["fast"]
    assert [call["model"] for call in core.calls] == ["fast-mini"]
    assert native.calls == []


def test_local_explicit_provider_wins_and_skips_core_model():
    local = Provider("local-ok")
    core = Provider("core-ok")
    native = Provider("native-ok")
    router = Router(core_route())
    plugin = make_plugin(
        Context(
            providers={"local-audit": local, "core-fast": core},
            native=native,
            router=router,
        )
    )

    assert ask(plugin, "local-audit") == "local-ok"

    # 本地显式命中时既不问核，也不附加核的 model。
    assert router.kinds == []
    assert local.calls[0]["model"] is None
    assert core.calls == []
    assert native.calls == []


def test_stale_local_provider_still_consults_core_route():
    """配置残留（provider 已失效）必须继续走核路由，而不是直接落原生。"""
    core = Provider("core-ok")
    native = Provider("native-ok")
    router = Router(core_route())
    plugin = make_plugin(
        Context(providers={"core-fast": core}, native=native, router=router)
    )

    assert ask(plugin, "removed-provider") == "core-ok"

    assert router.kinds == ["fast"]
    assert core.calls[0]["model"] == "fast-mini"
    assert native.calls == []


def test_core_route_without_model_keeps_provider_default():
    core = Provider("core-ok")
    router = Router(core_route(model=None))
    plugin = make_plugin(Context(providers={"core-fast": core}, router=router))

    assert ask(plugin) == "core-ok"
    assert core.calls[0]["model"] is None


def test_audit_llm_provider_config_is_the_local_explicit_layer():
    """_call_audit_llm 把 audit_llm_provider 配置交给第一层。"""
    local = Provider("local-ok")
    native = Provider("native-ok")
    router = Router(core_route())
    plugin = make_plugin(
        Context(providers={"local-audit": local}, native=native, router=router)
    )
    plugin.config.audit_llm_provider = "local-audit"

    assert asyncio.run(plugin._call_audit_llm("问题")) == "local-ok"

    assert local.calls[0]["model"] is None
    assert router.kinds == []
    assert native.calls == []


def test_contexts_are_still_forwarded_on_core_route():
    core = Provider("core-ok")
    router = Router(core_route())
    plugin = make_plugin(Context(providers={"core-fast": core}, router=router))
    contexts = [{"role": "user", "content": "历史消息"}]

    assert ask(plugin, contexts=contexts) == "core-ok"
    assert core.calls[0]["contexts"] == contexts


def test_legacy_context_get_provider_is_supported():
    """旧版框架只有 Context.get_provider。"""

    class LegacyContext(Context):
        get_provider_by_id = None

        def get_provider(self, provider_id):
            return self.providers.get(provider_id)

    local = Provider("local-ok")
    plugin = make_plugin(LegacyContext(providers={"local-audit": local}))

    assert ask(plugin, "local-audit") == "local-ok"
    assert local.calls[0]["model"] is None


def test_official_registry_only_env_still_routes_core_fast():
    """没有 get_star_instance 时，三层链的中间层仍要命中核路由。"""
    core = Provider("core-ok")
    native = Provider("native-ok")
    router = Router(core_route())
    plugin = make_plugin(
        OfficialOnlyContext(providers={"core-fast": core}, native=native, router=router)
    )

    assert ask(plugin) == "core-ok"

    assert router.kinds == ["fast"]
    assert [call["model"] for call in core.calls] == ["fast-mini"]
    assert native.calls == []


def test_official_registry_only_env_without_router_falls_back_to_native():
    native = Provider("native-ok")
    plugin = make_plugin(OfficialOnlyContext(native=native))

    assert ask(plugin) == "native-ok"

    assert native.calls[0]["model"] is None


def test_official_registry_only_env_without_live_instance_falls_back_to_native():
    """``star_cls`` 是 class（不是运行实例）时必须 fail-closed 落到原生。"""
    native = Provider("native-ok")
    context = OfficialOnlyContext(providers={"core-fast": UNUSED_PROVIDER}, native=native)
    context.metadata = SimpleNamespace(star_cls=Router)
    plugin = make_plugin(context)

    assert ask(plugin) == "native-ok"

    assert native.calls[0]["model"] is None


# --------------------------------------------------------- 核不可用 → 原生兜底


def _broken_kind_router():
    class WrongKindRouter(Router):
        def resolve_model_route(self, kind, **_kwargs):
            self.kinds.append(kind)
            return {**self.route, "kind": "conversation"}

    return WrongKindRouter(core_route())


def _raising_router():
    class RaisingRouter(Router):
        def resolve_model_route(self, kind, **_kwargs):
            raise RuntimeError("router failed")

    return RaisingRouter(core_route())


def _non_dict_router():
    class NonDictRouter(Router):
        def resolve_model_route(self, kind, **_kwargs):
            return ["not", "a", "dict"]

    return NonDictRouter(core_route())


@pytest.mark.parametrize(
    "router,providers",
    [
        pytest.param(None, {}, id="not-installed"),
        pytest.param(Router(core_route(), {**CONTRACT, "name": "other@1.0"}), {}, id="contract-name"),
        pytest.param(Router(core_route(), {**CONTRACT, "version": "2.0"}), {}, id="contract-major"),
        pytest.param(Router(core_route(), {**CONTRACT, "read_only": False}), {}, id="contract-read-only"),
        pytest.param(Router(core_route(), {**CONTRACT, "capabilities": ("status",)}), {}, id="contract-capability"),
        pytest.param(
            Router(core_route(source="astrbot")),
            {"core-fast": UNUSED_PROVIDER},
            id="source-not-core",
        ),
        pytest.param(
            Router(core_route(available=False)),
            {"core-fast": UNUSED_PROVIDER},
            id="not-available",
        ),
        pytest.param(
            _broken_kind_router(), {"core-fast": UNUSED_PROVIDER}, id="kind-mismatch"
        ),
        pytest.param(Router(core_route()), {}, id="provider-gone"),
        pytest.param(
            _raising_router(), {"core-fast": UNUSED_PROVIDER}, id="resolver-raises"
        ),
        pytest.param(
            _non_dict_router(), {"core-fast": UNUSED_PROVIDER}, id="non-dict-route"
        ),
    ],
)
def test_unavailable_core_route_falls_back_to_native(router, providers):
    native = Provider("native-ok")
    plugin = make_plugin(Context(providers=providers, native=native, router=router))

    assert ask(plugin) == "native-ok"

    assert [call["model"] for call in native.calls] == [None]


def test_missing_provider_getter_on_context_is_tolerated():
    """Context 既没有 get_provider_by_id 也没有 get_provider 时落到原生。"""

    class NativeOnlyContext:
        def __init__(self, native):
            self.native = native

        def get_using_provider(self):
            return self.native

        def get_star_instance(self, _plugin_name):
            return None

    native = Provider("native-ok")
    plugin = make_plugin(NativeOnlyContext(native))

    assert ask(plugin, "some-provider") == "native-ok"
    assert native.calls[0]["model"] is None


def test_adapter_crash_still_falls_back_to_native(monkeypatch):
    """适配层意外抛错时不能连带吞掉 AstrBot 原生兜底。"""

    async def boom(_context, _kind):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(main, "resolve_routed_model_route", boom)
    native = Provider("native-ok")
    plugin = make_plugin(Context(native=native))

    assert ask(plugin) == "native-ok"
    assert native.calls[0]["model"] is None


def test_no_provider_anywhere_returns_empty():
    plugin = make_plugin(Context())
    assert ask(plugin) == ""


# ------------------------------------------------------------- TypeError 兜底


def test_model_type_error_retries_without_model():
    """provider 拒绝 model 参数时去掉再试一次，调用仍然成功。"""
    provider = Provider("ok", accept_model=False)
    router = Router(core_route())
    native = Provider("native-ok")
    plugin = make_plugin(
        Context(providers={"core-fast": provider}, native=native, router=router)
    )

    assert ask(plugin) == "ok"

    assert len(provider.calls) == 2
    assert provider.calls[0]["model"] == "fast-mini"
    assert "model" not in provider.calls[1]
    assert native.calls == []


def test_legacy_provider_signature_without_model_still_succeeds():
    provider = LegacyProvider()
    router = Router(core_route())
    plugin = make_plugin(Context(providers={"core-fast": provider}, router=router))

    assert ask(plugin) == "legacy-ok"

    assert provider.calls == [
        {"prompt": "问题", "system_prompt": "", "contexts": []}
    ]


def test_model_is_forwarded_when_request_has_no_model_field(monkeypatch):
    """ProviderRequest 没有 model 字段时，model 仍在调用处显式传下去。"""
    import astrbot.api.provider as provider_module

    monkeypatch.setattr(
        provider_module, "ProviderRequest", LegacyProviderRequest, raising=False
    )
    provider = Provider("core-ok")
    plugin = make_plugin(
        Context(providers={"core-fast": provider}, router=Router(core_route()))
    )

    assert ask(plugin) == "core-ok"
    assert provider.calls[0]["model"] == "fast-mini"
