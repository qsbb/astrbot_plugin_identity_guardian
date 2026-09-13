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


def test_adapter_set_mode_supports_kernel_takeover(tmp_path):
    """核在读取 schema/snapshot 前会调用 series_control_set_mode。

    适配器缺少 set_mode 会让能力页显示“独立配置 / 读取失败”
    （2026-09-14 线上回归：AttributeError: 'SeriesControlAdapter' object has no attribute 'set_mode'）。
    """
    plugin = P(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    assert adapter.set_mode("managed") == {"success": True, "mode": "managed"}
    assert adapter._mode == "managed"
    assert adapter.series_control_snapshot()["fields"]["enabled"]["effective_value"] is True
    assert adapter.set_mode("native") == {"success": True, "mode": "native"}
    assert adapter._mode == "native"
    # 非法入参回落为 native，不抛异常
    assert adapter.set_mode("bogus") == {"success": True, "mode": "native"}


def test_main_entrypoint_targets_existing_adapter_method(tmp_path):
    """main.series_control_set_mode 必须命中真实存在的适配器方法。"""
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "self._series_control.set_mode(mode)" in source
    adapter = SeriesControlAdapter(P(tmp_path))
    assert hasattr(adapter, "set_mode")
