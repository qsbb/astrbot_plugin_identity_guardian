"""冷却与防刷屏服务。"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from astrbot.api import logger

from .config import Config


class CooldownService:
    """操作冷却与刷屏检测。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        # 操作冷却: (group_id, user_id, action) -> timestamp
        self._action_cooldowns: dict[tuple[str, str, str], float] = {}
        # 刷屏检测: (group_id, user_id) -> deque[timestamps]
        self._spam_tracker: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        # 熔断: 1小时内的操作时间戳列表
        self._action_history: deque[float] = deque()
        self._breaker_tripped = False

    def mark_action(self, group_id: str, user_id: str, action: str) -> None:
        """记录一次管理操作。"""
        now = time.time()
        key = (group_id, user_id, action)
        self._action_cooldowns[key] = now
        self._action_history.append(now)
        self._cleanup_history()

    def is_on_cooldown(self, group_id: str, user_id: str, action: str) -> bool:
        """检查是否在冷却期内。"""
        key = (group_id, user_id, action)
        ts = self._action_cooldowns.get(key)
        if ts is None:
            return False
        return (time.time() - ts) < self.config.action_cooldown_seconds

    def record_message(self, group_id: str, user_id: str) -> int:
        """记录一条消息并返回 10 秒内的消息数。

        用于刷屏检测。
        """
        now = time.time()
        key = (group_id, user_id)
        tracker = self._spam_tracker[key]
        tracker.append(now)
        # 清理 10 秒前的记录
        cutoff = now - 10
        while tracker and tracker[0] < cutoff:
            tracker.popleft()
        return len(tracker)

    def is_spamming(self, group_id: str, user_id: str) -> bool:
        """判断是否刷屏。"""
        threshold = self.config.spam_threshold
        if threshold <= 0:
            return False
        count = self.record_message(group_id, user_id)
        return count > threshold

    def check_breaker(self) -> bool:
        """检查熔断器是否触发。返回 True 表示已熔断。"""
        if self._breaker_tripped:
            return True
        self._cleanup_history()
        return len(self._action_history) >= self.config.circuit_breaker_threshold

    def trip_breaker(self) -> None:
        """手动触发熔断。"""
        self._breaker_tripped = True
        logger.warning("[idg] circuit breaker tripped")

    def reset_breaker(self) -> None:
        """重置熔断器。"""
        self._breaker_tripped = False
        self._action_history.clear()
        logger.info("[idg] circuit breaker reset")

    def _cleanup_history(self) -> None:
        """清理 1 小时前的历史记录。"""
        cutoff = time.time() - 3600
        while self._action_history and self._action_history[0] < cutoff:
            self._action_history.popleft()

    def clear(self) -> None:
        """清空所有状态。"""
        self._action_cooldowns.clear()
        self._spam_tracker.clear()
        self._action_history.clear()
        self._breaker_tripped = False

    def stats(self) -> dict[str, Any]:
        """返回当前状态统计。"""
        self._cleanup_history()
        return {
            "action_cooldowns": len(self._action_cooldowns),
            "spam_trackers": len(self._spam_tracker),
            "hourly_actions": len(self._action_history),
            "breaker_tripped": self._breaker_tripped,
            "breaker_threshold": self.config.circuit_breaker_threshold,
        }
