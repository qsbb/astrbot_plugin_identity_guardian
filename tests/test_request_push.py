"""入群申请事件驱动推送服务（core/request_push.py）测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.join_review_store import JoinReviewStore
from core.onebot import OneBotClient
from core.request_push import (
    PUSH_REPLY_HINT,
    RequestPushService,
    build_opinion_line,
    render_push_preview,
    resolve_push_targets,
)


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


def test_render_formatted_hidden_answer_does_not_call_opinion_llm(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, include_answer=False)
    prompts: list[str] = []

    async def llm_caller(prompt: str) -> str:
        prompts.append(prompt)
        return "不应被调用的评价"

    message = run(
        service.render_message(request, config, "申请群", llm_caller, make_decision())
    )

    assert "答案" not in message
    assert "溪流" not in message
    assert "答案完全正确" not in message
    assert prompts == []


def test_render_natural_redacts_answer_when_configured(tmp_path):
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural", include_answer=False)
    prompts: list[str] = []

    async def llm_caller(prompt: str) -> str:
        prompts.append(prompt)
        return "不应被调用的自然文案"

    preview = run(
        render_push_preview(request, config, "申请群", llm_caller, make_decision())
    )

    assert preview["style"] == "natural_redacted_formatted"
    assert "答案" not in preview["text"]
    assert "溪流" not in preview["text"]
    assert "答案完全正确" not in preview["text"]
    assert prompts == []


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
    prompt = prompts[0]
    # natural 提示词只给全部事实 + 场景，让 AI 按人设自由措辞
    for fact in ("小明", "200", "16", "口令？", "溪流", "申请群", "100"):
        assert fact in prompt
    # 唯一硬性要求：引导管理员引用本条消息回复同意或拒绝
    assert "引用本条消息回复同意或拒绝" in prompt
    # 不再限制字段罗列方式/措辞，也不强制旧版 200 字上限
    assert "不超过 200 字" not in prompt


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


# ------------------------------------------------------------------
# render_push_preview：与 render_message 同一路径，额外标注 style
# ------------------------------------------------------------------


def test_preview_formatted_marks_style(tmp_path):
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store)

    preview = run(render_push_preview(request, config, "申请群", None, make_decision()))

    assert preview["style"] == "formatted"
    assert preview["opinion_source"] == "decision"
    assert "入群申请待审核" in preview["text"]
    assert "看法：自动审核建议通过" in preview["text"]
    assert PUSH_REPLY_HINT in preview["text"]


def test_preview_natural_success_marks_style(tmp_path):
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural")

    async def llm_caller(prompt: str) -> str:
        return "自然文案"

    preview = run(render_push_preview(request, config, "申请群", llm_caller))

    assert preview == {"style": "natural", "text": "自然文案", "opinion_source": "llm"}


@pytest.mark.parametrize("llm_result", ["", "   ", None])
def test_preview_natural_empty_falls_back_and_marks_style(tmp_path, llm_result):
    """natural 回退格式化时 LLM 刚失败过：看法直接用自动审核结论，不再重试。"""
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural")
    calls: list[str] = []

    async def llm_caller(prompt: str):
        calls.append(prompt)
        return llm_result

    preview = run(
        render_push_preview(request, config, "申请群", llm_caller, make_decision())
    )

    assert preview["style"] == "natural_fallback_formatted"
    assert preview["opinion_source"] == "decision"
    assert "入群申请待审核" in preview["text"]
    assert "看法：自动审核建议通过" in preview["text"]
    assert PUSH_REPLY_HINT in preview["text"]
    # natural 整段生成已含看法，回退后不重复调 LLM 生成看法
    assert len(calls) == 1


def test_preview_natural_exception_falls_back_and_marks_style(tmp_path):
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="natural")

    async def llm_caller(prompt: str):
        raise RuntimeError("llm down")

    preview = run(render_push_preview(request, config, "申请群", llm_caller))

    assert preview["style"] == "natural_fallback_formatted"
    assert preview["opinion_source"] == "none"
    assert "答案：溪流" in preview["text"]


def test_preview_shares_render_path_with_render_message(tmp_path):
    """预览与生产推送共用同一渲染实现，两者文案逐字一致。"""
    service, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store)

    preview = run(render_push_preview(request, config, "申请群", None, make_decision()))
    message = run(
        service.render_message(request, config, "申请群", None, make_decision())
    )

    assert preview["text"] == message


# ------------------------------------------------------------------
# formatted 样式的 LLM 一句话看法
# ------------------------------------------------------------------


def test_preview_formatted_uses_llm_opinion(tmp_path):
    """formatted + caller 可用：看法由 LLM 一句话生成，不再用 decision 行。"""
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="formatted")
    prompts: list[str] = []

    async def llm_caller(prompt: str) -> str:
        prompts.append(prompt)
        return "答案靠谱，正是群内暗号"

    preview = run(
        render_push_preview(request, config, "申请群", llm_caller, make_decision())
    )

    assert preview["style"] == "formatted"
    assert preview["opinion_source"] == "llm"
    assert "看法：答案靠谱，正是群内暗号" in preview["text"]
    assert "自动审核" not in preview["text"]
    assert PUSH_REPLY_HINT in preview["text"]
    assert len(prompts) == 1
    # 看法提示词带上问答与昵称、来源群，且要求纯文本短评
    assert "口令？" in prompts[0] and "溪流" in prompts[0]
    assert "小明" in prompts[0] and "申请群" in prompts[0]
    assert "80" in prompts[0]


def test_preview_formatted_llm_opinion_collapsed_to_one_line(tmp_path):
    """LLM 返回多行/超长时折叠成一行并截断。"""
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="formatted")

    async def llm_caller(prompt: str) -> str:
        return "第一行\n第二行　" + "长" * 200

    preview = run(render_push_preview(request, config, "申请群", llm_caller))

    assert preview["opinion_source"] == "llm"
    opinion_lines = [
        line for line in preview["text"].splitlines() if line.startswith("看法：")
    ]
    assert len(opinion_lines) == 1
    assert "第一行 第二行" in opinion_lines[0]
    assert len(opinion_lines[0]) <= 3 + 120


@pytest.mark.parametrize("llm_result", ["", "   ", None])
def test_preview_formatted_llm_empty_falls_back_to_decision(tmp_path, llm_result):
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="formatted")

    async def llm_caller(prompt: str):
        return llm_result

    preview = run(
        render_push_preview(request, config, "申请群", llm_caller, make_decision())
    )

    assert preview["style"] == "formatted"
    assert preview["opinion_source"] == "decision"
    assert "看法：自动审核建议通过" in preview["text"]


def test_preview_formatted_llm_exception_falls_back_to_decision(tmp_path):
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="formatted")

    async def llm_caller(prompt: str):
        raise RuntimeError("llm down")

    preview = run(
        render_push_preview(request, config, "申请群", llm_caller, make_decision())
    )

    assert preview["opinion_source"] == "decision"
    assert "看法：自动审核建议通过" in preview["text"]


def test_preview_formatted_without_caller_and_decision_marks_none(tmp_path):
    """caller 与 decision 都没有：看法行如实标注未经过自动审核。"""
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_style="formatted")

    preview = run(render_push_preview(request, config, "申请群", None, None))

    assert preview["opinion_source"] == "none"
    assert "看法：该申请未经过自动审核。" in preview["text"]


# ------------------------------------------------------------------
# resolve_push_targets
# ------------------------------------------------------------------


def test_resolve_push_targets_falls_back_to_source_group(tmp_path):
    """push_group_ids 留空：回退到申请所属群本身。"""
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_group_ids=[])
    assert resolve_push_targets(request, config) == ["100"]


def test_resolve_push_targets_uses_configured_groups(tmp_path):
    """push_group_ids 非空：按配置顺序返回。"""
    _, store = make_service(tmp_path)
    request = add_request(store)
    config = make_config(store, push_group_ids=["300", "301"])
    assert resolve_push_targets(request, config) == ["300", "301"]
