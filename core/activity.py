"""最近调用记录，只保存在内存里，重启即清空。"""

from __future__ import annotations

import copy
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

# 属于用户内容的字段：关闭“记录搜索词”时从详情里去掉
PRIVATE_KEYS = frozenset({"query", "url", "ori_question", "results", "title"})


def _strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


@dataclass(slots=True)
class ActivityEntry:
    ts: float
    kind: str  # search / fetch
    target: str  # 搜索词或链接（关闭记录时为空）
    outcome: str  # billable / empty / cache_hit / failed / blocked
    request_id: str = ""  # 与工具结果第一行、日志中的 #xxxxxx 对应
    status: int = 0  # HTTP 状态码，0 表示没有发请求或网络错误
    latency_ms: int = 0
    results: int = 0
    note: str = ""  # 失败原因 / 是否降级等
    # 请求参数、每次尝试、返回摘要、错误、路由决策等，供面板展开查看
    detail: dict[str, Any] = field(default_factory=dict)


class ActivityLog:
    def __init__(
        self,
        max_entries: int = 100,
        *,
        record_targets: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._entries: deque[ActivityEntry] = deque(maxlen=max_entries)
        self.record_targets = record_targets
        self._clock = clock

    def add(
        self,
        kind: str,
        target: str,
        outcome: str,
        *,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> ActivityEntry:
        detail = copy.deepcopy(detail or {})
        if not self.record_targets:
            detail = _strip_private(detail)
        entry = ActivityEntry(
            ts=self._clock(),
            kind=kind,
            target=target[:200] if self.record_targets else "",
            outcome=outcome,
            detail=detail,
            **fields,
        )
        self._entries.append(entry)
        return entry

    def annotate(self, request_id: str, **detail: Any) -> bool:
        """给已有记录补充信息（例如“已放出备用工具”）。找不到返回 False。"""
        if not request_id:
            return False
        for entry in reversed(self._entries):
            if entry.request_id == request_id:
                entry.detail.update(detail)
                return True
        return False

    def find(self, request_id: str) -> dict | None:
        for entry in reversed(self._entries):
            if entry.request_id == request_id:
                return asdict(entry)
        return None

    def recent(self, limit: int = 50) -> list[dict]:
        items = list(self._entries)[-max(0, limit) :]
        return [asdict(e) for e in reversed(items)]

    def clear(self) -> None:
        self._entries.clear()
