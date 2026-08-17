"""OneBot V11 API 封装测试。"""

import asyncio
from types import SimpleNamespace

from core.onebot import OneBotClient


class _Bot:
    def __init__(self, response):
        self.response = response

    async def call_action(self, action, **params):
        return self.response


class _RaisingBot:
    """模拟 aiocqhttp 在 status == failed 时抛 ActionFailed。"""

    def __init__(self, exc):
        self.exc = exc

    async def call_action(self, action, **params):
        raise self.exc


class _ActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed 的结构。"""

    def __init__(self, result):
        self.result = result
        super().__init__(f"retcode={result.get('retcode')}")


def test_unwrapped_none_is_success():
    """aiocqhttp 拆包后写操作返回 None，必须视为成功。

    这是回归点：此前用 resp["status"] == "ok" 判定，
    导致所有写操作（改名片/禁言/踢人等）实际生效却被报为失败。
    """
    event = SimpleNamespace(bot=_Bot(None))
    assert asyncio.run(OneBotClient().call(event, "set_group_card")) == {}


def test_unwrapped_dict_is_returned_as_data():
    """拆包后的查询结果原样返回。"""
    data = {"role": "admin", "card": "测试名片"}
    event = SimpleNamespace(bot=_Bot(data))
    assert asyncio.run(OneBotClient().call(event, "get_group_member_info")) == data


def test_unwrapped_list_is_returned_as_data():
    """成员列表是 list，不能被信封判定吞掉。"""
    data = [{"user_id": 1}, {"user_id": 2}]
    event = SimpleNamespace(bot=_Bot(data))
    assert asyncio.run(OneBotClient().call(event, "get_group_member_list")) == data


def test_action_failed_exception_returns_none():
    """ActionFailed 表示真实失败，返回 None。"""
    exc = _ActionFailed({"retcode": 100, "wording": "权限不足"})
    event = SimpleNamespace(bot=_RaisingBot(exc))
    assert asyncio.run(OneBotClient().call(event, "set_group_card")) is None


def test_data_with_status_field_is_not_treated_as_envelope():
    """业务 data 恰好含 status 字段时不应被误判为响应信封。"""
    data = {"status": "online", "user_id": 123}
    event = SimpleNamespace(bot=_Bot(data))
    assert asyncio.run(OneBotClient().call(event, "get_group_member_info")) == data


def test_envelope_ok_with_null_data_returns_success_marker():
    """兼容返回完整信封的适配器：成功且 data 为 null。"""
    event = SimpleNamespace(bot=_Bot({"status": "ok", "retcode": 0, "data": None}))
    assert asyncio.run(OneBotClient().call(event, "set_group_card")) == {}


def test_envelope_ok_with_data_returns_data():
    """兼容返回完整信封的适配器：成功且带 data。"""
    data = {"role": "member"}
    event = SimpleNamespace(
        bot=_Bot({"status": "ok", "retcode": 0, "data": data}),
    )
    assert asyncio.run(OneBotClient().call(event, "get_group_member_info")) == data


def test_envelope_failed_returns_none():
    """兼容返回完整信封的适配器：失败返回 None。"""
    event = SimpleNamespace(
        bot=_Bot({"status": "failed", "retcode": 100, "msg": "denied"}),
    )
    assert asyncio.run(OneBotClient().call(event, "set_group_card")) is None


def test_missing_bot_returns_none():
    """event 上没有 bot 时安全返回 None。"""
    assert asyncio.run(OneBotClient().call(SimpleNamespace(), "set_group_card")) is None


def test_set_group_card_reports_success_on_none_response():
    """端到端：改名片成功后必须返回 (True, "")。"""
    event = SimpleNamespace(bot=_Bot(None))
    ok, err = asyncio.run(
        OneBotClient().set_group_card(event, 10001, 20002, "测试名片")
    )
    assert ok is True
    assert err == ""


def test_set_group_card_reports_failure_on_action_failed():
    """端到端：真实失败仍要返回 (False, 原因)。"""
    exc = _ActionFailed({"retcode": 100, "wording": "权限不足"})
    event = SimpleNamespace(bot=_RaisingBot(exc))
    ok, err = asyncio.run(
        OneBotClient().set_group_card(event, 10001, 20002, "测试名片")
    )
    assert ok is False
    assert err == "set_group_card failed"


def test_send_group_message_with_id_returns_message_id():
    """推送审批链路依赖 send_group_msg 返回的 message_id。"""
    event = SimpleNamespace(bot=_Bot({"message_id": 12345}))
    ok, message_id, err = asyncio.run(
        OneBotClient().send_group_message_with_id(event, 10001, "推送文案")
    )
    assert ok is True
    assert message_id == "12345"
    assert err == ""


def test_send_group_message_with_id_success_without_id():
    """写操作 data 为 null（拆包后 None）时视为成功但无 message_id。"""
    event = SimpleNamespace(bot=_Bot(None))
    ok, message_id, err = asyncio.run(
        OneBotClient().send_group_message_with_id(event, 10001, "推送文案")
    )
    assert (ok, message_id, err) == (True, "", "")


def test_send_group_message_with_id_failure():
    exc = _ActionFailed({"retcode": 100, "wording": "权限不足"})
    event = SimpleNamespace(bot=_RaisingBot(exc))
    ok, message_id, err = asyncio.run(
        OneBotClient().send_group_message_with_id(event, 10001, "推送文案")
    )
    assert (ok, message_id, err) == (False, "", "send_group_msg failed")


def test_send_group_message_keeps_legacy_signature():
    """原 send_group_message 仍返回 (ok, err)，供 join_notification 使用。"""
    event = SimpleNamespace(bot=_Bot({"message_id": 12345}))
    ok, err = asyncio.run(OneBotClient().send_group_message(event, 10001, "通知"))
    assert (ok, err) == (True, "")
