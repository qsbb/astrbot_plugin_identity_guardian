"""能力映射测试。"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.capability import (  # noqa: E402
    ALL_TOOL_NAMES,
    CAPABILITY_MAP,
    capabilities_for_role,
    filter_tools_for_role,
    llm_tool_name,
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


def _tool(name):
    return {"type": "function", "function": {"name": name}}


def test_filter_member_tools():
    """普通成员只看到 member 工具，其他插件工具不受影响。"""
    tools = [
        _tool("set_self_card"),
        _tool("get_group_member_info"),
        _tool("mute_member"),
        _tool("set_member_title"),
        _tool("web_search"),
    ]
    filtered = filter_tools_for_role(tools, "member")
    names = [llm_tool_name(tool) for tool in filtered]
    assert names == ["set_self_card", "get_group_member_info", "web_search"]


def test_filter_admin_tools():
    """管理员保留管理工具，但看不到群主专属工具。"""
    tools = [
        _tool("set_self_card"),
        _tool("mute_member"),
        _tool("set_member_title"),
        _tool("set_group_admin"),
    ]
    filtered = filter_tools_for_role(tools, "admin")
    names = [llm_tool_name(tool) for tool in filtered]
    assert names == ["set_self_card", "mute_member"]


def test_filter_owner_tools():
    """群主可以看到本插件全部工具。"""
    tools = [_tool(meta["tool_name"]) for meta in CAPABILITY_MAP.values()]
    assert filter_tools_for_role(tools, "owner") == tools


def test_filter_unknown_role_hides_all_plugin_tools():
    """身份未知时隐藏全部本插件工具，保留其他插件工具。"""
    tools = [_tool("set_self_card"), _tool("mute_member"), _tool("web_search")]
    filtered = filter_tools_for_role(tools, "unknown")
    assert [llm_tool_name(tool) for tool in filtered] == ["web_search"]


def test_filter_supports_tool_objects():
    """过滤逻辑兼容 FunctionTool 风格对象。"""

    class Tool:
        def __init__(self, name):
            self.name = name

    tools = [Tool("set_self_card"), Tool("mute_member"), Tool("web_search")]
    filtered = filter_tools_for_role(tools, "member")
    assert [llm_tool_name(tool) for tool in filtered] == ["set_self_card", "web_search"]


def _registered_llm_tool_names():
    """用 AST 解析 main.py，收集 @filter.llm_tool(name=...) 注册的工具名。"""
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if not (isinstance(func, ast.Attribute) and func.attr == "llm_tool"):
                continue
            for kw in deco.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(kw.value.value)
    return names


def test_every_capability_has_registered_tool():
    """CAPABILITY_MAP 中每个能力都必须有对应的已注册 LLM 工具。"""
    registered = _registered_llm_tool_names()
    missing = sorted(set(ALL_TOOL_NAMES) - registered)
    assert not missing, f"以下能力缺少 llm_tool 注册: {missing}"


def test_no_orphan_registered_tool():
    """已注册的 LLM 工具都必须在 CAPABILITY_MAP 中有能力定义。"""
    registered = _registered_llm_tool_names()
    orphan = sorted(registered - set(ALL_TOOL_NAMES))
    assert not orphan, f"以下工具未在 CAPABILITY_MAP 中定义: {orphan}"


def test_readonly_tools_registered():
    """两个只读工具已注册。"""
    registered = _registered_llm_tool_names()
    assert "get_group_member_info" in registered
    assert "list_group_members" in registered
