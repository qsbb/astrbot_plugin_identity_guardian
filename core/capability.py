"""能力映射表与工具注册辅助。"""

from __future__ import annotations

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
    "set_member_card": {"min_role": "admin", "tool_name": "set_member_card"},
    "set_group_name": {"min_role": "admin", "tool_name": "set_group_name"},
    "set_whole_ban": {"min_role": "admin", "tool_name": "set_whole_ban"},
    "approve_join_request": {"min_role": "admin", "tool_name": "approve_join_request"},
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
