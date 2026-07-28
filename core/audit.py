"""入群审核服务。

仅高置信度正确时自动通过；
错误、不确定、解析失败或知识不足时不处理，保留 QQ 待审状态。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

from .config import Config
from .knowledge import KnowledgeService
from .models import JoinDecision, JoinVerdict
from .onebot import OneBotClient
from .prompts import build_answer_judge_prompt


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
        flag = str(raw.get("flag", ""))
        sub_type = str(raw.get("sub_type", "add"))
        user_id = str(raw.get("user_id", ""))
        group_id = str(raw.get("group_id", ""))

        comment = str(raw.get("comment", ""))

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
        q_match = re.search(r"问题[：:]\s*(.+?)(?:\n|$)", comment)
        return q_match.group(1).strip() if q_match else ""

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
                logger.warning("[idg] join audit LLM failed: %s", exc)
                return JoinDecision(
                    verdict=JoinVerdict.UNCERTAIN.value,
                    confidence=0.0,
                    reason=f"LLM 判断失败: {exc}",
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
        return (
            self.config.join_audit_mode == "approve_only"
            and decision.verdict == JoinVerdict.CORRECT.value
            and decision.confidence >= self.config.join_approve_threshold
        )

    async def handle_request(
        self,
        event: Any,
        raw: dict[str, Any],
    ) -> JoinDecision:
        """处理入群申请事件。

        仅高置信度正确时自动通过，其他情况不处理。
        """
        flag, sub_type, user_id, group_id, answer = self.parse_request(raw)
        question = self.extract_question(str(raw.get("comment", "")))

        # 获取知识库证据
        evidence: list[Any] = []
        if self.config.enable_active_learner_recall:
            evidence = await self.knowledge.recall_safe(
                query=f"{question}\n{answer}", scope=group_id
            )

        decision = await self.judge_answer(
            question=question,
            answer=answer,
            configured_questions=self.config.join_questions,
            evidence=evidence,
        )

        # notify_only 只做判断和通知，绝不能触发 OneBot 放行接口。
        if self.should_auto_approve(decision):
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
            else:
                logger.warning("[idg] join request approve failed: %s", err)
        else:
            logger.info(
                "[idg] join request not processed (verdict=%s confidence=%.2f): "
                "user=%s group=%s — left for manual review",
                decision.verdict,
                decision.confidence,
                user_id,
                group_id,
            )

        return decision
