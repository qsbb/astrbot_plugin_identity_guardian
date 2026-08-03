"""Quest raw identity authorization contract tests."""

from copy import deepcopy

import pytest

from tests.test_main_handlers import main, plugin_instance


def _request(**overrides):
    request = {
        "api_principal": "astrbot-api",
        "client_id": "quest-living-room",
        "platform_id": "aiocqhttp",
        "bot_id": "bot-1",
        "user_id": "owner-1",
        "group_id": None,
    }
    request.update(overrides)
    return request


def _plugin():
    plugin = plugin_instance()
    plugin.config = main.Config(
        {
            "owner_users": ["owner-1"],
            "quest_session_owner_bindings": [
                "astrbot-api|quest-living-room|aiocqhttp|bot-1|owner-1"
            ],
        }
    )
    plugin._stopped = False
    return plugin


def test_contract_is_exact_read_only_and_versioned():
    contract = _plugin().quest_session_authorization_contract()

    assert contract["name"] == "identity.quest_session_authorization"
    assert contract["version"] == "1.0"
    assert contract["capabilities"] == ("authorize_read_only_session",)
    assert contract["method"] == "authorize_quest_session"
    assert contract["timeout_ms"] == 1000
    assert contract["permission_identity_fields"] == (
        "platform_id",
        "bot_id",
        "user_id",
    )
    assert contract["group_id_role"] == "session_context_only"
    assert contract["cross_platform_inheritance"] is False
    assert contract["grants_platform_action"] is False
    assert contract["request_schema"]["additionalProperties"] is False
    assert contract["request_schema"]["properties"]["group_id"] == {
        "type": ("string", "null")
    }
    assert contract["response_schema"]["additionalProperties"] is False
    assert contract["response_schema"]["properties"]["grants_platform_action"] == {
        "const": False
    }


def test_exact_private_owner_binding_is_authorized_without_side_effects():
    plugin = _plugin()
    request = _request()
    request_before = deepcopy(request)
    config_before = deepcopy(plugin.config._raw)

    result = plugin.authorize_quest_session(request)

    assert result == {
        "contract_version": "1.0",
        "status": "authorized",
        "authorized": True,
        "reason": "authorized_private_owner_identity",
        "access": "read_only_context",
        "owner_confirmed": True,
        "grants_platform_action": False,
    }
    assert request == request_before
    assert plugin.config._raw == config_before


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"group_id": "group-1"}, "private_session_required"),
        ({"api_principal": "other-api"}, "quest_identity_not_allowlisted"),
        ({"client_id": "other-client"}, "quest_identity_not_allowlisted"),
        ({"platform_id": "telegram"}, "quest_identity_not_allowlisted"),
        ({"bot_id": "bot-2"}, "quest_identity_not_allowlisted"),
        ({"user_id": "person-owner"}, "owner_not_configured"),
        ({"group_id": 123}, "invalid_group_id"),
        ({"group_id": "   "}, "private_session_required"),
        ({"user_id": "owner|1"}, "invalid_user_id"),
    ],
)
def test_group_alias_cross_identity_and_malformed_fields_are_denied(
    overrides, reason
):
    result = _plugin().authorize_quest_session(_request(**overrides))

    assert result["status"] == "denied"
    assert result["authorized"] is False
    assert result["reason"] == reason
    assert result["grants_platform_action"] is False


@pytest.mark.parametrize(
    "missing_field",
    [
        "api_principal",
        "client_id",
        "platform_id",
        "bot_id",
        "user_id",
        "group_id",
    ],
)
def test_every_missing_field_is_denied(missing_field):
    request = _request()
    del request[missing_field]

    result = _plugin().authorize_quest_session(request)

    assert result["status"] == "denied"
    assert result["reason"] == f"missing_{missing_field}"


def test_non_owner_is_denied_even_when_exact_binding_exists():
    plugin = _plugin()
    plugin.config = main.Config(
        {
            "owner_users": [],
            "quest_session_owner_bindings": [
                "astrbot-api|quest-living-room|aiocqhttp|bot-1|owner-1"
            ],
        }
    )

    result = plugin.authorize_quest_session(_request())

    assert result["reason"] == "owner_not_configured"
    assert result["authorized"] is False


def test_invalid_request_and_extra_fields_are_denied():
    plugin = _plugin()

    assert plugin.authorize_quest_session(None)["reason"] == "invalid_request"
    result = plugin.authorize_quest_session(_request(extra="value"))
    assert result["reason"] == "unexpected_fields"
    assert result["authorized"] is False


def test_unavailable_and_internal_error_fail_closed():
    plugin = _plugin()
    plugin.config = main.Config({"enabled": False})
    result = plugin.authorize_quest_session(_request())
    assert result["status"] == "unavailable"
    assert result["reason"] == "plugin_disabled"

    plugin = _plugin()
    plugin._stopped = True
    result = plugin.authorize_quest_session(_request())
    assert result["status"] == "unavailable"
    assert result["reason"] == "guard_stopped"

    class BrokenConfig:
        @property
        def enabled(self):
            raise RuntimeError("broken config")

    plugin = plugin_instance()
    plugin.config = BrokenConfig()
    result = plugin.authorize_quest_session(_request())
    assert result["status"] == "error"
    assert result["authorized"] is False
    assert result["reason"] == "authorization_error"
    assert result["grants_platform_action"] is False
