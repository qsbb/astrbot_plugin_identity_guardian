"""Tests for the isolated join-review JSON store."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.join_review_store import (
    JoinReviewStore,
    RequestNotActionable,
    ValidationError,
)


def run(coro):
    return asyncio.run(coro)


def test_unconfigured_group_defaults_off_without_writing(tmp_path):
    store = JoinReviewStore(tmp_path)

    config = run(store.get_group_config("bot-a", "10001"))

    assert config.configured is False
    assert config.auto_audit_enabled is False
    assert config.review_send_enabled is False
    assert config.notify_target == "target_group"
    assert config.include_answer is True
    assert not (tmp_path / "join_review.json").exists()


def test_group_configs_are_isolated_by_platform_and_group_and_persist(tmp_path):
    store = JoinReviewStore(tmp_path)
    run(
        store.upsert_group_config(
            platform_id="bot-a",
            group_id="10001",
            auto_audit_enabled=True,
            review_send_enabled=False,
            notify_target="both",
            specified_group_ids=["90001", "90001", "90002"],
            include_answer=False,
        )
    )
    run(
        store.upsert_group_config(
            platform_id="bot-b",
            group_id="10001",
            review_send_enabled=True,
        )
    )

    reloaded = JoinReviewStore(tmp_path)
    a = run(reloaded.get_group_config("bot-a", "10001"))
    b = run(reloaded.get_group_config("bot-b", "10001"))
    other = run(reloaded.get_group_config("bot-a", "10002"))

    assert a.configured and a.auto_audit_enabled and not a.review_send_enabled
    assert a.specified_group_ids == ("90001", "90002")
    assert not a.include_answer
    assert b.configured and not b.auto_audit_enabled and b.review_send_enabled
    assert not other.configured
    assert not list(tmp_path.glob(".*.tmp"))


def test_specified_notification_target_requires_strict_whitelist(tmp_path):
    store = JoinReviewStore(tmp_path)
    with pytest.raises(ValidationError, match="specified_groups_required"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                notify_target="specified_groups",
            )
        )
    with pytest.raises(ValidationError, match="invalid_specified_group_id"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                notify_target="both",
                specified_group_ids=["not-a-group"],
            )
        )
    with pytest.raises(ValidationError, match="invalid_notify_target"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                notify_target="arbitrary_group",
            )
        )


def test_batch_validates_before_write_and_rejects_duplicates(tmp_path):
    store = JoinReviewStore(tmp_path)
    with pytest.raises(ValidationError, match="duplicate_group_config"):
        run(
            store.batch_upsert_group_configs(
                [
                    {"platform_id": "bot-a", "group_id": "10001"},
                    {
                        "platform_id": "bot-a",
                        "group_id": "10001",
                        "auto_audit_enabled": True,
                    },
                ]
            )
        )
    assert run(store.list_group_configs()) == []
    assert not (tmp_path / "join_review.json").exists()

    result = run(
        store.batch_upsert_group_configs(
            [
                {
                    "platform_id": "bot-a",
                    "group_id": "10001",
                    "auto_audit_enabled": True,
                },
                {
                    "platform_id": "bot-a",
                    "group_id": "10002",
                    "review_send_enabled": True,
                },
            ]
        )
    )
    assert len(result) == 2


def _add(store, **overrides):
    values = {
        "platform_id": "bot-a",
        "group_id": "10001",
        "user_id": "20001",
        "nickname": "Alice",
        "level": "7",
        "question": "1+1=?",
        "answer": "2",
        "sub_type": "add",
        "flag": "onebot-secret-flag",
        "request_id": "opaque-request-id",
    }
    values.update(overrides)
    return run(store.add_request(**values))


def test_request_public_projection_never_exposes_flag_or_internal_error(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add(store, nickname="", level="")
    internal_file = json.loads((tmp_path / "join_review.json").read_text("utf-8"))

    public = request.to_public_dict()
    hidden_answer = request.to_public_dict(include_answer=False)

    assert internal_file["requests"][0]["flag"] == "onebot-secret-flag"
    assert public["nickname"] == "未知"
    assert public["level"] == "未知"
    assert public["answer"] == "2"
    assert "answer" not in hidden_answer
    for forbidden in ("flag", "platform_error", "notified_targets"):
        assert forbidden not in public


def test_request_text_limits_and_event_deduplication(tmp_path):
    store = JoinReviewStore(tmp_path)
    first = _add(store)
    duplicate = run(
        store.add_request(
            platform_id="bot-a",
            group_id="10001",
            user_id="20001",
            flag="onebot-secret-flag",
            sub_type="add",
            request_id="different-id",
        )
    )
    assert duplicate.request_id == first.request_id
    assert len(run(store.list_requests())) == 1

    with pytest.raises(ValidationError, match="question_too_long"):
        _add(
            store,
            request_id="another",
            flag="another-flag",
            question="x" * 2049,
        )


def test_successful_action_transitions_only_after_platform_success(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add(store)
    calls = []

    async def platform_action(internal_request):
        calls.append((internal_request.flag, internal_request.sub_type))
        assert internal_request.status == "pending"
        return True, ""

    updated = run(
        store.process_request(
            request.request_id,
            status="approved",
            platform_action=platform_action,
        )
    )
    assert updated.status == "approved"
    assert calls == [("onebot-secret-flag", "add")]
    with pytest.raises(RequestNotActionable, match="already_processed"):
        run(store.claim_request(request.request_id))


def test_platform_failure_is_not_false_success_and_can_retry(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add(store)

    failed = run(
        store.process_request(
            request.request_id,
            status="rejected",
            platform_action=lambda _request: (False, "api unavailable"),
        )
    )
    assert failed.status == "platform_error"
    assert failed.platform_error == "api unavailable"
    assert "platform_error" not in failed.to_public_dict()

    retried = run(
        store.process_request(
            request.request_id,
            status="rejected",
            platform_action=lambda _request: True,
        )
    )
    assert retried.status == "rejected"


def test_action_started_before_expiry_records_actual_platform_success(tmp_path):
    now = [100.0]
    store = JoinReviewStore(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    request = _add(store, created_at=100.0)
    claim = run(store.claim_request(request.request_id))
    now[0] = 111.0

    updated = run(
        store.finish_request(claim, platform_succeeded=True, status="approved")
    )

    assert updated.status == "approved"


def test_concurrent_admin_actions_only_call_platform_once(tmp_path):
    async def scenario():
        store = JoinReviewStore(tmp_path)
        request = await store.add_request(
            platform_id="bot-a",
            group_id="10001",
            user_id="20001",
            flag="secret",
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        call_count = 0

        async def slow_action(_request):
            nonlocal call_count
            call_count += 1
            entered.set()
            await release.wait()
            return True

        first = asyncio.create_task(
            store.process_request(
                request.request_id,
                status="approved",
                platform_action=slow_action,
            )
        )
        await entered.wait()
        with pytest.raises(RequestNotActionable, match="busy"):
            await store.process_request(
                request.request_id,
                status="rejected",
                platform_action=slow_action,
            )
        release.set()
        result = await first
        return result, call_count

    result, call_count = run(scenario())
    assert result.status == "approved"
    assert call_count == 1


def test_expiration_on_cleanup_and_startup_prevents_action(tmp_path):
    now = [100.0]
    store = JoinReviewStore(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    request = _add(store, created_at=100.0)
    now[0] = 111.0

    assert run(store.cleanup_expired()) == 1
    assert run(store.get_request(request.request_id)).status == "expired"
    with pytest.raises(RequestNotActionable, match="expired"):
        run(store.claim_request(request.request_id))

    # Loading an old actionable record performs startup expiry as well.
    payload = json.loads((tmp_path / "join_review.json").read_text("utf-8"))
    payload["requests"][0]["status"] = "pending"
    (tmp_path / "join_review.json").write_text(json.dumps(payload), encoding="utf-8")
    reloaded = JoinReviewStore(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    assert run(reloaded.get_request(request.request_id)).status == "expired"


def test_notification_delivery_is_idempotent_and_failure_can_retry(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add(store)

    first = run(store.claim_notification(request.request_id, "bot-a:90001"))
    assert first is not None
    assert run(store.claim_notification(request.request_id, "bot-a:90001")) is None
    assert not run(store.finish_notification(first, succeeded=False))
    assert run(store.get_request(request.request_id)).status == "pending"

    retry = run(store.claim_notification(request.request_id, "bot-a:90001"))
    assert retry is not None
    assert run(store.finish_notification(retry, succeeded=True))
    assert run(store.notification_sent(request.request_id, "bot-a:90001"))
    assert run(store.claim_notification(request.request_id, "bot-a:90001")) is None

    reloaded = JoinReviewStore(tmp_path)
    assert run(reloaded.notification_sent(request.request_id, "bot-a:90001"))


def test_close_releases_process_local_claims(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add(store)
    action_claim = run(store.claim_request(request.request_id))
    notification_claim = run(
        store.claim_notification(request.request_id, "bot-a:90001")
    )
    assert action_claim is not None and notification_claim is not None

    run(store.close())

    assert run(store.claim_request(request.request_id)) is not None
    assert run(store.claim_notification(request.request_id, "bot-a:90001")) is not None


def test_pinned_and_push_groups_roundtrip_and_validation(tmp_path):
    """按群置顶标记与推送群列表持久化，且推送群逐条严格校验。"""
    store = JoinReviewStore(tmp_path)
    run(
        store.upsert_group_config(
            platform_id="bot-a",
            group_id="10001",
            pinned=True,
            push_group_ids=["90001", "90001", "90002"],
            push_style="natural",
        )
    )

    reloaded = JoinReviewStore(tmp_path)
    config = run(reloaded.get_group_config("bot-a", "10001"))

    assert config.pinned is True
    assert config.push_group_ids == ("90001", "90002")
    assert config.push_style == "natural"
    assert config.to_dict()["push_group_ids"] == ["90001", "90002"]

    other = run(reloaded.get_group_config("bot-a", "10002"))
    assert other.pinned is False
    assert other.push_group_ids == ()
    assert other.push_style == "formatted"

    with pytest.raises(ValidationError, match="invalid_push_group_id"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                push_group_ids=["not-a-group"],
            )
        )
    with pytest.raises(ValidationError, match="invalid_push_style"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                push_style="fancy",
            )
        )


# ----------------------------------------------------- 推送消息映射（push_refs）


def _add_pending_request(store, **overrides):
    values = {
        "platform_id": "bot-a",
        "group_id": "10001",
        "user_id": "20001",
        "nickname": "小明",
        "flag": "flag-1",
    }
    values.update(overrides)
    return run(store.add_request(**values))


def test_push_ref_record_find_and_persist(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add_pending_request(store)

    assert run(store.record_push_ref(request.request_id, "30001", "1001")) is True
    # 幂等：重复记录同一映射不翻倍
    assert run(store.record_push_ref(request.request_id, "30001", "1001")) is True
    assert run(store.record_push_ref(request.request_id, "30002", "1002")) is True

    found = run(store.find_request_by_push_ref("bot-a", "30001", "1001"))
    assert found is not None and found.request_id == request.request_id
    found = run(store.find_request_by_push_ref("bot-a", "30002", "1002"))
    assert found is not None and found.request_id == request.request_id

    # 重载后映射仍在
    reloaded = JoinReviewStore(tmp_path)
    found = run(reloaded.find_request_by_push_ref("bot-a", "30002", "1002"))
    assert found is not None and found.request_id == request.request_id
    assert {tuple(sorted(ref.items())) for ref in found.push_refs} == {
        (("group_id", "30001"), ("message_id", "1001")),
        (("group_id", "30002"), ("message_id", "1002")),
    }


def test_push_ref_find_misses_on_wrong_scope(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add_pending_request(store)
    run(store.record_push_ref(request.request_id, "30001", "1001"))

    assert run(store.find_request_by_push_ref("bot-b", "30001", "1001")) is None
    assert run(store.find_request_by_push_ref("bot-a", "30002", "1001")) is None
    assert run(store.find_request_by_push_ref("bot-a", "30001", "9999")) is None
    with pytest.raises(ValidationError, match="invalid_push_ref_message_id"):
        run(store.find_request_by_push_ref("bot-a", "30001", ""))


def test_push_ref_unknown_request_returns_false(tmp_path):
    store = JoinReviewStore(tmp_path)
    assert run(store.record_push_ref("missing", "30001", "1001")) is False


def test_push_ref_expires_with_pending_ttl(tmp_path):
    now = [1000.0]
    store = JoinReviewStore(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    request = _add_pending_request(store)
    run(store.record_push_ref(request.request_id, "30001", "1001"))

    assert run(store.find_request_by_push_ref("bot-a", "30001", "1001")) is not None

    now[0] += 11
    found = run(store.find_request_by_push_ref("bot-a", "30001", "1001"))
    # 过期后申请被转为 expired，引用审批按已处理对待
    assert found is not None and found.status == "expired"


def test_push_ref_validation(tmp_path):
    store = JoinReviewStore(tmp_path)
    request = _add_pending_request(store)

    with pytest.raises(ValidationError, match="invalid_push_ref_group_id"):
        run(store.record_push_ref(request.request_id, "not-a-group", "1001"))
    with pytest.raises(ValidationError, match="invalid_push_ref_message_id"):
        run(store.record_push_ref(request.request_id, "30001", ""))


def test_group_join_questions_roundtrip_and_validation(tmp_path):
    """按群入群问答预设：去空去重、持久化回读与严格校验。"""
    store = JoinReviewStore(tmp_path)
    config = run(
        store.upsert_group_config(
            platform_id="bot-a",
            group_id="10001",
            join_questions=[
                {"question": "口令？", "answers": ["溪流", "溪流", "  ", "小溪"]},
                {"question": "", "answers": ["任意问题答案"]},
            ],
        )
    )
    assert config.join_questions == (
        {"question": "口令？", "answers": ("溪流", "小溪")},
        {"question": "", "answers": ("任意问题答案",)},
    )

    reloaded = JoinReviewStore(tmp_path)
    config2 = run(reloaded.get_group_config("bot-a", "10001"))
    assert config2.join_questions == config.join_questions
    assert config2.to_dict()["join_questions"] == [
        {"question": "口令？", "answers": ["溪流", "小溪"]},
        {"question": "", "answers": ["任意问题答案"]},
    ]

    with pytest.raises(ValidationError, match="join_question_answers_required"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                join_questions=[{"question": "口令？", "answers": []}],
            )
        )
    with pytest.raises(ValidationError, match="too_many_join_questions"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                join_questions=[{"question": "", "answers": ["a"]}] * 51,
            )
        )
    with pytest.raises(ValidationError, match="invalid_join_questions"):
        run(
            store.upsert_group_config(
                platform_id="bot-a",
                group_id="10001",
                join_questions="口令？|溪流",
            )
        )
