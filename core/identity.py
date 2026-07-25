"""身份管理：bot、发送者与目标群角色查询和缓存。"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .config import Config
from .models import ActorContext, Role
from .onebot import OneBotClient
from .relationship import RelationshipService

# OneBot role 值映射：1=owner, 2=admin, 3=member
_OB_ROLE_MAP: dict[int, str] = {1: "owner", 2: "admin", 3: "member"}

# 缓存结构: (group_id, user_id) -> (role_str, timestamp)
_ROLE_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

# OneBot 实现之间 role 字段类型不统一，这里统一归一化
_VALID_ROLES = frozenset(("owner", "admin", "member"))


def extract_ob_role(info: Any) -> str | None:
    """从 get_group_member_info 响应中解析角色。

    返回 None 表示无法确定角色（查询失败或字段缺失/不可识别），
    调用方应按降级处理且不要缓存该结果。
    """
    if not isinstance(info, dict):
        return None

    ob_role = info.get("role")
    if isinstance(ob_role, str):
        role = ob_role.strip().lower()
        return role if role in _VALID_ROLES else None
    if isinstance(ob_role, dict):
        name = str(ob_role.get("name", "")).strip().lower()
        return name if name in _VALID_ROLES else None
    if isinstance(ob_role, int) and not isinstance(ob_role, bool):
        return _OB_ROLE_MAP.get(ob_role)
    return None


class IdentityManager:
    """身份查询与缓存管理。"""

    def __init__(
        self,
        config: Config,
        onebot: OneBotClient,
        relationship: RelationshipService,
    ) -> None:
        self.config = config
        self.onebot = onebot
        self.relationship = relationship

    def _cache_get(self, group_id: str, user_id: str) -> str | None:
        """从缓存获取角色。"""
        key = (group_id, user_id)
        entry = _ROLE_CACHE.get(key)
        if entry is None:
            return None
        role, ts = entry
        if time.time() - ts > self.config.identity_refresh_interval:
            return None
        return role

    def _cache_set(self, group_id: str, user_id: str, role: str) -> None:
        """设置缓存。"""
        _ROLE_CACHE[(group_id, user_id)] = (role, time.time())

    def clear_cache(self) -> None:
        """清空所有缓存。"""
        _ROLE_CACHE.clear()

    async def get_role(self, event: Any, group_id: str, user_id: str) -> str:
        """获取用户在群中的角色。

        优先从缓存获取，其次调用 OneBot API 查询，失败时回退为 member。
        """
        cached = self._cache_get(group_id, user_id)
        if cached is not None:
            return cached

        try:
            gid = int(group_id)
            uid = int(user_id)
        except (ValueError, TypeError):
            return Role.MEMBER.value

        info = await self.onebot.get_group_member_info(event, gid, uid, no_cache=False)
        role = extract_ob_role(info)
        if role is None:
            # 查询失败或响应里没有可用的 role 字段时降级为 member，但不写入缓存。
            # 否则一次接口抖动就会把真实的管理员身份在整个刷新周期内锁死为普通成员，
            # 导致本该允许的管理动作被持续拒绝。
            logger.debug(
                "[idg] get_role failed for %s in %s, fallback to member (not cached)",
                user_id,
                group_id,
            )
            return Role.MEMBER.value

        self._cache_set(group_id, user_id, role)
        return role

    async def get_actor_context(
        self,
        event: Any,
        platform_id: str,
        group_id: str,
        self_id: str,
        sender_id: str,
        target_id: str | None = None,
    ) -> ActorContext:
        """构建完整的身份与关系上下文。

        从平台事件获取真实身份，不信任聊天文本中的自称。
        """
        bot_role = await self.get_role(event, group_id, self_id)
        requester_role = await self.get_role(event, group_id, sender_id)
        requester_relation = self.relationship.relation_for(
            sender_id, requester_role, bot_role
        )

        target_role: str | None = None
        target_relation: str | None = None
        if target_id and target_id != sender_id:
            target_role = await self.get_role(event, group_id, target_id)
            target_relation = self.relationship.relation_for(
                target_id, target_role or "member", bot_role
            )

        return ActorContext(
            bot_role=bot_role,
            bot_id=self_id,
            requester_id=sender_id,
            requester_role=requester_role,
            requester_relation=requester_relation,
            target_id=target_id,
            target_role=target_role,
            target_relation=target_relation,
            group_id=group_id,
            platform_id=platform_id,
        )

    async def get_sender_name(self, event: Any, group_id: str, sender_id: str) -> str:
        """获取发送者显示名称。"""
        try:
            gid = int(group_id)
            uid = int(sender_id)
            info = await self.onebot.get_group_member_info(event, gid, uid)
            if info and isinstance(info, dict):
                card = info.get("card") or ""
                nickname = info.get("nickname") or ""
                return card or nickname or f"用户{sender_id[-4:]}"
        except (ValueError, TypeError):
            pass
        return f"用户{sender_id[-4:]}" if sender_id else "用户"
