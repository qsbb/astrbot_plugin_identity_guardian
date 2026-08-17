"""Optional identity control plane shared with trusted series consumers.

The control plane persists raw platform authorization only. Natural-person
continuity, relationship scores, and display names are deliberately excluded.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from typing import Any

from .config import Config

IDENTITY_CONTROL_PLANE_CONTRACT_NAME = "identity.control_plane"
IDENTITY_CONTROL_PLANE_CONTRACT_VERSION = "1.0"

QUEST_BINDING_FIELDS = (
    "api_principal_digest",
    "client_id",
    "platform_id",
    "bot_id",
    "user_id",
)
_BINDING_SEPARATOR = "|"
_MAX_VALUE_LENGTH = 256
_PRINCIPAL_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def principal_digest(api_principal: str) -> str:
    """Derive the non-reversible binding value for one authenticated principal."""
    encoded = str(api_principal).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_quest_owner_binding(request: object) -> tuple[dict[str, str] | None, str]:
    """Validate and normalize an administrator-supplied Quest owner binding."""
    if not isinstance(request, dict):
        return None, "invalid_request"
    unexpected = set(request) - set(QUEST_BINDING_FIELDS)
    if unexpected:
        return None, "unexpected_fields"
    for field in QUEST_BINDING_FIELDS:
        if field not in request:
            return None, f"missing_{field}"

    values: dict[str, str] = {}
    for field in QUEST_BINDING_FIELDS:
        value = request[field]
        if not isinstance(value, str):
            return None, f"invalid_{field}"
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > _MAX_VALUE_LENGTH
            or _BINDING_SEPARATOR in normalized
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
        ):
            return None, f"invalid_{field}"
        if field == "api_principal_digest" and not _PRINCIPAL_DIGEST_RE.fullmatch(
            normalized
        ):
            return None, "invalid_api_principal_digest"
        values[field] = normalized
    return values, "ready"


def updated_quest_owner_settings(
    config: Config, values: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Return deduplicated owners and bindings for one explicit client upsert.

    A rebind replaces only the same authenticated principal + client pair. Old
    owners are not removed because they may still authorize ordinary platform
    events or another Quest client.
    """
    owner_users = list(
        dict.fromkeys(
            str(item).strip() for item in config.owner_users if str(item).strip()
        )
    )
    if values["user_id"] not in owner_users:
        owner_users.append(values["user_id"])

    replacement = _BINDING_SEPARATOR.join(
        values[field] for field in QUEST_BINDING_FIELDS
    )
    client_id = values["client_id"]
    bindings: list[str] = []
    for item in config.quest_session_owner_bindings:
        normalized = str(item).strip()
        if not normalized:
            continue
        parts = normalized.split(_BINDING_SEPARATOR)
        # A client has one active principal binding. Removing every old entry
        # for that client also migrates legacy plaintext principals away.
        if len(parts) == len(QUEST_BINDING_FIELDS) and parts[1] == client_id:
            continue
        if normalized not in bindings:
            bindings.append(normalized)
    bindings.append(replacement)
    return owner_users, bindings


def control_plane_result(
    config: Config | None,
    *,
    status: str,
    reason: str,
    updated: bool = False,
    authorized: bool = False,
    config_writable: bool = False,
) -> dict[str, object]:
    """Build the identifier-free control-plane response."""
    owner_count = len(config.owner_users) if config is not None else 0
    binding_count = (
        len(config.quest_session_owner_bindings) if config is not None else 0
    )
    return {
        "contract_version": IDENTITY_CONTROL_PLANE_CONTRACT_VERSION,
        "status": status,
        "reason": reason,
        "updated": bool(updated),
        "authorized": bool(authorized),
        "config_writable": bool(config_writable),
        "owner_count": owner_count,
        "quest_binding_count": binding_count,
        "grants_platform_action": False,
    }


def native_config_writable(config: Any) -> bool:
    return callable(getattr(config, "save_config_async", None))


def contract() -> dict[str, object]:
    """Return the strict provider declaration consumed by trusted plugins."""
    value_schema = {"type": "string", "min_length": 1, "max_length": 256}
    return {
        "name": IDENTITY_CONTROL_PLANE_CONTRACT_NAME,
        "version": IDENTITY_CONTROL_PLANE_CONTRACT_VERSION,
        "plugin": "astrbot_plugin_identity_guardian",
        "capabilities": (
            "read_status",
            "upsert_quest_owner_binding",
            "authorize_quest_session",
        ),
        "methods": (
            "get_identity_control_plane",
            "upsert_quest_owner_binding",
            "authorize_quest_session",
        ),
        "privacy": "counts_only",
        "principal_storage": "sha256_digest_only",
        "permission_identity_mode": "raw_platform_identity_tuple",
        "natural_person_grants_permission": False,
        "provider_present_fallback": "deny_without_local_merge",
        "request_schema": {
            "type": "object",
            "required": QUEST_BINDING_FIELDS,
            "additionalProperties": False,
            "properties": {
                **{field: value_schema for field in QUEST_BINDING_FIELDS},
                "api_principal_digest": {
                    "type": "string",
                    "pattern": "^sha256:[0-9a-f]{64}$",
                },
            },
            "normalization": "strip_outer_whitespace",
            "forbidden_characters": ("|", "control_characters"),
        },
        "response_schema": {
            "type": "object",
            "required": (
                "contract_version",
                "status",
                "reason",
                "updated",
                "authorized",
                "config_writable",
                "owner_count",
                "quest_binding_count",
                "grants_platform_action",
            ),
            "additionalProperties": False,
            "properties": {
                "contract_version": {"const": IDENTITY_CONTROL_PLANE_CONTRACT_VERSION},
                "status": {
                    "enum": ("ready", "saved", "rejected", "unavailable", "error")
                },
                "reason": {"type": "string"},
                "updated": {"type": "boolean"},
                "authorized": {"type": "boolean"},
                "config_writable": {"type": "boolean"},
                "owner_count": {"type": "integer", "minimum": 0},
                "quest_binding_count": {"type": "integer", "minimum": 0},
                "grants_platform_action": {"const": False},
            },
        },
    }


class IdentityControlPlane:
    """Persist and expose the authoritative raw-platform identity settings."""

    def __init__(
        self,
        *,
        config: Config,
        native_config: Any,
        logger: Any,
        stopped: Callable[[], bool],
        diagnostic: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.native_config = native_config
        self.logger = logger
        self.stopped = stopped
        self.diagnostic = diagnostic
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, object]:
        writable = native_config_writable(self.native_config)
        if not self.config.enabled:
            return control_plane_result(
                self.config,
                status="unavailable",
                reason="plugin_disabled",
                config_writable=writable,
            )
        if self.stopped():
            return control_plane_result(
                self.config,
                status="unavailable",
                reason="guard_stopped",
                config_writable=writable,
            )
        return control_plane_result(
            self.config,
            status="ready",
            reason="ready",
            config_writable=writable,
        )

    async def upsert(self, request: object) -> dict[str, object]:
        writable = native_config_writable(self.native_config)
        if not self.config.enabled:
            return control_plane_result(
                self.config,
                status="unavailable",
                reason="plugin_disabled",
                config_writable=writable,
            )
        if self.stopped():
            return control_plane_result(
                self.config,
                status="unavailable",
                reason="guard_stopped",
                config_writable=writable,
            )
        values, reason = normalize_quest_owner_binding(request)
        if values is None:
            return control_plane_result(
                self.config,
                status="rejected",
                reason=reason,
                config_writable=writable,
            )
        if not writable:
            return control_plane_result(
                self.config,
                status="unavailable",
                reason="native_config_unavailable",
                config_writable=False,
            )

        async with self._lock:
            owner_users, bindings = updated_quest_owner_settings(self.config, values)
            changes = {
                "owner_users": owner_users,
                "quest_session_owner_bindings": bindings,
            }
            try:
                committed = await self.native_config.save_config_async(changes)
            except Exception as exc:
                self.logger.warning(
                    "[idg] identity control plane save failed: error_type=%s",
                    type(exc).__name__,
                )
                return control_plane_result(
                    self.config,
                    status="error",
                    reason="config_save_failed",
                    config_writable=True,
                )
            if committed is not True:
                return control_plane_result(
                    self.config,
                    status="rejected",
                    reason="config_save_superseded",
                    config_writable=True,
                )

            refreshed = Config({**self.config._raw, **changes})
            self.config._raw = refreshed._raw
            if self.diagnostic is not None:
                self.diagnostic(
                    "identity.control_plane.updated",
                    "Quest 主人身份绑定已更新",
                    details={
                        "owner_count": len(self.config.owner_users),
                        "quest_binding_count": len(
                            self.config.quest_session_owner_bindings
                        ),
                    },
                )
            return control_plane_result(
                self.config,
                status="saved",
                reason="quest_owner_binding_saved",
                updated=True,
                authorized=True,
                config_writable=True,
            )
