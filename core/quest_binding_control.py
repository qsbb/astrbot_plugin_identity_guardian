"""Quest-only read-context bindings that never modify platform owner roles."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

from .config import Config
from .identity_control_plane import (
    QUEST_BINDING_FIELDS,
    normalize_quest_owner_binding,
)

QUEST_BINDING_CONTROL_CONTRACT_NAME = "identity.quest_binding_control"
QUEST_BINDING_CONTROL_CONTRACT_VERSION = "1.0"
_BINDING_SEPARATOR = "|"
_REVOKE_FIELDS = ("api_principal_digest", "client_id")
_RECORD_PREFIX = "qrb1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_only_binding_record(values: dict[str, str]) -> str:
    client_hash = _digest(values["client_id"])
    principal_client_hash = _digest(
        values["api_principal_digest"] + _BINDING_SEPARATOR + values["client_id"]
    )
    binding = _BINDING_SEPARATOR.join(values[field] for field in QUEST_BINDING_FIELDS)
    return f"{_RECORD_PREFIX}:{client_hash}:{principal_client_hash}:{_digest(binding)}"


def updated_read_only_bindings(config: Config, values: dict[str, str]) -> list[str]:
    replacement = read_only_binding_record(values)
    client_id = values["client_id"]
    client_hash = _digest(client_id)
    bindings: list[str] = []
    for item in config.quest_session_read_only_bindings:
        normalized = str(item).strip()
        if not normalized:
            continue
        digest_parts = normalized.split(":", 3)
        raw_parts = normalized.split(_BINDING_SEPARATOR)
        if (
            len(digest_parts) == 4
            and digest_parts[0] == _RECORD_PREFIX
            and digest_parts[1] == client_hash
        ) or (
            len(raw_parts) == len(QUEST_BINDING_FIELDS) and raw_parts[1] == client_id
        ):
            continue
        if normalized not in bindings:
            bindings.append(normalized)
    bindings.append(replacement)
    return bindings


def owner_bindings_without_client(config: Config, client_id: str) -> list[str]:
    bindings: list[str] = []
    for item in config.quest_session_owner_bindings:
        normalized = str(item).strip()
        if not normalized:
            continue
        parts = normalized.split(_BINDING_SEPARATOR)
        if len(parts) == len(QUEST_BINDING_FIELDS) and parts[1] == client_id:
            continue
        if normalized not in bindings:
            bindings.append(normalized)
    return bindings


def normalize_revoke_request(request: object) -> tuple[dict[str, str] | None, str]:
    if not isinstance(request, dict) or set(request) != set(_REVOKE_FIELDS):
        return None, "invalid_request"
    values: dict[str, str] = {}
    for field in _REVOKE_FIELDS:
        value = request.get(field)
        if not isinstance(value, str):
            return None, f"invalid_{field}"
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 256
            or _BINDING_SEPARATOR in normalized
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
        ):
            return None, f"invalid_{field}"
        values[field] = normalized
    digest, reason = normalize_quest_owner_binding(
        {
            "api_principal_digest": values["api_principal_digest"],
            "client_id": values["client_id"],
            "platform_id": "validation-placeholder",
            "bot_id": "validation-placeholder",
            "user_id": "validation-placeholder",
        }
    )
    if digest is None:
        return None, reason
    return values, "ready"


def result(
    config: Config | None,
    *,
    status: str,
    reason: str,
    updated: bool = False,
    authorized: bool = False,
    config_writable: bool = False,
) -> dict[str, object]:
    return {
        "contract_version": QUEST_BINDING_CONTROL_CONTRACT_VERSION,
        "status": status,
        "reason": reason,
        "updated": bool(updated),
        "authorized": bool(authorized),
        "config_writable": bool(config_writable),
        "read_only_binding_count": (
            len(config.quest_session_read_only_bindings) if config is not None else 0
        ),
        "grants_owner": False,
        "grants_platform_action": False,
    }


def contract() -> dict[str, object]:
    return {
        "name": QUEST_BINDING_CONTROL_CONTRACT_NAME,
        "version": QUEST_BINDING_CONTROL_CONTRACT_VERSION,
        "plugin": "astrbot_plugin_identity_guardian",
        "capabilities": (
            "upsert_read_only_quest_binding",
            "revoke_read_only_quest_binding",
        ),
        "methods": ("upsert_quest_binding", "revoke_quest_binding"),
        "privacy": "counts_only",
        "principal_storage": "sha256_digest_only",
        "owner_users_mutated": False,
        "natural_person_grants_permission": False,
        "grants_owner": False,
        "grants_platform_action": False,
        "provider_present_fallback": "deny_without_local_merge",
        "request_fields": QUEST_BINDING_FIELDS,
        "revoke_request_fields": _REVOKE_FIELDS,
        "response_fields": (
            "contract_version",
            "status",
            "reason",
            "updated",
            "authorized",
            "config_writable",
            "read_only_binding_count",
            "grants_owner",
            "grants_platform_action",
        ),
    }


class QuestBindingControl:
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

    async def upsert(self, request: object) -> dict[str, object]:
        writable = callable(getattr(self.native_config, "save_config_async", None))
        if not self.config.enabled:
            return result(
                self.config,
                status="unavailable",
                reason="plugin_disabled",
                config_writable=writable,
            )
        if self.stopped():
            return result(
                self.config,
                status="unavailable",
                reason="guard_stopped",
                config_writable=writable,
            )
        values, reason = normalize_quest_owner_binding(request)
        if values is None:
            return result(
                self.config,
                status="rejected",
                reason=reason,
                config_writable=writable,
            )
        if not writable:
            return result(
                self.config,
                status="unavailable",
                reason="native_config_unavailable",
            )

        async with self._lock:
            bindings = updated_read_only_bindings(self.config, values)
            owner_bindings = owner_bindings_without_client(
                self.config, values["client_id"]
            )
            changes = {
                "quest_session_read_only_bindings": bindings,
                "quest_session_owner_bindings": owner_bindings,
            }
            try:
                committed = await self.native_config.save_config_async(changes)
            except Exception as exc:
                self.logger.warning(
                    "[idg] Quest read-only binding save failed: error_type=%s",
                    type(exc).__name__,
                )
                return result(
                    self.config,
                    status="error",
                    reason="config_save_failed",
                    config_writable=True,
                )
            if committed is not True:
                return result(
                    self.config,
                    status="rejected",
                    reason="config_save_superseded",
                    config_writable=True,
                )

            refreshed = Config({**self.config._raw, **changes})
            self.config._raw = refreshed._raw
            if self.diagnostic is not None:
                self.diagnostic(
                    "identity.quest_binding.updated",
                    "Quest 只读身份绑定已更新",
                    details={"read_only_binding_count": len(bindings)},
                )
            return result(
                self.config,
                status="saved",
                reason="quest_read_only_binding_saved",
                updated=True,
                authorized=True,
                config_writable=True,
            )

    async def revoke(self, request: object) -> dict[str, object]:
        writable = callable(getattr(self.native_config, "save_config_async", None))
        if not self.config.enabled:
            return result(
                self.config,
                status="unavailable",
                reason="plugin_disabled",
                config_writable=writable,
            )
        if self.stopped():
            return result(
                self.config,
                status="unavailable",
                reason="guard_stopped",
                config_writable=writable,
            )
        values, reason = normalize_revoke_request(request)
        if values is None:
            return result(
                self.config,
                status="rejected",
                reason=reason,
                config_writable=writable,
            )
        if not writable:
            return result(
                self.config,
                status="unavailable",
                reason="native_config_unavailable",
            )

        async with self._lock:
            retained: list[str] = []
            removed = False
            client_hash = _digest(values["client_id"])
            principal_client_hash = _digest(
                values["api_principal_digest"]
                + _BINDING_SEPARATOR
                + values["client_id"]
            )
            for item in self.config.quest_session_read_only_bindings:
                normalized = str(item).strip()
                parts = normalized.split(":", 3)
                if (
                    len(parts) == 4
                    and parts[0] == _RECORD_PREFIX
                    and parts[1] == client_hash
                    and parts[2] == principal_client_hash
                ):
                    removed = True
                    continue
                if normalized and normalized not in retained:
                    retained.append(normalized)
            changes = {"quest_session_read_only_bindings": retained}
            try:
                committed = await self.native_config.save_config_async(changes)
            except Exception as exc:
                self.logger.warning(
                    "[idg] Quest read-only binding revoke failed: error_type=%s",
                    type(exc).__name__,
                )
                return result(
                    self.config,
                    status="error",
                    reason="config_save_failed",
                    config_writable=True,
                )
            if committed is not True:
                return result(
                    self.config,
                    status="rejected",
                    reason="config_save_superseded",
                    config_writable=True,
                )
            refreshed = Config({**self.config._raw, **changes})
            self.config._raw = refreshed._raw
            return result(
                self.config,
                status="revoked",
                reason="quest_read_only_binding_revoked",
                updated=removed,
                authorized=False,
                config_writable=True,
            )
