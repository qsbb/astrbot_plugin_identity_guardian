"""身份解析与角色缓存测试。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from core.config import Config
from core.identity import IdentityManager, extract_ob_role
from core.relationship import RelationshipService


class _FakeOneBot:
    """可控的 OneBot 桩：按序返回预设响应并记录调用次数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get_group_member_info(self, event, group_id, user_id, no_cache=False):
        self.calls += 1
        if not self.responses:
            return None
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _make_manager(responses, refresh_interval=300):
    cfg = Config(
        {
            "owner_users": [],
            "protected_users": [],
            "blacklist_users": [],
            "identity_refresh_interval": refresh_interval,
        }
    )
    onebot = _FakeOneBot(responses)
    manager = IdentityManager(cfg, onebot, RelationshipService(cfg))
    manager.clear_cache()
    return manager, onebot


# ------------------------------------------------------------------
# extract_ob_role
# ------------------------------------------------------------------


def test_extract_role_from_string():
    assert extract_ob_role({"role": "admin"}) == "admin"


def test_extract_role_normalizes_case_and_space():
    assert extract_ob_role({"role": " ADMIN "}) == "admin"


def test_extract_role_from_int_mapping():
    assert extract_ob_role({"role": 1}) == "owner"
    assert extract_ob_role({"role": 2}) == "admin"
    assert extract_ob_role({"role": 3}) == "member"


def test_extract_role_from_dict_name():
    assert extract_ob_role({"role": {"name": "owner"}}) == "owner"


def test_extract_role_returns_none_when_unavailable():
    assert extract_ob_role(None) is None
    assert extract_ob_role({}) is None
    assert extract_ob_role({"role": ""}) is None
    assert extract_ob_role({"role": "banana"}) is None
    assert extract_ob_role({"role": 99}) is None


# ------------------------------------------------------------------
# get_role 缓存行为
# ------------------------------------------------------------------


def test_get_role_returns_admin_and_caches():
    manager, onebot = _make_manager([{"role": "admin"}])
    first = asyncio.run(manager.get_role(SimpleNamespace(), "123", "555"))
    second = asyncio.run(manager.get_role(SimpleNamespace(), "123", "555"))
    assert first == "admin"
    assert second == "admin"
    # 第二次应命中缓存，不再打接口
    assert onebot.calls == 1


def test_failed_lookup_is_not_cached_and_recovers():
    """接口抖动后应能恢复真实身份，而不是把 member 锁死一个刷新周期。"""
    manager, onebot = _make_manager([None, {"role": "admin"}])
    first = asyncio.run(manager.get_role(SimpleNamespace(), "123", "555"))
    second = asyncio.run(manager.get_role(SimpleNamespace(), "123", "555"))
    assert first == "member"
    assert second == "admin"
    assert onebot.calls == 2


def test_role_cache_isolated_by_platform_id():
    manager, onebot = _make_manager([{"role": "admin"}, {"role": "member"}])
    event = SimpleNamespace()

    first = asyncio.run(manager.get_role(event, "123", "555", platform_id="qq-main"))
    second = asyncio.run(
        manager.get_role(event, "123", "555", platform_id="qq-secondary")
    )

    assert first == "admin"
    assert second == "member"
    assert onebot.calls == 2


def test_invalid_group_id_returns_member_without_call():
    manager, onebot = _make_manager([{"role": "admin"}])
    role = asyncio.run(manager.get_role(SimpleNamespace(), "not-a-group", "555"))
    assert role == "member"
    assert onebot.calls == 0


# ------------------------------------------------------------------
# actor context
# ------------------------------------------------------------------


def test_actor_context_carries_bot_id():
    """bot_id 必须进入上下文，供策略层识别自指操作。"""
    manager, _ = _make_manager([{"role": "member"}])
    actor = asyncio.run(
        manager.get_actor_context(
            SimpleNamespace(),
            "aiocqhttp#1",
            "123",
            "555",
            "999",
        )
    )
    assert actor.bot_id == "555"
    assert actor.bot_role == "member"


def test_canonical_person_alias_never_grants_platform_permission():
    config = Config({"owner_users": ["qq-raw-10001"]})

    assert config.is_owner("qq-raw-10001")
    assert not config.is_owner("person_summer")
    assert not config.is_owner("telegram-raw-42")


def test_shared_boundary_summary_never_contains_account_identifiers():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text("utf-8")
    start = source.index('"permission_identity"')
    summary = source[start : source.index("add_reason(", start)]

    assert '"mode": "raw_platform_account"' in summary
    assert '"platform_id"' not in summary
    assert '"user_id"' not in summary
