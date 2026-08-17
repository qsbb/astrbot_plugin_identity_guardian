"""Versioned identity control-plane persistence and authorization tests."""

import asyncio
from copy import deepcopy

import pytest

from core.identity_control_plane import principal_digest
from tests.test_main_handlers import main, plugin_instance


class SavingConfig(dict):
    def __init__(self, values=None, *, committed=True, raises=False):
        super().__init__(values or {})
        self.committed = committed
        self.raises = raises
        self.calls = []

    async def save_config_async(self, changes):
        self.calls.append(deepcopy(changes))
        if self.raises:
            raise RuntimeError("save failed")
        if self.committed:
            self.update(deepcopy(changes))
        return self.committed


def _request(**overrides):
    request = {
        "api_principal_digest": principal_digest("api_key:test-only-principal"),
        "client_id": "quest-main",
        "platform_id": "aiocqhttp",
        "bot_id": "bot-main",
        "user_id": "owner-main",
    }
    request.update(overrides)
    return request


def _runtime_request():
    return {
        "api_principal": "api_key:test-only-principal",
        "client_id": "quest-main",
        "platform_id": "aiocqhttp",
        "bot_id": "bot-main",
        "user_id": "owner-main",
        "group_id": None,
    }


def _plugin(native=None):
    native = native or SavingConfig()
    plugin = plugin_instance()
    plugin.config = main.Config(native)
    plugin._native_config = native
    plugin._stopped = False
    plugin.logger = main.logger
    return plugin


def test_control_plane_contract_is_explicit_private_and_fail_closed():
    contract = _plugin().identity_control_plane_contract()

    assert contract["name"] == "identity.control_plane"
    assert contract["version"] == "1.0"
    assert contract["methods"] == (
        "get_identity_control_plane",
        "upsert_quest_owner_binding",
        "authorize_quest_session",
    )
    assert contract["privacy"] == "counts_only"
    assert contract["principal_storage"] == "sha256_digest_only"
    assert contract["natural_person_grants_permission"] is False
    assert contract["provider_present_fallback"] == "deny_without_local_merge"
    assert contract["request_schema"]["additionalProperties"] is False
    assert contract["request_schema"]["required"] == (
        "api_principal_digest",
        "client_id",
        "platform_id",
        "bot_id",
        "user_id",
    )
    assert contract["response_schema"]["additionalProperties"] is False


def test_status_exposes_only_counts_and_capabilities():
    native = SavingConfig(
        {
            "owner_users": ["owner-main"],
            "quest_session_owner_bindings": [
                "legacy-principal|quest-main|aiocqhttp|bot-main|owner-main"
            ],
        }
    )
    result = _plugin(native).get_identity_control_plane()

    assert result == {
        "contract_version": "1.0",
        "status": "ready",
        "reason": "ready",
        "updated": False,
        "authorized": False,
        "config_writable": True,
        "owner_count": 1,
        "quest_binding_count": 1,
        "grants_platform_action": False,
    }
    serialized = repr(result)
    assert "owner-main" not in serialized
    assert "legacy-principal" not in serialized


def test_upsert_atomically_saves_owner_digest_binding_and_authorizes():
    native = SavingConfig(
        {
            "owner_users": ["owner-old"],
            "quest_session_owner_bindings": [
                "legacy-secret|quest-main|aiocqhttp|bot-old|owner-old",
                "other-principal|quest-other|aiocqhttp|bot-other|owner-other",
            ],
        }
    )
    plugin = _plugin(native)

    result = asyncio.run(plugin.upsert_quest_owner_binding(_request()))

    assert result["status"] == "saved"
    assert result["updated"] is True
    assert result["authorized"] is True
    assert result["owner_count"] == 2
    assert result["quest_binding_count"] == 2
    assert native["owner_users"] == ["owner-old", "owner-main"]
    assert all(
        "test-only-principal" not in item
        for item in native["quest_session_owner_bindings"]
    )
    assert all(
        "legacy-secret" not in item for item in native["quest_session_owner_bindings"]
    )
    assert native["quest_session_owner_bindings"][-1].startswith("sha256:")
    assert plugin.authorize_quest_session(_runtime_request())["authorized"] is True

    refreshed = _plugin(native)
    assert refreshed.authorize_quest_session(_runtime_request())["authorized"] is True


@pytest.mark.parametrize(
    "committed,raises,reason",
    [(False, False, "config_save_superseded"), (True, True, "config_save_failed")],
)
def test_failed_save_does_not_change_runtime_or_persisted_state(
    committed, raises, reason
):
    native = SavingConfig(
        {"owner_users": ["owner-old"], "quest_session_owner_bindings": []},
        committed=committed,
        raises=raises,
    )
    plugin = _plugin(native)
    before_runtime = deepcopy(plugin.config._raw)
    before_native = deepcopy(dict(native))

    result = asyncio.run(plugin.upsert_quest_owner_binding(_request()))

    assert result["reason"] == reason
    assert result["updated"] is False
    assert result["authorized"] is False
    assert plugin.config._raw == before_runtime
    assert dict(native) == before_native


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "missing_api_principal_digest"),
        (
            _request(api_principal_digest="api_key:plaintext"),
            "invalid_api_principal_digest",
        ),
        (_request(user_id="owner|injected"), "invalid_user_id"),
        ({**_request(), "extra": "value"}, "unexpected_fields"),
    ],
)
def test_invalid_upsert_is_rejected_without_saving(payload, reason):
    native = SavingConfig()
    result = asyncio.run(_plugin(native).upsert_quest_owner_binding(payload))

    assert result["status"] == "rejected"
    assert result["reason"] == reason
    assert native.calls == []


def test_present_but_disabled_control_plane_does_not_invite_local_fallback():
    native = SavingConfig({"enabled": False})
    plugin = _plugin(native)

    status = plugin.get_identity_control_plane()
    saved = asyncio.run(plugin.upsert_quest_owner_binding(_request()))

    assert status["status"] == "unavailable"
    assert status["reason"] == "plugin_disabled"
    assert saved["status"] == "unavailable"
    assert native.calls == []


def test_legacy_plaintext_binding_remains_read_only_compatible():
    plugin = _plugin(
        SavingConfig(
            {
                "owner_users": ["owner-main"],
                "quest_session_owner_bindings": [
                    "api_key:test-only-principal|quest-main|aiocqhttp|bot-main|owner-main"
                ],
            }
        )
    )

    assert plugin.authorize_quest_session(_runtime_request())["authorized"] is True
