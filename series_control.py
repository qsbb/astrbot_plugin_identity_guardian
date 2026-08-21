"""Public ``series.control@1.0`` adapter for safe runtime switches."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONTRACT_NAME = "series.control@1.0"
PLUGIN_ID = "astrbot_plugin_identity_guardian"
SERIES_ID = "ningxin_suxi"
FIELDS = {
    "enabled": {"type": "bool", "default": True},
    "auto_moderate": {"type": "bool", "default": False},
    "join_audit_mode": {
        "type": "string",
        "default": "off",
        "enum": ["off", "approve_only", "notify_only"],
    },
    "enable_api_guard": {"type": "bool", "default": True},
}


class SeriesControlAdapter:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._path = Path(plugin.data_dir) / "series-control.json"
        self._overlay: dict[str, Any] = {}
        self._revision = 0
        self._mode = "native"
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        revision = raw.get("revision", 0)
        self._revision = (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
            else 0
        )
        values = raw.get("overrides")
        self._overlay = self._clean(values if isinstance(values, dict) else {})
        if self._overlay:
            self._mode = "managed"

    @staticmethod
    def _clean(values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in values.items():
            spec = FIELDS.get(name)
            if spec is None:
                continue
            if spec["type"] == "bool" and isinstance(value, bool):
                result[name] = value
            elif spec["type"] == "string" and value in spec["enum"]:
                result[name] = value
        return result

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="series-control-", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema_version": 1,
                        "revision": self._revision,
                        "overrides": self._overlay,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _native(self, field: str) -> Any:
        return self.plugin.config.get(field, FIELDS[field]["default"])

    def _effective(self, field: str, force_overlay: bool = False) -> Any:
        if force_overlay or self._mode == "managed":
            return self._overlay.get(field, self._native(field))
        return self._native(field)

    def sync_runtime(self, force_overlay: bool = False) -> None:
        values = {name: self._effective(name, force_overlay) for name in FIELDS}
        hook = getattr(self.plugin, "_apply_series_control_runtime", None)
        if callable(hook):
            hook(values)

    def series_control_contract(self) -> dict[str, Any]:
        return {
            "name": CONTRACT_NAME,
            "version": "1.0",
            "series_id": SERIES_ID,
            "plugin_id": PLUGIN_ID,
            "plugin_name": "序",
            "capabilities": [
                "read_schema",
                "read_snapshot",
                "validate_patch",
                "apply_patch",
                "reset_override",
            ],
            "read_only": False,
            "secrets_in_response": False,
            "max_patch_fields": len(FIELDS),
        }

    def series_control_schema(self) -> dict[str, Any]:
        fields: dict[str, dict[str, Any]] = {}
        for name, spec in FIELDS.items():
            field = {
                "type": spec["type"],
                "default": spec["default"],
                "control": "overrideable",
                "secret": False,
                "restart_required": False,
            }
            if "enum" in spec:
                field["enum"] = list(spec["enum"])
            fields[name] = field
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": "1.0",
            "plugin_id": PLUGIN_ID,
            "revision": self._revision,
            "fields": fields,
        }

    def series_control_snapshot(self) -> dict[str, Any]:
        raw = getattr(self.plugin.config, "_raw", {})
        return {
            "status": "ok",
            "revision": self._revision,
            "fields": {
                name: {
                    "native_configured": name in raw,
                    "managed_configured": name in self._overlay,
                    "effective_source": "managed"
                    if self._mode == "managed" and name in self._overlay
                    else "plugin",
                    "effective_value": self._effective(name),
                }
                for name in FIELDS
            },
        }

    def validate_series_control_patch(
        self, patch: dict[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        if expected_revision != self._revision:
            return {
                "status": "error",
                "reason": "REVISION_CONFLICT",
                "revision": self._revision,
            }
        if not isinstance(patch, dict) or not patch or len(patch) > len(FIELDS):
            return {
                "status": "error",
                "reason": "INVALID_PATCH",
                "revision": self._revision,
            }
        for name, value in patch.items():
            if name not in FIELDS:
                return {
                    "status": "error",
                    "reason": "UNKNOWN_FIELD",
                    "field": str(name),
                }
            if name not in self._clean({name: value}):
                reason = (
                    "INVALID_TYPE"
                    if FIELDS[name]["type"] == "bool"
                    else "INVALID_VALUE"
                )
                return {"status": "error", "reason": reason, "field": name}
        return {
            "status": "ok",
            "reason": "VALID",
            "revision": self._revision,
            "patch": dict(patch),
        }

    def apply_series_control_patch(
        self, patch: dict[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        result = self.validate_series_control_patch(
            patch, expected_revision=expected_revision
        )
        if result.get("status") != "ok":
            return result
        before = (dict(self._overlay), self._revision, self._mode)
        self._overlay.update(result["patch"])
        self._mode = "managed"
        self._revision += 1
        try:
            self._persist()
            self.sync_runtime(force_overlay=True)
        except Exception:
            self._overlay, self._revision, self._mode = before
            return {
                "status": "error",
                "reason": "APPLY_FAILED_ROLLED_BACK",
                "revision": self._revision,
            }
        return {
            "status": "ok",
            "reason": "APPLIED",
            "revision": self._revision,
            "fields": self.series_control_snapshot()["fields"],
        }

    def reset_series_control_override(
        self, fields: list[str] | None = None, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        if expected_revision is not None and expected_revision != self._revision:
            return {
                "status": "error",
                "reason": "REVISION_CONFLICT",
                "revision": self._revision,
            }
        names = list(self._overlay) if fields is None else fields
        if any(name not in FIELDS for name in names):
            return {
                "status": "error",
                "reason": "UNKNOWN_FIELD",
                "revision": self._revision,
            }
        before = (dict(self._overlay), self._revision, self._mode)
        for name in names:
            self._overlay.pop(name, None)
        self._revision += 1
        if not self._overlay:
            self._mode = "native"
        try:
            self._persist()
            self.sync_runtime()
        except Exception:
            self._overlay, self._revision, self._mode = before
            return {
                "status": "error",
                "reason": "APPLY_FAILED_ROLLED_BACK",
                "revision": self._revision,
            }
        return {
            "status": "ok",
            "reason": "RESET",
            "revision": self._revision,
            "fields": self.series_control_snapshot()["fields"],
        }


# Compatibility helpers for callers from the previous adapter draft.
def contract(plugin: Any) -> dict[str, Any]:
    return plugin._series_control.series_control_contract()


def schema(plugin: Any) -> dict[str, Any]:
    return plugin._series_control.series_control_schema()


def snapshot(plugin: Any) -> dict[str, Any]:
    return plugin._series_control.series_control_snapshot()


def validate(
    plugin: Any, patch: dict[str, Any], *, expected_revision: int
) -> dict[str, Any]:
    return plugin._series_control.validate_series_control_patch(
        patch, expected_revision=expected_revision
    )


def apply(
    plugin: Any, patch: dict[str, Any], *, expected_revision: int
) -> dict[str, Any]:
    return plugin._series_control.apply_series_control_patch(
        patch, expected_revision=expected_revision
    )


def reset(plugin: Any, fields=None, *, expected_revision=None) -> dict[str, Any]:
    return plugin._series_control.reset_series_control_override(
        fields, expected_revision=expected_revision
    )


def set_mode(plugin: Any, mode: str) -> dict[str, Any]:
    plugin._series_control._mode = mode if mode in {"native", "managed"} else "native"
    plugin._series_control.sync_runtime()
    return {"success": True, "mode": plugin._series_control._mode}
