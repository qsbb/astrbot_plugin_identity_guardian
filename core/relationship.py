"""关系解析：主人/友好用户/普通成员。"""

from __future__ import annotations

from .config import Config
from .models import Relation


class RelationshipService:
    """用户与 bot 的关系解析服务。

    关系来源优先级：
    1. 事件中的真实 QQ id → owner_users 主人配置
    2. friendly_users 友好用户配置
    3. 群成员资料中的 owner/admin role
    4. normal

    关系名称仅供提示词解释，最终工具授权始终由 PolicyEngine 决定。
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def relation_for(
        self,
        user_id: str,
        user_role: str,
        bot_role: str = "member",
    ) -> str:
        """解析用户与 bot 的关系。

        Args:
            user_id: 用户 QQ 号
            user_role: 用户在群中的角色 (owner/admin/member)
            bot_role: bot 在群中的角色

        Returns:
            关系字符串: owner / friendly / normal / unknown
        """
        uid = str(user_id)

        # 1. 检查是否是 bot 主人
        if self.config.is_owner(uid):
            return Relation.OWNER.value

        # 2. 检查是否在友好用户列表中
        if self.config.is_friendly(uid):
            return Relation.FRIENDLY.value

        # 3. 群主和管理员自动视为友好用户
        if user_role in ("owner", "admin"):
            return Relation.FRIENDLY.value

        # 4. 普通成员
        return Relation.NORMAL.value

    def is_protected(self, user_id: str, user_role: str = "member") -> bool:
        """判断用户是否受强保护。

        强保护用户包括：
        - owner_users 中的主人
        - protected_users 列表中的用户
        - 群主和管理员
        """
        uid = str(user_id)
        if self.config.is_protected(uid):
            return True
        if self.config.is_owner(uid):
            return True
        if user_role in ("owner", "admin"):
            return True
        return False

    def is_owner(self, user_id: str) -> bool:
        """判断是否是 bot 主人。"""
        return self.config.is_owner(str(user_id))

    def is_friendly(self, user_id: str, user_role: str = "member") -> bool:
        """判断是否是友好用户。"""
        uid = str(user_id)
        if self.config.is_owner(uid) or self.config.is_friendly(uid):
            return True
        if user_role in ("owner", "admin"):
            return True
        return False
