"""凝心溯溪-序插件主类。

动态识别 bot、发送者与目标身份及关系，向 LLM 注入受控行动边界，
并提供自助动作、互动反应、群信息管理和仅自动通过的入群审核能力。
"""

from __future__ import annotations

import asyncio
import functools
import pathlib
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import __version__
from .core.audit import JoinAuditService
from .core.audit_log import AuditLogger
from .core.capability import ALL_TOOL_NAMES, filter_request_tools_for_role
from .core.config import Config
from .core.confirm import ConfirmService
from .core.cooldown import CooldownService
from .core.identity import IdentityManager
from .core.knowledge import KnowledgeService
from .core.moderation import ModerationService
from .core.models import ActionDecision, ActorContext, TriggerSource
from .core.onebot import OneBotClient
from .core.policy import PolicyEngine
from .core.prompts import SECURITY_RULES, build_identity_prompt
from .core.relationship import RelationshipService
from .core.request_context import (
    OWNER_IDENTITY_GUARDIAN,
    PHASE_LLM_REQUEST,
    add_prompt_fragment,
    add_reason,
    ensure_context,
    set_artifact,
    set_flag,
)
from .core.welcome import WelcomeService

PLUGIN_NAME = "astrbot_plugin_identity_guardian"
LOG_PREFIX = "[idg]"


def _resolve_event(*candidates: Any) -> Any | None:
    """从候选实参中挑出真正的 AstrMessageEvent。

    正常情况下框架把 event 作为第一个实参传入。但热重载残留 partial 套娃时，
    形参会整体错位（详见 ``_unwrap_registry_handlers`` 的说明），此时 event 形参
    拿到的是插件实例。这里按鸭子类型识别真正的 event，避免直接抛 AttributeError。
    """
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, IdentityGuardianPlugin):
            continue
        if callable(getattr(candidate, "get_platform_name", None)):
            return candidate
    return None


def _resolve_llm_request(*candidates: Any) -> Any | None:
    """从候选实参中挑出真正的 ProviderRequest。

    与 ``_resolve_event`` 配套：形参错位时 req 也会跟着挪位。ProviderRequest 的稳定
    特征是带 ``system_prompt`` 或 ``prompt`` 属性，且不是 event、不是插件实例。
    """
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, IdentityGuardianPlugin):
            continue
        if callable(getattr(candidate, "get_platform_name", None)):
            continue
        if hasattr(candidate, "system_prompt") or hasattr(candidate, "prompt"):
            return candidate
    return None


@register(
    PLUGIN_NAME,
    "Justice-ocr",
    "凝心溯溪-序，关系感知、权限边界与群组行动",
    __version__,
)
class IdentityGuardianPlugin(Star):
    """凝心溯溪-序插件。"""

    PLUGIN_HEALTH_CONTRACT = "plugin.health@1.0"
    _current_instance: Any = None
    _PLUGIN_IDENTIFIERS = (PLUGIN_NAME, "IdentityGuardianPlugin")

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.logger = logger

        # 数据目录
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        pathlib.Path(self.data_dir).mkdir(parents=True, exist_ok=True)

        # 配置
        self.config = Config(config)
        self.config.apply_log_level()

        if not self.config.enabled:
            logger.info("%s plugin disabled by config", LOG_PREFIX)
            return

        # 核心服务
        self.onebot = OneBotClient()
        self.relationship = RelationshipService(self.config)
        self.identity = IdentityManager(self.config, self.onebot, self.relationship)
        self.policy = PolicyEngine(self.config)
        self.audit_log = AuditLogger(self.data_dir)
        self.cooldown = CooldownService(self.config)
        self.confirm = ConfirmService()
        self.welcome = WelcomeService(self.config)
        self.knowledge = KnowledgeService(self.config, context)

        # LLM 审核服务（延迟初始化 LLM caller）
        self._llm_caller = None
        self.moderation = ModerationService(self.config, llm_caller=None)
        self.join_audit = JoinAuditService(
            self.config, self.onebot, self.knowledge, llm_caller=None
        )

        # 紧急停止标志
        self._stopped = False

        # 后台任务句柄
        self._bg_tasks: list[Any] = []

        # 热重载防护
        IdentityGuardianPlugin._current_instance = self
        self._unwrap_stale_partials()

        logger.info(
            "%s v%s loaded | bot_owners=%d protected=%d join_audit=%s "
            "moderation=%s api_guard=%s",
            LOG_PREFIX,
            __version__,
            len(self.config.owner_users),
            len(self.config.protected_users),
            self.config.join_audit_mode,
            self.config.auto_moderate,
            self.config.enable_api_guard,
        )

    def plugin_health(self) -> dict[str, object]:
        configured = bool(getattr(getattr(self, "config", None), "enabled", False))
        services_ready = not configured or all(
            getattr(self, name, None) is not None
            for name in ("identity", "policy", "cooldown", "confirm")
        )
        stopped = bool(getattr(self, "_stopped", False)) if configured else False
        checks = {
            "config_ready": getattr(self, "config", None) is not None,
            "services_ready": services_ready,
            "guard_accepting_actions": not stopped,
        }
        reasons = [name.upper() for name, passed in checks.items() if not passed]
        return {
            "status": "ok" if not reasons else "degraded",
            "checks": checks,
            "reasons": reasons,
            "version": __version__,
        }

    # ------------------------------------------------------------------
    # 热重载防护
    # ------------------------------------------------------------------

    def _unwrap_stale_partials(self) -> None:
        """在 __init__ 中预防性拆解 partial 套娃。"""
        try:
            self._unwrap_registry_handlers()
        except Exception as exc:
            self.logger.debug("%s _unwrap_stale_partials skipped: %s", LOG_PREFIX, exc)

    def _unwrap_registry_handlers(self) -> None:
        """将 registry 中本插件的 handler 重置为原始未绑定函数。

        AstrBot 加载插件时会用 ``functools.partial(raw_handler, star_instance)``
        预置 ``self``。若重载时 registry 里残留的已是 partial，再包一层就会变成
        ``partial(partial(raw, 旧实例), 新实例)``——调用 ``handler(event)`` 实际等价于
        ``raw(旧实例, 新实例, event)``，于是 ``event`` 形参收到的是插件实例本身，
        真正的 event 被挤进 ``*args``，表现为
        ``'IdentityGuardianPlugin' object has no attribute 'get_platform_name'``。
        因此这里在每次 ``__init__`` 时先剥回未绑定函数，交由框架重新绑定。
        """
        registry = None
        for module_path in (
            "astrbot.core.star.star_handler",
            "astrbot.core.star.star_handlers_registry",
            "astrbot.core.star",
        ):
            try:
                mod = __import__(module_path, fromlist=["star_handlers_registry"])
                registry = getattr(mod, "star_handlers_registry", None)
                if registry is not None:
                    break
            except Exception:
                continue
        if registry is None:
            return
        self._apply_unwrap(registry)

    def _apply_unwrap(self, registry: Any) -> None:
        """在给定 registry 上执行拆解，便于单测直接注入。"""
        # 上游字段名为 _handlers，旧实现误用 handlers 导致整个防护静默失效；
        # 这里按优先级探测，并保留可迭代 registry 的兜底。
        handlers: Any = None
        for attr in ("_handlers", "handlers"):
            candidate = getattr(registry, attr, None)
            if isinstance(candidate, list):
                handlers = candidate
                break
        if handlers is None:
            try:
                handlers = list(registry)
            except Exception:
                return

        unwrapped = 0
        for handler in handlers:
            # 上游字段名为 handler_full_name，旧实现误用 full_name 取到空串，
            # 导致所有 handler 都匹配不上本插件。
            full_name = str(
                getattr(handler, "handler_full_name", None)
                or getattr(handler, "full_name", "")
                or ""
            )
            module_path = str(getattr(handler, "handler_module_path", "") or "")
            haystack = f"{full_name} {module_path}"
            if not any(ident in haystack for ident in self._PLUGIN_IDENTIFIERS):
                continue
            current = getattr(handler, "handler", None)
            original = current
            while isinstance(original, functools.partial):
                original = original.func
            if original is not None and original is not current:
                try:
                    handler.handler = original
                    unwrapped += 1
                except Exception:
                    pass
        if unwrapped:
            self.logger.info("%s unwrapped %d stale partial(s)", LOG_PREFIX, unwrapped)

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    async def _call_audit_llm(self, prompt: str) -> str:
        """调用审核用 LLM，留空则回退主对话 LLM。"""
        try:
            provider_id = self.config.audit_llm_provider
            provider = None

            if provider_id:
                get_provider = getattr(self.context, "get_provider", None)
                if callable(get_provider):
                    try:
                        provider = get_provider(provider_id)
                    except Exception:
                        provider = None

            if provider is None:
                # 回退到主对话 provider
                get_using = getattr(self.context, "get_using_provider", None)
                if callable(get_using):
                    provider = get_using()

            if provider is None:
                self.logger.warning("%s no LLM provider available", LOG_PREFIX)
                return ""

            # 调用 LLM
            from astrbot.api.provider import ProviderRequest

            req = ProviderRequest(prompt=prompt, system_prompt="")
            resp = await provider.text_chat(**req.__dict__)
            if hasattr(resp, "completion_text"):
                return str(resp.completion_text)
            return str(resp)
        except Exception as exc:
            self.logger.warning("%s audit LLM call failed: %s", LOG_PREFIX, exc)
            return ""

    def _ensure_llm_caller(self) -> None:
        """延迟绑定 LLM caller。"""
        if self._llm_caller is None:
            self._llm_caller = self._call_audit_llm
            self.moderation._llm_caller = self._llm_caller
            self.join_audit._llm_caller = self._llm_caller

    # ------------------------------------------------------------------
    # on_llm_request: 身份上下文注入
    # ------------------------------------------------------------------

    def _filter_tools_for_bot_role(self, req: Any, bot_role: str) -> int:
        """按当前群中的 bot 身份移除无权限的本插件工具。"""
        try:
            return filter_request_tools_for_role(req, bot_role)
        except Exception as exc:  # pragma: no cover - 防御性
            self.logger.debug("%s tool filter skipped: %s", LOG_PREFIX, exc)
            return 0

    # priority=800：凝心溯溪系列 on_llm_request 区间为 200-800，数值越大越先执行。
    # 身份与行动边界属于安全层，必须先于知识注入（知 700）、表达约束（情 600）和
    # 沉默判断（言 500）生效；不显式声明时实际顺序取决于 AstrBot 插件加载次序，
    # 会导致安全边界在知识注入之后才生效这类不可复现故障。
    @filter.on_llm_request(priority=800)
    async def on_llm_request(
        self, event: AstrMessageEvent, req: Any, *args: Any, **kwargs: Any
    ) -> None:
        """向 LLM 请求注入身份与行动边界上下文。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return
        if not plugin.config.enabled or plugin._stopped:
            return

        # 形参可能因 partial 套娃整体错位，这里按鸭子类型取回真正的 event 与 req。
        resolved_event = _resolve_event(event, self, req, *args)
        if resolved_event is None:
            plugin.logger.debug(
                "%s on_llm_request received no usable event", LOG_PREFIX
            )
            return
        if resolved_event is not event:
            req = _resolve_llm_request(event, req, *args)
            if req is None:
                plugin.logger.debug(
                    "%s on_llm_request received no usable request", LOG_PREFIX
                )
                return
        event = resolved_event

        # 仅 aiocqhttp 平台
        if event.get_platform_name() != "aiocqhttp":
            return

        group_id = event.get_group_id()
        if not group_id:
            return  # 私聊不注入群身份

        self_id = event.get_self_id()
        sender_id = event.get_sender_id()
        if not self_id or not sender_id:
            return

        request_context = ensure_context(event, PHASE_LLM_REQUEST)

        plugin._ensure_llm_caller()

        # 构建身份上下文
        try:
            actor = await plugin.identity.get_actor_context(
                event,
                event.get_platform_id(),
                group_id,
                self_id,
                sender_id,
            )
        except Exception as exc:
            plugin.logger.debug("%s identity lookup failed: %s", LOG_PREFIX, exc)
            return

        removed = plugin._filter_tools_for_bot_role(req, actor.bot_role)
        if removed:
            plugin.logger.debug(
                "%s filtered %d unavailable tool(s) for bot_role=%s group=%s",
                LOG_PREFIX,
                removed,
                actor.bot_role,
                group_id,
            )

        # 生成允许行动描述
        allowed = plugin.policy.allowed_actions(actor)

        # 获取群信息
        group_meta = await plugin.onebot.get_group_info_safe(event, group_id)

        # 构建提示词
        prompt = build_identity_prompt(actor, allowed, group_meta)
        add_prompt_fragment(
            request_context,
            OWNER_IDENTITY_GUARDIAN,
            "identity.boundary",
            prompt,
            priority=100,
            source="astrbot_plugin_identity_guardian",
            metadata={"kind": "platform_permission_boundary"},
        )
        add_prompt_fragment(
            request_context,
            OWNER_IDENTITY_GUARDIAN,
            "identity.security_rules",
            SECURITY_RULES,
            priority=110,
            source="astrbot_plugin_identity_guardian",
            metadata={"kind": "security_rules"},
        )

        def mark_boundary_ready() -> None:
            set_flag(
                request_context,
                OWNER_IDENTITY_GUARDIAN,
                "boundary_ready",
                True,
            )
            set_artifact(
                request_context,
                OWNER_IDENTITY_GUARDIAN,
                "boundary",
                {
                    "bot_role": str(actor.bot_role),
                    "allowed_action_count": len(allowed),
                    "filtered_tool_count": removed,
                    "permission_identity": {"mode": "raw_platform_account"},
                },
            )
            add_reason(
                request_context,
                OWNER_IDENTITY_GUARDIAN,
                "IDENTITY_BOUNDARY_READY",
            )

        # 注入到 extra_user_content_parts
        try:
            from astrbot.core.agent.message import TextPart

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                parts.append(TextPart(text=prompt))
                # 安全规则仅在会话首次注入
                parts.append(TextPart(text=SECURITY_RULES))
                mark_boundary_ready()
                return
        except Exception as exc:
            plugin.logger.debug("%s TextPart inject failed: %s", LOG_PREFIX, exc)

        # 降级到 system_prompt
        try:
            current = getattr(req, "system_prompt", None) or ""
            req.system_prompt = current + "\n\n" + prompt + "\n\n" + SECURITY_RULES
            mark_boundary_ready()
        except Exception as exc:
            add_reason(
                request_context,
                OWNER_IDENTITY_GUARDIAN,
                "IDENTITY_BOUNDARY_INJECTION_FAILED",
            )
            plugin.logger.warning(
                "%s prompt inject fallback failed: %s", LOG_PREFIX, exc
            )

    # ------------------------------------------------------------------
    # 事件监听: notice / request
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=500)
    async def on_event(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ) -> None:
        """处理 notice 和 request 类事件。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return
        if not plugin.config.enabled or plugin._stopped:
            return
        # 形参可能因 partial 套娃整体错位，这里按鸭子类型取回真正的 event。
        event = _resolve_event(event, self, *args)
        if event is None:
            plugin.logger.debug("%s on_event received no usable event", LOG_PREFIX)
            return
        if event.get_platform_name() != "aiocqhttp":
            return

        raw = getattr(event, "message_obj", None)
        if raw is None:
            return
        raw_msg = getattr(raw, "raw_message", None)
        if not isinstance(raw_msg, dict):
            return

        post_type = raw_msg.get("post_type")

        if post_type == "notice":
            await plugin._handle_notice(event, raw_msg)
        elif post_type == "request":
            await plugin._handle_request(event, raw_msg)

    async def _handle_notice(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> None:
        """处理 notice 事件。"""
        notice_type = raw.get("notice_type")

        if notice_type == "group_increase":
            user_id = str(raw.get("user_id", ""))
            self_id = event.get_self_id()
            if user_id and self_id and user_id == self_id:
                # bot 自己进群
                group_id = str(raw.get("group_id", ""))
                group_meta = await self.onebot.get_group_info_safe(event, group_id)
                group_name = str(group_meta.get("group_name", ""))
                text = await self.welcome.on_bot_join(event, group_id, group_name)
                if text:
                    try:
                        await event.send(event.plain_result(text))
                    except Exception as exc:
                        self.logger.debug("%s welcome send failed: %s", LOG_PREFIX, exc)

        elif notice_type == "group_decrease":
            # bot 被踢 / 主动退群时清理该群身份缓存
            user_id = str(raw.get("user_id", ""))
            self_id = event.get_self_id()
            group_id = str(raw.get("group_id", ""))
            if user_id and self_id and user_id == self_id:
                self.identity.clear_cache()
                self.logger.info(
                    "%s bot left group %s, cache cleared", LOG_PREFIX, group_id
                )

        elif notice_type == "notify":
            sub_type = raw.get("sub_type", "")
            if sub_type == "group_admin_change":
                # 管理员变更：刷新身份缓存
                group_id = str(raw.get("group_id", ""))
                self.identity.clear_cache()
                self.logger.info(
                    "%s admin change in group %s, cache refreshed",
                    LOG_PREFIX,
                    group_id,
                )

    async def _handle_request(
        self, event: AstrMessageEvent, raw: dict[str, Any]
    ) -> None:
        """处理 request 事件（入群申请）。"""
        req_type = raw.get("request_type")
        if req_type != "group":
            return

        if self.config.join_audit_mode == "off":
            return

        self._ensure_llm_caller()

        try:
            decision = await self.join_audit.handle_request(event, raw)

            # notify_only 下所有结论都交由人工；approve_only 下仅通知未自动放行项。
            if (
                not self.join_audit.should_auto_approve(decision)
                and self.config.audit_notify_targets
            ):
                await self._notify_audit_targets(event, raw, decision)
        except Exception as exc:
            self.logger.warning("%s join audit error: %s", LOG_PREFIX, exc)

    async def _notify_audit_targets(
        self,
        event: AstrMessageEvent,
        raw: dict[str, Any],
        decision: Any,
    ) -> None:
        """通知审核目标。"""
        user_id = str(raw.get("user_id", ""))
        group_id = str(raw.get("group_id", ""))
        comment = str(raw.get("comment", ""))[:200]
        text = (
            f"入群申请待人工复核\n"
            f"群: {group_id}  用户: {user_id}\n"
            f"附言: {comment}\n"
            f"插件判断: {decision.verdict} (置信度: {decision.confidence:.2f})\n"
            f"原因: {decision.reason}\n"
            f"请在 QQ 群审核入口处理。"
        )
        for target in self.config.audit_notify_targets:
            try:
                from astrbot.api.message_components import Plain

                await StarTools.send_message(target, [Plain(text=text)])
            except Exception as exc:
                self.logger.debug(
                    "%s notify target %s failed: %s", LOG_PREFIX, target, exc
                )

    # ------------------------------------------------------------------
    # LLM 工具
    # ------------------------------------------------------------------

    async def _get_actor(
        self, event: AstrMessageEvent, target_id: str | None = None
    ) -> ActorContext | None:
        """从事件构建 ActorContext。"""
        group_id = event.get_group_id()
        if not group_id:
            return None
        self_id = event.get_self_id()
        sender_id = event.get_sender_id()
        if not self_id or not sender_id:
            return None
        try:
            return await self.identity.get_actor_context(
                event,
                event.get_platform_id(),
                group_id,
                self_id,
                sender_id,
                target_id=target_id,
            )
        except Exception as exc:
            self.logger.debug("%s _get_actor failed: %s", LOG_PREFIX, exc)
            return None

    def _check_guard(self) -> bool:
        """检查 API 层护栏。"""
        if self._stopped:
            return False
        if self.config.enable_api_guard and self.cooldown.check_breaker():
            self.cooldown.trip_breaker()
            return False
        return True

    async def _execute_with_guard(
        self,
        event: AstrMessageEvent,
        action: str,
        params: dict[str, Any],
        trigger_source: str = "llm_autonomous",
        target_id: str | None = None,
    ) -> str:
        """统一的工具执行入口：策略检查 → 执行 → 审计。"""
        if not self._check_guard():
            return "操作已被安全护栏拦截（紧急停止或熔断）。"

        actor = await self._get_actor(event, target_id)
        if actor is None:
            return "无法获取身份上下文。"

        decision = self.policy.evaluate(actor, action, params, trigger_source)
        if not decision.allowed:
            return f"操作未获授权：{decision.reason}。"

        # 熔断检查
        if self.config.enable_api_guard and self.cooldown.check_breaker():
            self.cooldown.trip_breaker()
            return "全局熔断已触发，需 /idg reset_breaker 恢复。"

        # 二次确认
        if decision.requires_confirmation:
            confirm_action = decision.action
            confirm_params = dict(decision.params)
            # 延迟执行时 event 的发送者会变成审批人，必须把原目标固化进参数。
            if confirm_action in {"mute_current_sender", "request_self_mute"}:
                confirm_action = "mute_member"
                confirm_params["user_id"] = target_id or actor.requester_id
            confirm_id = self.confirm.create(
                action=confirm_action,
                params=confirm_params,
                group_id=actor.group_id,
                target_user=target_id or actor.requester_id,
            )
            return f"已转人工确认，确认 id={confirm_id}。"

        # 执行
        result = await self._execute_action(event, decision, target_id)
        return result

    async def _approve_pending_action(
        self, event: AstrMessageEvent, confirm_id: str
    ) -> str:
        """在原群内重新授权并原子消费待确认操作。"""
        entry = self.confirm.get(confirm_id)
        if entry is None:
            return f"未找到确认 ID: {confirm_id}"

        current_group = str(event.get_group_id() or "")
        if not current_group or current_group != str(entry.group_id):
            return "该确认只能在创建它的群聊中审批。"
        if not self._check_guard():
            return "操作已被安全护栏拦截（紧急停止或熔断）。"

        actor = await self._get_actor(event, entry.target_user)
        if actor is None:
            return "无法获取审批人的身份上下文。"
        decision = self.policy.evaluate(
            actor,
            entry.action,
            dict(entry.params),
            TriggerSource.EXPLICIT_REQUEST.value,
        )
        if not decision.allowed:
            return f"审批时重新校验未通过：{decision.reason}。"

        # 只有全部实时校验通过后才消费，避免校验失败导致确认记录丢失。
        consumed = self.confirm.approve(confirm_id)
        if consumed is None:
            return f"确认 ID 已被处理: {confirm_id}"
        executable = ActionDecision(
            allowed=True,
            action=decision.action,
            params=decision.params,
            requires_confirmation=False,
        )
        result = await self._execute_action(event, executable, entry.target_user)
        return f"已批准 {entry.action}。\n{result}"

    async def _execute_readonly(
        self,
        event: AstrMessageEvent,
        action: str,
        params: dict[str, Any],
    ) -> str:
        """只读查询入口：策略检查 → 查询 → 返回摘要，不写审计与冷却。"""
        if self._stopped:
            return "操作已被安全护栏拦截（紧急停止）。"

        group_id_str = event.get_group_id()
        if not group_id_str:
            return "该工具仅在群聊中可用。"

        actor = await self._get_actor(event)
        if actor is None:
            return "无法获取身份上下文。"

        decision = self.policy.evaluate(
            actor, action, params, TriggerSource.EXPLICIT_REQUEST.value
        )
        if not decision.allowed:
            return f"操作未获授权：{decision.reason}。"

        try:
            group_id = int(group_id_str)
        except (ValueError, TypeError):
            return "群 ID 无效。"

        if action == "get_group_member_info":
            try:
                uid = int(params.get("user_id", 0))
            except (ValueError, TypeError):
                return "用户 ID 无效。"
            if uid <= 0:
                return "用户 ID 无效。"
            info = await self.onebot.get_group_member_info(event, group_id, uid)
            if not info:
                return "查询失败，未获取到该成员信息。"
            return (
                f"成员 {info.get('user_id', uid)}："
                f"昵称={info.get('nickname', '未知')}，"
                f"群名片={info.get('card') or '（未设置）'}，"
                f"角色={info.get('role', 'member')}，"
                f"头衔={info.get('title') or '（无）'}"
            )

        if action == "list_group_members":
            members = await self.onebot.get_group_member_list(event, group_id)
            if not members:
                return "查询失败，未获取到群成员列表。"
            owners = [m for m in members if m.get("role") == "owner"]
            admins = [m for m in members if m.get("role") == "admin"]

            def _label(member: dict) -> str:
                return str(
                    member.get("card")
                    or member.get("nickname")
                    or member.get("user_id", "")
                )

            parts = [f"当前群共 {len(members)} 名成员"]
            if owners:
                parts.append("群主：" + "、".join(_label(m) for m in owners))
            if admins:
                parts.append("管理员：" + "、".join(_label(m) for m in admins))
            return "；".join(parts) + "。"

        return f"未实现的查询: {action}"

    async def _execute_action(
        self,
        event: AstrMessageEvent,
        decision: ActionDecision,
        target_id: str | None,
    ) -> str:
        """执行具体的 OneBot API 调用。"""
        action = decision.action
        params = decision.params
        group_id_str = event.get_group_id()

        try:
            group_id = int(group_id_str)
        except (ValueError, TypeError):
            return "群 ID 无效。"

        ok = False
        err = ""

        if action == "mute_current_sender":
            uid = int(event.get_sender_id())
            ok, err = await self.onebot.set_group_ban(
                event, group_id, uid, int(params.get("duration", 300))
            )

        elif action == "request_self_mute":
            uid = int(event.get_sender_id())
            ok, err = await self.onebot.set_group_ban(
                event, group_id, uid, int(params.get("duration", 300))
            )

        elif action == "mute_member":
            uid = int(params.get("user_id", 0))
            if uid <= 0:
                return "目标用户 ID 无效。"
            ok, err = await self.onebot.set_group_ban(
                event, group_id, uid, int(params.get("duration", 300))
            )

        elif action == "unmute_member":
            uid = int(params.get("user_id", 0))
            if uid <= 0:
                return "目标用户 ID 无效。"
            ok, err = await self.onebot.set_group_ban(event, group_id, uid, 0)

        elif action == "kick_member":
            uid = int(params.get("user_id", 0))
            if uid <= 0:
                return "目标用户 ID 无效。"
            ok, err = await self.onebot.set_group_kick(
                event,
                group_id,
                uid,
                reject_add_request=bool(params.get("reject_add_request", False)),
            )

        elif action == "delete_message":
            msg_id = int(params.get("message_id", 0))
            if msg_id <= 0:
                return "消息 ID 无效。"
            ok, err = await self.onebot.delete_msg(event, msg_id)

        elif action == "set_member_card":
            uid = int(params.get("user_id", 0))
            if uid <= 0:
                return "目标用户 ID 无效。"
            ok, err = await self.onebot.set_group_card(
                event, group_id, uid, str(params.get("card", ""))
            )

        elif action == "set_self_card":
            uid = int(event.get_self_id())
            ok, err = await self.onebot.set_group_card(
                event, group_id, uid, str(params.get("card", ""))
            )

        elif action == "set_member_title":
            uid = int(params.get("user_id", 0))
            if uid <= 0:
                return "目标用户 ID 无效。"
            ok, err = await self.onebot.set_group_special_title(
                event, group_id, uid, str(params.get("title", ""))
            )

        elif action == "set_group_admin":
            uid = int(params.get("user_id", 0))
            if uid <= 0:
                return "目标用户 ID 无效。"
            ok, err = await self.onebot.set_group_admin(
                event, group_id, uid, bool(params.get("enable", True))
            )

        elif action == "set_group_name":
            ok, err = await self.onebot.set_group_name(
                event, group_id, str(params.get("group_name", ""))
            )

        elif action == "set_whole_ban":
            ok, err = await self.onebot.set_group_whole_ban(
                event, group_id, bool(params.get("enable", True))
            )

        elif action == "approve_join_request":
            ok, err = await self.onebot.set_group_add_request(
                event,
                str(params.get("flag", "")),
                str(params.get("sub_type", "add")),
                approve=True,
                reason="",
            )

        else:
            return f"未实现的动作: {action}"

        # 记录冷却和审计
        self.cooldown.mark_action(
            group_id_str, target_id or event.get_sender_id(), action
        )

        actor = await self._get_actor(event, target_id)
        if actor:
            self.audit_log.write_from_decision(actor, decision, ok, err)

        if ok:
            return f"已执行 {action}。"
        return f"执行失败：{err}"

    # --- 具体工具定义 ---

    @filter.llm_tool(name="mute_current_sender")
    async def mute_current_sender(
        self,
        event: AstrMessageEvent,
        duration: int,
        reason: str,
        intent: str = "reaction",
    ):
        """短时禁言当前消息发送者。目标由事件绑定，不能指定第三人。
        适用于对方辱骂、骚扰 bot 后的反应，或与主人互动时的玩笑式短禁言。

        Args:
            duration(int): 禁言时长（秒），短时建议 60-600
            reason(string): 禁言原因
            intent(string): 意图标签：reaction（反应）/ playful（玩笑）
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "mute_current_sender",
            {"duration": duration, "reason": reason, "intent": intent},
            trigger_source=TriggerSource.LLM_AUTONOMOUS.value,
        )

    @filter.llm_tool(name="request_self_mute")
    async def request_self_mute(
        self,
        event: AstrMessageEvent,
        duration: int,
        reason: str,
    ):
        """响应当前发送者对其本人的禁言请求。目标由系统绑定为请求者本人，不接受 user_id。
        仅当用户明确要求禁言自己时调用。

        Args:
            duration(int): 禁言时长（秒）
            reason(string): 用户请求的原因
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "request_self_mute",
            {"duration": duration, "reason": reason},
            trigger_source=TriggerSource.SELF_SERVICE.value,
        )

    @filter.llm_tool(name="mute_member")
    async def mute_member(
        self,
        event: AstrMessageEvent,
        user_id: str,
        duration: int,
        reason: str = "",
    ):
        """禁言指定群成员。仅友好用户（主人/管理员）请求时可用，不能因普通成员请求执行。

        Args:
            user_id(string): 被禁言用户的 QQ 号
            duration(int): 禁言时长（秒），0 表示解除禁言
            reason(string): 禁言原因
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "mute_member",
            {"user_id": user_id, "duration": duration, "reason": reason},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
            target_id=user_id,
        )

    @filter.llm_tool(name="unmute_member")
    async def unmute_member(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ):
        """解除指定群成员的禁言。

        Args:
            user_id(string): 要解除禁言的用户 QQ 号
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "unmute_member",
            {"user_id": user_id},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
            target_id=user_id,
        )

    @filter.llm_tool(name="kick_member")
    async def kick_member(
        self,
        event: AstrMessageEvent,
        user_id: str,
        reason: str = "",
    ):
        """踢出指定群成员。高风险操作，需人工确认。仅友好用户请求时可用。

        Args:
            user_id(string): 被踢出用户的 QQ 号
            reason(string): 踢出原因
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "kick_member",
            {"user_id": user_id, "reason": reason},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
            target_id=user_id,
        )

    @filter.llm_tool(name="delete_message")
    async def delete_message(
        self,
        event: AstrMessageEvent,
        message_id: int,
    ):
        """撤回一条群消息。

        Args:
            message_id(int): 要撤回的消息 ID
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "delete_message",
            {"message_id": message_id},
            trigger_source=TriggerSource.LLM_AUTONOMOUS.value,
        )

    @filter.llm_tool(name="set_member_card")
    async def set_member_card(
        self,
        event: AstrMessageEvent,
        user_id: str,
        card: str,
    ):
        """设置群成员名片。普通成员只能修改自己的名片。

        Args:
            user_id(string): 目标用户 QQ 号
            card(string): 新名片内容
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "set_member_card",
            {"user_id": user_id, "card": card},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
            target_id=user_id,
        )

    @filter.llm_tool(name="set_self_card")
    async def set_self_card(
        self,
        event: AstrMessageEvent,
        card: str,
    ):
        """修改 bot 自己的群名片。

        Args:
            card(string): 新名片内容
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "set_self_card",
            {"card": card},
            trigger_source=TriggerSource.LLM_AUTONOMOUS.value,
        )

    @filter.llm_tool(name="set_member_title")
    async def set_member_title(
        self,
        event: AstrMessageEvent,
        user_id: str,
        title: str,
    ):
        """设置群成员专属头衔。仅群主可操作。

        Args:
            user_id(string): 目标用户 QQ 号
            title(string): 头衔内容
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "set_member_title",
            {"user_id": user_id, "title": title},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
            target_id=user_id,
        )

    @filter.llm_tool(name="set_group_name")
    async def set_group_name_tool(
        self,
        event: AstrMessageEvent,
        group_name: str,
    ):
        """修改群名称。需人工确认。

        Args:
            group_name(string): 新群名
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "set_group_name",
            {"group_name": group_name},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
        )

    @filter.llm_tool(name="set_group_admin")
    async def set_group_admin_tool(
        self,
        event: AstrMessageEvent,
        user_id: str,
        enable: bool = True,
    ):
        """设置或撤销群管理员。仅群主可操作，需人工确认。默认仅任命，撤销需配置开启。

        Args:
            user_id(string): 目标用户 QQ 号
            enable(bool): true=任命管理员, false=撤销管理员
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "set_group_admin",
            {"user_id": user_id, "enable": enable},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
            target_id=user_id,
        )

    @filter.llm_tool(name="set_whole_ban")
    async def set_whole_ban_tool(
        self,
        event: AstrMessageEvent,
        enable: bool = True,
    ):
        """开启或关闭全员禁言。高风险操作，需人工确认。

        Args:
            enable(bool): true=开启全员禁言, false=关闭
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_with_guard(
            event,
            "set_whole_ban",
            {"enable": enable},
            trigger_source=TriggerSource.EXPLICIT_REQUEST.value,
        )

    @filter.llm_tool(name="approve_join_request")
    async def approve_join_request(
        self,
        event: AstrMessageEvent,
        flag: str,
        sub_type: str = "add",
        approve: bool = True,
        reason: str = "",
    ):
        """处理入群申请。仅高置信度正确时建议通过，错误答案不自动拒绝。

        Args:
            flag(string): 申请 flag，由 OneBot 事件提供
            sub_type(string): add（加群）或 invite（邀请）
            approve(bool): true=通过, false=拒绝
            reason(string): 处理原因
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        # 入群审核只允许 bot 自主通过正确答案；不通过 LLM 直接拒绝
        if not approve:
            return "入群申请不自动拒绝，请在 QQ 群审核入口人工处理。"
        return await plugin._execute_with_guard(
            event,
            "approve_join_request",
            {"flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason},
            trigger_source=TriggerSource.JOIN_AUDIT.value,
        )

    @filter.llm_tool(name="get_group_member_info")
    async def get_group_member_info_tool(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ):
        """查询群成员信息（只读，无副作用）。返回群名片、角色、头衔与入群时间。

        Args:
            user_id(string): 被查询用户的 QQ 号
        """
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_readonly(
            event, "get_group_member_info", {"user_id": user_id}
        )

    @filter.llm_tool(name="list_group_members")
    async def list_group_members_tool(self, event: AstrMessageEvent):
        """列出当前群成员概况（只读，无副作用）。返回成员总数与管理层名单。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            return "插件初始化中，请稍后重试。"
        return await plugin._execute_readonly(event, "list_group_members", {})

    # ------------------------------------------------------------------
    # /idg 指令组
    # ------------------------------------------------------------------

    @filter.command_group("idg")
    def idg_group(self):
        """凝心溯溪-序指令组。"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("status")
    async def idg_status(self, event: AstrMessageEvent):
        """查看插件状态。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        stats = plugin.cooldown.stats()
        current_group = str(event.get_group_id() or "")
        pending = [
            item
            for item in plugin.confirm.list_pending()
            if current_group and str(item.group_id) == current_group
        ]
        lines = [
            f"凝心溯溪-序 {__version__}",
            f"状态: {'已停止' if plugin._stopped else '运行中'}",
            f"bot 身份刷新间隔: {plugin.config.identity_refresh_interval}s",
            f"入群审核: {plugin.config.join_audit_mode}",
            f"内容审核: {'开启' if plugin.config.auto_moderate else '关闭'}",
            f"API 护栏: {'开启' if plugin.config.enable_api_guard else '关闭'}",
            f"熔断状态: {'已触发' if stats['breaker_tripped'] else '正常'}",
            f"1h 操作数: {stats['hourly_actions']}/{stats['breaker_threshold']}",
            f"冷却中: {stats['action_cooldowns']} 条",
            f"待确认: {len(pending)} 条",
            f"知识库: {'可用' if plugin.knowledge.is_available() else '不可用'}",
        ]
        if pending:
            for p in pending[:5]:
                lines.append(f"  - [{p.confirm_id}] {p.action} → {p.target_user}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("stop")
    async def idg_stop(self, event: AstrMessageEvent):
        """紧急停止所有管理工具。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        plugin._stopped = True
        yield event.plain_result("已紧急停止所有管理操作。使用 /idg resume 恢复。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("resume")
    async def idg_resume(self, event: AstrMessageEvent):
        """恢复管理工具。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        plugin._stopped = False
        yield event.plain_result("已恢复管理操作。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("reset_breaker")
    async def idg_reset_breaker(self, event: AstrMessageEvent):
        """重置熔断器。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        plugin.cooldown.reset_breaker()
        yield event.plain_result("熔断器已重置。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("refresh")
    async def idg_refresh(self, event: AstrMessageEvent):
        """刷新身份缓存。"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        plugin.identity.clear_cache()
        yield event.plain_result("身份缓存已清空，将在下次请求时重新获取。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("approve")
    async def idg_approve(self, event: AstrMessageEvent, confirm_id: str = ""):
        """批准待确认操作。用法: /idg approve <confirm_id>"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        if not confirm_id:
            yield event.plain_result("用法: /idg approve <confirm_id>")
            return
        result = await plugin._approve_pending_action(event, confirm_id)
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @idg_group.command("reject")
    async def idg_reject(self, event: AstrMessageEvent, confirm_id: str = ""):
        """拒绝待确认操作。用法: /idg reject <confirm_id>"""
        plugin = IdentityGuardianPlugin._current_instance or self
        if not isinstance(plugin, IdentityGuardianPlugin):
            yield event.plain_result("插件初始化中。")
            return
        if not confirm_id:
            yield event.plain_result("用法: /idg reject <confirm_id>")
            return
        pending = plugin.confirm.get(confirm_id)
        if pending is None:
            yield event.plain_result(f"未找到确认 ID: {confirm_id}")
            return
        if str(event.get_group_id() or "") != str(pending.group_id):
            yield event.plain_result("该确认只能在创建它的群聊中拒绝。")
            return
        entry = plugin.confirm.reject(confirm_id)
        if entry is None:
            yield event.plain_result(f"未找到确认 ID: {confirm_id}")
            return
        yield event.plain_result(f"已拒绝 {entry.action}。")

    @idg_group.command("help")
    async def idg_help(self, event: AstrMessageEvent):
        """查看帮助。"""
        lines = [
            "凝心溯溪-序指令列表:",
            "  /idg status - 查看状态",
            "  /idg stop - 紧急停止",
            "  /idg resume - 恢复运行",
            "  /idg reset_breaker - 重置熔断器",
            "  /idg refresh - 刷新身份缓存",
            "  /idg approve <id> - 批准待确认操作",
            "  /idg reject <id> - 拒绝待确认操作",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 生命周期：initialize / terminate
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件激活后调用，启动后台任务。"""
        if not self.config.enabled:
            return
        # 启动后台任务：定时刷新身份缓存、清理过期待审
        try:
            self._bg_tasks.append(asyncio.create_task(self._refresh_loop()))
            self._bg_tasks.append(asyncio.create_task(self._expire_pending_loop()))
            self.logger.info("%s background tasks started", LOG_PREFIX)
        except RuntimeError:
            # 某些环境下事件循环尚未就绪，降级为无后台任务
            self.logger.debug("%s no running loop, skip bg tasks", LOG_PREFIX)

    async def _refresh_loop(self) -> None:
        """定时刷新身份缓存。"""
        while not self._stopped:
            try:
                await asyncio.sleep(self.config.identity_refresh_interval)
                if self._stopped:
                    break
                self.identity.clear_cache()
                self.logger.debug("%s scheduled identity cache refresh", LOG_PREFIX)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.debug("%s refresh loop error: %s", LOG_PREFIX, exc)

    async def _expire_pending_loop(self) -> None:
        """定时清理过期待确认条目。"""
        # 默认每 5 分钟清理一次
        while not self._stopped:
            try:
                await asyncio.sleep(300)
                if self._stopped:
                    break
                expired = self.confirm.cleanup_expired(ttl_seconds=300)
                if expired:
                    self.logger.info(
                        "%s expired %d pending confirm(s)", LOG_PREFIX, expired
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.debug("%s expire loop error: %s", LOG_PREFIX, exc)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载时清理资源。"""
        IdentityGuardianPlugin._current_instance = None
        self._stopped = True

        # 取消后台任务
        for task in self._bg_tasks:
            try:
                task.cancel()
            except Exception:
                pass
        self._bg_tasks.clear()

        try:
            self.cooldown.clear()
            self.confirm.clear()
            self.identity.clear_cache()
        except Exception:
            pass

        self._cleanup_llm_tools()
        self._cleanup_star_handlers()

        self.logger.info("%s plugin terminated", LOG_PREFIX)

    def _cleanup_llm_tools(self) -> None:
        """清理注册的 LLM 工具。"""
        for method_name in (
            "remove_llm_tool",
            "remove_llm_tools",
            "unregister_llm_tool",
        ):
            method = getattr(self.context, method_name, None)
            if not callable(method):
                continue
            try:
                for name in ALL_TOOL_NAMES:
                    method(name)
                self.logger.info(
                    "%s cleanup: removed LLM tools via %s", LOG_PREFIX, method_name
                )
                return
            except Exception as exc:
                self.logger.debug("%s %s failed: %s", LOG_PREFIX, method_name, exc)

        # 兜底：直接操作 func_tool_manager
        try:
            func_tool_manager = getattr(
                self.context, "_func_tool_manager", None
            ) or getattr(self.context, "func_tool_manager", None)
            if func_tool_manager is None:
                return
            tools = getattr(func_tool_manager, "tools", None)
            if not isinstance(tools, list):
                return
            before = len(tools)
            func_tool_manager.tools = [
                t for t in tools if getattr(t, "name", "") not in ALL_TOOL_NAMES
            ]
            after = len(func_tool_manager.tools)
            if before != after:
                self.logger.info(
                    "%s cleanup: removed %d stale LLM tool(s)",
                    LOG_PREFIX,
                    before - after,
                )
        except Exception as exc:
            self.logger.debug(
                "%s func_tool_manager cleanup skipped: %s", LOG_PREFIX, exc
            )

    def _cleanup_star_handlers(self) -> None:
        """清理 star_handlers_registry 中本插件的 handler。"""
        try:
            from astrbot.core.star.star_handlers_registry import star_handlers_registry
        except Exception:
            try:
                from astrbot.core.star import star_handlers_registry
            except Exception:
                self.logger.debug(
                    "%s star_handlers_registry not importable, skip cleanup",
                    LOG_PREFIX,
                )
                return

        handlers = getattr(star_handlers_registry, "handlers", None)
        if not isinstance(handlers, list):
            return

        def _is_our_handler(handler: Any) -> bool:
            full_name = str(getattr(handler, "full_name", "") or "")
            if not full_name:
                return False
            return any(ident in full_name for ident in self._PLUGIN_IDENTIFIERS)

        stale = [h for h in handlers if _is_our_handler(h)]
        if not stale:
            return
        try:
            star_handlers_registry.handlers = [
                h for h in handlers if not _is_our_handler(h)
            ]
            self.logger.info(
                "%s cleanup: removed %d stale handler(s)", LOG_PREFIX, len(stale)
            )
        except Exception as exc:
            self.logger.debug(
                "%s failed to reassign handlers list: %s", LOG_PREFIX, exc
            )
