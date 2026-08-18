"""待审入群申请的事件驱动推送服务。

申请进入人工待审（JoinReviewRuntime 返回 pending_review）后，
把申请推送到申请所属群按群配置的推送群（``GroupReviewConfig.push_group_ids``），
推送群留空时回退推送到申请所属群本身。文案样式由按群 ``push_style`` 决定：
natural 走 LLM 整段生成（由调用方注入带人格的 caller，失败回退格式化），
formatted 复用入群通知模板并附看法行——caller 可用时看法由 LLM 一句话生成，
失败/不可用回退自动审核结论；两种样式都附引用回复审批引导。

推送消息走 OneBot ``send_group_msg`` 以拿到 ``message_id``——引用本条推送
回复「同意/不同意」的群内审批依赖该 ID 定位申请；发送成功后经
``store.record_push_ref`` 记录映射。投递幂等：复用 store 的通知 claim
事务，target_key 用 ``push:{group_id}`` 前缀，同一申请对同一推送群只推一次。
服务不持有全局配置，按群配置对象由调用方每次传入。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .group_discovery import get_aiocqhttp_bot
from .join_notification import JoinNotificationService
from .join_review_store import GroupReviewConfig, JoinRequest, JoinReviewStore
from .onebot import OneBotClient
from .prompts import build_push_message_prompt, build_push_opinion_prompt

PUSH_REPLY_HINT = (
    "同意请引用本条消息回复『同意』，拒绝回复『拒绝』，或到入群审核管理页处理。"
)

_DECISION_OPINION: dict[str, str] = {
    "correct": "自动审核建议通过",
    "incorrect": "自动审核认为答案不靠谱，建议谨慎",
    "uncertain": "自动审核无法确定，建议人工复核",
    "unavailable": "自动审核不可用，请人工复核",
}


def resolve_push_targets(request: JoinRequest, config: GroupReviewConfig) -> list[str]:
    """解析申请的实际推送目标群：push_group_ids 非空时按配置，否则回退申请所属群。"""
    return list(config.push_group_ids) or [request.group_id]


def build_opinion_line(decision: Any) -> str:
    """把自动审核 decision 渲染成一行「看法」；None 表示未经过自动审核。"""
    if decision is None:
        return "看法：该申请未经过自动审核。"
    verdict = str(getattr(decision, "verdict", "") or "")
    opinion = _DECISION_OPINION.get(verdict, "自动审核未给出结论")
    try:
        confidence = float(getattr(decision, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(getattr(decision, "reason", "") or "").strip()[:100]
    line = f"看法：{opinion}（置信度 {confidence:.2f}）"
    return f"{line}：{reason}" if reason else f"{line}。"


def _formatted_message(
    request: JoinRequest,
    config: GroupReviewConfig,
    source_group_name: str,
    decision: Any = None,
    opinion: str = "",
) -> str:
    body = JoinNotificationService.build_message(
        request,
        include_answer=config.include_answer,
        source_group_name=source_group_name,
        show_source=True,
    )
    opinion_line = f"看法：{opinion}" if opinion else build_opinion_line(decision)
    return f"{body}\n{opinion_line}\n{PUSH_REPLY_HINT}"


async def _llm_opinion(
    request: JoinRequest,
    source_group_name: str,
    llm_caller: Any,
) -> str:
    """formatted 样式的 LLM 一句话看法；caller 不可用/失败/空结果返回空串。"""
    if llm_caller is None:
        return ""
    try:
        result = await llm_caller(
            build_push_opinion_prompt(
                question=str(getattr(request, "question", "") or ""),
                answer=str(getattr(request, "answer", "") or ""),
                nickname=str(getattr(request, "nickname", "") or ""),
                source_group_name=source_group_name,
            )
        )
    except Exception:
        return ""
    if result is None:
        return ""
    # 折叠换行并截断，保证看法只占一行。
    return " ".join(str(result).split())[:120]


async def render_push_preview(
    request: JoinRequest,
    config: GroupReviewConfig,
    source_group_name: str,
    llm_caller: Any,
    decision: Any = None,
) -> dict[str, str]:
    """零副作用渲染推送文案，返回 ``{"style", "text", "opinion_source"}``。

    与生产推送同一条渲染路径（``RequestPushService.render_message`` 委托本
    函数），额外标注实际走的样式与看法来源：natural 空结果或异常回退格式化
    模板时 ``style`` 为 ``natural_fallback_formatted``；``opinion_source``
    为 ``llm``（LLM 生成）/ ``decision``（自动审核结论）/ ``none``（无）。
    两种样式合计最多一次额外 LLM 调用：natural 整段生成已含看法，不再单独
    生成；natural 回退格式化时 LLM 刚失败过，看法直接用自动审核结论。
    """
    style = "formatted"
    opinion = ""
    if config.push_style == "natural" and llm_caller is not None:
        text = ""
        try:
            result = await llm_caller(
                build_push_message_prompt(request, source_group_name)
            )
            text = "" if result is None else str(result).strip()
        except Exception:
            text = ""
        if text:
            return {"style": "natural", "text": text, "opinion_source": "llm"}
        style = "natural_fallback_formatted"
    else:
        opinion = await _llm_opinion(request, source_group_name, llm_caller)
    if opinion:
        opinion_source = "llm"
    elif decision is not None:
        opinion_source = "decision"
    else:
        opinion_source = "none"
    return {
        "style": style,
        "text": _formatted_message(
            request, config, source_group_name, decision, opinion
        ),
        "opinion_source": opinion_source,
    }


class RequestPushService:
    def __init__(self, store: JoinReviewStore, onebot: OneBotClient) -> None:
        self.store = store
        self.onebot = onebot

    async def render_message(
        self,
        request: JoinRequest,
        config: GroupReviewConfig,
        source_group_name: str,
        llm_caller: Any,
        decision: Any = None,
    ) -> str:
        """渲染推送文案；natural 走 LLM，空结果或异常回退格式化模板。"""
        preview = await render_push_preview(
            request, config, source_group_name, llm_caller, decision
        )
        return preview["text"]

    async def push_for_request(
        self,
        context: Any,
        request: JoinRequest,
        config: GroupReviewConfig,
        llm_caller: Any,
        logger: Any,
        decision: Any = None,
    ) -> tuple[list[str], list[str], list[str]]:
        """逐群推送申请文案，返回 (成功群列表, 已推过跳过列表, 失败原因列表)。

        目标群取自按群配置 push_group_ids；为空时回退到申请所属群本身
        （与 ``resolve_push_targets`` 同一解析，供通知去重共用）。
        每个目标群先 claim 再发送，单个群失败不中断其余群；
        失败释放占位，事件重复投递时可重试。发送成功后记录推送消息映射
        （push_refs），供群内引用回复审批定位申请。
        """
        target_group_ids = resolve_push_targets(request, config)

        bot = get_aiocqhttp_bot(context, request.platform_id)
        if bot is None:
            return [], [], ["平台不可用，无法推送"]
        # bot 仅用于解析来源群名，取不到不影响推送。
        source_group_name = await self._source_group_name(bot, request)

        message: str | None = None
        sent: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for gid in target_group_ids:
            claim = await self.store.claim_notification(
                request.request_id, f"push:{gid}"
            )
            if claim is None:
                skipped.append(gid)
                continue
            try:
                if message is None:
                    message = await self.render_message(
                        request, config, source_group_name, llm_caller, decision
                    )
                ok, message_id, err = await self.onebot.send_group_message_with_id(
                    bot, int(gid), message
                )
                if ok and message_id:
                    await self.store.record_push_ref(
                        request.request_id, gid, message_id
                    )
                await self.store.finish_notification(claim, succeeded=ok)
            except asyncio.CancelledError:
                await asyncio.shield(self.store.release_notification(claim))
                raise
            except Exception as exc:
                await asyncio.shield(self.store.release_notification(claim))
                failed.append(f"{gid}：{type(exc).__name__}: {exc}")
                if logger is not None:
                    logger.debug("[idg] push join request to %s failed: %s", gid, exc)
                continue
            if ok:
                sent.append(gid)
            else:
                failed.append(f"{gid}：{err or '发送失败'}")
        return sent, skipped, failed

    async def _source_group_name(self, bot: Any, request: JoinRequest) -> str:
        try:
            info = await self.onebot.get_group_info_for_bot(
                bot, int(request.group_id), no_cache=False
            )
        except Exception:
            return "未知群名"
        name = str((info or {}).get("group_name", "")).strip()
        return name or "未知群名"


__all__ = [
    "PUSH_REPLY_HINT",
    "RequestPushService",
    "build_opinion_line",
    "render_push_preview",
    "resolve_push_targets",
]
