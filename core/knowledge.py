"""active_learner 知识库只读检索桥接。

通过 AstrBot 插件服务发现机制获取 active_learner 的公开接口，
不直接导入对方内部存储类，不直接调用面向主 LLM 的 FunctionTool，
也不读取对方私有数据库文件。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import Config
from .models import KnowledgeEvidence


class KnowledgeService:
    """active_learner 知识检索桥接服务。"""

    def __init__(self, config: Config, context: Any = None) -> None:
        self.config = config
        self.context = context
        self._provider: Any = None
        self._initialized = False

    async def _ensure_provider(self) -> bool:
        """延迟初始化知识库 provider。

        尝试通过 context 获取 active_learner 的公开服务接口。
        """
        if self._initialized:
            return self._provider is not None

        self._initialized = True
        if self.context is None:
            return False

        # 尝试通过 context 获取已加载的插件实例
        try:
            # 方式1：通过 context.get_star_instance
            get_star = getattr(self.context, "get_star_instance", None)
            if callable(get_star):
                instance = get_star("astrbot_plugin_active_learner")
                if instance is not None:
                    # 检查是否有 recall 方法
                    recall = getattr(instance, "recall", None)
                    if callable(recall):
                        self._provider = instance
                        logger.info(
                            "[idg] active_learner provider connected via get_star_instance"
                        )
                        return True

            # 方式2：通过 star_handlers_registry 查找
            try:
                from astrbot.core.star.star_handlers_registry import (
                    star_handlers_registry,
                )

                for handler in getattr(star_handlers_registry, "handlers", []):
                    full_name = str(getattr(handler, "full_name", "") or "")
                    if "astrbot_plugin_active_learner" in full_name:
                        instance = getattr(handler, "handler", None)
                        if instance is not None:
                            # 尝试获取绑定的实例
                            func = getattr(instance, "__self__", None)
                            if func is not None:
                                recall = getattr(func, "recall", None)
                                if callable(recall):
                                    self._provider = func
                                    logger.info(
                                        "[idg] active_learner provider connected via registry"
                                    )
                                    return True
            except Exception:
                pass

        except Exception as exc:
            logger.debug("[idg] active_learner provider lookup failed: %s", exc)

        logger.info("[idg] active_learner not available, knowledge recall disabled")
        return False

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
            # 调用 active_learner 的公开 recall 方法
            results = await self._provider.recall(query=query, scope=scope)
            if not isinstance(results, list):
                return []

            evidence: list[KnowledgeEvidence] = []
            for item in results[:5]:
                if isinstance(item, dict):
                    evidence.append(
                        KnowledgeEvidence(
                            content=str(item.get("content", "")),
                            source=str(item.get("source", "")),
                            score=float(item.get("score", 0.0)),
                        )
                    )
                elif isinstance(item, str):
                    evidence.append(KnowledgeEvidence(content=item))
                elif hasattr(item, "content"):
                    evidence.append(
                        KnowledgeEvidence(
                            content=str(getattr(item, "content", "")),
                            source=str(getattr(item, "source", "")),
                            score=float(getattr(item, "score", 0.0)),
                        )
                    )
            return evidence
        except Exception as exc:
            logger.warning("[idg] knowledge recall failed: %s", exc)
            return []

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
