"""入群申请事件驱动推送服务（core/request_push.py）测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.join_review_store import JoinReviewStore
from core.onebot import OneBotClient
from core.request_push import PUSH_REPLY_HINT, RequestPushService, build_opinion_line


def run(awaitable):
    return asyncio.run(awaitable)


class FakeBot:
    """最小 aiocqhttp Bot 桩：来源群名查询 + 带 message_id 的群发送。"""

    def __init__(self, fail_send: tuple[str, ...] = ()) -> None:
        self.fail_send = {str(g) for g in fail_send}
        self.sent: list[tuple[int, str]] = []
        self._next_message_id = 1000

    async def call_action(self, action: str, **params):
        if action == "get_group_info":
            return {"group_id": params["group_id"], "group_name": "申请群"}
        if action == "send_group_msg":
            group_id = int(params["group_id"])
            if str(group_id) in self.fail_send:
                raise RuntimeError("send failed")
            self.sent.append((group_id, str(params["message"])))
            self._next_message_id += 1
            return {"message_id": self._next_message_id}
        raise AssertionError(action)


class FakePlatform:
    def __init__(self, platform_id: str, bot: FakeBot | None) -> None:
        self._id = platform_id
        self._bot = bot

    def meta(self):
        return SimpleNamespace(id=self._id, name="aiocqhttp")

    def get_client(self):
        return self._bot


class FakeContext:
    """最小上下文桩：按 platform_id 解析 Bot。"""

    def __init__(
        self, platform_id: str = "qq-main", bot: FakeBot | None = None
    ) -> None:
        self._platform = FakePlatform(platform_id, bot)

    def get_platform_inst(self, platform_id: str):
        if platform_id == self._platform._id:
            return self._platform
        return None


def make_service(tmp_path):
    store = JoinReviewStore(tmp_path)
    return RequestPushService(store, OneBotClient()), store


def add_request(store, **overrides):
    values = {
        "request_id": "r1",
        "platform_id": "qq-main",
        "group_id": "100",
        "user_id": "200",
        "nickname": "小明",
        "level": "16",
        "question": "口令？",
        "answer": "溪流",
        "sub_type": "add",
        "flag": "flag-1",
    }
    values.update(overrides)
    return run(store.add_request(**values))


def make_config(store, **overrides):
    values = {
        "platform_id": "qq-main",
        "group_id": "100",
        "push_group_ids": ["300"],
    }
    values.update(overrides)
    return run(store.upsert_group_config(**values))


def make_decision(verdict="correct", confidence=0.95, reason="答案完全正确"):
    return SimpleNamespace(verdict=verdict, confidence=confidence, reason=reason)


# ------------------------------------------------------------------
# build_opinion_line
# ------------------------------------------------------------------


def test_opinion_line_without_decision():
    assert build_opinion_line(None) == "看法：该申请未经过自动审核。"


def test_opinion_line_with_decision():
    line = build_opinion_line(make_decision())
    assert line.startswith("看法：自动审核建议通过（置信度 0.95）")
    assert "答案完全正确" in line


# ------------------------------------------------------------------
# render_message
# ------------------------------------------------------------------


def test_render_formatted_has_answer_source_opinion_and_reply_hint(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store)

    message = run(
        service.render_message(request, config, "申请群", None, make_decision())
    )

    assert "入群申请待审核" in message
    assert "答案：溪流" in message
    assert "来源群：申请群（100）" in message
    assert "看法：自动审核建议通过" in message
    assert PUSH_REPLY_HINT in message


def test_render_formatted_without_decision_marks_no_auto_audit(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store)

    message = run(service.render_message(request, config, "申请群", None, None))

    assert "看法：该申请未经过自动审核。" in message
    assert PUSH_REPLY_HINT in message


def test_render_formatted_hides_answer_when_configured(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, include_answer=False)

    message = run(service.render_message(request, config, "申请群", None, None))

    assert "答案" not in message
    assert PUSH_REPLY_HINT in message


def test_render_natural_uses_llm_caller_and_asks_for_opinion(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural")
    prompts: list[str] = []

    async def llm_caller(prompt: str) -> str:
        prompts.append(prompt)
        return "自然语言通知文案"

    message = run(service.render_message(request, config, "申请群", llm_caller))

    assert message == "自然语言通知文案"
    assert len(prompts) == 1
    assert "口令？" in prompts[0]
    # natural 提示词要求 LLM 给出看法并引导引用回复审批
    assert "看法" in prompts[0]
    assert "引用" in prompts[0]
    assert "同意" in prompts[0]
    assert "管理页" in prompts[0]


@pytest.mark.parametrize("llm_result", ["", "   ", None])
def test_render_natural_falls_back_on_empty_llm(tmp_path, llm_result):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural")

    async def llm_caller(prompt: str):
        return llm_result

    message = run(service.render_message(request, config, "申请群", llm_caller))

    assert "入群申请待审核" in message
    assert PUSH_REPLY_HINT in message


def test_render_natural_falls_back_on_llm_exception(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural")

    async def llm_caller(prompt: str):
        raise RuntimeError("llm down")

    message = run(service.render_message(request, config, "申请群", llm_caller))

    assert "入群申请待审核" in message
    assert "答案：溪流" in message


# ------------------------------------------------------------------
# push_for_request
# ------------------------------------------------------------------


def _push(service, store, context, config=None, llm_caller=None, decision=None):
    request = add_request(store)
    config = config or make_config(store)
    return run(
        service.push_for_request(context, request, config, llm_caller, None, decision)
    )


def test_push_without_push_groups_falls_back_to_source_group(tmp_path):
    """推送群留空：回退推送到申请所属群本身。"""
    service, store = make_service(tmp_path)
    bot = FakeBot()
    context = FakeContext(bot=bot)
    config = make_config(store, push_group_ids=[])

    sent, skipped, failed = _push(service, store, context, config=config)

    assert (sent, skipped, failed) == (["100"], [], [])
    assert [group_id for group_id, _ in bot.sent] == [100]


def test_push_without_bot_reports_platform_unavailable(tmp_path):
    service, store = make_service(tmp_path)
    context = FakeContext(bot=None)
    config = make_config(store)

    sent, skipped, failed = _push(service, store, context, config=config)

    assert sent == [] and skipped == []
    assert failed and "平台不可用" in failed[0]


def test_push_success_via_onebot_and_records_push_ref(tmp_path):
    service, store = make_service(tmp_path)
    config = make_config(store, push_group_ids=["300", "301"])
    bot = FakeBot()
    context = FakeContext(bot=bot)

    sent, skipped, failed = _push(
        service, store, context, config=config, decision=make_decision()
    )

    assert sent == ["300", "301"]
    assert skipped == [] and failed == []
    # 走 OneBot 发送：拿到 message_id 并记录推送映射
    assert [group_id for group_id, _ in bot.sent] == [300, 301]
    request = run(store.get_request("r1"))
    refs = {(ref["group_id"], ref["message_id"]) for ref in request.push_refs}
    assert refs == {("300", "1001"), ("301", "1002")}
    # 文案只渲染一次，逐群群发同一内容，带来源群、看法与审批引导
    texts = [text for _, text in bot.sent]
    assert texts[0] == texts[1]
    assert "来源群：申请群（100）" in texts[0]
    assert "看法：自动审核建议通过" in texts[0]
    assert PUSH_REPLY_HINT in texts[0]


def test_push_records_send_failure_per_group(tmp_path):
    service, store = make_service(tmp_path)
    config = make_config(store, push_group_ids=["300", "301"])
    bot = FakeBot(fail_send=("300",))
    context = FakeContext(bot=bot)

    sent, skipped, failed = _push(service, store, context, config=config)

    assert sent == ["301"]
    assert len(failed) == 1
    assert "300" in failed[0]
    assert [group_id for group_id, _ in bot.sent] == [301]


def test_push_is_idempotent_per_target(tmp_path):
    """同一申请对同一推送群只推一次；重复事件投递时跳过已成功的群。"""
    service, store = make_service(tmp_path)
    config = make_config(store, push_group_ids=["300", "301"])
    bot = FakeBot(fail_send=("301",))
    context = FakeContext(bot=bot)
    request = add_request(store)

    first = run(service.push_for_request(context, request, config, None, None))
    assert first[0] == ["300"]
    assert first[2] and "301" in first[2][0]

    # 失败群释放占位可重试；成功群已记录 notified，不再重推。
    bot.fail_send.clear()
    second = run(service.push_for_request(context, request, config, None, None))
    assert second[0] == ["301"]
    assert second[1] == ["300"]
    assert [group_id for group_id, _ in bot.sent] == [300, 301]

    third = run(service.push_for_request(context, request, config, None, None))
    assert third == ([], ["300", "301"], [])
    assert len(bot.sent) == 2


def test_push_natural_style_uses_llm(tmp_path):
    service, store = make_service(tmp_path)
    config = make_config(store, push_style="natural")
    bot = FakeBot()
    context = FakeContext(bot=bot)

    async def llm_caller(prompt: str) -> str:
        return "人格化审核请求文案"

    sent, _, failed = _push(
        service, store, context, config=config, llm_caller=llm_caller
    )

    assert sent == ["300"] and failed == []
    assert bot.sent[0][1] == "人格化审核请求文案"
