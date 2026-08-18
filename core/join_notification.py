"""Idempotent delivery of pending join-review notifications."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .join_review_store import GroupReviewConfig, JoinRequest, JoinReviewStore
from .onebot import OneBotClient


@dataclass(frozen=True, slots=True)
class NotificationResult:
    sent: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class JoinNotificationService:
    def __init__(self, store: JoinReviewStore, onebot: OneBotClient) -> None:
        self.store = store
        self.onebot = onebot

    @staticmethod
    def _targets(config: GroupReviewConfig) -> list[tuple[str, bool]]:
        targets: dict[str, bool] = {}
        if config.notify_target in {"target_group", "both"}:
            targets[config.group_id] = False
        if config.notify_target in {"specified_groups", "both"}:
            for group_id in config.specified_group_ids:
                targets[group_id] = True
        return list(targets.items())

    @staticmethod
    def build_message(
        request: JoinRequest,
        *,
        include_answer: bool,
        source_group_name: str = "未知群名",
        show_source: bool = False,
    ) -> str:
        lines = [
            "入群申请待审核",
            f"昵称：{request.nickname or '未知'}",
            f"QQ：{request.user_id}",
            f"等级：{request.level or '未知'}",
            f"问题：{request.question or '未知'}",
        ]
        if include_answer:
            lines.append(f"答案：{request.answer or '未知'}")
        if show_source:
            lines.append(
                f"来源群：{source_group_name or '未知群名'}（{request.group_id}）"
            )
        return "\n".join(lines)

    async def notify(
        self,
        bot: Any,
        request: JoinRequest,
        config: GroupReviewConfig,
        exclude_group_ids: Iterable[str] = (),
    ) -> NotificationResult:
        """发送待审通知；``exclude_group_ids`` 中的群不发送（用于与推送去重）。"""
        if (
            request.platform_id != config.platform_id
            or request.group_id != config.group_id
        ):
            raise ValueError("request_config_scope_mismatch")

        excluded = {str(g) for g in exclude_group_ids}
        source_info = await self.onebot.get_group_info_for_bot(
            bot, int(request.group_id), no_cache=False
        )
        source_name = str((source_info or {}).get("group_name", "")).strip()
        if not source_name:
            source_name = "未知群名"

        sent: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for target_group_id, show_source in self._targets(config):
            if target_group_id in excluded:
                continue
            target_key = f"group:{target_group_id}"
            claim = await self.store.claim_notification(request.request_id, target_key)
            if claim is None:
                skipped.append(target_group_id)
                continue
            try:
                message = self.build_message(
                    request,
                    include_answer=config.include_answer,
                    source_group_name=source_name,
                    show_source=show_source,
                )
                ok, _ = await self.onebot.send_group_message(
                    bot, int(target_group_id), message
                )
                await self.store.finish_notification(claim, succeeded=ok)
            except asyncio.CancelledError:
                await asyncio.shield(self.store.release_notification(claim))
                raise
            except Exception:
                await asyncio.shield(self.store.release_notification(claim))
                raise
            if ok:
                sent.append(target_group_id)
            else:
                failed.append(target_group_id)
        return NotificationResult(tuple(sent), tuple(skipped), tuple(failed))


__all__ = ["JoinNotificationService", "NotificationResult"]
