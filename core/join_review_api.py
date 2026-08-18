"""Authenticated Plugin Page API for per-group join-request review."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from astrbot.api.web import json_response, request
except ImportError:  # pragma: no cover - isolated unit tests mock AstrBot
    json_response = None
    request = None

from .group_discovery import JoinedGroup, discover_joined_groups
from .join_review import GuardBlockedError, JoinReviewRuntime, resolve_presets
from .join_review_store import (
    JoinReviewStore,
    RequestNotActionable,
    ValidationError,
)
from .models import JoinDecision

PLUGIN_ID = "astrbot_plugin_identity_guardian"
ROUTE_PREFIX = f"/{PLUGIN_ID}/join-review"
_BATCH_ACTIONS = frozenset(
    {
        "add",
        "enable_auto_audit",
        "enable_review_send",
        "disable_all",
        "apply_legacy",
    }
)


class JoinReviewPageAPI:
    """Small adapter between AstrBot's authenticated Page bridge and core services."""

    def __init__(
        self,
        *,
        context: Any,
        config: Any,
        store: JoinReviewStore,
        runtime: JoinReviewRuntime,
        logger: Any,
        ensure_llm: Any = None,
        push_preview: Any = None,
        result_reply_preview: Any = None,
        list_providers: Any = None,
        save_settings: Any = None,
    ) -> None:
        self.context = context
        self.config = config
        self.store = store
        self.runtime = runtime
        self.logger = logger
        # 模拟诊断前确保审核 LLM caller 已绑定（main 的延迟绑定钩子）。
        self.ensure_llm = ensure_llm
        # 模拟申请的推送文案预览钩子（main 注入，带人格/近期群消息上下文）。
        self.push_preview = push_preview
        # 模拟申请的审批结果回复预览钩子（main 注入，与生产同一渲染链路）。
        self.result_reply_preview = result_reply_preview
        # 全局设置：列举可用 LLM provider / 写回插件配置（main 注入）。
        self.list_providers = list_providers
        self.save_settings = save_settings

    def register(self) -> bool:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            self.logger.warning(
                "[idg] context.register_web_api unavailable; join-review Page disabled"
            )
            return False
        routes = (
            ("joined-groups", self.joined_groups, ["GET"], "刷新已加入群"),
            ("groups", self.groups, ["GET"], "读取入群审核群配置"),
            ("groups/update", self.update_group, ["POST"], "保存单群审核配置"),
            ("groups/batch", self.batch_groups, ["POST"], "批量保存群审核配置"),
            ("requests", self.requests, ["GET"], "读取入群待审申请"),
            ("approve", self.approve, ["POST"], "批准入群申请"),
            ("reject", self.reject, ["POST"], "驳回入群申请"),
            ("simulate", self.simulate, ["POST"], "模拟入群申请诊断（零副作用）"),
            ("settings", self.settings, ["GET"], "读取入群审核全局设置"),
            ("settings/update", self.update_settings, ["POST"], "保存入群审核全局设置"),
        )
        for suffix, handler, methods, description in routes:
            register(f"{ROUTE_PREFIX}/{suffix}", handler, methods, description)
        return True

    @staticmethod
    def _response(payload: dict[str, Any], status: int = 200) -> Any:
        body = {"success": status < 400, **payload}
        if callable(json_response):
            return json_response(body, status_code=status)
        return body if status == 200 else (body, status)

    @classmethod
    def _error(cls, code: str, status: int = 400) -> Any:
        return cls._response({"error": code}, status)

    @staticmethod
    async def _payload() -> dict[str, Any] | None:
        if request is None:
            return None
        try:
            value = await request.json(default={})
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    async def _discovered(self) -> list[JoinedGroup]:
        return await discover_joined_groups(self.context, self.runtime.onebot)

    @staticmethod
    def _discovery_map(
        groups: list[JoinedGroup],
    ) -> dict[tuple[str, str], JoinedGroup]:
        return {(item.platform_id, item.group_id): item for item in groups}

    async def _validate_config_scope(
        self, payload: Mapping[str, Any], discovered: list[JoinedGroup]
    ) -> None:
        platform_id = str(payload.get("platform_id") or "").strip()
        group_id = str(payload.get("group_id") or "").strip()
        rows = self._discovery_map(discovered)
        source = rows.get((platform_id, group_id))
        if source is None:
            raise ValidationError("group_not_joined")
        if not source.can_review:
            raise ValidationError("insufficient_permission")
        specified = payload.get("specified_group_ids", ())
        if not isinstance(specified, (list, tuple)):
            raise ValidationError("invalid_specified_group_ids")
        for target_group_id in specified:
            if (platform_id, str(target_group_id).strip()) not in rows:
                raise ValidationError("specified_group_not_joined")
        push_groups = payload.get("push_group_ids", ())
        if not isinstance(push_groups, (list, tuple)):
            raise ValidationError("invalid_push_group_ids")
        for push_group_id in push_groups:
            if (platform_id, str(push_group_id).strip()) not in rows:
                raise ValidationError("push_group_not_joined")

    async def joined_groups(self) -> Any:
        try:
            groups = [item.to_dict() for item in await self._discovered()]
            return self._response({"data": {"groups": groups}})
        except Exception as exc:
            self.logger.warning(
                "[idg] join-review discovery failed: %s", type(exc).__name__
            )
            return self._error("group_discovery_failed", 502)

    def _legacy_available(self) -> bool:
        return str(getattr(self.config, "join_audit_mode", "off")) in {
            "approve_only",
            "notify_only",
        }

    async def groups(self) -> Any:
        configs = await self.store.list_group_configs()
        actionable = await self.store.list_requests(
            status=("pending", "platform_error")
        )
        counts: dict[tuple[str, str], int] = {}
        for item in actionable:
            key = (item.platform_id, item.group_id)
            counts[key] = counts.get(key, 0) + 1
        rows = []
        for config in configs:
            value = config.to_dict()
            value["pending_count"] = counts.get(
                (config.platform_id, config.group_id), 0
            )
            rows.append(value)
        return self._response(
            {
                "data": {
                    "groups": rows,
                    "legacy_available": self._legacy_available(),
                }
            }
        )

    async def update_group(self) -> Any:
        payload = await self._payload()
        if payload is None:
            return self._error("invalid_request")
        allowed = {
            "platform_id",
            "group_id",
            "auto_audit_enabled",
            "review_send_enabled",
            "notify_target",
            "specified_group_ids",
            "include_answer",
            "pinned",
            "push_group_ids",
            "push_style",
            "join_questions",
        }
        if set(payload) != allowed:
            return self._error("invalid_group_config")
        try:
            await self._validate_config_scope(payload, await self._discovered())
            config = await self.store.upsert_group_config(**payload)
        except ValidationError as exc:
            return self._error(str(exc))
        except Exception as exc:
            self.logger.warning(
                "[idg] join-review group update failed: %s", type(exc).__name__
            )
            return self._error("config_persist_failed", 500)
        return self._response({"data": {"group": config.to_dict()}})

    async def batch_groups(self) -> Any:
        payload = await self._payload()
        if payload is None or set(payload) != {"action", "groups"}:
            return self._error("invalid_request")
        action = payload.get("action")
        selected = payload.get("groups")
        if action not in _BATCH_ACTIONS or not isinstance(selected, list):
            return self._error("invalid_batch")
        if not selected or len(selected) > 100:
            return self._error("invalid_batch")
        discovered = await self._discovered()
        seen: set[tuple[str, str]] = set()
        updates: list[dict[str, Any]] = []
        try:
            for selected_group in selected:
                if not isinstance(selected_group, dict) or set(selected_group) != {
                    "platform_id",
                    "group_id",
                }:
                    raise ValidationError("invalid_group_selection")
                platform_id = str(selected_group["platform_id"]).strip()
                group_id = str(selected_group["group_id"]).strip()
                key = (platform_id, group_id)
                if key in seen:
                    raise ValidationError("duplicate_group_config")
                seen.add(key)
                await self._validate_config_scope(selected_group, discovered)
                current = await self.store.get_group_config(platform_id, group_id)
                auto_enabled = current.auto_audit_enabled
                review_enabled = current.review_send_enabled
                if action == "add":
                    auto_enabled = False
                    review_enabled = False
                elif action == "enable_auto_audit":
                    auto_enabled = True
                elif action == "enable_review_send":
                    review_enabled = True
                elif action == "disable_all":
                    auto_enabled = False
                    review_enabled = False
                elif action == "apply_legacy":
                    mode = str(getattr(self.config, "join_audit_mode", "off"))
                    auto_enabled = mode == "approve_only"
                    review_enabled = mode == "notify_only"
                updates.append(
                    {
                        "platform_id": platform_id,
                        "group_id": group_id,
                        "auto_audit_enabled": auto_enabled,
                        "review_send_enabled": review_enabled,
                        "notify_target": current.notify_target,
                        "specified_group_ids": list(current.specified_group_ids),
                        "include_answer": current.include_answer,
                        "pinned": current.pinned,
                        "push_group_ids": list(current.push_group_ids),
                        "push_style": current.push_style,
                        "join_questions": [
                            {
                                "question": item["question"],
                                "answers": list(item["answers"]),
                            }
                            for item in current.join_questions
                        ],
                    }
                )
            configs = await self.store.batch_upsert_group_configs(updates)
        except ValidationError as exc:
            return self._error(str(exc))
        except Exception as exc:
            self.logger.warning(
                "[idg] join-review batch update failed: %s", type(exc).__name__
            )
            return self._error("config_persist_failed", 500)
        return self._response(
            {"data": {"groups": [item.to_dict() for item in configs]}}
        )

    async def requests(self) -> Any:
        rows = []
        for item in await self.store.list_requests():
            config = await self.store.get_group_config(item.platform_id, item.group_id)
            rows.append(item.to_public_dict(include_answer=config.include_answer))
        return self._response({"data": {"requests": rows}})

    async def _process(self, *, approve: bool) -> Any:
        payload = await self._payload()
        allowed = {"request_id"} if approve else {"request_id", "reason"}
        if (
            payload is None
            or not set(payload) <= allowed
            or "request_id" not in payload
        ):
            return self._error("invalid_request")
        reason = str(payload.get("reason") or "").strip()
        if len(reason) > 256:
            return self._error("reason_too_long")
        try:
            updated = await self.runtime.process_request(
                self.context,
                str(payload["request_id"]),
                approve=approve,
                reason=reason,
            )
        except RequestNotActionable as exc:
            status = 409 if exc.reason in {"busy", "already_processed"} else 410
            return self._error(exc.reason, status)
        except GuardBlockedError:
            # 紧急停止/熔断护栏生效：前端尚无专属文案，使用通用错误体 + 503。
            return self._error("guard_blocked", 503)
        except ValidationError as exc:
            return self._error(str(exc))
        except Exception as exc:
            self.logger.warning(
                "[idg] join-review action failed: %s", type(exc).__name__
            )
            return self._error("platform_error", 502)
        if updated.status == "platform_error":
            return self._error("platform_error", 502)
        config = await self.store.get_group_config(
            updated.platform_id, updated.group_id
        )
        return self._response(
            {
                "data": {
                    "request": updated.to_public_dict(
                        include_answer=config.include_answer
                    )
                }
            }
        )

    async def approve(self) -> Any:
        return await self._process(approve=True)

    async def reject(self) -> Any:
        return await self._process(approve=False)

    async def simulate(self) -> Any:
        """模拟一次入群申请，走真实三段自动审核链路并返回逐步诊断。

        零副作用：不触平台批准/拒绝 API、不写待审记录、不发通知/推送、
        不写审计日志。``would`` 仅按该群当前开关说明实际事件会发生什么；
        ``would == "pending_review"`` 时附 ``push_preview`` 推送文案预览与
        ``result_reply_preview`` 审批结果回复预览（都只生成不发送），
        预览生成失败时对应字段为 None。
        """
        payload = await self._payload()
        if payload is None:
            return self._error("invalid_request")
        allowed = {"platform_id", "group_id", "question", "answer"}
        if not set(payload) <= allowed or not {
            "platform_id",
            "group_id",
            "answer",
        } <= set(payload):
            return self._error("invalid_request")
        question = str(payload.get("question") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            return self._error("invalid_answer")
        if len(question) > 2048 or len(answer) > 2048:
            return self._error("simulate_text_too_long")
        try:
            discovered = await self._discovered()
            await self._validate_config_scope(payload, discovered)
            config = await self.store.get_group_config(
                payload["platform_id"], payload["group_id"]
            )
        except ValidationError as exc:
            return self._error(str(exc))
        if callable(self.ensure_llm):
            try:
                self.ensure_llm()
            except Exception:
                pass
        try:
            presets = resolve_presets(config)
            report = await self.runtime.audit.simulate_auto_audit(
                question=question,
                answer=answer,
                group_id=str(payload["group_id"]).strip(),
                configured_questions=presets,
            )
        except Exception as exc:
            self.logger.warning(
                "[idg] join-review simulate failed: %s", type(exc).__name__
            )
            return self._error("simulate_failed", 500)
        final = report["final"]
        decision = JoinDecision(
            verdict=str(final.get("verdict", "uncertain")),
            confidence=float(final.get("confidence", 0.0)),
            reason=str(final.get("reason", "")),
        )
        approvable = self.runtime.audit.is_approvable(decision)
        if not config.auto_audit_enabled and not config.review_send_enabled:
            would = "ignored"
        elif config.auto_audit_enabled and approvable:
            would = "approve"
        elif config.review_send_enabled:
            would = "pending_review"
        else:
            would = "left_on_platform"
        presets_source = (
            "group"
            if config.join_questions
            else ("global" if getattr(self.config, "join_questions", []) else "none")
        )
        preview = None
        result_reply = None
        if would == "pending_review":
            # 仅转人工待审会触发推送，此时给出推送文案预览（零副作用）。
            source = self._discovery_map(discovered).get(
                (str(payload["platform_id"]).strip(), str(payload["group_id"]).strip())
            )
            if callable(self.push_preview):
                try:
                    preview = await self.push_preview(
                        platform_id=str(payload["platform_id"]).strip(),
                        group_id=str(payload["group_id"]).strip(),
                        question=question,
                        answer=answer,
                        config=config,
                        decision=decision,
                        source_group_name=(
                            source.group_name if source is not None else "未知群名"
                        ),
                    )
                except Exception as exc:
                    self.logger.warning(
                        "[idg] join-review push preview failed: %s", type(exc).__name__
                    )
                    preview = None
            if callable(self.result_reply_preview):
                try:
                    result_reply = await self.result_reply_preview(
                        platform_id=str(payload["platform_id"]).strip(),
                        group_id=str(payload["group_id"]).strip(),
                    )
                except Exception as exc:
                    self.logger.warning(
                        "[idg] join-review result reply preview failed: %s",
                        type(exc).__name__,
                    )
                    result_reply = None
        return self._response(
            {
                "data": {
                    "stages": report["stages"],
                    "final": report["final"],
                    "would": would,
                    "presets_source": presets_source,
                    "push_preview": preview,
                    "result_reply_preview": result_reply,
                }
            }
        )

    def _provider_options(self) -> list[dict[str, str]]:
        """经 main 注入钩子列举可用 LLM provider；失败/未注入返回空列表。"""
        if not callable(self.list_providers):
            return []
        try:
            providers = self.list_providers() or []
        except Exception:
            return []
        return [
            {"id": str(item.get("id") or ""), "label": str(item.get("label") or "")}
            for item in providers
            if isinstance(item, Mapping) and str(item.get("id") or "").strip()
        ]

    async def settings(self) -> Any:
        """读取全局设置：当前生效值 + 可用 provider 列表。"""
        return self._response(
            {
                "data": {
                    "audit_llm_provider": str(
                        getattr(self.config, "audit_llm_provider", "") or ""
                    ),
                    "enable_active_learner_recall": bool(
                        getattr(self.config, "enable_active_learner_recall", False)
                    ),
                    "providers": self._provider_options(),
                }
            }
        )

    async def update_settings(self) -> Any:
        """保存全局设置：严格字段校验后经 main 钩子原子写回，失败不落盘。"""
        payload = await self._payload()
        if payload is None:
            return self._error("invalid_request")
        allowed = {"audit_llm_provider", "enable_active_learner_recall"}
        if not set(payload) <= allowed or not allowed <= set(payload):
            return self._error("invalid_request")
        provider_id = str(payload.get("audit_llm_provider") or "").strip()
        recall = payload.get("enable_active_learner_recall")
        if not isinstance(recall, bool):
            return self._error("invalid_recall_flag")
        if provider_id and provider_id not in {
            item["id"] for item in self._provider_options()
        }:
            return self._error("invalid_provider")
        if not callable(self.save_settings):
            return self._error("settings_unavailable", 503)
        try:
            result = await self.save_settings(
                audit_llm_provider=provider_id,
                enable_active_learner_recall=recall,
            )
        except Exception as exc:
            self.logger.warning(
                "[idg] join-review settings save failed: %s", type(exc).__name__
            )
            return self._error("settings_save_failed", 500)
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            code = "settings_save_failed"
            if isinstance(result, Mapping) and str(result.get("error") or "").strip():
                code = str(result["error"])
            return self._error(code, 500)
        return self._response(
            {
                "data": {
                    "audit_llm_provider": provider_id,
                    "enable_active_learner_recall": recall,
                }
            }
        )


__all__ = ["JoinReviewPageAPI", "ROUTE_PREFIX"]
