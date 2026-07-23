"""内容审核服务（独立 LLM，B+C 方案）。

第二层防线：对未命中关键词规则的消息，调用独立 LLM 判断是否违规。
与主对话 LLM 完全隔离，无注入风险。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

from .config import Config
from .models import ModerationResult, Punishment, PunishmentLevel
from .prompts import build_moderation_prompt


class ModerationService:
    """内容审核服务。"""

    def __init__(self, config: Config, llm_caller: Any = None) -> None:
        self.config = config
        self._llm_caller = llm_caller
        self._compiled_rules: list[re.Pattern] = []
        self._compile_rules()

    def _compile_rules(self) -> None:
        """编译关键词正则规则。"""
        self._compiled_rules = []
        for pattern in self.config.moderation_rules:
            try:
                self._compiled_rules.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[idg] invalid moderation rule '%s': %s", pattern, exc)

    def reload_rules(self) -> None:
        """重新编译规则。"""
        self._compile_rules()

    def check_rules(self, message: str) -> ModerationResult:
        """第一层：关键词正则预筛。命中即固定处罚。"""
        for pattern in self._compiled_rules:
            if pattern.search(message):
                return ModerationResult(
                    is_violation=True,
                    level=PunishmentLevel.MUTE_SHORT.value,
                    reason=f"命中关键词规则: {pattern.pattern}",
                    confidence=1.0,
                )
        return ModerationResult()

    async def check_llm(self, message: str) -> ModerationResult:
        """第二层：独立 LLM 审核。"""
        if self._llm_caller is None:
            return ModerationResult()

        prompt = build_moderation_prompt(message)
        try:
            response = await self._llm_caller(prompt)
            return self._parse_llm_result(response)
        except Exception as exc:
            logger.warning("[idg] moderation LLM failed: %s", exc)
            return ModerationResult()

    async def moderate(self, message: str) -> ModerationResult:
        """完整审核流程：先关键词，后 LLM。"""
        # 第一层：关键词
        result = self.check_rules(message)
        if result.is_violation:
            return result

        # 第二层：独立 LLM
        if self.config.auto_moderate:
            result = await self.check_llm(message)
            if result.confidence < self.config.manual_threshold:
                return ModerationResult()

        return result

    def determine_punishment(self, result: ModerationResult) -> Punishment:
        """根据审核结果确定处罚。"""
        if not result.is_violation:
            return Punishment()

        level = result.level
        auto_max = self.config.auto_confirm_threshold

        # 如果处罚等级超过自动执行上限，降级为警告
        level_order = [
            PunishmentLevel.NONE.value,
            PunishmentLevel.WARN.value,
            PunishmentLevel.MUTE_SHORT.value,
            PunishmentLevel.MUTE_LONG.value,
            PunishmentLevel.DELETE.value,
            PunishmentLevel.KICK.value,
        ]
        if level_order.index(level) > level_order.index(auto_max):
            level = PunishmentLevel.WARN.value

        if level == PunishmentLevel.WARN.value:
            return Punishment(level=level)
        if level == PunishmentLevel.MUTE_SHORT.value:
            return Punishment(level=level, mute_duration=300)
        if level == PunishmentLevel.MUTE_LONG.value:
            return Punishment(
                level=level, mute_duration=min(3600, self.config.max_mute_seconds)
            )
        if level == PunishmentLevel.DELETE.value:
            return Punishment(level=level, delete_msg=True, mute_duration=300)
        if level == PunishmentLevel.KICK.value:
            return Punishment(level=level, kick=True)

        return Punishment()

    def _parse_llm_result(self, response: str) -> ModerationResult:
        """解析 LLM 返回的审核结果。"""
        try:
            json_match = re.search(r"\{[^}]+\}", response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response)

            return ModerationResult(
                is_violation=bool(data.get("is_violation", False)),
                level=str(data.get("level", "none")),
                reason=str(data.get("reason", "")),
                confidence=float(data.get("confidence", 0.0)),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[idg] failed to parse moderation result: %s", exc)
            return ModerationResult()
