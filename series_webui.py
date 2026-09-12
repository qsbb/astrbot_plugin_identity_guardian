"""``series.webui@2.0`` 统一面板适配层（序）。

核 WebUI 只负责鉴权、转发和展示；入群申请状态、并发 claim、平台审批与
持久化仍归 ``JoinReviewStore`` / ``JoinReviewRuntime`` 所有。本层不读取
OneBot ``flag``，也不把申请答案、内部错误或平台凭据返回给核。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .core.join_review import GuardBlockedError
from .core.join_review_store import RequestNotActionable, ValidationError

CONTRACT_NAME = "series.webui@2.0"
CONTRACT_VERSION = "2.0"
PLUGIN_ID = "astrbot_plugin_identity_guardian"
SERIES_ID = "ningxin_suxi"
PANEL_ID = "join_review"
APPROVE_ACTION_ID = "approve_join_request"
REJECT_ACTION_ID = "reject_join_request"
REFRESH_ACTION_ID = "refresh_join_requests"
MAX_ROWS = 100
MAX_REQUEST_ID_CHARS = 128
MAX_REJECT_REASON_CHARS = 256

_WEBUI_CAPABILITIES = ("generic_table", "generic_actions", "idempotency")
_MODULE_CAPABILITIES = ("control", "diagnostics", "identity_control_plane")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ROLE_LEVELS = {"viewer": 0, "admin": 1, "owner": 2}
_REQUIRED_ROLES = {
    REFRESH_ACTION_ID: "admin",
    APPROVE_ACTION_ID: "owner",
    REJECT_ACTION_ID: "owner",
}


def _failure(code: str, message: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"success": False, "error": code}
    if message:
        payload["message"] = message
    return payload


class SeriesWebUIPanels:
    """实现序插件侧的 series.webui@2.0 契约。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def contract(self) -> dict[str, Any]:
        return {
            "name": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
            "plugin_id": PLUGIN_ID,
            "series_id": SERIES_ID,
            "state_owner": "plugin",
            "managed": {
                "supported": True,
                "level": "actions",
                "preferred_surface": "kernel",
            },
            "standalone": {
                "available": True,
                "entry": "/pages/join_review",
                "pages": ["join_review"],
            },
            "capabilities": list(_WEBUI_CAPABILITIES),
            "module_capabilities": list(_MODULE_CAPABILITIES),
            "panels": [
                {
                    "id": PANEL_ID,
                    "title": "入群待审",
                    "description": "只展示待审摘要；批准与驳回走现有审核状态机",
                    "actions": self.actions(),
                }
            ],
        }

    def actions(self) -> list[dict[str, Any]]:
        request_id_field = {
            "name": "request_id",
            "label": "申请 ID",
            "type": "text",
            "required": True,
            "hint": "从待审列表复制；该值不是 OneBot flag",
        }
        reject_reason_field = {
            "name": "reason",
            "label": "驳回原因（可选）",
            "type": "text",
            "required": False,
            "hint": "最多 256 字，只发送给平台，不包含申请答案",
        }
        return [
            {
                "id": REFRESH_ACTION_ID,
                "label": "刷新待审列表",
                "effect": "idempotent",
                "idempotency_required": False,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "",
                "timeout_seconds": 5,
                "payload_fields": (),
            },
            {
                "id": APPROVE_ACTION_ID,
                "label": "批准入群",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "owner",
                "confirm": "确认批准该入群申请？该操作会改变真实群成员状态。",
                "timeout_seconds": 20,
                "payload_fields": (request_id_field,),
            },
            {
                "id": REJECT_ACTION_ID,
                "label": "驳回入群",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "owner",
                "confirm": "确认驳回该入群申请？该操作会改变真实群成员状态。",
                "timeout_seconds": 20,
                "payload_fields": (request_id_field, reject_reason_field),
            },
        ]

    async def panel_data(self, panel: str) -> dict[str, Any]:
        if panel != PANEL_ID:
            return _failure("UNKNOWN_PANEL")
        try:
            requests = await self.plugin.join_review_store.list_public_requests(
                include_answer=False
            )
        except Exception:  # noqa: BLE001 — 不向核暴露存储异常正文
            return _failure("SERVICE_UNAVAILABLE", "待审记录读取失败")
        rows = []
        for request in requests[:MAX_ROWS]:
            if not isinstance(request, Mapping):
                continue
            request_id = str(
                request.get("request_id") or request.get("id") or ""
            ).strip()
            if not _REQUEST_ID.fullmatch(request_id):
                continue
            rows.append(
                {
                    "request_id": request_id,
                    "group": (
                        f"{request.get('platform_id') or ''}:"
                        f"{request.get('group_id') or ''}"
                    ),
                    "user": str(
                        request.get("user_id") or request.get("sender_id") or ""
                    ),
                    "kind": str(request.get("request_kind") or request.get("sub_type") or ""),
                    "status": str(request.get("status") or ""),
                    "created": str(request.get("created_at") or ""),
                }
            )
        return {
            "success": True,
            "title": "入群待审",
            "description": f"共 {len(requests)} 条，展示前 {len(rows)} 条",
            "columns": [
                {"key": "request_id", "label": "申请 ID"},
                {"key": "group", "label": "目标群"},
                {"key": "user", "label": "用户"},
                {"key": "kind", "label": "类型"},
                {"key": "status", "label": "状态"},
                {"key": "created", "label": "创建时间"},
            ],
            "rows": rows,
            "actions": self.actions(),
            "footer": (
                "批准/驳回仅接受待审列表中的 request_id，并复用插件审核服务；"
                "申请答案、OneBot flag 和内部错误不会进入核 WebUI。"
            ),
        }

    async def panel_action(
        self,
        panel: str,
        action: str,
        payload: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            return _failure("INVALID_PAYLOAD")
        if panel != PANEL_ID:
            return _failure("UNKNOWN_PANEL")
        if action in _REQUIRED_ROLES and self._role_forbidden(action, context):
            return _failure("ROLE_FORBIDDEN", "当前角色无权执行该动作")
        if action == REFRESH_ACTION_ID:
            if set(payload):
                return _failure("INVALID_PAYLOAD", "刷新动作不接受参数")
            return await self._refresh()
        if action not in {APPROVE_ACTION_ID, REJECT_ACTION_ID}:
            return _failure("UNKNOWN_ACTION")
        allowed = (
            {"request_id"}
            if action == APPROVE_ACTION_ID
            else {"request_id", "reason"}
        )
        if not set(payload) <= allowed or "request_id" not in payload:
            return _failure("INVALID_PAYLOAD")
        request_id = self._request_id(payload.get("request_id"))
        if request_id is None:
            return _failure("INVALID_REQUEST_ID", "request_id 格式无效")
        reason = ""
        if action == REJECT_ACTION_ID:
            reason, failure = self._reject_reason(payload.get("reason"))
            if failure is not None:
                return failure
        approve = action == APPROVE_ACTION_ID
        runtime = getattr(self.plugin, "join_review_runtime", None) or getattr(
            self.plugin, "join_review", None
        )
        if runtime is None or not callable(getattr(runtime, "process_request", None)):
            return _failure("SERVICE_UNAVAILABLE", "入群审核服务不可用")
        try:
            updated = await runtime.process_request(
                self.plugin.context,
                request_id,
                approve=approve,
                reason=reason,
            )
        except RequestNotActionable as exc:
            return _failure(self._not_actionable_code(exc.reason))
        except GuardBlockedError:
            return _failure("GUARD_BLOCKED", "审核护栏已阻止本次操作")
        except ValidationError:
            return _failure("INVALID_REQUEST", "申请参数无效")
        except Exception as exc:  # noqa: BLE001 — 失败只记录类型名，不回传正文
            logger = getattr(self.plugin, "logger", None)
            if logger is not None and callable(getattr(logger, "warning", None)):
                logger.warning(
                    "[idg] series webui join-review action failed: %s",
                    type(exc).__name__,
                )
            return _failure("PLATFORM_ERROR", "平台操作失败，请查看插件诊断")
        expected = "approved" if approve else "rejected"
        if str(getattr(updated, "status", "")) != expected:
            return _failure("PLATFORM_ERROR", "平台未确认操作结果")
        request_id = str(getattr(updated, "request_id", request_id))
        return {
            "success": True,
            "message": "已批准入群申请" if approve else "已驳回入群申请",
            "request_id": request_id,
            "status": expected,
        }

    async def _refresh(self) -> dict[str, Any]:
        try:
            requests = await self.plugin.join_review_store.list_public_requests(
                include_answer=False
            )
        except Exception:  # noqa: BLE001
            return _failure("SERVICE_UNAVAILABLE", "待审记录读取失败")
        return {
            "success": True,
            "message": f"待审记录已刷新，共 {len(requests)} 条",
            "count": len(requests),
        }

    @staticmethod
    def _role_forbidden(
        action: str, context: Mapping[str, Any] | None
    ) -> bool:
        if not isinstance(context, Mapping):
            return False
        actor = context.get("actor")
        role = actor.get("role") if isinstance(actor, Mapping) else context.get("role")
        if not role:
            return False
        return _ROLE_LEVELS.get(str(role), -1) < _ROLE_LEVELS[_REQUIRED_ROLES[action]]

    @staticmethod
    def _request_id(raw: Any) -> str | None:
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        if not value or len(value) > MAX_REQUEST_ID_CHARS:
            return None
        return value if _REQUEST_ID.fullmatch(value) else None

    @staticmethod
    def _reject_reason(raw: Any) -> tuple[str, dict[str, Any] | None]:
        if raw is None:
            return "", None
        if not isinstance(raw, str):
            return "", _failure("INVALID_REJECT_REASON", "驳回原因必须是文本")
        reason = raw.strip()
        if len(reason) > MAX_REJECT_REASON_CHARS:
            return "", _failure("REJECT_REASON_TOO_LONG", "驳回原因不能超过 256 字")
        if any(ord(char) < 0x20 and char not in "\n\t" for char in reason):
            return "", _failure("INVALID_REJECT_REASON", "驳回原因包含控制字符")
        return reason, None

    @staticmethod
    def _not_actionable_code(reason: str) -> str:
        return {
            "not_found": "REQUEST_NOT_FOUND",
            "expired": "REQUEST_EXPIRED",
            "already_processed": "REQUEST_ALREADY_PROCESSED",
            "busy": "REQUEST_BUSY",
            "invalid_claim": "REQUEST_NOT_ACTIONABLE",
        }.get(str(reason), "REQUEST_NOT_ACTIONABLE")
