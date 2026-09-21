"""核统一模型路由适配层测试（series.model_router@1.x）。

覆盖契约校验的 5 步：取插件实例（``get_star_instance`` 优先，官方
``get_registered_star`` 兜底）→ 校验只读契约（名字/主版本/read_only/
resolve 能力）→ 调用 resolve_model_route（含 TypeError 兜底）→ 校验
kind/source/available → provider_id 非空且在 AstrBot 中仍然存在。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from model_router import resolve_model_route, resolve_provider_id

CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.0",
    "read_only": True,
    "capabilities": ("resolve", "status"),
}

# 核把契约 version 从 1.0 升到 1.1（name 不变）：主版本比较必须仍然接受。
CONTRACT_1_1 = {**CONTRACT, "version": "1.1"}


_MISSING = object()


class Router:
    """新版核：resolver 接受位置参数 kind + 关键字 plugin_override。"""

    def __init__(self, route, contract=_MISSING):
        self.route = route
        self.contract = CONTRACT if contract is _MISSING else contract
        self.kinds: list[str] = []

    def series_model_router_contract(self):
        return self.contract

    def resolve_model_route(self, kind, **_kwargs):
        self.kinds.append(kind)
        return {**self.route, "kind": kind}


class Context:
    def __init__(self, router, providers=()):
        self.router = router
        self.providers = set(providers)

    def get_star_instance(self, plugin_name):
        if plugin_name != "astrbot_plugin_update_manager":
            return None
        return self.router

    def get_provider_by_id(self, provider_id):
        return object() if provider_id in self.providers else None


def core_route(**overrides):
    route = {"source": "core", "available": True, "provider_id": "core-fast"}
    route.update(overrides)
    return route


def run(awaitable):
    return asyncio.run(awaitable)


# ------------------------------------------------------------ resolve_provider_id


def test_accepts_compatible_core_route():
    context = Context(Router(core_route()), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == "core-fast"


def test_kind_defaults_to_conversation():
    """resolve_provider_id 默认 kind="conversation"。"""
    router = Router(core_route(provider_id="core-chat"))
    context = Context(router, providers=("core-chat",))
    assert run(resolve_provider_id(context)) == "core-chat"
    assert router.kinds == ["conversation"]


def test_accepts_minor_version_upgrade():
    """核 1.1 只升次版本：按主版本比较，不做精确匹配。"""
    context = Context(Router(core_route(), CONTRACT_1_1), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == "core-fast"


def test_accepts_legacy_resolver_signature():
    """核的 resolver 只接受 kind 时仍能工作（TypeError 兜底）。"""

    class LegacyRouter(Router):
        def resolve_model_route(self, kind):
            self.kinds.append(kind)
            return {**self.route, "kind": kind}

    router = LegacyRouter(core_route())
    context = Context(router, providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == "core-fast"
    assert router.kinds == ["fast"]


def test_accepts_async_contract_and_resolver():
    class AsyncRouter(Router):
        async def series_model_router_contract(self):
            return self.contract

        async def resolve_model_route(self, kind, **_kwargs):
            self.kinds.append(kind)
            return {**self.route, "kind": kind}

    class AsyncContext(Context):
        def get_star_instance(self, plugin_name):
            if plugin_name != "astrbot_plugin_update_manager":
                return None

            async def resolve():
                return self.router

            return resolve()

    context = AsyncContext(AsyncRouter(core_route()), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == "core-fast"


@pytest.mark.parametrize(
    "contract",
    [
        pytest.param({**CONTRACT, "name": "series.other@1.0"}, id="wrong-name"),
        pytest.param({**CONTRACT, "version": "2.0"}, id="wrong-major"),
        pytest.param({**CONTRACT, "read_only": False}, id="not-read-only"),
        pytest.param({**CONTRACT, "read_only": "true"}, id="read-only-not-bool"),
        pytest.param({**CONTRACT, "capabilities": ("status",)}, id="no-resolve"),
        pytest.param({**CONTRACT, "capabilities": None}, id="no-capabilities"),
        pytest.param(None, id="no-contract"),
        pytest.param("series.model_router@1.0", id="non-dict-contract"),
    ],
)
def test_rejects_incompatible_contract(contract):
    context = Context(Router(core_route(), contract), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == ""
    assert run(resolve_model_route(context, "fast")) == {}


@pytest.mark.parametrize(
    "source",
    ["astrbot", "plugin", "", None],
)
def test_rejects_non_core_source(source):
    context = Context(Router(core_route(source=source)), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == ""
    assert run(resolve_model_route(context, "fast")) == {}


@pytest.mark.parametrize(
    "available",
    [False, "true", None, 1, 0],
)
def test_rejects_unavailable_route(available):
    context = Context(Router(core_route(available=available)), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == ""


def test_rejects_kind_mismatch():
    class WrongKindRouter(Router):
        def resolve_model_route(self, kind, **_kwargs):
            self.kinds.append(kind)
            return {**self.route, "kind": "tts"}

    context = Context(WrongKindRouter(core_route()), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == ""
    assert run(resolve_model_route(context, "fast")) == {}


def test_rejects_route_without_provider():
    for provider_id in ("", "   ", None, 7):
        context = Context(
            Router(core_route(provider_id=provider_id)), providers=("core-fast", "7")
        )
        assert run(resolve_provider_id(context, "fast")) == ""


def test_rejects_provider_missing_from_astrbot():
    """核给的 provider 已从 AstrBot 消失时必须 fail-closed。"""
    context = Context(Router(core_route()), providers=())
    assert run(resolve_provider_id(context, "fast")) == ""
    assert run(resolve_model_route(context, "fast")) == {}


def test_rejects_provider_lookup_failure():
    class BrokenContext(Context):
        def get_provider_by_id(self, provider_id):
            raise RuntimeError("provider manager unavailable")

    context = BrokenContext(Router(core_route()), providers=("core-fast",))
    assert run(resolve_provider_id(context, "fast")) == ""


def test_tolerates_missing_provider_getter():
    """没有 get_provider_by_id 的框架版本跳过复核而不是失败。"""

    class MinimalContext:
        def get_star_instance(self, _plugin_name):
            return Router(core_route(model="fast-mini"))

    route = run(resolve_model_route(MinimalContext(), "fast"))
    assert route["provider_id"] == "core-fast"
    assert route["model"] == "fast-mini"


@pytest.mark.parametrize("kind", ["", "   ", None, 0])
def test_rejects_blank_kind(kind):
    context = Context(Router(core_route()), providers=("core-fast",))
    assert run(resolve_provider_id(context, kind)) == ""
    assert run(resolve_model_route(context, kind)) == {}


def test_rejects_missing_router_and_broken_resolver():
    class RaisingRouter(Router):
        def resolve_model_route(self, kind, **_kwargs):
            raise RuntimeError("router failed")

    class NonDictRouter(Router):
        def resolve_model_route(self, kind, **_kwargs):
            return ["not", "a", "dict"]

    class DeclareRaisesRouter(Router):
        def series_model_router_contract(self):
            raise RuntimeError("contract failed")

    assert run(resolve_provider_id(Context(None, providers=()), "fast")) == ""
    assert run(resolve_provider_id(Context(RaisingRouter({}), ("core-fast",)), "fast")) == ""
    assert run(resolve_provider_id(Context(NonDictRouter({}), ("core-fast",)), "fast")) == ""
    assert (
        run(resolve_provider_id(Context(DeclareRaisesRouter({}), ("core-fast",)), "fast"))
        == ""
    )


def test_rejects_context_without_star_instance_lookup():
    class NoLookupContext:
        def get_provider_by_id(self, _provider_id):
            return object()

    assert run(resolve_provider_id(NoLookupContext(), "fast")) == ""


def test_raises_free_when_star_lookup_raises():
    class RaisingLookupContext:
        def get_star_instance(self, _plugin_name):
            raise RuntimeError("star manager unavailable")

    assert run(resolve_provider_id(RaisingLookupContext(), "fast")) == ""


# ------------------------------------------------------------ resolve_model_route


def test_returns_full_core_route():
    """整条核路由原样返回（含 model / voice / fallback_from / configured）。"""
    route = core_route(
        model="fast-mini",
        voice="",
        fallback_from="plugin",
        configured="core",
    )
    context = Context(Router(route, CONTRACT_1_1), providers=("core-fast",))

    result = run(resolve_model_route(context, "fast"))

    assert result == {**route, "kind": "fast"}


def test_provider_id_agrees_with_resolve_provider_id():
    context = Context(Router(core_route(model="m")), providers=("core-fast",))
    provider_id = run(resolve_provider_id(context, "fast"))
    route = run(resolve_model_route(context, "fast"))
    assert provider_id == route["provider_id"] == "core-fast"


def test_strips_provider_id_whitespace():
    context = Context(
        Router(core_route(provider_id="  core-fast  ")), providers=("core-fast",)
    )
    route = run(resolve_model_route(context, "fast"))
    assert route["provider_id"] == "core-fast"


# --------------------------------------------------------- 核实例解析（官方 API）


class OfficialOnlyContext:
    """只实现 AstrBot 官方 API 的 Context：没有 get_star_instance。"""

    def __init__(self, metadata=None, providers=()):
        self.metadata = metadata
        self.providers = set(providers)
        self.lookups: list[str] = []

    def get_registered_star(self, plugin_name):
        self.lookups.append(plugin_name)
        return self.metadata

    def get_provider_by_id(self, provider_id):
        return object() if provider_id in self.providers else None


class RaisingRegistryContext(OfficialOnlyContext):
    def get_registered_star(self, plugin_name):
        raise RuntimeError("star registry unavailable")


class CorePluginClass:
    """坑：``StarMetadata`` 上挂的是插件类而不是运行实例。"""

    def __init__(self, route):
        self.route = route

    def series_model_router_contract(self):
        return CONTRACT

    def resolve_model_route(self, kind, **_kwargs):
        return {**self.route, "kind": kind}


def star_metadata(**attributes):
    return SimpleNamespace(**attributes)


def test_resolves_core_instance_from_official_registry():
    router = Router(core_route(model="fast-mini"))
    context = OfficialOnlyContext(
        star_metadata(star_cls=router), providers=("core-fast",)
    )

    assert run(resolve_provider_id(context, "fast")) == "core-fast"
    assert run(resolve_model_route(context, "fast"))["model"] == "fast-mini"
    assert context.lookups == ["astrbot_plugin_update_manager"] * 2


@pytest.mark.parametrize(
    "attribute", ["star_cls", "star", "instance", "star_instance", "plugin"]
)
def test_reads_metadata_instance_attributes(attribute):
    router = Router(core_route())
    context = OfficialOnlyContext(
        star_metadata(**{attribute: router}), providers=("core-fast",)
    )

    assert run(resolve_provider_id(context, "fast")) == "core-fast"


def test_get_star_instance_wins_over_official_registry():
    """非官方速查接口存在且命中时优先，不回归。"""
    preferred = Router(core_route(provider_id="core-preferred"))
    registry_router = Router(core_route(provider_id="core-registry"))
    context = Context(preferred, providers=("core-preferred", "core-registry"))
    context.get_registered_star = lambda _name: star_metadata(star_cls=registry_router)

    assert run(resolve_provider_id(context, "fast")) == "core-preferred"
    assert preferred.kinds == ["fast"]
    assert registry_router.kinds == []


def test_skips_class_attribute_and_keeps_looking():
    """``star_cls`` 是 class 时跳过，继续用后面真正的实例属性。"""
    router = Router(core_route())
    context = OfficialOnlyContext(
        star_metadata(star_cls=CorePluginClass, star=router),
        providers=("core-fast",),
    )

    assert run(resolve_provider_id(context, "fast")) == "core-fast"


def test_accepts_awaitable_lookups():
    class AsyncOfficialContext:
        def __init__(self, instance, metadata):
            self.instance = instance
            self.metadata = metadata

        async def get_star_instance(self, _plugin_name):
            return self.instance

        async def get_registered_star(self, _plugin_name):
            return self.metadata

        def get_provider_by_id(self, provider_id):
            return object() if provider_id == "core-fast" else None

    router = Router(core_route())

    shortcut = AsyncOfficialContext(router, star_metadata(star_cls=None))
    assert run(resolve_provider_id(shortcut, "fast")) == "core-fast"

    registry = AsyncOfficialContext(None, star_metadata(star_cls=router))
    assert run(resolve_provider_id(registry, "fast")) == "core-fast"


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(OfficialOnlyContext(None), id="registered-star-none"),
        pytest.param(RaisingRegistryContext(), id="registered-star-raises"),
        pytest.param(
            OfficialOnlyContext(star_metadata(star_cls=CorePluginClass)),
            id="star-cls-is-class",
        ),
        pytest.param(OfficialOnlyContext(star_metadata()), id="no-instance-attribute"),
    ],
)
def test_unresolvable_core_instance_fails_closed(context):
    assert run(resolve_provider_id(context, "fast")) == ""
    assert run(resolve_model_route(context, "fast")) == {}


def test_survives_broken_context_attributes():
    class BrokenContext:
        @property
        def get_star_instance(self):
            raise RuntimeError("attribute exploded")

        @property
        def get_registered_star(self):
            raise RuntimeError("attribute exploded")

        def get_provider_by_id(self, _provider_id):
            return object()

    assert run(resolve_provider_id(BrokenContext(), "fast")) == ""
