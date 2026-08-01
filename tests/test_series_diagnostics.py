from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_identity_guardian.series_diagnostics import (
    diagnostic_clear,
    diagnostic_event,
    diagnostic_events,
    logger,
)


def test_series_diagnostics_are_bounded_redacted_and_isolated():
    diagnostic_clear()
    diagnostic_event(
        "identity.policy",
        "decision for 123456789",
        details={"allowed": False, "user_id": "123456789"},
    )
    logger.warning(
        'failed authorization=secret message="private text" '
        "https://example.test/path?token=secret for 123456789 "
        "alice@example.com Abcdef1234567890Ghijkl private chat body "
        "uid=user-a token abcdefghijk"
    )
    payload = diagnostic_events(after_seq=0, limit=10)
    serialized = str(payload["events"])
    assert payload["stream_id"]
    assert diagnostic_events()["stream_id"] == payload["stream_id"]
    log_detail = payload["events"][1]["details"]["log_detail"]
    assert "private chat body" in log_detail
    assert "user-a" not in serialized
    assert "abcdefghijk" not in serialized
    assert logger.propagate is False
    assert payload["events"][0]["details"] == {"allowed": False}
    assert "123456789" not in str(payload["events"])
    assert "token=secret" not in str(payload["events"])
    assert "authorization=secret" not in str(payload["events"])
    assert "private text" not in str(payload["events"])
    assert "alice@example.com" not in str(payload["events"])
    assert "Abcdef1234567890Ghijkl" not in str(payload["events"])
    assert any(
        type(handler).__name__ == "DiagnosticBuffer" for handler in logger.handlers
    )


def test_series_diagnostics_cursor_and_capacity():
    diagnostic_clear()
    base = diagnostic_events()["next_seq"]
    for index in range(1005):
        diagnostic_event("identity.event", index)
    payload = diagnostic_events(after_seq=base + 1000, limit=20)
    assert [event["seq"] for event in payload["events"]] == list(
        range(base + 1001, base + 1006)
    )
    assert payload["dropped_before"] == base + 5
    old_stream_id = payload["stream_id"]
    diagnostic_clear()
    assert diagnostic_events()["stream_id"] != old_stream_id
