"""Read-only discovery of aiocqhttp Bots and their joined groups."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .onebot import OneBotClient


@dataclass(frozen=True, slots=True)
class JoinedGroup:
    platform_id: str
    bot_id: str
    bot_nickname: str
    group_id: str
    group_name: str
    bot_role: str
    can_review: bool
    member_count: int | None = None
    max_member_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _platform_meta(instance: Any) -> tuple[str, str]:
    try:
        meta = instance.meta()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return "", ""
    return (
        str(getattr(meta, "id", "") or "").strip(),
        str(getattr(meta, "name", "") or "").strip().casefold(),
    )


def _platform_instances(context: Any) -> list[Any]:
    manager = getattr(context, "platform_manager", None)
    getter = getattr(manager, "get_insts", None)
    if callable(getter):
        try:
            values = getter()
            return list(values) if values is not None else []
        except (RuntimeError, TypeError, ValueError):
            return []
    values = getattr(manager, "platform_insts", None)
    return list(values) if isinstance(values, (list, tuple)) else []


def get_aiocqhttp_platform(context: Any, platform_id: str) -> Any | None:
    """Resolve one configured platform through AstrBot's public context API."""
    getter = getattr(context, "get_platform_inst", None)
    if callable(getter):
        try:
            candidate = getter(platform_id)
        except (RuntimeError, TypeError, ValueError):
            candidate = None
        candidate_id, adapter_name = _platform_meta(candidate)
        if candidate_id == platform_id and adapter_name == "aiocqhttp":
            return candidate
    for candidate in _platform_instances(context):
        candidate_id, adapter_name = _platform_meta(candidate)
        if candidate_id == platform_id and adapter_name == "aiocqhttp":
            return candidate
    return None


def get_aiocqhttp_bot(context: Any, platform_id: str) -> Any | None:
    platform = get_aiocqhttp_platform(context, platform_id)
    getter = getattr(platform, "get_client", None)
    bot = getter() if callable(getter) else None
    return bot if callable(getattr(bot, "call_action", None)) else None


async def get_bot_group_role(
    onebot: OneBotClient, bot: Any, group_id: str, bot_id: str = ""
) -> str:
    if not bot_id:
        login = await onebot.get_login_info(bot)
        bot_id = str((login or {}).get("user_id", ""))
    if not bot_id.isdigit() or not str(group_id).isdigit():
        return "unknown"
    info = await onebot.get_group_member_info_for_bot(
        bot, int(group_id), int(bot_id), no_cache=True
    )
    role = str((info or {}).get("role", "unknown")).casefold()
    return role if role in {"owner", "admin", "member"} else "unknown"


async def discover_joined_groups(
    context: Any, onebot: OneBotClient | None = None
) -> list[JoinedGroup]:
    """Query all active aiocqhttp adapters without changing plugin config."""
    client = onebot or OneBotClient()
    discovered: list[JoinedGroup] = []
    for platform in _platform_instances(context):
        platform_id, adapter_name = _platform_meta(platform)
        getter = getattr(platform, "get_client", None)
        try:
            bot = getter() if callable(getter) else None
        except (AttributeError, RuntimeError, TypeError, ValueError):
            bot = None
        if (
            not platform_id
            or adapter_name != "aiocqhttp"
            or not callable(getattr(bot, "call_action", None))
        ):
            continue
        login = await client.get_login_info(bot) or {}
        bot_id = str(login.get("user_id", "")).strip()
        bot_nickname = str(login.get("nickname", "")).strip()
        groups = await client.get_group_list(bot) or []
        for group in groups:
            group_id = str(group.get("group_id", "")).strip()
            if not group_id.isdigit() or group_id == "0":
                continue
            role = await get_bot_group_role(client, bot, group_id, bot_id)
            group_info = await client.get_group_info_for_bot(
                bot, int(group_id), no_cache=False
            )
            member_count = group.get("member_count")
            max_member_count = group.get("max_member_count")
            discovered.append(
                JoinedGroup(
                    platform_id=platform_id,
                    bot_id=bot_id,
                    bot_nickname=bot_nickname,
                    group_id=group_id,
                    group_name=str((group_info or {}).get("group_name", "")).strip()
                    or "未知群名",
                    bot_role=role,
                    can_review=role in {"owner", "admin"},
                    member_count=(
                        int(member_count)
                        if isinstance(member_count, int)
                        and not isinstance(member_count, bool)
                        else None
                    ),
                    max_member_count=(
                        int(max_member_count)
                        if isinstance(max_member_count, int)
                        and not isinstance(max_member_count, bool)
                        else None
                    ),
                )
            )
    return sorted(discovered, key=lambda item: (item.platform_id, int(item.group_id)))


# Short alias used by integrations that do not need to expose the transport
# detail in their naming.
discover_groups = discover_joined_groups


__all__ = [
    "JoinedGroup",
    "discover_joined_groups",
    "discover_groups",
    "get_aiocqhttp_bot",
    "get_aiocqhttp_platform",
    "get_bot_group_role",
]
