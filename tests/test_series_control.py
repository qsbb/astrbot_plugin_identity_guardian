import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_identity_guardian.series_control import SeriesControlAdapter
from tests.test_main_handlers import main


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
        self.config._raw.update(v)


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


class NativeConfig:
    """假的 AstrBot 托管配置：save_config_async 真写文件。"""

    def __init__(self, path: Path, *, committed: bool = True) -> None:
        self.config_path = path
        self.committed = committed
        self.calls: list[dict] = []

    async def save_config_async(self, changes):
        self.calls.append(dict(changes))
        if not self.committed:
            return False
        data = {}
        if self.config_path.is_file():
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data.update(changes)
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return True


def _plugin_with_native(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    plugin = main.IdentityGuardianPlugin.__new__(main.IdentityGuardianPlugin)
    plugin.logger = logging.getLogger("idg-test")
    plugin.data_dir = tmp_path
    plugin.config = main.Config({"enabled": True, "auto_moderate": False})
    native_path = tmp_path / "native-config.json"
    native_path.write_text(
        json.dumps({"enabled": True, "auto_moderate": False}), encoding="utf-8"
    )
    plugin._native_config = NativeConfig(native_path)
    plugin._series_control = SeriesControlAdapter(plugin)
    return plugin


def test_snapshot_exposes_native_value_and_keeps_it_raw_after_takeover(tmp_path):
    """核「一键读取」依赖 native_value：接管覆盖后原生值必须保持真值。"""
    adapter = SeriesControlAdapter(P(tmp_path))
    fields = adapter.series_control_snapshot()["fields"]
    assert all("native_value" in item for item in fields.values())
    assert fields["auto_moderate"]["native_value"] is False
    assert adapter.apply_series_control_patch(
        {"auto_moderate": True}, expected_revision=0
    )["status"] == "ok"
    fields = adapter.series_control_snapshot()["fields"]
    assert fields["auto_moderate"]["native_value"] is False
    assert fields["auto_moderate"]["effective_value"] is True
    assert fields["auto_moderate"]["effective_source"] == "managed"


def test_native_write_persists_backup_and_rolls_back_on_failure(tmp_path):
    """一键固化：先备份、再原子落盘；落盘失败回滚内存不改真值。"""
    plugin = _plugin_with_native(tmp_path)
    result = asyncio.run(
        plugin.series_control_native_write({"auto_moderate": True}, expected_revision=0)
    )
    assert result["status"] == "ok" and result["reason"] == "APPLIED"
    assert result["written"] == ["auto_moderate"]
    assert result["backup_id"]
    assert plugin._native_config.calls == [{"auto_moderate": True}]
    saved = json.loads(
        (tmp_path / "native-config.json").read_text(encoding="utf-8")
    )
    assert saved["auto_moderate"] is True
    backups = sorted(tmp_path.glob("native-backup-*.json"))
    assert backups, "固化前必须先写备份"
    backup_raw = json.loads(backups[-1].read_text(encoding="utf-8"))
    assert backup_raw["auto_moderate"] is False
    # 固化成功后，适配器记住的新原生值与运行时保持一致
    assert (
        plugin.series_control_snapshot()["fields"]["auto_moderate"]["native_value"]
        is True
    )
    assert plugin.config.auto_moderate is True

    # 落盘未确认（superseded）：内存回滚，磁盘真值不变
    failing = _plugin_with_native(tmp_path / "rollback")
    failing._native_config.committed = False
    failed = asyncio.run(
        failing.series_control_native_write({"auto_moderate": True}, expected_revision=0)
    )
    assert failed["status"] == "error"
    assert failed["reason"] == "PERSIST_SUPERSEDED"
    assert failing.config.auto_moderate is False
    assert json.loads(
        (tmp_path / "rollback" / "native-config.json").read_text(encoding="utf-8")
    )["auto_moderate"] is False


def test_native_write_rejects_unknown_field_type_and_revision(tmp_path):
    plugin = _plugin_with_native(tmp_path)

    def call(patch, revision):
        return asyncio.run(
            plugin.series_control_native_write(patch, expected_revision=revision)
        )

    assert call({"owner_users": []}, 0)["reason"] == "UNKNOWN_FIELD"
    assert call({"auto_moderate": "yes"}, 0)["reason"] == "INVALID_TYPE"
    assert call({"join_audit_mode": "bad"}, 0)["reason"] == "INVALID_VALUE"
    assert call({"auto_moderate": True}, 7)["reason"] == "REVISION_CONFLICT"
    # 全部被拒：既没写原生配置，也没动内存
    assert plugin._native_config.calls == []
    assert plugin.config.auto_moderate is False
