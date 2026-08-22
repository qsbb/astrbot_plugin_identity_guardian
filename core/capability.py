"""能力映射表与工具注册辅助。"""

from __future__ import annotations

from typing import Any

# 角色权限等级：数字越大权限越高
ROLE_LEVEL: dict[str, int] = {
    "member": 0,
    "admin": 1,
    "owner": 2,
    "unknown": -1,
}

# 能力定义（单一数据源）
# min_role 表示执行该 OneBot Action 所需的最低 bot 群角色
CAPABILITY_MAP: dict[str, dict[str, str]] = {
    "mute_current_sender": {"min_role": "admin", "tool_name": "mute_current_sender"},
    "request_self_mute": {"min_role": "admin", "tool_name": "request_self_mute"},
    "mute_member": {"min_role": "admin", "tool_name": "mute_member"},
    "unmute_member": {"min_role": "admin", "tool_name": "unmute_member"},
    "delete_message": {"min_role": "admin", "tool_name": "delete_message"},
    "kick_member": {"min_role": "admin", "tool_name": "kick_member"},
    "leave_group": {"min_role": "admin", "tool_name": "leave_group"},
    "set_member_card": {"min_role": "admin", "tool_name": "set_member_card"},
    "set_group_name": {"min_role": "admin", "tool_name": "set_group_name"},
    "set_whole_ban": {"min_role": "admin", "tool_name": "set_whole_ban"},
    "set_member_title": {"min_role": "owner", "tool_name": "set_member_title"},
    "set_group_admin": {"min_role": "owner", "tool_name": "set_group_admin"},
    "set_self_card": {"min_role": "member", "tool_name": "set_self_card"},
    "get_group_member_info": {
        "min_role": "member",
        "tool_name": "get_group_member_info",
    },
    "list_group_members": {"min_role": "member", "tool_name": "list_group_members"},
}

# 所有 LLM 工具名
ALL_TOOL_NAMES: list[str] = [cap["tool_name"] for cap in CAPABILITY_MAP.values()]
RETIRED_TOOL_NAMES: frozenset[str] = frozenset({"approve_join_request"})
ALL_MANAGED_TOOL_NAMES: frozenset[str] = frozenset(ALL_TOOL_NAMES) | RETIRED_TOOL_NAMES


def llm_tool_name(tool: Any) -> str:
    """兼容字典 schema 与 FunctionTool 对象，提取工具名。"""
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name", ""))
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", "") or "")


def filter_tools_for_role(tools: list[Any], role: str) -> list[Any]:
    """保留其他插件工具，并隐藏当前 bot 角色不可用的本插件工具。"""
    allowed_tool_names = {
        CAPABILITY_MAP[capability]["tool_name"]
        for capability in capabilities_for_role(role)
    }
    return [
        tool
        for tool in tools
        if llm_tool_name(tool) not in ALL_MANAGED_TOOL_NAMES
        or llm_tool_name(tool) in allowed_tool_names
    ]


def blocked_tool_names_for_role(role: str) -> set[str]:
    """返回当前 bot 角色下应当隐藏的本插件工具名集合。"""
    allowed = {
        CAPABILITY_MAP[capability]["tool_name"]
        for capability in capabilities_for_role(role)
    }
    return {name for name in ALL_MANAGED_TOOL_NAMES if name not in allowed}


def filter_request_tools_for_role(req: Any, role: str) -> int:
    """按 bot 群角色移除请求中不可用的本插件工具，返回移除数量。

    AstrBot 的 ProviderRequest 用 ``func_tool``（ToolSet）承载工具，
    而不是 ``tools``。ToolSet 由 ``get_full_tool_set()`` 每次请求新建，
    因此原地移除只影响本次请求，不会污染全局工具表。
    同时兼容仅有 ``tools`` 列表的旧结构，便于测试与向后兼容。
    """
    blocked = blocked_tool_names_for_role(role)
    if not blocked:
        return 0

    removed = 0

    tool_set = getattr(req, "func_tool", None)
    if tool_set is not None:
        tools = getattr(tool_set, "tools", None)
        if isinstance(tools, list):
            present = [
                name
                for name in (llm_tool_name(tool) for tool in tools)
                if name in blocked
            ]
            remover = getattr(tool_set, "remove_tool", None)
            if callable(remover):
                for name in present:
                    remover(name)
            else:
                tool_set.tools = [
                    tool for tool in tools if llm_tool_name(tool) not in blocked
                ]
            removed += len(present)

    legacy = getattr(req, "tools", None)
    if isinstance(legacy, list) and legacy:
        kept = [tool for tool in legacy if llm_tool_name(tool) not in blocked]
        removed += len(legacy) - len(kept)
        req.tools = kept

    return removed


def capabilities_for_role(role: str) -> list[str]:
    """返回该 bot 角色拥有 OneBot 权限前提的能力 id 列表。

    注意：这只是 OneBot 权限前提，不代表调用已获策略授权。
    最终授权由 PolicyEngine 决定。
    """
    bot_level = ROLE_LEVEL.get(role, -1)
    return [
        cap_id
        for cap_id, meta in CAPABILITY_MAP.items()
        if bot_level >= ROLE_LEVEL.get(meta["min_role"], 999)
    ]


def min_role_for_capability(cap_id: str) -> str:
    """返回该能力所需的最低角色。"""
    meta = CAPABILITY_MAP.get(cap_id)
    return meta["min_role"] if meta else "owner"


def tool_name_for_capability(cap_id: str) -> str:
    """返回该能力对应的工具名。"""
    meta = CAPABILITY_MAP.get(cap_id)
    return meta["tool_name"] if meta else ""
