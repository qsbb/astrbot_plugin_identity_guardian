"""统一授权策略引擎。

所有有副作用的工具调用都必须通过 PolicyEngine.evaluate() 进行授权判断。
不信任 LLM 传入的参数表达的关系，只信任平台事件与配置中的身份数据。
"""

from __future__ import annotations

from typing import Any

from .capability import CAPABILITY_MAP, ROLE_LEVEL, capabilities_for_role
from .config import Config
from .models import ActionDecision, ActorContext


class PolicyEngine:
    """身份×关系×目标×动作统一授权策略。"""

    def __init__(self, config: Config) -> None:
        self.config = config

    def allowed_actions(self, context: ActorContext) -> list[str]:
        """生成当前事件允许的行动范围描述（供提示词注入）。

        返回人类可读的行动描述列表，不是能力 id。
        """
        bot_caps = capabilities_for_role(context.bot_role)
        descriptions: list[str] = []

        is_sender_protected = self._is_protected(
            context.requester_id, context.requester_role
        )
        is_friendly = context.requester_relation in ("owner", "friendly")

        if "mute_current_sender" in bot_caps:
            if is_sender_protected and self.config.allow_playful_mute_protected:
                descriptions.append(
                    f"你可以对当前发送者执行不超过 "
                    f"{self.config.playful_mute_max_seconds} 秒的玩笑式短禁言"
                )
            elif not is_sender_protected:
                descriptions.append(
                    "你可以因对方直接辱骂或持续骚扰你，对当前发送者进行短时禁言"
                )

        if "request_self_mute" in bot_caps:
            descriptions.append("对方可以请求你禁言他自己，目标将由系统绑定为对方本人")

        if "mute_member" in bot_caps and not is_friendly:
            descriptions.append("对方不能要求你处罚其他成员")

        if "set_member_title" in bot_caps:
            descriptions.append(
                "如果对方请求头衔，你可以根据人物性格和当前情绪决定是否授予"
            )

        if "set_member_card" in bot_caps:
            descriptions.append("对方可以请求你修改他自己的群名片")

        if "set_self_card" in bot_caps:
            descriptions.append(
                "要改你自己的群名片时使用 set_self_card，不要用 set_member_card 传你自己的 QQ 号"
            )

        descriptions.append("高风险操作不能仅因普通成员请求执行")
        descriptions.append("是否行动由你结合当前情绪、人设和上下文决定")

        return descriptions

    def evaluate(
        self,
        context: ActorContext,
        action: str,
        params: dict[str, Any],
        trigger_source: str = "llm_autonomous",
    ) -> ActionDecision:
        """评估某个动作是否被授权。

        这是所有有副作用的工具调用的统一入口。
        """
        # 0. 动作归一化：改自己名片不需要管理员权限
        action, params = self._normalize_action(context, action, params)

        # 1. 检查能力是否存在
        cap = CAPABILITY_MAP.get(action)
        if cap is None:
            return ActionDecision(
                allowed=False, action=action, reason=f"未知能力: {action}"
            )

        # 2. 检查 bot 是否有该能力的 OneBot 权限前提
        bot_level = ROLE_LEVEL.get(context.bot_role, -1)
        required_level = ROLE_LEVEL.get(cap["min_role"], 999)
        if bot_level < required_level:
            return ActionDecision(
                allowed=False,
                action=action,
                reason=f"bot 身份({context.bot_role})无此权限",
            )

        # 3. 熔断检查
        # (由调用方在 main.py 中通过 circuit breaker 实现)

        # 4. 黑名单检查
        target = self._resolve_target(context, action, params)
        if target and self.config.is_blacklisted(target):
            # 黑名单用户触发即踢
            if action == "kick_member":
                return ActionDecision(
                    allowed=True,
                    action="kick_member",
                    params={**params, "reject_add_request": True},
                    reason="黑名单用户",
                )
            return ActionDecision(
                allowed=False,
                action=action,
                reason="目标在黑名单中",
            )

        # 5. 按动作类型分别检查
        return self._check_action(context, action, params, trigger_source, target)

    def _normalize_action(
        self,
        context: ActorContext,
        action: str,
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """把语义等价但权限要求更低的动作重写为目标绑定动作。

        LLM 常把「改你自己的名片」表达为 set_member_card(user_id=<bot 自己>)。
        改自己名片在 OneBot 中只需 member 权限，
        因此这里重写为 set_self_card，避免被管理员权限门误拒。
        """
        if action != "set_member_card":
            return action, params
        if not context.bot_id:
            return action, params
        if str(params.get("user_id", "")) != str(context.bot_id):
            return action, params
        return "set_self_card", {"card": params.get("card", "")}

    def _check_action(
        self,
        context: ActorContext,
        action: str,
        params: dict[str, Any],
        trigger_source: str,
        target: str | None,
    ) -> ActionDecision:
        """按动作类型进行授权检查。"""
        is_sender_protected = self._is_protected(
            context.requester_id, context.requester_role
        )
        is_friendly_requester = context.requester_relation in ("owner", "friendly")
        target_is_requester = target is not None and target == context.requester_id

        if action == "mute_current_sender":
            return self._check_mute_current_sender(context, params, is_sender_protected)

        if action == "request_self_mute":
            return self._check_request_self_mute(
                context, params, trigger_source, target_is_requester
            )

        if action == "mute_member":
            return self._check_mute_member(
                context, params, target, is_friendly_requester
            )

        if action == "unmute_member":
            return self._check_unmute_member(
                context, params, target, is_friendly_requester
            )

        if action == "kick_member":
            return self._check_kick_member(
                context, params, target, is_friendly_requester
            )

        if action == "delete_message":
            return self._check_delete_message(context, params)

        if action == "set_member_card":
            return self._check_set_card(
                context, params, target, is_friendly_requester, target_is_requester
            )

        if action == "set_self_card":
            return ActionDecision(allowed=True, action=action, params=params)

        if action == "set_member_title":
            return self._check_set_title(context, params, target, is_friendly_requester)

        if action == "set_group_admin":
            return self._check_set_admin(context, params, target)

        if action == "set_group_name":
            return self._check_set_group_name(context, params, is_friendly_requester)

        if action == "set_whole_ban":
            return self._check_whole_ban(context, params, is_friendly_requester)

        if action == "approve_join_request":
            return self._check_approve_join(context, params, is_friendly_requester)

        # 只读工具默认允许
        if action in ("get_group_member_info", "list_group_members"):
            return ActionDecision(allowed=True, action=action, params=params)

        return ActionDecision(allowed=False, action=action, reason="未匹配任何授权规则")

    def _check_mute_current_sender(
        self,
        context: ActorContext,
        params: dict[str, Any],
        is_sender_protected: bool,
    ) -> ActionDecision:
        """检查 mute_current_sender 授权。"""
        duration = int(params.get("duration", 0))
        if duration <= 0:
            return ActionDecision(
                allowed=False, action="mute_current_sender", reason="禁言时长必须大于0"
            )

        if is_sender_protected:
            if not self.config.allow_playful_mute_protected:
                return ActionDecision(
                    allowed=False,
                    action="mute_current_sender",
                    reason="目标受强保护且未开启玩笑禁言",
                )
            if duration > self.config.playful_mute_max_seconds:
                return ActionDecision(
                    allowed=False,
                    action="mute_current_sender",
                    reason=f"玩笑禁言上限 {self.config.playful_mute_max_seconds} 秒",
                )
            # 玩笑禁言不需要确认
            return ActionDecision(
                allowed=True,
                action="mute_current_sender",
                params={**params, "duration": duration},
            )

        # 非保护用户
        if duration > self.config.max_mute_seconds:
            duration = self.config.max_mute_seconds

        requires_confirm = duration > self.config.confirm_mute_threshold
        return ActionDecision(
            allowed=True,
            action="mute_current_sender",
            params={**params, "duration": duration},
            requires_confirmation=requires_confirm,
        )

    def _check_request_self_mute(
        self,
        context: ActorContext,
        params: dict[str, Any],
        trigger_source: str,
        target_is_requester: bool,
    ) -> ActionDecision:
        """检查 request_self_mute 授权。"""
        if trigger_source != "self_service":
            return ActionDecision(
                allowed=False,
                action="request_self_mute",
                reason="此工具仅响应自助禁言请求",
            )
        if not target_is_requester:
            return ActionDecision(
                allowed=False,
                action="request_self_mute",
                reason="目标必须为请求者本人",
            )
        duration = int(params.get("duration", 0))
        if duration <= 0 or duration > self.config.max_mute_seconds:
            duration = min(max(duration, 1), self.config.max_mute_seconds)
        return ActionDecision(
            allowed=True,
            action="request_self_mute",
            params={**params, "duration": duration},
        )

    def _check_mute_member(
        self,
        context: ActorContext,
        params: dict[str, Any],
        target: str | None,
        is_friendly_requester: bool,
    ) -> ActionDecision:
        """检查 mute_member 授权。"""
        if target is None:
            return ActionDecision(
                allowed=False, action="mute_member", reason="缺少目标用户"
            )
        if self._is_protected(target, context.target_role or "member"):
            return ActionDecision(
                allowed=False,
                action="mute_member",
                reason="目标用户受强保护",
            )
        if not is_friendly_requester:
            return ActionDecision(
                allowed=False,
                action="mute_member",
                reason="普通成员不能请求禁言他人",
            )
        duration = int(params.get("duration", 0))
        if duration > self.config.max_mute_seconds:
            duration = self.config.max_mute_seconds
        requires_confirm = duration > self.config.confirm_mute_threshold
        return ActionDecision(
            allowed=True,
            action="mute_member",
            params={**params, "duration": duration},
            requires_confirmation=requires_confirm,
        )

    def _check_unmute_member(
        self,
        context: ActorContext,
        params: dict[str, Any],
        target: str | None,
        is_friendly_requester: bool,
    ) -> ActionDecision:
        """检查 unmute_member 授权。解除禁言是低风险操作，友好用户可请求。"""
        if target is None:
            return ActionDecision(
                allowed=False, action="unmute_member", reason="缺少目标用户"
            )
        if not is_friendly_requester:
            # 普通成员可以请求解除自己的禁言
            if target != context.requester_id:
                return ActionDecision(
                    allowed=False,
                    action="unmute_member",
                    reason="普通成员不能请求解除他人禁言",
                )
        return ActionDecision(allowed=True, action="unmute_member", params=params)

    def _check_kick_member(
        self,
        context: ActorContext,
        params: dict[str, Any],
        target: str | None,
        is_friendly_requester: bool,
    ) -> ActionDecision:
        """检查 kick_member 授权。踢出是高风险操作。"""
        if target is None:
            return ActionDecision(
                allowed=False, action="kick_member", reason="缺少目标用户"
            )
        if self._is_protected(target, context.target_role or "member"):
            return ActionDecision(
                allowed=False,
                action="kick_member",
                reason="目标用户受强保护，不可踢出",
            )
        if not is_friendly_requester:
            return ActionDecision(
                allowed=False,
                action="kick_member",
                reason="普通成员不能请求踢出他人",
            )
        return ActionDecision(
            allowed=True,
            action="kick_member",
            params=params,
            requires_confirmation=True,
        )

    def _check_delete_message(
        self, context: ActorContext, params: dict[str, Any]
    ) -> ActionDecision:
        """检查 delete_message 授权。撤回是中风险操作。"""
        message_id = params.get("message_id")
        if not message_id:
            return ActionDecision(
                allowed=False, action="delete_message", reason="缺少消息 ID"
            )
        return ActionDecision(allowed=True, action="delete_message", params=params)

    def _check_set_card(
        self,
        context: ActorContext,
        params: dict[str, Any],
        target: str | None,
        is_friendly_requester: bool,
        target_is_requester: bool,
    ) -> ActionDecision:
        """检查 set_member_card 授权。"""
        if target is None:
            return ActionDecision(
                allowed=False, action="set_member_card", reason="缺少目标用户"
            )
        if self._is_protected(target, context.target_role or "member"):
            if not is_friendly_requester:
                return ActionDecision(
                    allowed=False,
                    action="set_member_card",
                    reason="目标受保护且请求者非友好用户",
                )
        if not is_friendly_requester and not target_is_requester:
            return ActionDecision(
                allowed=False,
                action="set_member_card",
                reason="普通成员只能修改自己的名片",
            )
        return ActionDecision(allowed=True, action="set_member_card", params=params)

    def _check_set_title(
        self,
        context: ActorContext,
        params: dict[str, Any],
        target: str | None,
        is_friendly_requester: bool,
    ) -> ActionDecision:
        """检查 set_member_title 授权。只有群主可以设头衔。"""
        if context.bot_role != "owner":
            return ActionDecision(
                allowed=False,
                action="set_member_title",
                reason="仅群主可设置头衔",
            )
        if target is None:
            return ActionDecision(
                allowed=False, action="set_member_title", reason="缺少目标用户"
            )
        if self._is_protected(target, context.target_role or "member"):
            if not is_friendly_requester:
                return ActionDecision(
                    allowed=False,
                    action="set_member_title",
                    reason="目标受保护且请求者非友好用户",
                )
        return ActionDecision(allowed=True, action="set_member_title", params=params)

    def _check_set_admin(
        self, context: ActorContext, params: dict[str, Any], target: str | None
    ) -> ActionDecision:
        """检查 set_group_admin 授权。"""
        if context.bot_role != "owner":
            return ActionDecision(
                allowed=False,
                action="set_group_admin",
                reason="仅群主可设置管理员",
            )
        enable = params.get("enable", True)
        if not enable and not self.config.enable_set_admin_revoke:
            return ActionDecision(
                allowed=False,
                action="set_group_admin",
                reason="未开启撤销管理员权限",
            )
        return ActionDecision(
            allowed=True,
            action="set_group_admin",
            params=params,
            requires_confirmation=True,
        )

    def _check_set_group_name(
        self, context: ActorContext, params: dict[str, Any], is_friendly: bool
    ) -> ActionDecision:
        """检查 set_group_name 授权。"""
        if not is_friendly:
            return ActionDecision(
                allowed=False,
                action="set_group_name",
                reason="普通成员不能修改群名",
            )
        return ActionDecision(
            allowed=True,
            action="set_group_name",
            params=params,
            requires_confirmation=True,
        )

    def _check_whole_ban(
        self, context: ActorContext, params: dict[str, Any], is_friendly: bool
    ) -> ActionDecision:
        """检查 set_whole_ban 授权。全员禁言是高风险操作。"""
        if not is_friendly:
            return ActionDecision(
                allowed=False,
                action="set_whole_ban",
                reason="普通成员不能请求全员禁言",
            )
        return ActionDecision(
            allowed=True,
            action="set_whole_ban",
            params=params,
            requires_confirmation=True,
        )

    def _check_approve_join(
        self, context: ActorContext, params: dict[str, Any], is_friendly: bool
    ) -> ActionDecision:
        """检查 approve_join_request 授权。"""
        if self.config.join_audit_mode != "approve_only":
            return ActionDecision(
                allowed=False,
                action="approve_join_request",
                reason="当前入群审核模式不允许自动通过",
            )
        if not is_friendly:
            return ActionDecision(
                allowed=False,
                action="approve_join_request",
                reason="普通成员不能处理入群请求",
            )
        return ActionDecision(
            allowed=True, action="approve_join_request", params=params
        )

    def _is_protected(self, user_id: str, role: str = "member") -> bool:
        """判断用户是否受强保护。"""
        uid = str(user_id)
        if self.config.is_protected(uid):
            return True
        if self.config.is_owner(uid):
            return True
        if role in ("owner", "admin"):
            return True
        return False

    def _resolve_target(
        self, context: ActorContext, action: str, params: dict[str, Any]
    ) -> str | None:
        """从参数或上下文中解析目标用户。

        对于目标绑定工具（mute_current_sender, request_self_mute, set_self_card），
        目标由事件硬绑定，不接受 LLM 传入的 user_id。
        """
        if action in ("mute_current_sender", "request_self_mute"):
            return context.requester_id
        if action == "set_self_card":
            # set_self_card 修改的是 bot 自己的名片
            return None
        return str(params.get("user_id", "")) or None
