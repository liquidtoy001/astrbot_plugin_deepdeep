"""带过期时间的小型 LRU 缓存。群聊里多人问同一件事时只计费一次。"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 256,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max(1, max_entries)
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: Hashable) -> V | None:
        if not self.enabled:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Hashable, value: V) -> None:
        if not self.enabled:
            return
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
