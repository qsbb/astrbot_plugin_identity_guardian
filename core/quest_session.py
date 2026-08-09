"""Read-only authorization for Quest sessions.

This module compares only explicitly configured raw platform bindings. It does not
read natural-person mappings or grant any platform action.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .identity_control_plane import principal_digest

QUEST_SESSION_AUTH_CONTRACT_NAME = "identity.quest_session_authorization"
QUEST_SESSION_AUTH_CONTRACT_VERSION = "1.0"

_REQUIRED_FIELDS = (
    "api_principal",
    "client_id",
    "platform_id",
    "bot_id",
    "user_id",
    "group_id",
)
_IDENTITY_FIELDS = _REQUIRED_FIELDS[:-1]
_BINDING_SEPARATOR = "|"


def decision(status: str, reason: str) -> dict[str, object]:
    """Build the stable, identifier-free authorization response."""
    authorized = status == "authorized"
    return {
        "contract_version": QUEST_SESSION_AUTH_CONTRACT_VERSION,
        "status": status,
        "authorized": authorized,
        "reason": reason,
        "access": "read_only_context" if authorized else "none",
        "owner_confirmed": authorized,
        "grants_platform_action": False,
    }


def authorize(config: Config, request: object) -> dict[str, object]:
    """Authorize one private Quest session using an exact configured binding."""
    if not isinstance(request, dict):
        return decision("denied", "invalid_request")

    unexpected = set(request) - set(_REQUIRED_FIELDS)
    if unexpected:
        return decision("denied", "unexpected_fields")

    for field in _REQUIRED_FIELDS:
        if field not in request:
            return decision("denied", f"missing_{field}")

    values: dict[str, str] = {}
    for field in _IDENTITY_FIELDS:
        value = request[field]
        if not isinstance(value, str) or not value.strip():
            return decision("denied", f"invalid_{field}")
        normalized = value.strip()
        if _BINDING_SEPARATOR in normalized:
            return decision("denied", f"invalid_{field}")
        values[field] = normalized

    group_id: Any = request["group_id"]
    if group_id is not None and not isinstance(group_id, str):
        return decision("denied", "invalid_group_id")
    if isinstance(group_id, str) and group_id != "":
        return decision("denied", "private_session_required")

    if not config.is_owner(values["user_id"]):
        return decision("denied", "owner_not_configured")

    legacy_binding = _BINDING_SEPARATOR.join(values[field] for field in _IDENTITY_FIELDS)
    digest_binding = _BINDING_SEPARATOR.join(
        (
            principal_digest(values["api_principal"]),
            values["client_id"],
            values["platform_id"],
            values["bot_id"],
            values["user_id"],
        )
    )
    if not any(
        candidate in config.quest_session_owner_bindings
        for candidate in (digest_binding, legacy_binding)
    ):
        return decision("denied", "quest_identity_not_allowlisted")

    return decision("authorized", "authorized_private_owner_identity")
