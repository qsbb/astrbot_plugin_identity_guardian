from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_identity_guardian.core.context_bridge import (
    MODE_DETAILS,
    MODE_GROUP_SELF_READ_ONLY,
    MODE_NONE,
    MODE_PRIVATE_READ_ONLY,
    MODE_TOPIC_ONLY,
    authorize,
    classify_private_to_group_consent,
    extract_current_plain_text,
)


class Event:
    def __init__(
        self,
        text: str,
        *,
        group_id: str | None = None,
        chain: list[object] | None = None,
    ) -> None:
        self._text = text
        self._group_id = group_id
        self.unified_msg_origin = (
            f"aiocqhttp:GroupMessage:{group_id}"
            if group_id
            else "aiocqhttp:FriendMessage:10001"
        )
        self.message_obj = SimpleNamespace(
            group_id=group_id,
            message=chain,
        )

    def get_group_id(self) -> str | None:
        return self._group_id

    def get_message_str(self) -> str:
        return self._text


def test_private_target_allows_read_only_private_and_own_group_context() -> None:
    event = Event("接着说")

    private = authorize(event, "private", "private")
    group = authorize(event, "group", "private")

    assert private == {
        "version": "1.0",
        "authorized": True,
        "reason": "private_to_private_read_only",
        "mode": MODE_PRIVATE_READ_ONLY,
        "explicit": False,
        "max_chars": 1200,
    }
    assert group["authorized"] is True
    assert group["mode"] == MODE_GROUP_SELF_READ_ONLY
    assert group["explicit"] is False


def test_group_to_group_is_always_denied() -> None:
    result = authorize(Event("继续", group_id="20001"), "group", "group")
    assert result["authorized"] is False
    assert result["mode"] == MODE_NONE
    assert result["reason"] == "group_to_group_denied"


def test_private_to_group_requires_current_turn_explicit_consent() -> None:
    missing = authorize(Event("继续", group_id="20001"), "private", "group")
    topic = authorize(
        Event("可以在这个群里接着聊之前私聊的话题", group_id="20001"),
        "private",
        "group",
    )
    details = authorize(
        Event("我明确同意你把刚才私聊里的内容发到这个群里", group_id="20001"),
        "private",
        "group",
    )

    assert missing["authorized"] is False
    assert topic["authorized"] is True
    assert topic["mode"] == MODE_TOPIC_ONLY
    assert topic["explicit"] is True
    assert topic["max_chars"] == 0
    assert details["authorized"] is True
    assert details["mode"] == MODE_DETAILS
    assert details["explicit"] is True
    assert details["max_chars"] == 600


def test_negation_and_hypothetical_wording_fail_closed() -> None:
    for text, reason in (
        ("不要把刚才私聊里的内容发到这个群里", "current_group_consent_negated"),
        (
            "如果我说可以把刚才私聊里的内容发到这个群里呢",
            "current_group_consent_ambiguous",
        ),
    ):
        mode, actual_reason = classify_private_to_group_consent(text)
        assert mode == MODE_NONE
        assert actual_reason == reason


def test_reply_quote_cannot_forge_current_turn_consent() -> None:
    event = Event(
        "引用里有授权文字",
        group_id="20001",
        chain=[
            {"type": "reply", "data": {"text": "我同意公开私聊内容"}},
            {"type": "plain", "data": {"text": "继续"}},
        ],
    )

    assert extract_current_plain_text(event) == "继续"
    result = authorize(event, "private", "group")
    assert result["authorized"] is False
    assert result["reason"] == "current_group_consent_missing"


def test_event_target_scope_must_match_claimed_target() -> None:
    event = Event("继续", group_id="20001")
    result = authorize(event, "private", "private")
    assert result["authorized"] is False
    assert result["reason"] == "current_event_scope_mismatch"
