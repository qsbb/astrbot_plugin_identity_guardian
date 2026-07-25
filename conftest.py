"""pytest 配置：mock astrbot 框架依赖。

插件 core 模块依赖 astrbot.api.logger，
在测试环境中框架不可用，需要 mock。
"""

import logging
import os
import sys
import types
from unittest.mock import MagicMock

# 创建 mock astrbot 包
_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_api_star = types.ModuleType("astrbot.api.star")
_astrbot_api_event = types.ModuleType("astrbot.api.event")
_astrbot_api_provider = types.ModuleType("astrbot.api.provider")
_astrbot_api_config = types.ModuleType("astrbot.api.config")
_astrbot_core = types.ModuleType("astrbot.core")
_astrbot_core_star = types.ModuleType("astrbot.core.star")
_astrbot_core_star_registry = types.ModuleType(
    "astrbot.core.star.star_handlers_registry"
)
_astrbot_core_agent = types.ModuleType("astrbot.core.agent")
_astrbot_core_agent_message = types.ModuleType("astrbot.core.agent.message")
_astrbot_api_message_components = types.ModuleType("astrbot.api.message_components")

# logger 使用标准 logging
_test_logger = logging.getLogger("idg_test")
_test_logger.setLevel(logging.DEBUG)
_astrbot_api.logger = _test_logger

# mock Star, Context, StarTools, register
# Star 必须是真正的类：插件类继承它，若用 MagicMock 实例做基类，
# 插件类本身会退化成 MagicMock，导致 __new__ / isinstance 全部失效。
class _Star:
    """最小 Star 基类桩，只保留 context 存取语义。"""

    def __init__(self, context=None, *args, **kwargs):
        self.context = context


_astrbot_api_star.Star = _Star
_astrbot_api_star.Context = MagicMock()
_astrbot_api_star.StarTools = MagicMock()
_astrbot_api_star.StarTools.get_data_dir = MagicMock(return_value="/tmp/idg_test")


# register 必须是恒等装饰器：MagicMock 会把被装饰的插件类替换成 mock 返回值，
# 之后 __new__ / isinstance(x, IdentityGuardianPlugin) 都会失败。
def _register(*args, **kwargs):
    def decorator(cls):
        return cls

    return decorator


_astrbot_api_star.register = _register

# mock filter
_mock_filter = MagicMock()
_mock_filter.EventMessageType = MagicMock()
_mock_filter.EventMessageType.ALL = "all"
_mock_filter.EventMessageType.GROUP_MESSAGE = "group"
_mock_filter.EventMessageType.PRIVATE_MESSAGE = "private"
_mock_filter.PermissionType = MagicMock()
_mock_filter.PermissionType.ADMIN = "admin"
_mock_filter.PermissionType.OWNER = "owner"
_mock_filter.PermissionType.MEMBER = "member"
_mock_filter.PlatformAdapterType = MagicMock()
_mock_filter.PlatformAdapterType.AIOCQHTTP = "aiocqhttp"
_mock_filter.on_llm_request = lambda *a, **kw: lambda f: f
_mock_filter.event_message_type = lambda *a, **kw: lambda f: f
_mock_filter.llm_tool = lambda *a, **kw: lambda f: f
_mock_filter.command_group = lambda *a, **kw: lambda f: f
_mock_filter.on_llm_response = lambda *a, **kw: lambda f: f
_mock_filter.on_decorating_result = lambda *a, **kw: lambda f: f
_mock_filter.on_waiting_llm_request = lambda *a, **kw: lambda f: f
_astrbot_api_event.filter = _mock_filter
_astrbot_api_event.AstrMessageEvent = MagicMock()

# mock ProviderRequest
_astrbot_api_provider.ProviderRequest = MagicMock()

# mock AstrBotConfig
_astrbot_api_config.AstrBotConfig = MagicMock()
_astrbot_api.AstrBotConfig = MagicMock()


# mock TextPart
class _TextPart:
    def __init__(self, text=""):
        self.text = text


_astrbot_core_agent_message.TextPart = _TextPart


# mock message_components
class _Plain:
    def __init__(self, text=""):
        self.text = text


_astrbot_api_message_components.Plain = _Plain
_astrbot_api_message_components.Record = MagicMock()
_astrbot_api_message_components.File = MagicMock()
_astrbot_api_message_components.Image = MagicMock()

# mock star_handlers_registry
_astrbot_core_star_registry.star_handlers_registry = MagicMock()
_astrbot_core_star_registry.star_handlers_registry.handlers = []

# 注册到 sys.modules
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.star"] = _astrbot_api_star
sys.modules["astrbot.api.event"] = _astrbot_api_event
sys.modules["astrbot.api.provider"] = _astrbot_api_provider
sys.modules["astrbot.api.config"] = _astrbot_api_config
sys.modules["astrbot.api.message_components"] = _astrbot_api_message_components
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.star"] = _astrbot_core_star
sys.modules["astrbot.core.star.star_handlers_registry"] = _astrbot_core_star_registry
sys.modules["astrbot.core.agent"] = _astrbot_core_agent
sys.modules["astrbot.core.agent.message"] = _astrbot_core_agent_message

# 将插件根目录加入 path（仅在尚未加入时）
_PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
