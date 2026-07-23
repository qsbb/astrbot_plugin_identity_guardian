"""bot 进群欢迎服务。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import Config
from .prompts import BOT_JOIN_HINT


class WelcomeService:
    """bot 进群时的行为控制。"""

    def __init__(self, config: Config) -> None:
        self.config = config

    def should_speak(self) -> bool:
        """bot 进群是否应该主动发言。"""
        return self.config.welcome_bot_speak

    def get_template(self) -> str:
        """获取发言模板。"""
        return self.config.welcome_template

    def get_hint(self) -> str:
        """获取给 LLM 的进群提示。"""
        return BOT_JOIN_HINT

    async def on_bot_join(
        self, event: Any, group_id: str, group_name: str = ""
    ) -> str | None:
        """bot 进群处理。

        返回要发送的文本，或 None 表示不发言。
        """
        if not self.should_speak():
            logger.info("[idg] bot joined group %s, not speaking", group_id)
            return None

        template = self.get_template()
        if template:
            text = template.replace("{group_name}", group_name).replace(
                "{group_id}", group_id
            )
            return text

        return None
