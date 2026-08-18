from __future__ import annotations

import asyncio
from types import SimpleNamespace

import core.join_review_api as api_module
from core.join_review_api import JoinReviewPageAPI, ROUTE_PREFIX
from core.join_review import JoinReviewRuntime
from core.audit import AutoAuditResult
from core.models import JoinDecision
from core.join_review_store import JoinReviewStore
from core.onebot import OneBotClient


def run(awaitable):
    return asyncio.run(awaitable)


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


class Bot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"user_id": 90001, "nickname": "审核 Bot"}
        if action == "get_group_list":
            return [
                {"group_id": 100},
                {"group_id": 300},
            ]
        if action == "get_group_info":
            return {
                "group_id": params["group_id"],
                "group_name": f"群 {params['group_id']}",
            }
        if action == "get_group_member_info":
            return {"role": "admin" if params["group_id"] == 100 else "member"}
        if action == "set_group_add_request":
            return None
        raise AssertionError(action)


class Runtime:
    def __init__(self):
        self.onebot = OneBotClient()


def make_api(tmp_path):
    bot = Bot()
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq-main", name="aiocqhttp"),
        get_client=lambda: bot,
    )
    routes = []
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [platform]),
        get_platform_inst=lambda platform_id: (
            platform if platform_id == "qq-main" else None
        ),
        register_web_api=lambda *args: routes.append(args),
    )
    store = JoinReviewStore(tmp_path)
    config = SimpleNamespace(join_audit_mode="approve_only")
    logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    api = JoinReviewPageAPI(
        context=context,
        config=config,
        store=store,
        runtime=Runtime(),
        logger=logger,
    )
    return api, store, routes, bot


def response_data(value):
    if isinstance(value, tuple):
        value = value[0]
    return value


def test_routes_use_join_review_prefix_and_dashboard_registration(tmp_path):
    api, _, routes, _ = make_api(tmp_path)
    assert api.register() is True
    assert {route[0] for route in routes} == {
        f"{ROUTE_PREFIX}/joined-groups",
        f"{ROUTE_PREFIX}/groups",
        f"{ROUTE_PREFIX}/groups/update",
        f"{ROUTE_PREFIX}/groups/batch",
        f"{ROUTE_PREFIX}/requests",
        f"{ROUTE_PREFIX}/approve",
        f"{ROUTE_PREFIX}/reject",
    }
    assert all(len(route) == 4 for route in routes)


def test_joined_group_refresh_is_read_only_and_uses_group_info(tmp_path):
    api, store, _, bot = make_api(tmp_path)
    result = response_data(run(api.joined_groups()))
    assert result["success"] is True
    assert result["data"]["groups"][0]["group_name"] == "群 100"
    assert run(store.list_group_configs()) == []
    assert not store.path.exists()
    assert any(action == "get_group_info" for action, _ in bot.calls)


def test_single_update_is_strict_and_persists_only_joined_reviewable_group(
    tmp_path, monkeypatch
):
    api, store, _, _ = make_api(tmp_path)
    payload = {
        "platform_id": "qq-main",
        "group_id": "100",
        "auto_audit_enabled": True,
        "review_send_enabled": True,
        "notify_target": "both",
        "specified_group_ids": ["300"],
        "include_answer": False,
        "pinned": True,
        "push_group_ids": ["300"],
        "push_style": "natural",
        "join_questions": [{"question": "口令？", "answers": ["溪流", "小溪"]}],
    }
    monkeypatch.setattr(api_module, "request", FakeRequest(payload))
    result = response_data(run(api.update_group()))
    assert result["success"] is True
    stored = run(store.get_group_config("qq-main", "100"))
    assert stored.auto_audit_enabled is True
    assert stored.review_send_enabled is True
    assert stored.specified_group_ids == ("300",)
    assert stored.include_answer is False
    assert stored.pinned is True
    assert stored.push_group_ids == ("300",)
    assert stored.push_style == "natural"
    assert stored.join_questions == (
        {"question": "口令？", "answers": ("溪流", "小溪")},
    )

    monkeypatch.setattr(
        api_module,
        "request",
        FakeRequest({**payload, "group_id": "300"}),
    )
    denied = response_data(run(api.update_group()))
    assert denied["success"] is False
    assert denied["error"] == "insufficient_permission"


def test_push_group_ids_must_be_joined_groups(tmp_path, monkeypatch):
    """推送群必须是当前 Bot 已加入的群。"""
    api, store, _, _ = make_api(tmp_path)
    payload = {
        "platform_id": "qq-main",
        "group_id": "100",
        "auto_audit_enabled": False,
        "review_send_enabled": True,
        "notify_target": "target_group",
        "specified_group_ids": [],
        "include_answer": True,
        "pinned": False,
        "push_group_ids": ["999"],
        "push_style": "formatted",
        "join_questions": [],
    }
    monkeypatch.setattr(api_module, "request", FakeRequest(payload))
    result = response_data(run(api.update_group()))
    assert result["success"] is False
    assert result["error"] == "push_group_not_joined"
    assert run(store.get_group_config("qq-main", "100")).configured is False


def test_batch_preserves_pinned_and_push_groups(tmp_path, monkeypatch):
    """批量操作保留按群的置顶、推送群与问答预设配置。"""
    api, store, _, _ = make_api(tmp_path)
    run(
        store.upsert_group_config(
            platform_id="qq-main",
            group_id="100",
            pinned=True,
            push_group_ids=["300"],
            join_questions=[{"question": "", "answers": ["溪流"]}],
        )
    )
    monkeypatch.setattr(
        api_module,
        "request",
        FakeRequest(
            {
                "action": "enable_auto_audit",
                "groups": [{"platform_id": "qq-main", "group_id": "100"}],
            }
        ),
    )
    assert response_data(run(api.batch_groups()))["success"] is True
    stored = run(store.get_group_config("qq-main", "100"))
    assert stored.auto_audit_enabled is True
    assert stored.pinned is True
    assert stored.push_group_ids == ("300",)
    assert stored.join_questions == ({"question": "", "answers": ("溪流",)},)


def test_batch_add_and_explicit_legacy_application(tmp_path, monkeypatch):
    api, store, _, _ = make_api(tmp_path)
    selection = [{"platform_id": "qq-main", "group_id": "100"}]
    monkeypatch.setattr(
        api_module,
        "request",
        FakeRequest({"action": "add", "groups": selection}),
    )
    assert response_data(run(api.batch_groups()))["success"] is True
    initial = run(store.get_group_config("qq-main", "100"))
    assert initial.configured is True
    assert initial.auto_audit_enabled is False
    assert initial.review_send_enabled is False

    monkeypatch.setattr(
        api_module,
        "request",
        FakeRequest({"action": "apply_legacy", "groups": selection}),
    )
    assert response_data(run(api.batch_groups()))["success"] is True
    migrated = run(store.get_group_config("qq-main", "100"))
    assert migrated.auto_audit_enabled is True
    assert migrated.review_send_enabled is False


def test_requests_projection_never_exposes_flag_and_honors_answer_setting(tmp_path):
    api, store, _, _ = make_api(tmp_path)
    run(
        store.upsert_group_config(
            platform_id="qq-main",
            group_id="100",
            include_answer=False,
        )
    )
    run(
        store.add_request(
            platform_id="qq-main",
            group_id="100",
            user_id="200",
            nickname="",
            level="",
            question="问题",
            answer="答案",
            flag="internal-secret-flag",
        )
    )
    result = response_data(run(api.requests()))
    item = result["data"]["requests"][0]
    assert item["nickname"] == "未知"
    assert item["level"] == "未知"
    assert "answer" not in item
    assert "flag" not in item


def test_reject_route_rechecks_permission_and_commits_only_after_onebot_success(
    tmp_path, monkeypatch
):
    api, store, _, bot = make_api(tmp_path)
    api.runtime = JoinReviewRuntime(
        audit=SimpleNamespace(
            execute_auto_audit=lambda *_args: AutoAuditResult(
                JoinDecision("uncertain", 0.0, "")
            )
        ),
        onebot=api.runtime.onebot,
        store=store,
    )
    request = run(
        store.add_request(
            platform_id="qq-main",
            group_id="100",
            user_id="200",
            answer="answer",
            flag="server-only-flag",
        )
    )
    monkeypatch.setattr(
        api_module, "request", FakeRequest({"request_id": request.request_id})
    )

    result = response_data(run(api.reject()))

    assert result["success"] is True
    assert result["data"]["request"]["status"] == "rejected"
    assert "flag" not in result["data"]["request"]
    action = next(item for item in bot.calls if item[0] == "set_group_add_request")
    assert action[1]["approve"] is False
    assert action[1]["flag"] == "server-only-flag"


def test_guard_blocked_returns_503_and_keeps_request_pending(tmp_path, monkeypatch):
    """紧急停止/熔断护栏生效时，Page 审批返回 503 guard_blocked 且不落平台。"""
    api, store, _, bot = make_api(tmp_path)
    api.runtime = JoinReviewRuntime(
        audit=SimpleNamespace(
            execute_auto_audit=lambda *_args: AutoAuditResult(
                JoinDecision("uncertain", 0.0, "")
            )
        ),
        onebot=api.runtime.onebot,
        store=store,
        guard=lambda: False,
    )
    request = run(
        store.add_request(
            platform_id="qq-main",
            group_id="100",
            user_id="200",
            answer="answer",
            flag="server-only-flag",
        )
    )
    monkeypatch.setattr(
        api_module, "request", FakeRequest({"request_id": request.request_id})
    )

    value = run(api.approve())

    status = value[1] if isinstance(value, tuple) else 200
    body = response_data(value)
    assert status == 503
    assert body["success"] is False
    assert body["error"] == "guard_blocked"
    assert run(store.get_request(request.request_id)).status == "pending"
    assert not any(action == "set_group_add_request" for action, _ in bot.calls)
