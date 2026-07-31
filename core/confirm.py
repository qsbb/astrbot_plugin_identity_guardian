"""二次确认服务：高风险操作需人工批准。"""

from __future__ import annotations

import uuid
from typing import Any

try:
    from ..series_diagnostics import logger
except ImportError:  # 兼容旧测试直接把 core 当作顶层包导入
    from series_diagnostics import logger

from .models import ConfirmEntry


class ConfirmService:
    """待确认操作管理。"""

    def __init__(self) -> None:
        self._pending: dict[str, ConfirmEntry] = {}

    def create(
        self,
        action: str,
        params: dict[str, Any],
        group_id: str,
        target_user: str,
    ) -> str:
        """创建待确认条目，返回 confirm_id。"""
        confirm_id = uuid.uuid4().hex[:8]
        import time

        entry = ConfirmEntry(
            confirm_id=confirm_id,
            action=action,
            params=params,
            group_id=group_id,
            target_user=target_user,
            created_at=time.time(),
            status="pending",
        )
        self._pending[confirm_id] = entry
        logger.info(
            "[idg] confirmation created: id=%s action=%s target=%s",
            confirm_id,
            action,
            target_user,
        )
        return confirm_id

    def approve(self, confirm_id: str) -> ConfirmEntry | None:
        """批准待确认操作。"""
        entry = self._pending.get(confirm_id)
        if entry is None:
            return None
        entry.status = "approved"
        del self._pending[confirm_id]
        return entry

    def reject(self, confirm_id: str) -> ConfirmEntry | None:
        """拒绝待确认操作。"""
        entry = self._pending.get(confirm_id)
        if entry is None:
            return None
        entry.status = "rejected"
        del self._pending[confirm_id]
        return entry

    def get(self, confirm_id: str) -> ConfirmEntry | None:
        """获取待确认条目。"""
        return self._pending.get(confirm_id)

    def list_pending(self) -> list[ConfirmEntry]:
        """列出所有待确认条目。"""
        return list(self._pending.values())

    def cleanup_expired(self, ttl_seconds: int = 300) -> int:
        """清理过期条目，返回清理数量。"""
        import time

        now = time.time()
        expired = [
            cid
            for cid, entry in self._pending.items()
            if now - entry.created_at > ttl_seconds
        ]
        for cid in expired:
            del self._pending[cid]
        return len(expired)

    def clear(self) -> None:
        """清空所有待确认条目。"""
        self._pending.clear()
