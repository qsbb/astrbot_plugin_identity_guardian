"""入群审核测试。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit import JoinAuditService  # noqa: E402
from core.config import Config  # noqa: E402
from core.knowledge import KnowledgeService  # noqa: E402
from core.models import JoinVerdict  # noqa: E402
from core.onebot import OneBotClient  # noqa: E402


def _make_config(**overrides):
    defaults = {
        "join_questions": [
            "1+1=?|2,二",
        ],
        "join_approve_threshold": 0.9,
        "enable_active_learner_recall": False,
    }
    defaults.update(overrides)
    return Config(defaults)


def _make_service(config, llm_caller=None):
    knowledge = KnowledgeService(config)
    onebot = OneBotClient()
    return JoinAuditService(config, onebot, knowledge, llm_caller)


class RecordingOneBot:
    def __init__(self):
        self.calls = []

    async def set_group_add_request(
        self, event, flag, sub_type, approve=True, reason=""
    ):
        self.calls.append((event, flag, sub_type, approve, reason))
        return True, ""


def _correct_request():
    return {
        "flag": "request-flag",
        "sub_type": "add",
        "user_id": 123456,
        "group_id": 789,
        "comment": "问题：1+1=?\n答案：2",
    }


def test_parse_request_basic():
    """解析入群请求基本字段。"""
    svc = _make_service(_make_config())
    raw = {
        "flag": "test_flag",
        "sub_type": "add",
        "user_id": 123456,
        "group_id": 789,
        "comment": "答案：2",
    }
    flag, sub_type, user_id, group_id, answer = svc.parse_request(raw)
    assert flag == "test_flag"
    assert sub_type == "add"
    assert user_id == "123456"
    assert group_id == "789"
    assert answer == "2"


def test_parse_request_with_question():
    """解析带问题和答案的附言。"""
    svc = _make_service(_make_config())
    raw = {
        "flag": "f",
        "sub_type": "add",
        "user_id": 1,
        "group_id": 2,
        "comment": "问题：1+1=?\n答案：2",
    }
    _, _, _, _, answer = svc.parse_request(raw)
    assert answer == "2"
    question = svc.extract_question(raw["comment"])
    assert question == "1+1=?"


def test_exact_match_correct():
    """精确匹配参考答案 — correct。"""
    svc = _make_service(_make_config())
    import asyncio

    decision = asyncio.run(
        svc.judge_answer("1+1=?", "2", _make_config().join_questions)
    )
    assert decision.verdict == JoinVerdict.CORRECT.value
    assert decision.confidence >= 0.9


def test_fuzzy_match_correct():
    """模糊匹配参考答案 — correct。"""
    svc = _make_service(_make_config())
    import asyncio

    decision = asyncio.run(
        svc.judge_answer("1+1=?", "答案是2", _make_config().join_questions)
    )
    assert decision.verdict == JoinVerdict.CORRECT.value


def test_no_match_uncertain():
    """未匹配到答案 — uncertain。"""
    svc = _make_service(_make_config())
    import asyncio

    decision = asyncio.run(
        svc.judge_answer("1+1=?", "不知道", _make_config().join_questions)
    )
    assert decision.verdict == JoinVerdict.UNCERTAIN.value


def test_empty_answer():
    """空答案 — uncertain。"""
    svc = _make_service(_make_config())
    import asyncio

    decision = asyncio.run(svc.judge_answer("1+1=?", "", _make_config().join_questions))
    assert decision.verdict == JoinVerdict.UNCERTAIN.value


def test_llm_parse_correct():
    """LLM 返回 correct — 正确解析。"""

    async def mock_llm(prompt):
        return '{"verdict": "correct", "confidence": 0.95, "reason": "回答正确"}'

    svc = _make_service(_make_config(), llm_caller=mock_llm)
    import asyncio

    decision = asyncio.run(svc.judge_answer("未知问题", "某个答案", []))
    assert decision.verdict == JoinVerdict.CORRECT.value
    assert decision.confidence == 0.95


def test_llm_parse_uncertain():
    """LLM 返回 uncertain — 正确解析。"""

    async def mock_llm(prompt):
        return '{"verdict": "uncertain", "confidence": 0.3, "reason": "无法判断"}'

    svc = _make_service(_make_config(), llm_caller=mock_llm)
    import asyncio

    decision = asyncio.run(svc.judge_answer("未知问题", "模糊答案", []))
    assert decision.verdict == JoinVerdict.UNCERTAIN.value


def test_string_format_question_answers():
    """字符串格式 ``问题|答案1,答案2`` 正确解析为 question/answers。"""
    cfg = _make_config()
    questions = cfg.join_questions
    assert len(questions) == 1
    assert questions[0]["question"] == "1+1=?"
    assert questions[0]["answers"] == ["2", "二"]


def test_string_format_answer_only():
    """不含 ``|`` 时整体视为答案，问题为空。"""
    cfg = _make_config(join_questions=["技术交流"])
    questions = cfg.join_questions
    assert len(questions) == 1
    assert questions[0]["question"] == ""
    assert questions[0]["answers"] == ["技术交流"]


def test_legacy_dict_format_compatible():
    """旧的 dict 格式仍可正常解析。"""
    cfg = _make_config(
        join_questions=[{"question": "本群做什么的", "answers": ["技术交流", "编程"]}]
    )
    questions = cfg.join_questions
    assert len(questions) == 1
    assert questions[0]["question"] == "本群做什么的"
    assert questions[0]["answers"] == ["技术交流", "编程"]


def test_mixed_format():
    """字符串格式与 dict 格式混用 — 全部解析。"""
    cfg = _make_config(
        join_questions=["1+1=?|2", {"question": "2+2=?", "answers": ["4"]}]
    )
    questions = cfg.join_questions
    assert len(questions) == 2
    assert questions[0]["question"] == "1+1=?"
    assert questions[0]["answers"] == ["2"]
    assert questions[1]["question"] == "2+2=?"
    assert questions[1]["answers"] == ["4"]


def test_empty_entries_skipped():
    """空字符串条目被跳过。"""
    cfg = _make_config(join_questions=["", "  ", "1+1=?|2"])
    questions = cfg.join_questions
    assert len(questions) == 1


def test_notify_only_never_executes_automatic_approval():
    """notify_only 即使判断为高置信度正确，也不能调用放行接口。"""
    svc = _make_service(_make_config(join_audit_mode="notify_only"))
    onebot = RecordingOneBot()
    svc.onebot = onebot

    decision = asyncio.run(svc.handle_request(object(), _correct_request()))

    assert decision.verdict == JoinVerdict.CORRECT.value
    assert decision.confidence >= 0.9
    assert svc.should_auto_approve(decision) is False
    assert onebot.calls == []


def test_approve_only_executes_high_confidence_approval():
    """approve_only 保留原有的高置信度自动通过行为。"""
    svc = _make_service(_make_config(join_audit_mode="approve_only"))
    onebot = RecordingOneBot()
    svc.onebot = onebot

    decision = asyncio.run(svc.handle_request(object(), _correct_request()))

    assert svc.should_auto_approve(decision) is True
    assert len(onebot.calls) == 1
    assert onebot.calls[0][1:] == ("request-flag", "add", True, "")


# ----------------------------------------------------- execute_auto_audit 三段链路


class StubKnowledge:
    """知识联动桩：返回固定证据列表。"""

    def __init__(self, evidence=()):
        self._evidence = list(evidence)
        self.calls = 0

    async def recall_safe(self, query, scope=None):
        self.calls += 1
        return self._evidence


def _auto_raw(comment="问题：口令？\n答案：溪流"):
    return {
        "flag": "f1",
        "sub_type": "add",
        "user_id": 1,
        "group_id": 2,
        "comment": comment,
    }


def test_auto_audit_preset_exact_match_approves_without_llm():
    """预设精确命中：直接批准，不需要 LLM。"""

    async def llm(prompt):
        raise AssertionError("不应调用 LLM")

    svc = _make_service(_make_config(join_questions=[]), llm_caller=llm)
    onebot = RecordingOneBot()
    svc.onebot = onebot

    result = asyncio.run(
        svc.execute_auto_audit(
            object(),
            _auto_raw(),
            configured_questions=[{"question": "口令？", "answers": ["溪流"]}],
        )
    )

    assert result.platform_approved is True
    assert len(onebot.calls) == 1


def test_auto_audit_preset_llm_semantic_match_approves():
    """预设精确/模糊不中：LLM 对预设答案语义比对命中后批准。"""
    calls = []

    async def llm(prompt):
        calls.append(prompt)
        return '{"verdict": "correct", "confidence": 0.95, "reason": "语义一致"}'

    svc = _make_service(_make_config(join_questions=[]), llm_caller=llm)
    onebot = RecordingOneBot()
    svc.onebot = onebot

    result = asyncio.run(
        svc.execute_auto_audit(
            object(),
            _auto_raw("问题：口令？\n答案：小河"),
            configured_questions=[{"question": "口令？", "answers": ["溪流"]}],
        )
    )

    assert result.platform_approved is True
    assert len(calls) == 1
    assert "溪流" in calls[0]  # 预设答案进入 LLM 判定 prompt


def test_auto_audit_knowledge_evidence_approves_after_preset_miss():
    """预设不中 → 知识证据命中：带证据 LLM 判断批准后放行。"""
    calls = []

    async def llm(prompt):
        calls.append(prompt)
        if "大佬说过" in prompt:
            return '{"verdict": "correct", "confidence": 0.95, "reason": "证据支持"}'
        return '{"verdict": "uncertain", "confidence": 0.3, "reason": "预设不像"}'

    svc = _make_service(
        _make_config(join_questions=[], enable_active_learner_recall=True),
        llm_caller=llm,
    )
    svc.knowledge = StubKnowledge(evidence=["群里大佬说过答案是溪流"])
    onebot = RecordingOneBot()
    svc.onebot = onebot

    result = asyncio.run(
        svc.execute_auto_audit(
            object(),
            _auto_raw("问题：口令？\n答案：小河"),
            configured_questions=[{"question": "口令？", "answers": ["鹅卵石"]}],
        )
    )

    assert result.platform_approved is True
    assert svc.knowledge.calls == 1
    assert len(calls) == 2  # 预设语义一次 + 知识证据一次


def test_auto_audit_preset_miss_and_no_evidence_falls_back_uncertain():
    """预设不中 + 知识无证据：UNCERTAIN 兜底，不放行。"""
    svc = _make_service(
        _make_config(join_questions=[], enable_active_learner_recall=True),
        llm_caller=lambda prompt: asyncio.sleep(
            0, '{"verdict": "uncertain", "confidence": 0.3, "reason": "不像"}'
        ),
    )
    svc.knowledge = StubKnowledge(evidence=[])
    onebot = RecordingOneBot()
    svc.onebot = onebot

    result = asyncio.run(
        svc.execute_auto_audit(
            object(),
            _auto_raw("问题：口令？\n答案：小河"),
            configured_questions=[{"question": "口令？", "answers": ["鹅卵石"]}],
        )
    )

    assert result.decision.verdict == JoinVerdict.UNCERTAIN.value
    assert "无知识证据" in result.decision.reason
    assert result.platform_approved is False
    assert onebot.calls == []


def test_auto_audit_no_presets_no_recall_uncertain_without_llm():
    """无预设且联动关闭：直接 UNCERTAIN，且绝不调用 LLM 自由判断。"""

    async def llm(prompt):
        raise AssertionError("无预设无证据时不得调用 LLM")

    svc = _make_service(_make_config(join_questions=[]), llm_caller=llm)
    onebot = RecordingOneBot()
    svc.onebot = onebot

    result = asyncio.run(svc.execute_auto_audit(object(), _auto_raw()))

    assert result.decision.verdict == JoinVerdict.UNCERTAIN.value
    assert result.decision.reason == "无预设答案且无知识证据"
    assert onebot.calls == []


def test_auto_audit_per_group_presets_take_priority_over_global():
    """按群预设优先于全局回退；configured_questions=None 时回退全局。"""
    calls = []

    async def llm(prompt):
        calls.append(prompt)
        return '{"verdict": "uncertain", "confidence": 0.3, "reason": "不像"}'

    svc = _make_service(_make_config(join_questions=["口令？|溪流"]), llm_caller=llm)
    onebot = RecordingOneBot()
    svc.onebot = onebot

    # 按群预设存在时，全局预设不参与判定
    result = asyncio.run(
        svc.execute_auto_audit(
            object(),
            _auto_raw("问题：口令？\n答案：别的"),
            configured_questions=[{"question": "口令？", "answers": ["按群答案"]}],
        )
    )
    assert result.decision.verdict == JoinVerdict.UNCERTAIN.value
    assert "按群答案" in calls[0]
    assert "溪流" not in calls[0].split("参考答案")[1]

    # configured_questions=None：回退全局预设，精确命中直接批准
    result2 = asyncio.run(
        svc.execute_auto_audit(object(), _auto_raw(), configured_questions=None)
    )
    assert result2.platform_approved is True
