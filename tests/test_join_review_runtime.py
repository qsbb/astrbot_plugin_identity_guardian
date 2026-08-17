"""Runtime tests for scoped join-request review."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.audit import AutoAuditResult
from core.group_discovery import discover_joined_groups
from core.join_notification import JoinNotificationService
from core.join_review import GuardBlockedError, JoinReviewRuntime, parse_join_request
from core.join_review_store import JoinReviewStore
from core.models import JoinDecision
from core.onebot import OneBotClient


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_approval = False
        self.fail_send_groups: set[int] = set()

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"user_id": 90001, "nickname": "审核 Bot"}
        if action == "get_group_list":
            return [
                {
                    "group_id": 100,
                    "group_name": "申请群",
                    "member_count": 10,
                    "max_member_count": 200,
                }
            ]
        if action == "get_group_member_info":
            return {"role": "admin"}
        if action == "get_group_info":
            return {"group_id": params["group_id"], "group_name": "申请群"}
        if action == "send_group_msg":
            if params["group_id"] in self.fail_send_groups:
                raise RuntimeError("send failed")
            return None
        if action == "set_group_add_request":
            if self.fail_approval:
                raise RuntimeError("approval failed")
            return None
        raise AssertionError(action)


class Event:
    def __init__(self, bot: Bot, platform_id: str = "qq-main") -> None:
        self.bot = bot
        self.platform_id = platform_id

    def get_platform_id(self) -> str:
        return self.platform_id


class Audit:
    def __init__(self, result: AutoAuditResult) -> None:
        self.result = result
        self.calls = 0

    async def execute_auto_audit(self, event, raw):
        del event, raw
        self.calls += 1
        return self.result


def run(awaitable):
    return asyncio.run(awaitable)


def raw_request(**overrides):
    raw = {
        "request_type": "group",
        "group_id": 100,
        "user_id": 200,
        "flag": "internal-platform-flag",
        "sub_type": "add",
        "comment": "问题：口令？\n答案：溪流",
    }
    raw.update(overrides)
    return raw


def audit_result(*, approved: bool = False, error: str = "") -> AutoAuditResult:
    return AutoAuditResult(
        JoinDecision(verdict="correct", confidence=0.99, reason="matched"),
        approval_attempted=True,
        platform_approved=approved,
        platform_error=error,
    )


def make_runtime(tmp_path, result=None):
    bot = Bot()
    onebot = OneBotClient()
    store = JoinReviewStore(tmp_path)
    audit = Audit(result or audit_result())
    runtime = JoinReviewRuntime(audit, onebot, store)
    return runtime, store, audit, Event(bot), bot


def configure(store, *, auto: bool, send: bool, **overrides):
    values = {
        "platform_id": "qq-main",
        "group_id": "100",
        "auto_audit_enabled": auto,
        "review_send_enabled": send,
        "notify_target": "target_group",
        "include_answer": True,
    }
    values.update(overrides)
    return run(store.upsert_group_config(**values))


def test_default_both_off_ignores_without_llm_or_storage(tmp_path):
    runtime, store, audit, event, bot = make_runtime(tmp_path)
    result = run(runtime.handle_event(event, raw_request()))
    assert result.outcome == "ignored"
    assert audit.calls == 0
    assert run(store.list_requests()) == []
    assert bot.calls == []


def test_auto_only_leaves_unapproved_request_on_platform(tmp_path):
    runtime, store, audit, event, bot = make_runtime(tmp_path)
    configure(store, auto=True, send=False)
    result = run(runtime.handle_event(event, raw_request()))
    assert result.outcome == "left_on_platform"
    assert audit.calls == 1
    assert run(store.list_requests()) == []
    assert not any(action == "send_group_msg" for action, _ in bot.calls)


def test_send_only_skips_auto_audit_and_queues(tmp_path):
    runtime, store, audit, event, bot = make_runtime(tmp_path)
    configure(store, auto=False, send=True)
    result = run(runtime.handle_event(event, raw_request()))
    assert result.outcome == "pending_review"
    assert audit.calls == 0
    assert result.request is not None and result.request.status == "pending"
    assert any(action == "send_group_msg" for action, _ in bot.calls)


def test_both_enabled_stops_only_after_platform_approval(tmp_path):
    runtime, store, audit, event, bot = make_runtime(
        tmp_path, audit_result(approved=True)
    )
    configure(store, auto=True, send=True)
    result = run(runtime.handle_event(event, raw_request()))
    assert result.outcome == "auto_approved"
    assert run(store.list_requests()) == []
    assert not any(action == "send_group_msg" for action, _ in bot.calls)


def test_both_enabled_platform_failure_enters_manual_review(tmp_path):
    runtime, store, audit, event, _ = make_runtime(
        tmp_path, audit_result(approved=False, error="api failed")
    )
    configure(store, auto=True, send=True)
    result = run(runtime.handle_event(event, raw_request()))
    assert result.outcome == "pending_review"
    assert result.auto_audit is not None
    assert result.auto_audit.platform_approved is False
    assert result.request is not None and result.request.status == "pending"


@pytest.mark.parametrize(
    ("case", "audit_result_value"),
    [
        ("incorrect", AutoAuditResult(JoinDecision("incorrect", 0.99, "wrong"))),
        ("uncertain", AutoAuditResult(JoinDecision("uncertain", 0.3, "uncertain"))),
        ("low_confidence", AutoAuditResult(JoinDecision("correct", 0.6, "low"))),
        ("no_reference", AutoAuditResult(JoinDecision("unavailable", 0.0, "none"))),
        (
            "llm_error",
            AutoAuditResult(
                JoinDecision("unavailable", 0.0, "llm"),
                platform_error="llm_error",
            ),
        ),
        (
            "knowledge_error",
            AutoAuditResult(
                JoinDecision("unavailable", 0.0, "knowledge"),
                platform_error="knowledge_error",
            ),
        ),
        (
            "approval_api_error",
            AutoAuditResult(
                JoinDecision("correct", 0.99, "matched"),
                approval_attempted=True,
                platform_error="set_group_add_request failed",
            ),
        ),
    ],
)
def test_every_not_actually_approved_result_enters_manual_review(
    tmp_path, case, audit_result_value
):
    runtime, store, audit, event, bot = make_runtime(
        tmp_path / case, audit_result_value
    )
    configure(store, auto=True, send=True)

    result = run(runtime.handle_event(event, raw_request()))

    assert result.outcome == "pending_review"
    assert result.request is not None and result.request.status == "pending"
    assert audit.calls == 1
    assert any(action == "send_group_msg" for action, _ in bot.calls)


def test_fields_are_bounded_and_missing_identity_stays_unknown():
    parsed = parse_join_request(Event(Bot()), raw_request(comment="x" * 3000))
    assert parsed.nickname == ""
    assert parsed.level == ""
    assert len(parsed.answer) == 2048


def test_notification_variants_whitelist_and_idempotency(tmp_path):
    runtime, store, audit, event, bot = make_runtime(tmp_path)
    config = configure(
        store,
        auto=False,
        send=True,
        notify_target="both",
        specified_group_ids=[300],
        include_answer=False,
    )
    request = run(runtime._store_request(parse_join_request(event, raw_request())))
    service = JoinNotificationService(store, runtime.onebot)
    first = run(service.notify(bot, request, config))
    second = run(service.notify(bot, request, config))
    assert first.sent == ("100", "300")
    assert second.skipped == ("100", "300")
    messages = [params for action, params in bot.calls if action == "send_group_msg"]
    assert "来源群" not in messages[0]["message"]
    assert "来源群：申请群（100）" in messages[1]["message"]
    assert "答案：" not in messages[0]["message"]
    assert {item["group_id"] for item in messages} == {100, 300}


def test_notification_failure_keeps_pending_and_can_retry(tmp_path):
    runtime, store, _, event, bot = make_runtime(tmp_path)
    config = configure(store, auto=False, send=True)
    request = run(runtime._store_request(parse_join_request(event, raw_request())))
    bot.fail_send_groups.add(100)
    first = run(runtime.notification.notify(bot, request, config))
    assert first.failed == ("100",)
    assert run(store.get_request(request.request_id)).status == "pending"
    bot.fail_send_groups.clear()
    second = run(runtime.notification.notify(bot, request, config))
    assert second.sent == ("100",)


def test_group_discovery_uses_public_platform_and_reports_permission():
    bot = Bot()
    platform = SimpleNamespace(
        get_client=lambda: bot,
        meta=lambda: SimpleNamespace(id="qq-main", name="aiocqhttp"),
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(get_insts=lambda: [platform])
    )
    rows = run(discover_joined_groups(context))
    assert len(rows) == 1
    assert rows[0].platform_id == "qq-main"
    assert rows[0].bot_id == "90001"
    assert rows[0].bot_role == "admin"
    assert rows[0].can_review is True


def test_manual_action_uses_stored_flag_and_commits_after_platform_success(tmp_path):
    runtime, store, _, event, bot = make_runtime(tmp_path)
    request = run(runtime._store_request(parse_join_request(event, raw_request())))
    platform = SimpleNamespace(
        get_client=lambda: bot,
        meta=lambda: SimpleNamespace(id="qq-main", name="aiocqhttp"),
    )
    context = SimpleNamespace(
        get_platform_inst=lambda platform_id: (
            platform if platform_id == "qq-main" else None
        ),
        platform_manager=SimpleNamespace(get_insts=lambda: [platform]),
    )
    updated = run(runtime.process_request(context, request.request_id, approve=False))
    assert updated.status == "rejected"
    action = next(item for item in bot.calls if item[0] == "set_group_add_request")
    assert action[1]["flag"] == "internal-platform-flag"
    assert action[1]["approve"] is False


def test_process_request_rejected_when_guard_blocks(tmp_path):
    """紧急停止/熔断护栏生效时，Page 审批在入口被拒绝且不触碰平台。"""
    bot = Bot()
    onebot = OneBotClient()
    store = JoinReviewStore(tmp_path)
    audit = Audit(audit_result())
    runtime = JoinReviewRuntime(audit, onebot, store, guard=lambda: False)
    request = run(runtime._store_request(parse_join_request(Event(bot), raw_request())))

    with pytest.raises(GuardBlockedError):
        run(
            runtime.process_request(SimpleNamespace(), request.request_id, approve=True)
        )

    assert run(store.get_request(request.request_id)).status == "pending"
    assert not any(action == "set_group_add_request" for action, _ in bot.calls)


def test_process_request_runs_when_guard_allows(tmp_path):
    """护栏通过时正常审批，确认 guard 回调确实被消费。"""
    bot = Bot()
    onebot = OneBotClient()
    store = JoinReviewStore(tmp_path)
    audit = Audit(audit_result())
    calls = []
    runtime = JoinReviewRuntime(
        audit, onebot, store, guard=lambda: calls.append("check") or True
    )
    request = run(runtime._store_request(parse_join_request(Event(bot), raw_request())))
    platform = SimpleNamespace(
        get_client=lambda: bot,
        meta=lambda: SimpleNamespace(id="qq-main", name="aiocqhttp"),
    )
    context = SimpleNamespace(
        get_platform_inst=lambda platform_id: (
            platform if platform_id == "qq-main" else None
        ),
        platform_manager=SimpleNamespace(get_insts=lambda: [platform]),
    )

    updated = run(runtime.process_request(context, request.request_id, approve=True))

    assert calls == ["check"]
    assert updated.status == "approved"
