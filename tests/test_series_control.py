from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_identity_guardian.series_control import SeriesControlAdapter


class P:
    def __init__(self, tmp_path):
        self.data_dir = tmp_path
        self.config = type(
            "C",
            (),
            {
                "_raw": {
                    "enabled": True,
                    "auto_moderate": False,
                    "join_audit_mode": "off",
                    "enable_api_guard": True,
                },
                "get": lambda s, k, d=None: s._raw.get(k, d),
                "apply_log_level": lambda s: None,
            },
        )()
        self.values = {}

    def _apply_series_control_runtime(self, v):
        self.values.update(v)


def test_safe_schema_and_runtime(tmp_path):
    a = SeriesControlAdapter(P(tmp_path))
    assert set(a.series_control_schema()["fields"]) == {
        "enabled",
        "auto_moderate",
        "join_audit_mode",
        "enable_api_guard",
    }
    assert (
        a.apply_series_control_patch(
            {"auto_moderate": True, "join_audit_mode": "approve_only"},
            expected_revision=0,
        )["status"]
        == "ok"
    )
    assert a.plugin.values["auto_moderate"] is True


def test_reject_identity_and_bad_mode(tmp_path):
    a = SeriesControlAdapter(P(tmp_path))
    assert (
        a.validate_series_control_patch({"owner_users": []}, expected_revision=0)[
            "reason"
        ]
        == "UNKNOWN_FIELD"
    )
    assert (
        a.validate_series_control_patch(
            {"join_audit_mode": "bad"}, expected_revision=0
        )["reason"]
        == "INVALID_VALUE"
    )
