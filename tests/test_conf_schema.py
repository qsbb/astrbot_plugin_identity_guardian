"""配置 schema 与 Config 默认值一致性测试。

_conf_schema.json 是 AstrBot WebUI 配置页的唯一事实源；
本测试确保它与 core.config 的默认值、类型保持一致，
避免配置页显示的内容与代码实际行为脱节。
"""

import json
import sys
from pathlib import Path

# 将插件根目录加入 sys.path 以便直接导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import _DEFAULTS  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

_TYPE_CHECKERS = {
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "list": lambda v: isinstance(v, list),
}


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_is_valid_json():
    """schema 是合法 JSON 且非空。"""
    schema = _load_schema()
    assert isinstance(schema, dict) and schema


def test_schema_keys_match_defaults():
    """schema 配置项与 Config 默认值集合完全一致。"""
    schema = _load_schema()
    assert set(schema.keys()) == set(_DEFAULTS.keys())


def test_schema_defaults_match_config():
    """每个配置项的 schema 默认值与 Config 默认值一致。"""
    schema = _load_schema()
    for key, default in _DEFAULTS.items():
        assert key in schema, f"schema 缺少配置项 {key}"
        assert schema[key]["default"] == default, f"{key} 默认值不一致"


def test_schema_types_match_defaults():
    """默认值类型与声明的 type 相符。"""
    schema = _load_schema()
    for key, entry in schema.items():
        type_name = entry.get("type")
        assert type_name in _TYPE_CHECKERS, f"{key} 类型未知: {type_name}"
        assert _TYPE_CHECKERS[type_name](entry["default"]), (
            f"{key} 默认值与声明类型 {type_name} 不符"
        )


def test_schema_entries_have_description():
    """每个配置项都有 description，否则配置页会显示空白标签。"""
    schema = _load_schema()
    for key, entry in schema.items():
        assert entry.get("description"), f"{key} 缺少 description"


def test_schema_options_contain_default():
    """带 options 的配置项，默认值必须在 options 中。"""
    schema = _load_schema()
    for key, entry in schema.items():
        options = entry.get("options")
        if options:
            assert entry["default"] in options, f"{key} 默认值不在 options 中"
