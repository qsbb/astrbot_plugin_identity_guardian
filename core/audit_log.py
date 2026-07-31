"""审计日志：按日期分文件，JSONL 格式。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from ..series_diagnostics import logger
except ImportError:  # 兼容旧测试直接把 core 当作顶层包导入
    from series_diagnostics import logger

from .models import ActionDecision, ActorContext, now_iso


class AuditLogger:
    """审计日志记录器。"""

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self) -> Path:
        """按日期分文件。"""
        date_str = datetime.now().strftime("%Y%m%d")
        return self._dir / f"audit-{date_str}.jsonl"

    def write(
        self,
        actor: ActorContext,
        action: str,
        target_user: str,
        params: dict[str, Any] | None = None,
        llm_summary: str = "",
        result: str = "",
        error: str | None = None,
    ) -> None:
        """写入审计日志。"""
        entry = {
            "ts": now_iso(),
            "platform_id": actor.platform_id,
            "group_id": actor.group_id,
            "bot_role": actor.bot_role,
            "requester_id": actor.requester_id,
            "requester_role": actor.requester_role,
            "requester_relation": actor.requester_relation,
            "action": action,
            "target_user": target_user,
            "params": params or {},
            "llm_summary": llm_summary,
            "result": result,
            "error": error,
        }
        try:
            with self._file_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[idg] audit log write failed: %s", exc)

    def write_from_decision(
        self,
        actor: ActorContext,
        decision: ActionDecision,
        success: bool,
        error: str = "",
    ) -> None:
        """从决策结果写入审计日志。"""
        self.write(
            actor=actor,
            action=decision.action,
            target_user=actor.target_id or actor.requester_id,
            params=decision.params,
            result="success" if success else "failed",
            error=error or None,
        )

    def read_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取最近的审计日志。"""
        entries: list[dict[str, Any]] = []
        files = sorted(self._dir.glob("audit-*.jsonl"), reverse=True)
        for f in files:
            if len(entries) >= limit:
                break
            try:
                with f.open("r", encoding="utf-8") as fh:
                    lines = fh.readlines()
                for line in reversed(lines):
                    if len(entries) >= limit:
                        break
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except Exception:
                continue
        return entries
