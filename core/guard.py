"""费用上限和熔断：决定当前还能不能调用 DeepSeek。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .pricing import Currency, format_amount, get_currency
from .stats import UsageStats

# 这些状态码说明账号或配置有问题，短时间内重试也没用，直接熔断更久
HARD_FAILURE_STATUSES = frozenset({401, 402})
# 这些状态码只和本次请求的内容有关（查询写错、某个网页抓不到），不计入熔断
REQUEST_SPECIFIC_STATUSES = frozenset({400, 403, 404, 413})


@dataclass(slots=True)
class Availability:
    ok: bool
    # budget_daily / budget_monthly / circuit_open / no_key / paused
    code: str = ""
    message: str = ""


class BudgetGuard:
    """日 / 月费用上限。0 表示不限制。

    上限和花费都按计费币种（currency）计算。调用前检查“已花费 + 本次单价”是否超出上限；
    并发请求可能让实际花费略超上限（最多超出同时在途的请求数 × 单价，不到一分钱）。
    """

    def __init__(
        self,
        stats: UsageStats,
        *,
        daily_limit: float = 0.0,
        monthly_limit: float = 0.0,
        currency: Currency | None = None,
        warn_ratio: float = 0.8,
    ) -> None:
        self.stats = stats
        self.daily_limit = max(0.0, daily_limit)
        self.monthly_limit = max(0.0, monthly_limit)
        self.currency = currency or get_currency(None)
        self.warn_ratio = warn_ratio

    def check(self, kind: str) -> Availability:
        price = self.stats.price
        if (
            self.daily_limit > 0
            and self.stats.cost(1) + price > self.daily_limit + 1e-12
        ):
            return Availability(
                False,
                "budget_daily",
                f"已达到今日费用上限 {format_amount(self.daily_limit, self.currency)}",
            )
        if (
            self.monthly_limit > 0
            and self.stats.month_cost() + price > self.monthly_limit + 1e-12
        ):
            return Availability(
                False,
                "budget_monthly",
                f"已达到本月费用上限 {format_amount(self.monthly_limit, self.currency)}",
            )
        return Availability(True)

    def snapshot(self) -> dict:
        today, month = self.stats.cost(1), self.stats.month_cost()

        def _ratio(spent: float, limit: float) -> float | None:
            return None if limit <= 0 else spent / limit

        daily_ratio = _ratio(today, self.daily_limit)
        monthly_ratio = _ratio(month, self.monthly_limit)
        return {
            "currency": self.currency.code,
            "symbol": self.currency.symbol,
            "price_per_1000": round(self.stats.price * 1000, 4),
            "today_cost": today,
            "month_cost": month,
            "daily_limit": self.daily_limit,
            "monthly_limit": self.monthly_limit,
            "daily_ratio": daily_ratio,
            "monthly_ratio": monthly_ratio,
            "warning": any(
                r is not None and r >= self.warn_ratio
                for r in (daily_ratio, monthly_ratio)
            ),
        }


class CircuitBreaker:
    """连续失败达到阈值后暂停使用 DeepSeek 一段时间，到期后放行一次试探请求。"""

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_seconds: float = 300,
        hard_cooldown_seconds: float = 1800,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = max(1.0, cooldown_seconds)
        self.hard_cooldown = max(self.cooldown, hard_cooldown_seconds)
        self._clock = clock
        self._wall = wall_clock
        self.consecutive_failures = 0
        self._open_until: float | None = None
        self._open_until_wall: float | None = None
        self.last_error = ""
        self.last_status = 0

    @property
    def is_open(self) -> bool:
        if self._open_until is None:
            return False
        if self._clock() >= self._open_until:
            # 冷却结束进入半开：放行请求，成功则关闭，失败则重新打开
            self._open_until = None
            self._open_until_wall = None
            return False
        return True

    def check(self) -> Availability:
        if self.is_open:
            remaining = int(self._open_until - self._clock()) if self._open_until else 0
            return Availability(
                False,
                "circuit_open",
                f"DeepSeek 连续调用失败（{self.last_error}），约 {max(1, remaining // 60)} 分钟后自动重试",
            )
        return Availability(True)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self._open_until = None
        self._open_until_wall = None

    def record_failure(self, status: int, message: str) -> bool:
        """记录一次失败，返回这次是否让熔断器打开。"""
        if status in REQUEST_SPECIFIC_STATUSES:
            return False
        self.consecutive_failures += 1
        self.last_error = message
        self.last_status = status
        if status in HARD_FAILURE_STATUSES:
            return self._open(self.hard_cooldown)
        if self.consecutive_failures >= self.threshold:
            return self._open(self.cooldown)
        return False

    def reset(self) -> None:
        self.record_success()
        self.last_error = ""
        self.last_status = 0

    def _open(self, seconds: float) -> bool:
        was_open = self._open_until is not None
        self._open_until = self._clock() + seconds
        self._open_until_wall = self._wall() + seconds
        return not was_open

    def snapshot(self) -> dict:
        is_open = self.is_open
        return {
            "open": is_open,
            "consecutive_failures": self.consecutive_failures,
            "threshold": self.threshold,
            "open_until": self._open_until_wall if is_open else None,
            "last_error": self.last_error,
            "last_status": self.last_status,
        }
