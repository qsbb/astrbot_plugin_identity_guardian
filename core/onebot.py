"""OneBot V11 API 封装。

统一封装 event.bot.call_action，处理超时、错误和日志。
"""

from __future__ import annotations

import asyncio
from typing import Any

try:
    from ..series_diagnostics import logger
except ImportError:  # 兼容旧测试直接把 core 当作顶层包导入
    from series_diagnostics import logger

# 诊断时只打印这几个关键参数，避免大对象或敏感信息
_DIAG_KEYS = (
    "group_id",
    "user_id",
    "message_id",
    "sub_type",
    "enable",
    "approve",
)


def _safe_params(params: dict) -> str:
    """提取关键诊断参数，便于日志排查。"""
    if not params:
        return "{}"
    parts = []
    for key in _DIAG_KEYS:
        if key in params:
            parts.append(f"{key}={params[key]!r}")
    return "{" + ", ".join(parts) + "}" if parts else "(non-diag)"


def _describe_exc(exc: Exception) -> str:
    """提取异常的可读描述。

    aiocqhttp 的 `ActionFailed` 只在 `str()` 里带 retcode，
    真正有用的 `wording` / `msg` 藏在 `result` 字典里，排错时需要看到。
    这里用鸭子类型取，避免为一个日志字段硬依赖 aiocqhttp。
    """
    detail = ""
    result = getattr(exc, "result", None)
    if isinstance(result, dict):
        detail = str(result.get("wording") or result.get("msg") or "")
    base = f"{type(exc).__name__}: {exc}"
    return f"{base} ({detail})" if detail else base


class OneBotClient:
    """OneBot V11 API 调用封装。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def call(self, event: Any, action: str, **params: Any) -> Any:
        """统一封装 call_action，处理超时与错误。

        成功返回 API 响应的 data；data 为 null 时返回 `{}` 作为成功标记，
        因此调用方一律用 `result is not None` 判断成败。失败返回 None。

        注意 aiocqhttp 的 `call_action` 已经由 `_handle_api_result` 拆包，
        **只返回 data 字段**，并在 `status == "failed"` 时抛 `ActionFailed`。
        写操作（如 set_group_card）成功时 data 为 null，即返回 None。
        因此不能用 `resp["status"] == "ok"` 判断成功，否则所有写操作都会
        被误判为失败。这里改为「未抛异常即成功」。

        为兼容个别把完整响应包原样返回的适配器，仍保留信封判定分支。
        查询类失败降级为 debug（上层有 fallback），写操作失败保留 warning。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning("[idg] no bot available for action %s", action)
            return None
        return await self.call_bot(bot, action, **params)

    async def call_bot(self, bot: Any, action: str, **params: Any) -> Any:
        """Call a OneBot action through a public ``bot.call_action`` object.

        Dashboard requests do not have an ``AstrMessageEvent``. AstrBot's
        platform adapter exposes its client through ``platform.get_client()``;
        accepting that returned object keeps Page operations on the same checked
        wrapper as event-driven calls.
        """
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            logger.warning("[idg] invalid bot for action %s", action)
            return None
        # 查询类动作失败时上层有 fallback，降级为 debug 避免刷屏
        is_query = action.startswith("get_") or action in ("get_login_info",)
        try:
            resp = await asyncio.wait_for(
                call_action(action, **params), timeout=self.timeout
            )
            # 兼容分支：少数适配器返回未拆包的完整响应信封。
            # 仅当同时含 status 与 retcode 时才认定为信封，避免把恰好带
            # status 字段的业务 data 误判。
            if isinstance(resp, dict) and "status" in resp and "retcode" in resp:
                if resp.get("status") == "failed":
                    msg = resp.get("msg") or resp.get("wording") or "unknown error"
                    log = logger.debug if is_query else logger.warning
                    log(
                        "[idg] action %s failed: %s | params=%s",
                        action,
                        msg,
                        _safe_params(params),
                    )
                    return None
                data = resp.get("data")
                return {} if data is None else data
            # 标准路径：aiocqhttp 已拆包，未抛异常即成功。
            # 写操作 data 为 null，用 {} 表示「成功但无数据」。
            return {} if resp is None else resp
        except asyncio.TimeoutError:
            log = logger.debug if is_query else logger.warning
            log("[idg] action %s timed out | params=%s", action, _safe_params(params))
            return None
        except Exception as exc:
            log = logger.debug if is_query else logger.warning
            description = (
                type(exc).__name__
                if action == "set_group_add_request"
                else _describe_exc(exc)
            )
            log(
                "[idg] action %s error: %s | params=%s",
                action,
                description,
                _safe_params(params),
            )
            return None

    async def get_group_list(self, target: Any) -> list[dict[str, Any]] | None:
        """Return groups joined by the Bot represented by an event or bot."""
        result = await self._call_target(target, "get_group_list")
        if not isinstance(result, list):
            return None
        return [item for item in result if isinstance(item, dict)]

    async def get_login_info(self, target: Any) -> dict[str, Any] | None:
        """Return the active OneBot account identity."""
        result = await self._call_target(target, "get_login_info")
        return result if isinstance(result, dict) else None

    async def _call_target(self, target: Any, action: str, **params: Any) -> Any:
        if callable(getattr(target, "call_action", None)):
            return await self.call_bot(target, action, **params)
        return await self.call(target, action, **params)

    async def get_group_member_info_for_bot(
        self, bot: Any, group_id: int, user_id: int, no_cache: bool = False
    ) -> dict[str, Any] | None:
        result = await self.call_bot(
            bot,
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=no_cache,
        )
        return result if isinstance(result, dict) else None

    async def get_group_info_for_bot(
        self, bot: Any, group_id: int, no_cache: bool = False
    ) -> dict[str, Any] | None:
        result = await self.call_bot(
            bot, "get_group_info", group_id=group_id, no_cache=no_cache
        )
        return result if isinstance(result, dict) else None

    async def send_group_message(
        self, target: Any, group_id: int, message: str
    ) -> tuple[bool, str]:
        """Send plain text to a group through an event or runtime Bot."""
        ok, _, err = await self.send_group_message_with_id(target, group_id, message)
        return ok, err

    async def send_group_message_with_id(
        self, target: Any, group_id: int, message: str
    ) -> tuple[bool, str, str]:
        """Send plain text to a group, returning (ok, message_id, error).

        OneBot V11 ``send_group_msg`` 成功时 data 带 ``message_id``，引用回复
        审批需要它追踪推送消息；取不到 message_id 时返回空串但视为发送成功。
        """
        result = await self._call_target(
            target, "send_group_msg", group_id=group_id, message=message
        )
        if result is None:
            return False, "", "send_group_msg failed"
        message_id = ""
        if isinstance(result, dict):
            message_id = str(result.get("message_id") or "")
        return True, message_id, ""

    async def set_group_add_request_for_bot(
        self,
        bot: Any,
        flag: str,
        sub_type: str,
        approve: bool,
        reason: str = "",
    ) -> tuple[bool, str]:
        result = await self.call_bot(
            bot,
            "set_group_add_request",
            flag=flag,
            sub_type=sub_type,
            approve=approve,
            reason=reason,
        )
        if result is not None:
            return True, ""
        return False, "set_group_add_request failed"

    async def invite_group_member_for_bot(
        self, bot: Any, group_id: int, user_id: int
    ) -> tuple[bool, str]:
        """Invite a member through an explicitly exposed adapter extension.

        OneBot V11 has no standard action for a Bot inviting another member.
        Adapters that support the operation may expose one of these direct
        methods; an unsupported adapter fails closed without guessing an
        action name or misusing ``set_group_add_request``.
        """
        for method_name in ("invite_group_member", "send_group_invite"):
            method = getattr(bot, method_name, None)
            if not callable(method):
                continue
            try:
                result = await asyncio.wait_for(
                    method(group_id=group_id, user_id=user_id),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                return False, "invite_group_member timed out"
            except Exception as exc:
                return False, f"invite_group_member failed: {type(exc).__name__}"
            if result is False:
                return False, "invite_group_member failed"
            return True, ""
        return False, "invite_group_member unsupported by adapter"

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

    async def set_group_leave(
        self, event: Any, group_id: int
    ) -> tuple[bool, str]:
        """退出当前群，不支持解散群。"""
        result = await self.call(
            event,
            "set_group_leave",
            group_id=group_id,
            is_dismiss=False,
        )
        if result is not None:
            return True, ""
        return False, "set_group_leave failed"

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
