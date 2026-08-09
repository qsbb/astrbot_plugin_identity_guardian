"""Quest-only binding control must never mutate platform owner roles."""

import asyncio
from copy import deepcopy

import pytest

from core.identity_control_plane import principal_digest
from core.quest_binding_control import read_only_binding_record
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
    value = {
        "api_principal_digest": principal_digest("api_key:test-principal"),
        "client_id": "quest-main",
        "platform_id": "onebot-main",
        "bot_id": "bot-main",
        "user_id": "ordinary-user",
    }
    value.update(overrides)
    return value


def _runtime_request():
    return {
        "api_principal": "api_key:test-principal",
        "client_id": "quest-main",
        "platform_id": "onebot-main",
        "bot_id": "bot-main",
        "user_id": "ordinary-user",
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


def test_contract_explicitly_forbids_owner_and_platform_grants():
    contract = _plugin().quest_binding_control_contract()

    assert contract["name"] == "identity.quest_binding_control"
    assert contract["version"] == "1.0"
    assert contract["methods"] == (
        "upsert_quest_binding",
        "revoke_quest_binding",
    )
    assert contract["owner_users_mutated"] is False
    assert contract["natural_person_grants_permission"] is False
    assert contract["grants_owner"] is False
    assert contract["grants_platform_action"] is False


def test_upsert_saves_only_read_only_binding_and_authorizes_non_owner_context():
    native = SavingConfig(
        {
            "owner_users": ["existing-owner"],
            "quest_session_owner_bindings": [
                "legacy|owner-client|onebot-main|owner-bot|existing-owner"
            ],
        }
    )
    plugin = _plugin(native)

    result = asyncio.run(plugin.upsert_quest_binding(_request()))

    assert result["status"] == "saved"
    assert result["authorized"] is True
    assert result["grants_owner"] is False
    assert native["owner_users"] == ["existing-owner"]
    assert native["quest_session_owner_bindings"] == [
        "legacy|owner-client|onebot-main|owner-bot|existing-owner"
    ]
    assert len(native["quest_session_read_only_bindings"]) == 1
    assert native["quest_session_read_only_bindings"][0].startswith("qrb1:")
    assert "ordinary-user" not in native["quest_session_read_only_bindings"][0]

    authorization = plugin.authorize_quest_session(_runtime_request())
    assert authorization == {
        "contract_version": "1.0",
        "status": "authorized",
        "authorized": True,
        "reason": "authorized_private_quest_identity",
        "access": "read_only_context",
        "owner_confirmed": False,
        "grants_platform_action": False,
    }


def test_rebind_replaces_only_same_read_only_client():
    native = SavingConfig(
        {
            "owner_users": [],
            "quest_session_read_only_bindings": [
                "old|quest-main|onebot-main|old-bot|old-user",
                "other|quest-other|onebot-main|other-bot|other-user",
            ],
        }
    )

    result = asyncio.run(_plugin(native).upsert_quest_binding(_request()))

    assert result["read_only_binding_count"] == 2
    assert all("old-user" not in item for item in native["quest_session_read_only_bindings"])
    assert any("quest-other" in item for item in native["quest_session_read_only_bindings"])
    assert native["owner_users"] == []


def test_upsert_migrates_same_client_owner_binding_without_removing_owner_role():
    digest = principal_digest("api_key:test-principal")
    native = SavingConfig(
        {
            "owner_users": ["ordinary-user"],
            "quest_session_owner_bindings": [
                f"{digest}|quest-main|onebot-main|bot-main|ordinary-user"
            ],
        }
    )
    plugin = _plugin(native)

    asyncio.run(plugin.upsert_quest_binding(_request()))

    assert native["owner_users"] == ["ordinary-user"]
    assert native["quest_session_owner_bindings"] == []
    authorization = plugin.authorize_quest_session(_runtime_request())
    assert authorization["reason"] == "authorized_private_quest_identity"
    assert authorization["owner_confirmed"] is False


def test_revoke_is_idempotent_and_removes_only_principal_client_binding():
    digest = principal_digest("api_key:test-principal")
    native = SavingConfig(
        {
            "quest_session_read_only_bindings": [
                read_only_binding_record(_request()),
                read_only_binding_record(
                    _request(
                        client_id="quest-other",
                        bot_id="bot-other",
                        user_id="other-user",
                    )
                ),
            ]
        }
    )
    plugin = _plugin(native)
    request = {"api_principal_digest": digest, "client_id": "quest-main"}

    first = asyncio.run(plugin.revoke_quest_binding(request))
    second = asyncio.run(plugin.revoke_quest_binding(request))

    assert first["status"] == "revoked"
    assert first["updated"] is True
    assert second["status"] == "revoked"
    assert second["updated"] is False
    assert native["quest_session_read_only_bindings"] == [
        read_only_binding_record(
            _request(
                client_id="quest-other",
                bot_id="bot-other",
                user_id="other-user",
            )
        )
    ]


@pytest.mark.parametrize(
    "committed,raises,reason",
    [(False, False, "config_save_superseded"), (True, True, "config_save_failed")],
)
def test_failed_save_changes_neither_runtime_nor_native_config(
    committed, raises, reason
):
    native = SavingConfig(
        {"owner_users": ["existing-owner"]},
        committed=committed,
        raises=raises,
    )
    plugin = _plugin(native)
    before_runtime = deepcopy(plugin.config._raw)
    before_native = deepcopy(dict(native))

    result = asyncio.run(plugin.upsert_quest_binding(_request()))

    assert result["reason"] == reason
    assert result["authorized"] is False
    assert plugin.config._raw == before_runtime
    assert dict(native) == before_native


def test_invalid_payload_is_rejected_without_save():
    native = SavingConfig()
    result = asyncio.run(
        _plugin(native).upsert_quest_binding(_request(user_id="bad|user"))
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_user_id"
    assert native.calls == []
