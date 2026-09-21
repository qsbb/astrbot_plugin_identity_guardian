"""Optional adapter for 核's versioned model-router contract.

核（astrbot_plugin_update_manager）通过 ``series_model_router_contract()``
声明只读的 ``series.model_router@1.0`` 契约，并提供
``resolve_model_route(kind, plugin_override=None)`` 统一解析 7 类职责的模型路由。

本适配层是单向、fail-closed 的：序自身的 ``audit_llm_provider`` /
``push_llm_provider`` 配置始终是第一优先；只有本地没有显式可用 provider 时才
向核要路由；核未安装、契约不匹配、来源不是 ``core``、``available`` 不为 True、
kind 不匹配或 provider 已在 AstrBot 中消失时一律返回空值，调用方继续走
AstrBot 原生兜底。核实例优先走可选的 ``context.get_star_instance``，缺失时回退
到官方 ``context.get_registered_star`` 返回的 ``StarMetadata``（运行实例挂在
``star_cls`` 等属性上）。需要核配置的 ``model`` / ``fallback_from`` 时用
:func:`resolve_model_route`，它与 :func:`resolve_provider_id` 共用同一套校验。
"""

from __future__ import annotations

import inspect
from typing import Any

ROUTER_PLUGIN_NAME = "astrbot_plugin_update_manager"
ROUTER_CONTRACT_NAME = "series.model_router@1.0"
ROUTER_CONTRACT_MAJOR = "1"
ROUTER_INSTANCE_ATTRIBUTES = ("star_cls", "star", "instance", "star_instance", "plugin")


async def resolve_provider_id(context: Any, kind: str = "conversation") -> str:
    """返回核路由选定的 provider_id；核不可用时返回空串。"""
    route = await _resolve_validated_route(context, kind)
    if route is None:
        return ""
    return route["provider_id"]


async def resolve_model_route(context: Any, kind: str) -> dict[str, Any]:
    """返回核给的完整路由 dict（含 ``model`` / ``fallback_from``）。

    校验规则与 :func:`resolve_provider_id` 完全一致：契约名精确匹配、主版本为
    ``1``、``read_only`` 为 True、声明了 ``resolve`` 能力、``kind`` 与请求一致、
    ``source == "core"``、``available is True``、``provider_id`` 非空且仍能在
    AstrBot 中取到 provider。任何异常或不匹配都返回空 dict。
    """
    route = await _resolve_validated_route(context, kind)
    if route is None:
        return {}
    return route


async def _resolve_validated_route(context: Any, kind: str) -> dict[str, Any] | None:
    """取一条核路由并完成全部校验；不可用返回 ``None``。"""
    if not isinstance(kind, str) or not kind.strip():
        return None
    kind = kind.strip()
    plugin = await _resolve_router_plugin(context)
    if plugin is None or not await _compatible(plugin):
        return None
    resolver = getattr(plugin, "resolve_model_route", None)
    if not callable(resolver):
        return None
    try:
        try:
            route = resolver(kind, plugin_override=None)
        except TypeError:
            # 旧版/精简版核只接受 kind 位置参数。
            route = resolver(kind)
        if inspect.isawaitable(route):
            route = await route
    except Exception:
        return None
    if not isinstance(route, dict):
        return None
    if route.get("kind") != kind:
        return None
    if route.get("source") != "core":
        return None
    if route.get("available") is not True:
        return None
    provider_id = _text(route.get("provider_id"))
    if not provider_id:
        return None
    provider_getter = getattr(context, "get_provider_by_id", None)
    if callable(provider_getter):
        try:
            if provider_getter(provider_id) is None:
                return None
        except Exception:
            return None
    return {**route, "provider_id": provider_id}


async def _resolve_router_plugin(context: Any) -> Any | None:
    """尽力取核插件实例；任何一步失败都只当作「取不到」，绝不外抛。

    ``Context.get_star_instance`` 不是 AstrBot 的公开接口（4.x 只有
    ``get_registered_star`` / ``get_all_stars``），所以它只能是第一优先的
    可选举措；真正的兜底是官方的 ``get_registered_star``：它返回
    ``StarMetadata``，运行中的实例挂在 ``star_cls``（4.x）或旧别名上。
    """
    instance = await _call_star_lookup(context, "get_star_instance")
    if instance is not None and not isinstance(instance, type):
        return instance
    metadata = await _call_star_lookup(context, "get_registered_star")
    if metadata is None:
        return None
    for attribute in ROUTER_INSTANCE_ATTRIBUTES:
        candidate = _star_metadata_attribute(metadata, attribute)
        if candidate is not None:
            return candidate
    return None


async def _call_star_lookup(context: Any, method_name: str) -> Any | None:
    """调用 context 上一个可选的星标查询；不可用或抛异常都返回 ``None``。"""
    try:
        getter = getattr(context, method_name, None)
    except Exception:
        return None
    if not callable(getter):
        return None
    try:
        value = getter(ROUTER_PLUGIN_NAME)
        if inspect.isawaitable(value):
            value = await value
    except Exception:
        return None
    return value


def _star_metadata_attribute(metadata: Any, attribute: str) -> Any | None:
    """按属性名从 ``StarMetadata`` 取运行实例；class 与异常一律跳过。"""
    try:
        value = getattr(metadata, attribute, None)
    except Exception:
        return None
    if value is None or isinstance(value, type):
        # AstrBot 4.x 把运行实例放在 ``star_cls``；取到 class 或什么都没取到
        # 都说明没有可用的活实例。
        return None
    return value


def _text(value: Any, limit: int = 256) -> str:
    """返回去除首尾空白的字符串字段；非字符串一律视为空。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


async def _compatible(plugin: Any) -> bool:
    """校验核声明的 ``series.model_router@1.x`` 只读契约。"""
    declare = getattr(plugin, "series_model_router_contract", None)
    if not callable(declare):
        return False
    try:
        contract = declare()
        if inspect.isawaitable(contract):
            contract = await contract
    except Exception:
        return False
    if not isinstance(contract, dict):
        return False
    if contract.get("name") != ROUTER_CONTRACT_NAME:
        return False
    # 主版本比较：核只升次版本（1.0 → 1.1）时适配层必须继续可用。
    version = str(contract.get("version") or "")
    if version.split(".", 1)[0] != ROUTER_CONTRACT_MAJOR:
        return False
    if contract.get("read_only") is not True:
        return False
    capabilities = contract.get("capabilities")
    if not isinstance(capabilities, (list, tuple, set, frozenset)):
        return False
    return "resolve" in capabilities
