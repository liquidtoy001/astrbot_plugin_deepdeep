"""所有付费搜索服务的本月花费估算，以及超出“每月花费上限”时要拿掉哪些工具。

调用次数不看调用日志：日志默认只保留 14 天，还可能被关掉，算不全一个月。来源是
- DeepSeek：UsageStats 的计费次数（DeepDeep 自己的工具），加上内置工具的成功调用
- Tavily：TavilySearch 的本月用量（DeepDeep 的工具），加上内置工具的成功调用
- 其他：ToolCaps 的月度计数（成功的调用；失败的不算，DeepDeep 自己缓存命中的也不算）

都是估算：价格来自 providers.py，别的插件自己的缓存和套餐折扣这里看不到。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from typing import Any

from . import catalog, providers
from .caps import ToolCaps
from .guard import BudgetGuard
from .providers import DEEPSEEK_BILLED_PATTERNS, OWN_TOOLS, TAVILY_TOOL, Provider
from .stats import MonthlyCounter, UsageStats

WARN_RATIO = 0.8
MAX_PRICE_PER_1000 = 100000.0


def parse_price_rules(lines: Any) -> dict[str, tuple[float, str]]:
    """用户给表里没有价格的服务填的单价。每行“服务 = 每千次价格 币种”，比如 bocha = 36 cny；
    认不出来的工具写成 tool:工具名。写错的行忽略。"""
    if not isinstance(lines, list):
        return {}
    out: dict[str, tuple[float, str]] = {}
    for line in lines:
        if not isinstance(line, str) or "=" not in line:
            continue
        key, _, value = line.rpartition("=")
        parts = value.split()
        if not key.strip() or len(parts) != 2 or parts[1] not in ("cny", "usd"):
            continue
        try:
            amount = float(parts[0])
        except ValueError:
            continue
        if 0 <= amount <= MAX_PRICE_PER_1000:
            out[key.strip()] = (amount, parts[1])
    return out


def format_price_rules(rules: dict[str, tuple[float, str]]) -> list[str]:
    return [f"{k} = {amount:g} {cur}" for k, (amount, cur) in rules.items()]


def provider_of(tool: str) -> Provider | None:
    lowered = tool.lower()
    if any(fnmatchcase(lowered, p) for p in DEEPSEEK_BILLED_PATTERNS):
        return providers.get("deepseek")
    entry = catalog.entry_for(tool)
    return providers.of_listing(entry.key) if entry is not None else None


def _name(provider: Provider) -> str:
    return provider.listings[0].name if provider.listings else provider.key


class Spending:
    def __init__(
        self,
        *,
        stats: UsageStats,
        budget: BudgetGuard,
        tool_caps: ToolCaps,
        tavily_usage: MonthlyCounter,
        usd_rate: Callable[[], float],
        is_search: Callable[[str], bool],
    ) -> None:
        self._stats = stats
        self._budget = budget
        self._caps = tool_caps
        self._tavily_usage = tavily_usage
        self._usd_rate = usd_rate
        self._is_search = is_search
        # 用户填的单价：{服务或 tool:工具名: (每千次价格, 币种)}
        self.custom_prices: dict[str, tuple[float, str]] = {}

    # ---------- 次数和金额 ----------

    def _counts(self) -> dict[str, dict[str, int]]:
        """本月每家、每个工具的调用次数：{服务: {工具: 次数}}；认不出服务的放在 ""。"""
        out: dict[str, dict[str, int]] = {}
        for tool, n in self._caps.counter.counts().items():
            if n <= 0 or not self._is_search(tool):
                continue
            p = provider_of(tool)
            out.setdefault(p.key if p else "", {})[tool] = n
        used = self._tavily_usage.count()
        if used:
            out.setdefault("tavily", {})[TAVILY_TOOL] = used
        price = self._stats.price
        billable = round(self._stats.month_cost() / price) if price > 0 else 0
        if billable:
            out.setdefault("deepseek", {})[OWN_TOOLS[0]] = billable
        return out

    def _unit_price(self, provider: Provider) -> float | None:
        """按计费币种算的单价。不知道价格、或者需要汇率但汇率是 0 时返回 None。"""
        if provider.key == "deepseek":
            return self._stats.price
        if provider.pricing is None:
            return self._custom_unit(provider.key)
        per_call = provider.pricing.usd_per_1000 / 1000
        if self._budget.currency.code == "usd":
            return per_call
        rate = self._usd_rate()
        return per_call * rate if rate > 0 else None

    def _custom_unit(self, key: str) -> float | None:
        rule = self.custom_prices.get(key)
        if rule is None:
            return None
        amount, cur = rule
        billing = self._budget.currency.code
        if cur == billing:
            return amount / 1000
        rate = self._usd_rate()
        if rate <= 0:
            return None
        return (amount * rate if cur == "usd" else amount / rate) / 1000

    def _cost(self, provider: Provider, calls: int) -> float | None:
        unit = self._unit_price(provider)
        if unit is None:
            return None
        free = provider.pricing.free_monthly if provider.pricing else 0
        return max(0, calls - free) * unit

    def total(self) -> float:
        out = 0.0
        for key, tools in self._counts().items():
            if key:
                out += self._cost(providers.get(key), sum(tools.values())) or 0.0
            else:
                for tool, n in tools.items():
                    out += n * (self._custom_unit(f"tool:{tool}") or 0.0)
        return out

    def other_cost(self) -> float:
        """除 DeepDeep 自己的 DeepSeek 工具之外的花费：加到 BudgetGuard 的本月花费上。"""
        return max(0.0, self.total() - self._stats.month_cost())

    # ---------- 上限 ----------

    def over_limit(self) -> bool:
        limit = self._budget.monthly_limit
        return limit > 0 and self.total() >= limit

    def blocked(self, names: Iterable[str], *, assume_over: bool = False) -> set[str]:
        """超出上限时要从请求里拿掉的工具。DeepDeep 自己的 DeepSeek 工具不在这里，还是走
        BudgetGuard 那一套；免费的、免费额度还没用完的（这个月继续用也不花钱）、不知道价格的都不拿。
        ``assume_over`` 用于路由预览：假设已经超了。"""
        if not assume_over and not self.over_limit():
            return set()
        counts = self._counts()
        out = set()
        for name in names:
            if name in OWN_TOOLS:
                continue
            p = provider_of(name)
            # 没填单价的不知道花不花钱，不拿；单价填 0 的就是不花钱
            if p is None:
                if self._custom_unit(f"tool:{name}"):
                    out.add(name)
                continue
            if not self._unit_price(p) or p.free:
                continue
            free = p.pricing.free_monthly if p.pricing else 0
            if sum(counts.get(p.key, {}).values()) >= free:
                out.add(name)
        return out

    # ---------- 面板 ----------

    def report(self, active_tools: Iterable[str]) -> dict[str, Any]:
        """``active_tools``：现在提供给模型的搜索工具。这个月用过的服务即使现在关了也列出来。"""
        active_tools = list(active_tools)
        counts = self._counts()
        for tool in active_tools:
            p = provider_of(tool)
            if self._is_search(tool):
                counts.setdefault(p.key if p else "", {})
        services = []
        excluded = []
        for p in providers.PROVIDERS:
            if p.key not in counts or p.free:
                continue
            calls = sum(counts[p.key].values())
            cost = self._cost(p, calls)
            if cost is None:
                excluded.append({"key": p.key, "name": _name(p), "calls": calls})
                continue
            free = p.pricing.free_monthly if p.pricing else 0
            services.append(
                {
                    "key": p.key,
                    "name": _name(p),
                    "calls": calls,
                    "free_quota": free,
                    "free_used": min(calls, free),
                    "cost": cost,
                    "note": p.pricing.note
                    if p.pricing
                    else ("" if p.key == "deepseek" else "按你填的单价计算"),
                }
            )
        # 认不出是哪一家的搜索工具
        unknown = dict(counts.get("", {}))
        for tool in active_tools:
            if self._is_search(tool) and provider_of(tool) is None:
                unknown.setdefault(tool, 0)
        for tool, n in sorted(unknown.items()):
            unit = self._custom_unit(f"tool:{tool}")
            if unit is None:
                excluded.append({"key": f"tool:{tool}", "name": tool, "calls": n})
                continue
            services.append(
                {
                    "key": f"tool:{tool}",
                    "name": tool,
                    "calls": n,
                    "free_quota": 0,
                    "free_used": 0,
                    "cost": n * unit,
                    "note": "按你填的单价计算",
                }
            )
        total = sum(s["cost"] for s in services)
        for s in services:
            s["share"] = s["cost"] / total if total > 0 else 0.0
        limit = self._budget.monthly_limit
        ratio = total / limit if limit > 0 else None
        return {
            "estimated": True,
            "currency": self._budget.currency.code,
            "symbol": self._budget.currency.symbol,
            "total": total,
            "limit": limit,
            "ratio": ratio,
            "over": limit > 0 and total >= limit,
            "warning": ratio is not None and ratio >= WARN_RATIO,
            "services": services,
            "excluded": excluded,
            "blocked": sorted(self.blocked(active_tools)),
        }
