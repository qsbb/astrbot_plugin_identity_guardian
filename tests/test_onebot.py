"""OneBot V11 API 封装测试。"""

import asyncio
from types import SimpleNamespace

from core.onebot import OneBotClient


class _Bot:
    def __init__(self, response):
        self.response = response

    async def call_action(self, action, **params):
        return self.response


def test_success_with_null_data_returns_success_marker():
    event = SimpleNamespace(bot=_Bot({"status": "ok", "data": None}))
    result = asyncio.run(OneBotClient().call(event, "set_group_card"))
    assert result == {}


def test_success_with_data_returns_data():
    data = {"role": "member"}
    event = SimpleNamespace(bot=_Bot({"status": "ok", "data": data}))
    result = asyncio.run(OneBotClient().call(event, "get_group_member_info"))
    assert result == data


def test_failed_response_returns_none():
    event = SimpleNamespace(bot=_Bot({"status": "failed", "msg": "denied"}))
    result = asyncio.run(OneBotClient().call(event, "set_group_card"))
    assert result is None
