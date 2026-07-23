"""能力映射测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.capability import (  # noqa: E402
    CAPABILITY_MAP,
    capabilities_for_role,
    min_role_for_capability,
    ROLE_LEVEL,
    tool_name_for_capability,
)


def test_role_level_ordering():
    """角色权限等级排序正确。"""
    assert ROLE_LEVEL["owner"] > ROLE_LEVEL["admin"] > ROLE_LEVEL["member"]


def test_owner_capabilities():
    """群主拥有全部能力。"""
    caps = capabilities_for_role("owner")
    assert "mute_member" in caps
    assert "set_member_title" in caps
    assert "set_group_admin" in caps
    assert "set_self_card" in caps
    assert len(caps) == len(CAPABILITY_MAP)


def test_admin_capabilities():
    """管理员缺少头衔和管理员设置。"""
    caps = capabilities_for_role("admin")
    assert "mute_member" in caps
    assert "set_member_title" not in caps
    assert "set_group_admin" not in caps
    assert "set_self_card" in caps


def test_member_capabilities():
    """普通成员只有自助和只读能力。"""
    caps = capabilities_for_role("member")
    assert "set_self_card" in caps
    assert "get_group_member_info" in caps
    assert "list_group_members" in caps
    assert "mute_member" not in caps
    assert "kick_member" not in caps
    assert "set_member_title" not in caps


def test_min_role_for_capability():
    """能力最低角色要求正确。"""
    assert min_role_for_capability("set_member_title") == "owner"
    assert min_role_for_capability("set_group_admin") == "owner"
    assert min_role_for_capability("mute_member") == "admin"
    assert min_role_for_capability("set_self_card") == "member"


def test_tool_name_mapping():
    """工具名映射一致。"""
    for cap_id, meta in CAPABILITY_MAP.items():
        assert tool_name_for_capability(cap_id) == meta["tool_name"]
