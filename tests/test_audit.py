"""入群审核测试。"""

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
