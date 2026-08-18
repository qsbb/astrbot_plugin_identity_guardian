"""入群审核服务。

仅高置信度正确时自动通过；
错误、不确定、解析失败或知识不足时不处理，保留 QQ 待审状态。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

try:
    from ..series_diagnostics import logger
except ImportError:  # 兼容旧测试直接把 core 当作顶层包导入
    from series_diagnostics import logger

from .config import Config
from .knowledge import KnowledgeService
from .models import JoinDecision, JoinVerdict
from .onebot import OneBotClient
from .prompts import build_answer_judge_prompt

MAX_JOIN_TEXT_LENGTH = 2048
MAX_JOIN_FLAG_LENGTH = 4096


def _bounded_join_text(value: Any, maximum: int) -> str:
    """Bound event-controlled text before it reaches prompts or logs."""
    if value is None:
        return ""
    return str(value).strip()[:maximum]


@dataclass(frozen=True, slots=True)
class AutoAuditResult:
    """Outcome of automatic auditing, including the platform side effect."""

    decision: JoinDecision
    approval_attempted: bool = False
    platform_approved: bool = False
    platform_error: str = ""


class JoinAuditService:
    """入群申请审核。"""

    def __init__(
        self,
        config: Config,
        onebot: OneBotClient,
        knowledge: KnowledgeService,
        llm_caller: Any = None,
    ) -> None:
        self.config = config
        self.onebot = onebot
        self.knowledge = knowledge
        self._llm_caller = llm_caller

    def parse_request(self, raw: dict[str, Any]) -> tuple[str, str, str, str, str]:
        """解析入群请求事件。

        Returns:
            (flag, sub_type, user_id, group_id, comment)
        """
        flag = _bounded_join_text(raw.get("flag", ""), MAX_JOIN_FLAG_LENGTH)
        sub_type = _bounded_join_text(raw.get("sub_type", "add"), 32)
        user_id = str(raw.get("user_id", ""))
        group_id = str(raw.get("group_id", ""))

        comment = _bounded_join_text(raw.get("comment", ""), MAX_JOIN_TEXT_LENGTH)

        # 尝试从 comment 中提取答案
        # QQ 入群附言通常格式: "问题：xxx\n答案：yyy" 或纯答案
        answer = comment

        # 匹配 "答案：yyy" 格式
        a_match = re.search(r"答案[：:]\s*(.+?)(?:\n|$)", comment)
        if a_match:
            answer = a_match.group(1).strip()

        return flag, sub_type, user_id, group_id, answer

    def extract_question(self, comment: str) -> str:
        """从 comment 中提取问题文本。"""
        comment = _bounded_join_text(comment, MAX_JOIN_TEXT_LENGTH)
        q_match = re.search(r"问题[：:]\s*(.+?)(?:\n|$)", comment)
        return (
            _bounded_join_text(q_match.group(1), MAX_JOIN_TEXT_LENGTH)
            if q_match
            else ""
        )

    async def judge_answer(
        self,
        question: str,
        answer: str,
        configured_questions: list[Any],
        evidence: list[Any] | None = None,
    ) -> JoinDecision:
        """判断入群答案是否正确。

        流程：
        1. 先尝试与配置的问答进行精确/模糊匹配
        2. 如果配置了 LLM，用 LLM 语义判断
        3. 不确定时返回 uncertain
        """
        if not answer:
            return JoinDecision(
                verdict=JoinVerdict.UNCERTAIN.value,
                confidence=0.0,
                reason="答案为空",
            )

        # 1. 尝试与配置问答匹配
        match_result = self._match_configured(question, answer, configured_questions)
        if match_result.verdict == JoinVerdict.CORRECT.value:
            return match_result

        # 2. 使用 LLM 语义判断
        if self._llm_caller is not None:
            evidence_text = ""
            if evidence:
                evidence_text = "; ".join(
                    str(getattr(e, "content", e)) if not isinstance(e, str) else e
                    for e in evidence[:5]
                )
            reference_answers: list[str] = []
            for q in configured_questions:
                if isinstance(q, dict):
                    q_text = str(q.get("question", ""))
                    if question and q_text and question in q_text:
                        reference_answers = [str(a) for a in q.get("answers", [])]
                        break
                    if not question:
                        reference_answers.extend(str(a) for a in q.get("answers", []))

            prompt = build_answer_judge_prompt(
                question=question,
                answer=answer,
                reference_answers=reference_answers,
                evidence=evidence_text,
            )
            try:
                llm_result = await self._llm_caller(prompt)
                return self._parse_llm_judgment(llm_result)
            except Exception as exc:
                logger.warning("[idg] join audit LLM failed: %s", type(exc).__name__)
                return JoinDecision(
                    verdict=JoinVerdict.UNCERTAIN.value,
                    confidence=0.0,
                    reason="LLM 判断失败",
                )

        # 3. 无法判断
        return JoinDecision(
            verdict=JoinVerdict.UNCERTAIN.value,
            confidence=0.3,
            reason="无配置参考答案且无 LLM 可用",
        )

    def _match_configured(
        self,
        question: str,
        answer: str,
        configured_questions: list[Any],
    ) -> JoinDecision:
        """与配置的问答进行精确/模糊匹配。"""
        answer_lower = answer.strip().lower()

        for q in configured_questions:
            if not isinstance(q, dict):
                continue
            q_text = str(q.get("question", ""))
            answers = q.get("answers", [])

            # 如果配置了问题，但请求中的问题不匹配，跳过
            if (
                q_text
                and question
                and q_text not in question
                and question not in q_text
            ):
                continue

            for ref_answer in answers:
                ref_lower = str(ref_answer).strip().lower()
                if not ref_lower:
                    continue
                # 精确匹配
                if answer_lower == ref_lower:
                    return JoinDecision(
                        verdict=JoinVerdict.CORRECT.value,
                        confidence=0.95,
                        reason="精确匹配参考答案",
                    )
                # 包含匹配
                if ref_lower in answer_lower or answer_lower in ref_lower:
                    return JoinDecision(
                        verdict=JoinVerdict.CORRECT.value,
                        confidence=0.85,
                        reason="模糊匹配参考答案",
                    )

        return JoinDecision(
            verdict=JoinVerdict.UNCERTAIN.value,
            confidence=0.2,
            reason="未匹配到配置参考答案",
        )

    def _parse_llm_judgment(self, response: str) -> JoinDecision:
        """解析 LLM 返回的 JSON 判断结果。"""
        try:
            # 尝试提取 JSON
            json_match = re.search(r"\{[^}]+\}", response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response)

            verdict = str(data.get("verdict", "uncertain")).lower()
            confidence = float(data.get("confidence", 0.0))
            reason = str(data.get("reason", ""))

            return JoinDecision(
                verdict=verdict,
                confidence=confidence,
                reason=reason,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[idg] failed to parse LLM judgment: %s", exc)
            return JoinDecision(
                verdict=JoinVerdict.UNCERTAIN.value,
                confidence=0.0,
                reason="LLM 响应解析失败",
            )

    def should_auto_approve(self, decision: JoinDecision) -> bool:
        """仅 ``approve_only`` 模式允许高置信度正确答案自动通过。"""
        return self.config.join_audit_mode == "approve_only" and self.is_approvable(
            decision
        )

    def is_approvable(self, decision: JoinDecision) -> bool:
        """Return whether a decision meets the configured approval threshold.

        Unlike :meth:`should_auto_approve`, this predicate is independent of
        the legacy global mode and is therefore suitable for per-group review.
        """
        return (
            decision.verdict == JoinVerdict.CORRECT.value
            and decision.confidence >= self.config.join_approve_threshold
        )

    async def _judge_against_presets(
        self,
        question: str,
        answer: str,
        presets: list[Any],
    ) -> JoinDecision | None:
        """LLM 语义比对「申请人答案 vs 适用预设答案」。

        预设项 question 为空时适用于任意问题；非空时与事件问题做包含式
        模糊匹配（与 ``_match_configured`` 同思路）。无 LLM、无适用预设
        答案或调用失败时返回 None，让调用方进入下一段判定。
        """
        if self._llm_caller is None:
            return None
        reference_answers: list[str] = []
        for q in presets:
            if not isinstance(q, dict):
                continue
            q_text = str(q.get("question", "")).strip()
            if (
                q_text
                and question
                and q_text not in question
                and question not in q_text
            ):
                continue
            reference_answers.extend(
                str(a).strip() for a in q.get("answers", []) if str(a).strip()
            )
        if not reference_answers:
            return None
        prompt = build_answer_judge_prompt(
            question=question,
            answer=answer,
            reference_answers=reference_answers,
            evidence="",
        )
        try:
            return self._parse_llm_judgment(await self._llm_caller(prompt))
        except Exception as exc:
            logger.warning("[idg] join audit preset LLM failed: %s", type(exc).__name__)
            return None

    async def _judge_with_knowledge(
        self, question: str, answer: str, group_id: str
    ) -> JoinDecision | None:
        """知联动判定：有证据才调 LLM；无证据/联动关闭/查询失败返回 None。"""
        if not self.config.enable_active_learner_recall or self._llm_caller is None:
            return None
        try:
            evidence = await self.knowledge.recall_safe(
                query=f"{question}\n{answer}", scope=group_id
            )
        except Exception as exc:
            logger.warning(
                "[idg] join audit knowledge recall failed: %s", type(exc).__name__
            )
            return None
        if not evidence:
            return None
        evidence_text = "; ".join(
            str(getattr(e, "content", e)) if not isinstance(e, str) else e
            for e in evidence[:5]
        )
        prompt = build_answer_judge_prompt(
            question=question,
            answer=answer,
            reference_answers=[],
            evidence=evidence_text,
        )
        try:
            return self._parse_llm_judgment(await self._llm_caller(prompt))
        except Exception as exc:
            logger.warning(
                "[idg] join audit knowledge LLM failed: %s", type(exc).__name__
            )
            return None

    async def execute_auto_audit(
        self,
        event: Any,
        raw: dict[str, Any],
        configured_questions: list[Any] | None = None,
    ) -> AutoAuditResult:
        """Judge and, when eligible, attempt approval without ever rejecting.

        严格三段顺序：
        1. 预设判定：``configured_questions``（按群预设，None 回退全局
           ``join_questions``）先精确/模糊匹配，不中再用 LLM 对预设答案做
           语义比对；该群与全局都无预设则直接进 2。
        2. 知联动判定：有知识证据才调 LLM 带证据判断。
        3. 兜底：都无高置信结果时返回 UNCERTAIN，不再让 LLM 在无参考
           答案、无知识证据的情况下自由判断。
        """
        flag, sub_type, user_id, group_id, answer = self.parse_request(raw)
        question = self.extract_question(str(raw.get("comment", "")))

        if not answer:
            return AutoAuditResult(
                decision=JoinDecision(
                    verdict=JoinVerdict.UNCERTAIN.value,
                    confidence=0.0,
                    reason="答案为空",
                )
            )

        presets = (
            list(configured_questions)
            if configured_questions is not None
            else self.config.join_questions
        )

        # 1. 预设判定
        decision: JoinDecision | None = None
        if presets:
            matched = self._match_configured(question, answer, presets)
            if matched.verdict == JoinVerdict.CORRECT.value:
                decision = matched
            else:
                decision = await self._judge_against_presets(question, answer, presets)

        # 2. 知联动判定
        if decision is None or not self.is_approvable(decision):
            knowledge_decision = await self._judge_with_knowledge(
                question, answer, group_id
            )
            if knowledge_decision is not None and self.is_approvable(
                knowledge_decision
            ):
                decision = knowledge_decision

        # 3. 兜底：无高置信结果，转忽略/人工
        if decision is None or not self.is_approvable(decision):
            decision = JoinDecision(
                verdict=JoinVerdict.UNCERTAIN.value,
                confidence=0.2,
                reason="未匹配预设答案且无知识证据"
                if presets
                else "无预设答案且无知识证据",
            )
            return AutoAuditResult(decision=decision)

        ok, err = await self.onebot.set_group_add_request(
            event, flag, sub_type, approve=True, reason=""
        )
        if ok:
            logger.info(
                "[idg] join request approved: user=%s group=%s confidence=%.2f",
                user_id,
                group_id,
                decision.confidence,
            )
            return AutoAuditResult(
                decision=decision,
                approval_attempted=True,
                platform_approved=True,
            )
        logger.warning("[idg] join request approve failed: %s", err)
        return AutoAuditResult(
            decision=decision,
            approval_attempted=True,
            platform_approved=False,
            platform_error=err or "platform_approval_failed",
        )

    async def handle_request(
        self,
        event: Any,
        raw: dict[str, Any],
    ) -> JoinDecision:
        """处理入群申请事件。

        仅高置信度正确时自动通过，其他情况不处理。
        """
        _, _, user_id, group_id, answer = self.parse_request(raw)
        question = self.extract_question(str(raw.get("comment", "")))
        if self.config.join_audit_mode == "approve_only":
            result = await self.execute_auto_audit(event, raw)
            decision = result.decision
        else:
            decision = await self.judge_answer(
                question=question,
                answer=answer,
                configured_questions=self.config.join_questions,
            )

        if not self.should_auto_approve(decision):
            logger.info(
                "[idg] join request not processed (verdict=%s confidence=%.2f): "
                "user=%s group=%s — left for manual review",
                decision.verdict,
                decision.confidence,
                user_id,
                group_id,
            )

        return decision


__all__ = ["AutoAuditResult", "JoinAuditService"]
