"""active_learner 知识库只读检索桥接。

通过 AstrBot 插件服务发现机制获取 active_learner 的公开接口，
不直接导入对方内部存储类，不直接调用面向主 LLM 的 FunctionTool，
也不读取对方私有数据库文件。

契约校验（CONVENTIONS.md 第 11 节）
-----------------------------------
早期实现靠 duck-typing 探测对端是否有 `recall` 方法。这种做法有两个后果：
对端没实现该方法时，桥接静默降级为「永远无证据」，日志只有一条 info，排查困难；
对端改了返回结构时，字段读空同样无声。

现在要求对端显式提供 `knowledge_contract()`，本模块校验其 major 版本：
- 缺少契约方法 → 视为不支持桥接，warning 告警；
- major 不一致 → 停用桥接并 warning 告警，不做兼容猜测；
- major 一致、minor 更高 → 允许，按已知字段读取（向后兼容的新增）。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import Config
from .models import KnowledgeEvidence

_LEARNER_PLUGIN_NAME = "astrbot_plugin_active_learner"

# 本模块支持的知识桥接契约。major 必须与对端一致，详见模块 docstring。
SUPPORTED_CONTRACT_NAME = "active_learner.knowledge"
SUPPORTED_CONTRACT_MAJOR = 1
SUPPORTED_CONTRACT_VERSION = "1.0"


def _parse_major(version: Any) -> int | None:
    """取语义化版本的 major 段，无法解析时返回 None。"""
    text = str(version or "").strip()
    if not text:
        return None
    head = text.split(".", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


class KnowledgeService:
    """active_learner 知识检索桥接服务。"""

    def __init__(self, config: Config, context: Any = None) -> None:
        self.config = config
        self.context = context
        self._provider: Any = None
        self._initialized = False
        self._contract_version: str = ""
        self._unavailable_reason: str = ""

    # -- provider 发现与契约校验 --------------------------------------

    def _iter_candidates(self) -> list[Any]:
        """列出可能的 active_learner 插件实例，按可靠性排序。"""
        candidates: list[Any] = []
        get_star = getattr(self.context, "get_star_instance", None)
        if callable(get_star):
            try:
                instance = get_star(_LEARNER_PLUGIN_NAME)
            except Exception as exc:
                logger.debug("[idg] get_star_instance 查找失败: %s", exc)
            else:
                if instance is not None:
                    candidates.append(instance)

        try:
            from astrbot.core.star.star_handlers_registry import (
                star_handlers_registry,
            )
        except Exception:
            return candidates

        for handler in getattr(star_handlers_registry, "handlers", []) or []:
            full_name = str(getattr(handler, "full_name", "") or "")
            if _LEARNER_PLUGIN_NAME not in full_name:
                continue
            bound = getattr(handler, "handler", None)
            owner = getattr(bound, "__self__", None)
            if owner is not None and owner not in candidates:
                candidates.append(owner)
        return candidates

    def _accept(self, instance: Any) -> bool:
        """校验候选实例的契约，通过则接入。"""
        declare = getattr(instance, "knowledge_contract", None)
        if not callable(declare):
            self._unavailable_reason = "对端未声明 knowledge_contract()"
            return False
        try:
            contract = declare()
        except Exception as exc:
            self._unavailable_reason = f"契约声明调用失败: {exc}"
            return False
        if not isinstance(contract, dict):
            self._unavailable_reason = "契约声明返回值不是 dict"
            return False

        name = str(contract.get("name") or "")
        if name != SUPPORTED_CONTRACT_NAME:
            self._unavailable_reason = (
                f"契约名不匹配: 期望 {SUPPORTED_CONTRACT_NAME}，实际 {name or '空'}"
            )
            return False

        version = str(contract.get("version") or "")
        major = _parse_major(version)
        if major is None:
            self._unavailable_reason = f"契约版本无法解析: {version or '空'}"
            return False
        if major != SUPPORTED_CONTRACT_MAJOR:
            self._unavailable_reason = (
                f"契约主版本不兼容: 本插件支持 {SUPPORTED_CONTRACT_VERSION}，"
                f"对端为 {version}"
            )
            return False

        if not callable(getattr(instance, "recall", None)):
            self._unavailable_reason = "对端声明了契约但缺少 recall()"
            return False

        self._provider = instance
        self._contract_version = version
        self._unavailable_reason = ""
        return True

    async def _ensure_provider(self) -> bool:
        """延迟初始化知识库 provider，并校验契约版本。"""
        if self._initialized:
            return self._provider is not None

        self._initialized = True
        if self.context is None:
            self._unavailable_reason = "无 context，无法发现对端插件"
            return False

        candidates = self._iter_candidates()
        if not candidates:
            # 未安装或未启用属正常部署形态，不告警。
            self._unavailable_reason = "未发现 active_learner 插件实例"
            logger.info("[idg] 知不可用，知识联动关闭: %s", self._unavailable_reason)
            return False

        for instance in candidates:
            if self._accept(instance):
                logger.info(
                    "[idg] 知识桥接已接入，契约 %s@%s",
                    SUPPORTED_CONTRACT_NAME,
                    self._contract_version,
                )
                return True

        # 对端在场却接不上：属于失配，必须告警而非静默降级。
        logger.warning(
            "[idg] 知识桥接停用（契约失配）: %s。入群审核将按无证据处理，保持待审。",
            self._unavailable_reason or "未知原因",
        )
        return False

    @property
    def contract_version(self) -> str:
        """已接入的对端契约版本，未接入时为空串。"""
        return self._contract_version

    @property
    def unavailable_reason(self) -> str:
        """桥接不可用的原因，供诊断页展示；可用时为空串。"""
        return self._unavailable_reason

    async def recall(self, query: str, scope: str = "") -> list[KnowledgeEvidence]:
        """检索知识库。

        Returns:
            证据列表，失败时返回空列表。
        """
        if not self.config.enable_active_learner_recall:
            return []

        if not await self._ensure_provider():
            return []

        try:
            # 契约 1.x：recall(query, scope, top_k) -> list[dict]
            results = await self._provider.recall(query=query, scope=scope)
        except Exception as exc:
            logger.warning("[idg] 知识检索调用失败: %s", exc)
            return []

        if not isinstance(results, list):
            # 契约要求返回 list；类型不符说明对端实现与声明的版本不一致。
            logger.warning(
                "[idg] 知识检索返回类型违反契约 %s: 期望 list，实际 %s",
                self._contract_version or "未知",
                type(results).__name__,
            )
            return []

        evidence: list[KnowledgeEvidence] = []
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            evidence.append(
                KnowledgeEvidence(
                    content=content,
                    source=str(item.get("source", "")),
                    score=score,
                )
            )
        return evidence

    async def recall_safe(self, query: str, scope: str = "") -> list[KnowledgeEvidence]:
        """安全检索知识库，失败时返回空列表。

        联动不可用或没有证据时返回空列表，
        调用方应将此视为 unavailable，不影响入群审核（保持待审）。
        """
        try:
            return await self.recall(query, scope)
        except Exception as exc:
            logger.debug("[idg] knowledge recall_safe swallowed: %s", exc)
            return []

    def is_available(self) -> bool:
        """检查知识库是否可用。"""
        return self._provider is not None
