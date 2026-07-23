"""OneBot V11 API 封装。

统一封装 event.bot.call_action，处理超时、错误和日志。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger


class OneBotClient:
    """OneBot V11 API 调用封装。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def call(self, event: Any, action: str, **params: Any) -> dict | None:
        """统一封装 call_action，处理超时与错误。

        返回 API 响应 data 字段，失败返回 None。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning("[idg] no bot available for action %s", action)
            return None
        try:
            resp = await asyncio.wait_for(
                bot.call_action(action, **params), timeout=self.timeout
            )
            if isinstance(resp, dict):
                if resp.get("status") == "ok":
                    return resp.get("data")
                logger.warning(
                    "[idg] action %s failed: %s", action, resp.get("msg", "")
                )
                return None
            return resp
        except asyncio.TimeoutError:
            logger.warning("[idg] action %s timed out", action)
            return None
        except Exception as exc:
            logger.warning("[idg] action %s error: %s", action, exc)
            return None

    async def get_group_member_info(
        self, event: Any, group_id: int, user_id: int, no_cache: bool = False
    ) -> dict | None:
        """获取群成员信息。"""
        return await self.call(
            event,
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=no_cache,
        )

    async def get_group_member_list(self, event: Any, group_id: int) -> list | None:
        """获取群成员列表。"""
        result = await self.call(event, "get_group_member_list", group_id=group_id)
        return result if isinstance(result, list) else None

    async def get_group_info(
        self, event: Any, group_id: int, no_cache: bool = False
    ) -> dict | None:
        """获取群信息。"""
        return await self.call(
            event, "get_group_info", group_id=group_id, no_cache=no_cache
        )

    async def get_group_info_safe(self, event: Any, group_id: str) -> dict:
        """安全获取群信息，失败返回空 dict。"""
        try:
            gid = int(group_id)
            info = await self.get_group_info(event, gid)
            return info if isinstance(info, dict) else {}
        except (ValueError, TypeError):
            return {}

    async def set_group_ban(
        self, event: Any, group_id: int, user_id: int, duration: int = 1800
    ) -> tuple[bool, str]:
        """禁言群成员。"""
        result = await self.call(
            event,
            "set_group_ban",
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        )
        if result is not None:
            return True, ""
        return False, "set_group_ban failed"

    async def set_group_whole_ban(
        self, event: Any, group_id: int, enable: bool = True
    ) -> tuple[bool, str]:
        """全员禁言。"""
        result = await self.call(
            event, "set_group_whole_ban", group_id=group_id, enable=enable
        )
        if result is not None:
            return True, ""
        return False, "set_group_whole_ban failed"

    async def set_group_kick(
        self,
        event: Any,
        group_id: int,
        user_id: int,
        reject_add_request: bool = False,
    ) -> tuple[bool, str]:
        """踢出群成员。"""
        result = await self.call(
            event,
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
            reject_add_request=reject_add_request,
        )
        if result is not None:
            return True, ""
        return False, "set_group_kick failed"

    async def delete_msg(self, event: Any, message_id: int) -> tuple[bool, str]:
        """撤回消息。"""
        result = await self.call(event, "delete_msg", message_id=message_id)
        if result is not None:
            return True, ""
        return False, "delete_msg failed"

    async def set_group_card(
        self, event: Any, group_id: int, user_id: int, card: str = ""
    ) -> tuple[bool, str]:
        """设置群名片。"""
        result = await self.call(
            event, "set_group_card", group_id=group_id, user_id=user_id, card=card
        )
        if result is not None:
            return True, ""
        return False, "set_group_card failed"

    async def set_group_special_title(
        self, event: Any, group_id: int, user_id: int, special_title: str = ""
    ) -> tuple[bool, str]:
        """设置群头衔。"""
        result = await self.call(
            event,
            "set_group_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=special_title,
        )
        if result is not None:
            return True, ""
        return False, "set_group_special_title failed"

    async def set_group_admin(
        self, event: Any, group_id: int, user_id: int, enable: bool = True
    ) -> tuple[bool, str]:
        """设置管理员。"""
        result = await self.call(
            event,
            "set_group_admin",
            group_id=group_id,
            user_id=user_id,
            enable=enable,
        )
        if result is not None:
            return True, ""
        return False, "set_group_admin failed"

    async def set_group_name(
        self, event: Any, group_id: int, group_name: str
    ) -> tuple[bool, str]:
        """修改群名。"""
        result = await self.call(
            event, "set_group_name", group_id=group_id, group_name=group_name
        )
        if result is not None:
            return True, ""
        return False, "set_group_name failed"

    async def set_group_add_request(
        self, event: Any, flag: str, sub_type: str, approve: bool, reason: str = ""
    ) -> tuple[bool, str]:
        """处理入群请求。"""
        result = await self.call(
            event,
            "set_group_add_request",
            flag=flag,
            sub_type=sub_type,
            approve=approve,
            reason=reason,
        )
        if result is not None:
            return True, ""
        return False, "set_group_add_request failed"
