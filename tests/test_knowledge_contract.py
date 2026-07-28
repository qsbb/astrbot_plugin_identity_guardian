"""序→知 知识桥接契约校验测试。

历史问题：桥接靠 duck-typing 探测对端 `recall` 方法，而知从未提供该方法，
结果桥接长期静默失效——没有异常、没有告警、审核永远拿不到证据。

以下用例锁定契约化后的行为：对端在场但契约不符时必须 warning 告警并停用，
不得退化成静默降级；契约相符时才接入并正常解析证据。

与项目其余测试保持一致，直接用 asyncio.run 驱动协程，不引入 pytest-asyncio。
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import knowledge as knowledge_mod  # noqa: E402
from core.config import Config  # noqa: E402
from core.knowledge import (  # noqa: E402
    SUPPORTED_CONTRACT_MAJOR,
    SUPPORTED_CONTRACT_NAME,
    KnowledgeService,
)


def _config(enabled: bool = True) -> Config:
    return Config({"enable_active_learner_recall": enabled})


class FakeLearner:
    """知的替身：可自定义契约声明与 recall 返回值。"""

    def __init__(self, contract, results=None):
        self._contract = contract
        self._results = results if results is not None else []
        self.calls: list[dict] = []

    def knowledge_contract(self):
        if isinstance(self._contract, Exception):
            raise self._contract
        return self._contract

    async def recall(self, query: str, scope: str = "", top_k: int = 5):
        self.calls.append({"query": query, "scope": scope, "top_k": top_k})
        return self._results


class NoContractLearner:
    """只有 recall、不声明契约：模拟未升级的旧版本对端。"""

    async def recall(self, query: str, scope: str = ""):
        return [{"content": "旧实现", "source": "legacy", "score": 1.0}]


class FakeContext:
    def __init__(self, instance):
        self._instance = instance

    def get_star_instance(self, name: str):
        return self._instance if name == "astrbot_plugin_active_learner" else None


def _valid_contract(version: str = "1.0"):
    return {
        "name": SUPPORTED_CONTRACT_NAME,
        "version": version,
        "plugin": "astrbot_plugin_active_learner",
        "capabilities": ("recall",),
    }


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    """隔离 star_handlers_registry，避免候选发现受全局桩影响。"""
    from astrbot.core.star import star_handlers_registry as registry_mod

    monkeypatch.setattr(registry_mod.star_handlers_registry, "handlers", [])


class _Captured:
    """收集桥接日志，断言告警而非静默降级。"""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    @property
    def warnings(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


def _recall(svc, query: str = "查询", scope: str = "", *, safe: bool = False):
    """执行一次检索，同时捕获桥接日志。"""
    captured = _Captured()
    handler = logging.Handler()
    handler.emit = captured.records.append  # type: ignore[method-assign]
    logger = knowledge_mod.logger
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        coro = svc.recall_safe(query, scope) if safe else svc.recall(query, scope)
        return asyncio.run(coro), captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


# -- 契约相符 ---------------------------------------------------------


def test_matching_contract_connects_and_parses_evidence():
    learner = FakeLearner(
        _valid_contract(),
        [{"content": "证据A", "source": "wiki", "score": 0.8}],
    )
    svc = KnowledgeService(_config(), FakeContext(learner))

    evidence, _ = _recall(svc, scope="group:1")

    assert svc.is_available()
    assert svc.contract_version == "1.0"
    assert svc.unavailable_reason == ""
    assert [e.content for e in evidence] == ["证据A"]
    assert evidence[0].source == "wiki"
    assert evidence[0].score == pytest.approx(0.8)
    assert learner.calls[0]["scope"] == "group:1"


def test_higher_minor_version_is_accepted():
    """minor 更高属向后兼容的新增，应继续接入。"""
    learner = FakeLearner(
        _valid_contract("1.7"),
        [{"content": "证据", "source": "s", "score": 1.0, "extra_field": "忽略"}],
    )
    svc = KnowledgeService(_config(), FakeContext(learner))

    evidence, _ = _recall(svc)

    assert svc.is_available()
    assert svc.contract_version == "1.7"
    assert len(evidence) == 1


# -- 契约失配：必须告警且停用 -----------------------------------------


def test_major_mismatch_disables_bridge_with_warning():
    incompatible = f"{SUPPORTED_CONTRACT_MAJOR + 1}.0"
    learner = FakeLearner(_valid_contract(incompatible), [{"content": "x"}])
    svc = KnowledgeService(_config(), FakeContext(learner))

    evidence, captured = _recall(svc)

    assert evidence == []
    assert not svc.is_available()
    assert "主版本不兼容" in svc.unavailable_reason
    assert captured.warnings, "契约失配必须 warning 告警，否则退化为静默失效"
    # 停用意味着根本不调用对端。
    assert learner.calls == []


def test_legacy_provider_without_contract_is_rejected():
    """未声明契约的旧对端不再被 duck-typing 接受。"""
    svc = KnowledgeService(_config(), FakeContext(NoContractLearner()))

    evidence, captured = _recall(svc)

    assert evidence == []
    assert not svc.is_available()
    assert "未声明 knowledge_contract" in svc.unavailable_reason
    assert captured.warnings


def test_contract_name_mismatch_is_rejected():
    learner = FakeLearner({"name": "other.contract", "version": "1.0"})
    svc = KnowledgeService(_config(), FakeContext(learner))

    evidence, _ = _recall(svc)
    assert evidence == []
    assert "契约名不匹配" in svc.unavailable_reason


def test_unparsable_version_is_rejected():
    svc = KnowledgeService(
        _config(), FakeContext(FakeLearner(_valid_contract("不是版本号")))
    )
    evidence, _ = _recall(svc)
    assert evidence == []
    assert "无法解析" in svc.unavailable_reason


def test_contract_declaration_raising_is_rejected():
    svc = KnowledgeService(_config(), FakeContext(FakeLearner(RuntimeError("boom"))))
    evidence, _ = _recall(svc)
    assert evidence == []
    assert "契约声明调用失败" in svc.unavailable_reason


def test_non_dict_contract_is_rejected():
    svc = KnowledgeService(_config(), FakeContext(FakeLearner("1.0")))
    evidence, _ = _recall(svc)
    assert evidence == []
    assert "不是 dict" in svc.unavailable_reason


# -- 返回值违约 -------------------------------------------------------


def test_non_list_result_warns_and_returns_empty():
    learner = FakeLearner(_valid_contract(), results="不是列表")
    svc = KnowledgeService(_config(), FakeContext(learner))

    evidence, captured = _recall(svc)

    assert evidence == []
    assert any("违反契约" in message for message in captured.warnings)


def test_malformed_items_are_skipped():
    """脏数据逐条跳过，不影响合法证据。"""
    learner = FakeLearner(
        _valid_contract(),
        [
            "字符串不再被接受",
            {"content": "  ", "source": "空白内容"},
            {"content": "有效", "source": "s", "score": "非数字"},
        ],
    )
    svc = KnowledgeService(_config(), FakeContext(learner))

    evidence, _ = _recall(svc)

    assert [e.content for e in evidence] == ["有效"]
    assert evidence[0].score == pytest.approx(0.0)


def test_recall_exception_is_swallowed_as_unavailable():
    class Boom(FakeLearner):
        async def recall(self, query: str, scope: str = "", top_k: int = 5):
            raise RuntimeError("检索炸了")

    svc = KnowledgeService(_config(), FakeContext(Boom(_valid_contract())))
    evidence, _ = _recall(svc, safe=True)
    assert evidence == []


# -- 开关与缺省 -------------------------------------------------------


def test_disabled_switch_skips_bridge_entirely():
    learner = FakeLearner(_valid_contract(), [{"content": "证据"}])
    svc = KnowledgeService(_config(enabled=False), FakeContext(learner))

    evidence, _ = _recall(svc)
    assert evidence == []
    assert learner.calls == []


def test_absent_provider_is_info_not_warning():
    """对端未安装属正常部署形态，不应告警。"""
    svc = KnowledgeService(_config(), FakeContext(None))

    evidence, captured = _recall(svc)

    assert evidence == []
    assert "未发现" in svc.unavailable_reason
    assert not captured.warnings


def test_missing_context_reports_reason():
    svc = KnowledgeService(_config(), None)
    evidence, _ = _recall(svc)
    assert evidence == []
    assert "无 context" in svc.unavailable_reason


def test_parse_major_handles_edge_cases():
    parse = knowledge_mod._parse_major
    assert parse("1.0") == 1
    assert parse("2") == 2
    assert parse(" 3.4.5 ") == 3
    assert parse("") is None
    assert parse(None) is None
    assert parse("v1.0") is None
