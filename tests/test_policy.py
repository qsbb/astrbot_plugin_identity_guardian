"""策略引擎测试 — 核心安全测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.models import ActorContext, TriggerSource  # noqa: E402
from core.policy import PolicyEngine  # noqa: E402


def _make_config(**overrides):
    """创建测试配置。"""
    defaults = {
        "owner_users": ["100"],
        "protected_users": ["200"],
        "allow_playful_mute_protected": False,
        "playful_mute_max_seconds": 60,
        "max_mute_seconds": 1800,
        "confirm_mute_threshold": 3600,
        "blacklist_users": [],
    }
    defaults.update(overrides)
    return Config(defaults)


def _make_actor(
    bot_role="admin",
    requester_id="999",
    requester_role="member",
    requester_relation="normal",
    target_id=None,
    target_role=None,
    target_relation=None,
    bot_id="555",
):
    """创建测试 ActorContext。"""
    return ActorContext(
        bot_role=bot_role,
        bot_id=bot_id,
        requester_id=requester_id,
        requester_role=requester_role,
        requester_relation=requester_relation,
        target_id=target_id,
        target_role=target_role,
        target_relation=target_relation,
        group_id="123456",
        platform_id="aiocqhttp#1",
    )


# ------------------------------------------------------------------
# mute_current_sender 测试
# ------------------------------------------------------------------


def test_mute_current_sender_normal():
    """bot 为管理员，对普通成员短禁言 — 允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor()
    decision = engine.evaluate(
        actor,
        "mute_current_sender",
        {"duration": 300, "reason": "骚扰"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is True
    assert decision.requires_confirmation is False


def test_mute_current_sender_protected_no_playful():
    """受保护用户，未开启玩笑禁言 — 拒绝。"""
    cfg = _make_config(protected_users=["999"])
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999", requester_role="admin", requester_relation="friendly"
    )
    decision = engine.evaluate(
        actor,
        "mute_current_sender",
        {"duration": 30, "reason": "互动"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is False
    assert "强保护" in decision.reason


def test_mute_current_sender_protected_playful():
    """受保护用户，开启玩笑禁言，时长在上限内 — 允许。"""
    cfg = _make_config(
        protected_users=["999"],
        allow_playful_mute_protected=True,
        playful_mute_max_seconds=60,
    )
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999", requester_role="admin", requester_relation="friendly"
    )
    decision = engine.evaluate(
        actor,
        "mute_current_sender",
        {"duration": 30, "reason": "害羞"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is True
    assert decision.requires_confirmation is False


def test_mute_current_sender_protected_playful_exceed():
    """受保护用户，开启玩笑禁言，但超时 — 拒绝。"""
    cfg = _make_config(
        protected_users=["999"],
        allow_playful_mute_protected=True,
        playful_mute_max_seconds=60,
    )
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999", requester_role="admin", requester_relation="friendly"
    )
    decision = engine.evaluate(
        actor,
        "mute_current_sender",
        {"duration": 120, "reason": "互动"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is False
    assert "60" in decision.reason


def test_mute_current_sender_exceed_max():
    """禁言时长超过上限 — 自动截断。"""
    cfg = _make_config(max_mute_seconds=600)
    engine = PolicyEngine(cfg)
    actor = _make_actor()
    decision = engine.evaluate(
        actor,
        "mute_current_sender",
        {"duration": 9999, "reason": "测试"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is True
    assert decision.params["duration"] == 600


# ------------------------------------------------------------------
# request_self_mute 测试
# ------------------------------------------------------------------


def test_request_self_mute_allowed():
    """用户请求禁言自己 — 允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(requester_id="999")
    decision = engine.evaluate(
        actor,
        "request_self_mute",
        {"duration": 100, "reason": "自我惩罚"},
        TriggerSource.SELF_SERVICE.value,
    )
    assert decision.allowed is True


def test_request_self_mute_wrong_trigger():
    """非自助触发 — 拒绝。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(requester_id="999")
    decision = engine.evaluate(
        actor,
        "request_self_mute",
        {"duration": 100, "reason": "测试"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is False
    assert "自助" in decision.reason


# ------------------------------------------------------------------
# mute_member 测试
# ------------------------------------------------------------------


def test_mute_member_by_friendly():
    """友好用户请求禁言他人 — 允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "mute_member",
        {"user_id": "888", "duration": 300},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True


def test_mute_member_by_normal_member():
    """普通成员请求禁言他人 — 拒绝。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999",
        requester_role="member",
        requester_relation="normal",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "mute_member",
        {"user_id": "888", "duration": 300},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert "普通成员" in decision.reason


def test_mute_member_protected_target():
    """禁言受保护用户 — 拒绝。"""
    cfg = _make_config(protected_users=["888"])
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="admin",
    )
    decision = engine.evaluate(
        actor,
        "mute_member",
        {"user_id": "888", "duration": 300},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert "保护" in decision.reason


# ------------------------------------------------------------------
# kick_member 测试
# ------------------------------------------------------------------


def test_kick_member_by_friendly():
    """友好用户请求踢出 — 允许但需确认。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "kick_member",
        {"user_id": "888"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True
    assert decision.requires_confirmation is True


def test_kick_member_protected():
    """踢出受保护用户 — 拒绝。"""
    cfg = _make_config(protected_users=["888"])
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="admin",
    )
    decision = engine.evaluate(
        actor,
        "kick_member",
        {"user_id": "888"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert "保护" in decision.reason


def test_kick_member_by_normal():
    """普通成员请求踢出 — 拒绝。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999",
        requester_role="member",
        requester_relation="normal",
        target_id="888",
    )
    decision = engine.evaluate(
        actor,
        "kick_member",
        {"user_id": "888"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False


# ------------------------------------------------------------------
# set_member_card 测试
# ------------------------------------------------------------------


def test_set_card_self():
    """普通成员修改自己的名片 — 允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999",
        requester_role="member",
        requester_relation="normal",
        target_id="999",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "set_member_card",
        {"user_id": "999", "card": "新名字"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True


def test_set_card_other_by_normal():
    """普通成员修改他人名片 — 拒绝。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="999",
        requester_role="member",
        requester_relation="normal",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "set_member_card",
        {"user_id": "888", "card": "新名字"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert "普通成员" in decision.reason


# ------------------------------------------------------------------
# set_member_title 测试
# ------------------------------------------------------------------


def test_set_title_owner():
    """群主设头衔 — 允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        bot_role="owner",
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "set_member_title",
        {"user_id": "888", "title": "大佬"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True


def test_set_title_admin():
    """管理员设头衔 — 拒绝（仅群主）。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        bot_role="admin",
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
    )
    decision = engine.evaluate(
        actor,
        "set_member_title",
        {"user_id": "888", "title": "大佬"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert "权限" in decision.reason


# ------------------------------------------------------------------
# bot 权限前提测试
# ------------------------------------------------------------------


def test_member_bot_cannot_mute():
    """bot 为普通成员时无法禁言 — 拒绝。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="member")
    decision = engine.evaluate(
        actor,
        "mute_current_sender",
        {"duration": 60, "reason": "测试"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is False
    assert "权限" in decision.reason


def test_member_bot_can_set_self_card():
    """bot 为普通成员时可以改自己名片 — 允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="member")
    decision = engine.evaluate(
        actor,
        "set_self_card",
        {"card": "新名字"},
        TriggerSource.LLM_AUTONOMOUS.value,
    )
    assert decision.allowed is True


def test_member_bot_set_member_card_on_self_is_rewritten():
    """bot 为普通成员，用 set_member_card 指向自己 — 重写为 set_self_card 并允许。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="member", bot_id="555")
    decision = engine.evaluate(
        actor,
        "set_member_card",
        {"user_id": "555", "card": "小心夏"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True
    assert decision.action == "set_self_card"
    assert decision.params == {"card": "小心夏"}


def test_member_bot_set_member_card_on_other_still_denied():
    """bot 为普通成员，改他人名片 — 仍然按权限拒绝，不被重写绕过。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="member", bot_id="555")
    decision = engine.evaluate(
        actor,
        "set_member_card",
        {"user_id": "888", "card": "小心夏"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert decision.action == "set_member_card"
    assert "bot 身份" in decision.reason


def test_admin_bot_set_member_card_on_self_is_rewritten():
    """bot 为管理员，指向自己的改名片同样归一化为 set_self_card。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="admin", bot_id="555")
    decision = engine.evaluate(
        actor,
        "set_member_card",
        {"user_id": "555", "card": "小心夏"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True
    assert decision.action == "set_self_card"


def test_set_member_card_without_bot_id_not_rewritten():
    """缺少 bot_id 时不做重写，保持原有行为。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="member", bot_id="")
    decision = engine.evaluate(
        actor,
        "set_member_card",
        {"user_id": "555", "card": "小心夏"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert decision.action == "set_member_card"


# ------------------------------------------------------------------
# 黑名单测试
# ------------------------------------------------------------------


def test_blacklist_kick():
    """黑名单用户触发踢出 — 自动踢出。"""
    cfg = _make_config(blacklist_users=["888"])
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "kick_member",
        {"user_id": "888"},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is True
    assert decision.params.get("reject_add_request") is True


def test_blacklist_mute():
    """黑名单用户不执行其他操作 — 拒绝。"""
    cfg = _make_config(blacklist_users=["888"])
    engine = PolicyEngine(cfg)
    actor = _make_actor(
        requester_id="100",
        requester_role="admin",
        requester_relation="friendly",
        target_id="888",
        target_role="member",
    )
    decision = engine.evaluate(
        actor,
        "mute_member",
        {"user_id": "888", "duration": 300},
        TriggerSource.EXPLICIT_REQUEST.value,
    )
    assert decision.allowed is False
    assert "黑名单" in decision.reason


# ------------------------------------------------------------------
# allowed_actions 测试
# ------------------------------------------------------------------


def test_allowed_actions_admin():
    """管理员 bot 的行动边界描述。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor()
    actions = engine.allowed_actions(actor)
    assert any("短时禁言" in a for a in actions)
    assert any("禁言他自己" in a for a in actions)
    assert any("不能要求你处罚其他成员" in a for a in actions)


def test_allowed_actions_member_bot():
    """普通成员 bot 的行动边界。"""
    cfg = _make_config()
    engine = PolicyEngine(cfg)
    actor = _make_actor(bot_role="member")
    actions = engine.allowed_actions(actor)
    # 普通成员 bot 没有管理能力
    assert any("高风险操作不能仅因普通成员请求执行" in a for a in actions)
