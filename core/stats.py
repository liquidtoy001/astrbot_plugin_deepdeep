"""按天统计调用次数和预估费用，保存在插件数据目录。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from .log import logger
from .pricing import CURRENCIES, DEFAULT_CURRENCY

KINDS = ("search", "fetch")

COUNTERS = ("billable", "empty", "failed", "cache_hit")
KEEP_DAYS = 62  # 足够覆盖“本月”和 WebUI 上的 30 天图表


class UsageStats:
    def __init__(
        self,
        path: Path | None,
        *,
        today: Callable[[], date] = date.today,
        price: float = CURRENCIES[DEFAULT_CURRENCY].price,
    ) -> None:
        self._path = path
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
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._days, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning(f"[deepdeep] 保存统计文件失败: {exc}")
