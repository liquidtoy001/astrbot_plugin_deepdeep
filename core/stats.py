"""按天统计调用次数和预估费用，保存在插件数据目录。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .log import logger
from .pricing import CURRENCIES, DEFAULT_CURRENCY

KINDS = ("search", "fetch")

COUNTERS = ("billable", "empty", "failed", "cache_hit")
KEEP_DAYS = 62  # 足够覆盖“本月”和 WebUI 上的 30 天图表


class _JsonFile:
    """整个替换地保存 JSON。在事件循环里不等磁盘：交给一个线程，连着保存时只写最新的，顺序不会乱。"""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._worker: threading.Thread | None = None

    def save(self, data: Any) -> None:
        if self._path is None:
            return
        text = json.dumps(data, ensure_ascii=False)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            self._write(text)
            return
        with self._lock:
            self._pending = text
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, name="deepdeep-stats", daemon=True
                )
                self._worker.start()

    def flush(self) -> None:
        """等还没写完的内容落盘。"""
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join()

    def _run(self) -> None:
        while True:
            with self._lock:
                text, self._pending = self._pending, None
                if text is None:
                    self._worker = None
                    return
            self._write(text)

    def _write(self, text: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning(f"[deepdeep] 保存统计文件失败: {exc}")


class UsageStats:
    def __init__(
        self,
        path: Path | None,
        *,
        today: Callable[[], date] = date.today,
        price: float = CURRENCIES[DEFAULT_CURRENCY].price,
    ) -> None:
        self._path = path
        self._file = _JsonFile(path)
        # 每次成功请求的单价（计费币种），切换币种时由插件更新；统计文件只存次数
        self.price = price
        self._today = today
        self._days: dict[str, dict[str, dict[str, int]]] = {}
        self._load()

    def record(self, kind: str, outcome: str) -> None:
        """kind: search / fetch；outcome: COUNTERS 之一。"""
        if outcome not in COUNTERS:
            raise ValueError(f"unknown outcome: {outcome}")
        day = self._days.setdefault(self._today().isoformat(), {})
        bucket = day.setdefault(kind, dict.fromkeys(COUNTERS, 0))
        bucket[outcome] = bucket.get(outcome, 0) + 1
        self._prune()
        self._save()

    def day(self, which: date | None = None) -> dict[str, dict[str, int]]:
        key = (which or self._today()).isoformat()
        return {
            kind: {c: self._days.get(key, {}).get(kind, {}).get(c, 0) for c in COUNTERS}
            for kind in KINDS
        }

    def cost(self, days: int = 1) -> float:
        """最近 ``days`` 天（含今天）的预估费用，美元。"""
        today = self._today()
        total = 0.0
        for offset in range(days):
            stats = self.day(today - timedelta(days=offset))
            for kind in KINDS:
                total += stats[kind]["billable"] * self.price
        return total

    def month_cost(self) -> float:
        """本自然月（含今天）的预估费用，美元。"""
        return self.cost(self._today().day)

    def series(self, days: int = 30) -> list[dict]:
        """最近 ``days`` 天的逐日数据，按日期升序，给 WebUI 画图用。"""
        today = self._today()
        out = []
        for offset in range(days - 1, -1, -1):
            d = today - timedelta(days=offset)
            stats = self.day(d)
            out.append(
                {
                    "date": d.isoformat(),
                    **stats,
                    "cost": sum(stats[k]["billable"] * self.price for k in KINDS),
                }
            )
        return out

    def billable(self, days: int = 1) -> int:
        today = self._today()
        return sum(
            self.day(today - timedelta(days=offset))[kind]["billable"]
            for offset in range(days)
            for kind in KINDS
        )

    def _prune(self) -> None:
        cutoff = (self._today() - timedelta(days=KEEP_DAYS)).isoformat()
        for key in [k for k in self._days if k < cutoff]:
            del self._days[key]

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._days = data
        except (OSError, ValueError) as exc:
            logger.warning(f"[deepdeep] 读取统计文件失败，将重新计数: {exc}")

    def _save(self) -> None:
        self._file.save(self._days)

    def flush(self) -> None:
        self._file.flush()


KEEP_MONTHS = 3


class MonthlyCounter:
    """按自然月、按名称计数（比如 Tavily 或其他搜索工具每月成功调用的次数），保存在插件数据目录。

    文件格式：{"2026-10": {"tavily": 123, "web_search_bocha": 4}, ...}，只保留最近 KEEP_MONTHS 个月。
    """

    def __init__(
        self, path: Path | None, *, today: Callable[[], date] = date.today
    ) -> None:
        self._path = path
        self._file = _JsonFile(path)
        self._today = today
        self._months: dict[str, dict[str, int]] = {}
        self._load()

    def _month(self) -> str:
        return self._today().strftime("%Y-%m")

    def count(self, key: str = "total") -> int:
        """本月已记录的次数，跨月后自动从 0 开始。"""
        return self._months.get(self._month(), {}).get(key, 0)

    def counts(self) -> dict[str, int]:
        """本月各名称的次数。"""
        return dict(self._months.get(self._month(), {}))

    def add(self, key: str = "total", n: int = 1) -> int:
        month = self._months.setdefault(self._month(), {})
        month[key] = month.get(key, 0) + n
        for old in sorted(self._months)[:-KEEP_MONTHS]:
            del self._months[old]
        self._save()
        return month[key]

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[deepdeep] 读取月度计数文件失败，将重新计数: {exc}")
            return
        if not isinstance(data, dict):
            return
        for month, values in data.items():
            if not isinstance(values, dict):
                continue
            self._months[str(month)] = {
                str(k): v
                for k, v in values.items()
                if isinstance(v, int) and not isinstance(v, bool)
            }

    def _save(self) -> None:
        self._file.save(self._months)

    def flush(self) -> None:
        self._file.flush()
