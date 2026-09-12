"""序：series.webui@2.0 管理面契约与审批动作测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_identity_guardian.core.join_review_store import (  # noqa: E402
    JoinReviewStore,
    RequestNotActionable,
)
from astrbot_plugin_identity_guardian.series_webui import (  # noqa: E402
    APPROVE_ACTION_ID,
    REJECT_ACTION_ID,
    REFRESH_ACTION_ID,
    SeriesWebUIPanels,
)


def run(awaitable):
    return asyncio.run(awaitable)


class StubRuntime:
    """记录适配层转交的审批参数；真实状态机由 core 测试覆盖。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.processed: set[str] = set()

    async def process_request(
        self,
        context,
        request_id: str,
        *,
        approve: bool,
        reason: str = "",
    ):
        self.calls.append(
            {
                "context": context,
                "request_id": request_id,
                "approve": approve,
                "reason": reason,
            }
        )
        if request_id == "missing-request":
            raise RequestNotActionable("not_found")
        if request_id in self.processed:
            raise RequestNotActionable("already_processed")
        self.processed.add(request_id)
        return SimpleNamespace(
            request_id=request_id,
            status="approved" if approve else "rejected",
        )


class FakePlugin:
    def __init__(self, tmp_path: Path) -> None:
        self.join_review_store = JoinReviewStore(tmp_path)
        self.join_review = StubRuntime()
        self.context = object()
        self.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)


@pytest.fixture()
def env(tmp_path: Path):
    plugin = FakePlugin(tmp_path)
    adapter = SeriesWebUIPanels(plugin)
    try:
        yield adapter, plugin
    finally:
        run(plugin.join_review_store.close())


async def seed_request(
    store: JoinReviewStore,
    *,
    request_id: str,
    answer: str = "secret-answer",
    flag: str = "onebot-secret-flag",
):
    return await store.add_request(
        request_id=request_id,
        platform_id="qq-main",
        group_id="10001",
        user_id="20002",
        nickname="申请人",
        question="入群问题",
        answer=answer,
        flag=flag,
    )


def response(adapter: SeriesWebUIPanels, action: str, payload: dict, role: str = "owner"):
    return run(
        adapter.panel_action(
            "join_review",
            action,
            payload,
            context={"actor": {"role": role}},
        )
    )


def test_contract_declares_owner_only_high_risk_actions(env):
    adapter, _ = env
    contract = adapter.contract()

    assert contract["name"] == "series.webui@2.0"
    assert contract["state_owner"] == "plugin"
    assert contract["managed"]["preferred_surface"] == "kernel"
    assert contract["standalone"]["entry"] == "/pages/join_review"
    assert {"generic_table", "generic_actions", "idempotency"} <= set(
        contract["capabilities"]
    )
    panel = contract["panels"][0]
    actions = {item["id"]: item for item in panel["actions"]}
    assert set(actions) == {
        REFRESH_ACTION_ID,
        APPROVE_ACTION_ID,
        REJECT_ACTION_ID,
    }
    for action_id in (APPROVE_ACTION_ID, REJECT_ACTION_ID):
        action = actions[action_id]
        assert action["min_role"] == "owner"
        assert action["effect"] == "non_idempotent"
        assert action["idempotency_required"] is True
        assert action["revision_required"] is False
        assert action["confirm"]
        fields = {field["name"] for field in action["payload_fields"]}
        assert "request_id" in fields
    assert actions[REFRESH_ACTION_ID]["min_role"] == "admin"
    assert actions[REFRESH_ACTION_ID]["effect"] == "idempotent"


def test_panel_rows_hide_answer_flag_and_internal_error(env):
    adapter, plugin = env
    run(seed_request(plugin.join_review_store, request_id="request-one"))

    data = run(adapter.panel_data("join_review"))

    assert data["success"] is True
    assert [row["request_id"] for row in data["rows"]] == ["request-one"]
    serialized = json.dumps(data, ensure_ascii=False)
    assert "secret-answer" not in serialized
    assert "onebot-secret-flag" not in serialized
    assert "question" not in serialized
    assert "answer" not in serialized
    assert "platform_error" not in serialized


def test_approve_and_reject_delegate_to_existing_runtime(env):
    adapter, plugin = env
    run(seed_request(plugin.join_review_store, request_id="approve-me"))
    run(seed_request(plugin.join_review_store, request_id="reject-me"))

    approved = response(adapter, APPROVE_ACTION_ID, {"request_id": "approve-me"})
    rejected = response(
        adapter,
        REJECT_ACTION_ID,
        {"request_id": "reject-me", "reason": "资料不匹配"},
    )

    assert approved == {
        "success": True,
        "message": "已批准入群申请",
        "request_id": "approve-me",
        "status": "approved",
    }
    assert rejected == {
        "success": True,
        "message": "已驳回入群申请",
        "request_id": "reject-me",
        "status": "rejected",
    }
    assert plugin.join_review.calls == [
        {
            "context": plugin.context,
            "request_id": "approve-me",
            "approve": True,
            "reason": "",
        },
        {
            "context": plugin.context,
            "request_id": "reject-me",
            "approve": False,
            "reason": "资料不匹配",
        },
    ]
    serialized = json.dumps([approved, rejected], ensure_ascii=False)
    assert "secret-answer" not in serialized
    assert "onebot-secret-flag" not in serialized


def test_unknown_and_repeated_requests_return_stable_codes(env):
    adapter, plugin = env
    run(seed_request(plugin.join_review_store, request_id="already-me"))

    first = response(adapter, APPROVE_ACTION_ID, {"request_id": "already-me"})
    repeated = response(adapter, APPROVE_ACTION_ID, {"request_id": "already-me"})
    missing = response(adapter, APPROVE_ACTION_ID, {"request_id": "missing-request"})

    assert first["success"] is True
    assert repeated["error"] == "REQUEST_ALREADY_PROCESSED"
    assert missing["error"] == "REQUEST_NOT_FOUND"


def test_adapter_also_rejects_insufficient_role_before_runtime(env):
    adapter, plugin = env
    run(seed_request(plugin.join_review_store, request_id="owner-only"))

    result = response(
        adapter,
        APPROVE_ACTION_ID,
        {"request_id": "owner-only"},
        role="admin",
    )

    assert result["success"] is False
    assert result["error"] == "ROLE_FORBIDDEN"
    assert plugin.join_review.calls == []


def test_invalid_payload_and_refresh_are_fail_closed(env):
    adapter, plugin = env
    run(seed_request(plugin.join_review_store, request_id="request-two"))

    assert response(adapter, APPROVE_ACTION_ID, {})["error"] == "INVALID_PAYLOAD"
    assert response(
        adapter,
        APPROVE_ACTION_ID,
        {"request_id": "request-two", "answer": "do-not-forward"},
    )["error"] == "INVALID_PAYLOAD"
    assert response(
        adapter,
        REJECT_ACTION_ID,
        {"request_id": "request-two", "reason": "x" * 257},
    )["error"] == "REJECT_REASON_TOO_LONG"
    refreshed = response(adapter, REFRESH_ACTION_ID, {}, role="admin")
    assert refreshed["success"] is True
    assert refreshed["count"] == 1
