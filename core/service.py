"""把客户端、缓存、费用上限、熔断、统计、格式化串起来。

不依赖 AstrBot，main.py 只做薄薄一层适配。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from .activity import ActivityLog
from .cache import TTLCache
from .client import (
    DeepSeekToolsClient,
    DeepSeekToolsError,
    FetchResult,
    SearchResult,
    validate_fetch_url,
)
from .formatter import (
    filter_results,
    format_fetch_result,
    format_search_results,
    has_date_filter,
    wants_recent,
)
from .guard import Availability, BudgetGuard, CircuitBreaker
from .log import logger
from .pricing import format_amount, format_money
from .stats import UsageStats


@dataclass(slots=True)
class Settings:
    default_count: int = 5
    max_count: int = 10
    min_rerank_score: float = 0.5
    include_content: bool = False
    content_chars_per_result: int = 800
    max_search_chars: int = 6000
    max_fetch_chars: int = 8000
    send_original_question: bool = True

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Settings:
        max_count = cfg_int(config, "max_count", 10, 1, 50)
        return cls(
            default_count=min(cfg_int(config, "default_count", 5, 1, 50), max_count),
            max_count=max_count,
            min_rerank_score=cfg_float(config, "min_rerank_score", 0.5, 0.0, 3.0),
            include_content=bool(config.get("include_content", False)),
            content_chars_per_result=cfg_int(
                config, "content_chars_per_result", 800, 0, 20000
            ),
            max_search_chars=cfg_int(config, "max_search_chars", 6000, 500, 100000),
            max_fetch_chars=cfg_int(config, "max_fetch_chars", 8000, 500, 200000),
            send_original_question=bool(config.get("send_original_question", True)),
        )


def cfg_int(config: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(config.get(key, default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def cfg_float(
    config: dict[str, Any], key: str, default: float, lo: float, hi: float
) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def coerce_count(value: Any, settings: Settings) -> int:
    """模型可能传 0、字符串、小数或干脆不传。"""
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        count = settings.default_count
    return min(count, settings.max_count)


@dataclass(slots=True)
class ToolOutcome:
    text: str
    # True 表示 DeepSeek 这次没帮上忙，值得换备用搜索工具再试
    should_fallback: bool = False
    reason: str = ""
    # 与日志、面板“最近调用”里的记录对应，方便排查
    request_id: str = ""


KIND_LABELS = {"search": "DeepSeek 搜索", "fetch": "DeepSeek 抓取"}


def new_request_id() -> str:
    return secrets.token_hex(3)


def status_line(kind: str, request_id: str, *parts: str) -> str:
    """工具结果第一行的统一格式，开启“显示工具调用结果”时用户也能看到。

    例：[DeepSeek 搜索 #a1b2c3] 成功 · 5 条结果 · 812 ms
    """
    return f"[{KIND_LABELS.get(kind, kind)} #{request_id}] " + " · ".join(
        p for p in parts if p
    )


class SearchService:
    def __init__(
        self,
        client: DeepSeekToolsClient,
        settings: Settings,
        *,
        cache: TTLCache[Any] | None = None,
        stats: UsageStats | None = None,
        budget: BudgetGuard | None = None,
        breaker: CircuitBreaker | None = None,
        activity: ActivityLog | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        # 注意用 is None：空的 TTLCache 长度为 0，布尔值为 False
        self.cache: TTLCache[Any] = cache if cache is not None else TTLCache(0)
        self.stats = stats if stats is not None else UsageStats(None)
        self.budget = budget if budget is not None else BudgetGuard(self.stats)
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self.activity = activity if activity is not None else ActivityLog()
        self._last_availability = Availability(True)

    def availability(self, kind: str = "search") -> Availability:
        avail = self._check_availability(kind)
        # 只在状态变化时记一条 INFO，避免每次请求都刷日志
        if avail.code != self._last_availability.code:
            if avail.ok:
                logger.info(
                    "[deepdeep] DeepSeek 恢复可用"
                    f"（此前：{self._last_availability.message}）"
                )
            else:
                logger.info(
                    f"[deepdeep] DeepSeek 暂不可用 code={avail.code}：{avail.message}"
                )
            self._last_availability = avail
        return avail

    def _check_availability(self, kind: str) -> Availability:
        if not self.client.has_key():
            return Availability(False, "no_key", "未配置 DeepSeek API Key")
        breaker = self.breaker.check()
        if not breaker.ok:
            return breaker
        return self.budget.check(kind)

    async def search(
        self, query: str, count: Any = None, original_question: str | None = None
    ) -> ToolOutcome:
        s = self.settings
        rid = new_request_id()
        query = (query or "").strip()
        if not query:
            return ToolOutcome(
                status_line("search", rid, "未调用", "搜索关键词为空")
                + "\n请提供要搜索的内容。",
                request_id=rid,
            )
        n = coerce_count(count, s)
        ori = original_question if s.send_original_question else None
        detail: dict[str, Any] = {
            "request": {
                "query": query,
                "count": n,
                "need_content": s.include_content,
                "ori_question": (ori or "").strip()[:128] or None,
            }
        }
        key = ("search", query, n, s.include_content)

        results: list[SearchResult] | None = self.cache.get(key)
        latency = 0
        if results is not None:
            outcome = "cache_hit"
            self.stats.record("search", outcome)
        else:
            avail = self.availability("search")
            if not avail.ok:
                return self._blocked("search", query, rid, avail, detail)
            started = time.monotonic()
            trace: list[dict] = []
            try:
                results = await self.client.search(
                    query,
                    count=n,
                    ori_question=ori,
                    need_content=s.include_content,
                    trace=trace,
                )
            except DeepSeekToolsError as exc:
                return self._on_error("search", query, rid, exc, started, trace, detail)
            latency = _ms(started)
            detail["attempts"] = trace
            self.breaker.record_success()
            # 空结果不计费，也不缓存（可能稍后就有了）
            outcome = "billable" if results else "empty"
            self.stats.record("search", outcome)
            if results:
                self.cache.set(key, results)

        body = format_search_results(
            query,
            results,
            min_score=s.min_rerank_score,
            max_chars=s.max_search_chars,
            include_content=s.include_content,
            content_chars=s.content_chars_per_result,
        )
        detail["results"] = [
            {
                "title": r.title,
                "url": r.url,
                "site": r.site_name,
                "date": r.date,
                "score": r.rerank_score,
                "authority": r.authority,
            }
            for r in results[:10]
        ]
        detail["output_chars"] = len(body)
        self.activity.add(
            "search",
            query,
            outcome,
            request_id=rid,
            status=0 if outcome == "cache_hit" else 200,
            latency_ms=latency,
            results=len(results),
            detail=detail,
        )
        self._log_call("search", rid, outcome, query, latency, len(results), detail)
        head = status_line(
            "search",
            rid,
            "缓存" if outcome == "cache_hit" else "成功",
            f"{len(results)} 条结果",
            f"{latency} ms" if latency else "",
        )
        # 没有结果、或结果相关度都很低：不算故障，但值得换个搜索工具再试
        soft = ""
        if not results:
            soft = "empty"
        elif (
            not has_date_filter(query)
            and not wants_recent(query)
            and filter_results(results, s.min_rerank_score)[1]
        ):
            soft = "low_quality"
        return ToolOutcome(
            f"{head}\n{body}",
            should_fallback=bool(soft),
            reason=soft,
            request_id=rid,
        )

    async def fetch(self, url: str) -> ToolOutcome:
        rid = new_request_id()
        # 本地校验失败时不发请求，也不计入统计
        try:
            url = validate_fetch_url(url)
        except DeepSeekToolsError as exc:
            return ToolOutcome(
                status_line("fetch", rid, "未调用", "链接无效")
                + f"\n网页抓取失败：{exc.message}",
                request_id=rid,
            )
        detail: dict[str, Any] = {"request": {"url": url}}
        key = ("fetch", url)
        result: FetchResult | None = self.cache.get(key)
        latency = 0
        if result is not None:
            outcome = "cache_hit"
            self.stats.record("fetch", outcome)
        else:
            avail = self.availability("fetch")
            if not avail.ok:
                return self._blocked("fetch", url, rid, avail, detail)
            started = time.monotonic()
            trace: list[dict] = []
            try:
                result = await self.client.fetch(url, trace=trace)
            except DeepSeekToolsError as exc:
                return self._on_error("fetch", url, rid, exc, started, trace, detail)
            latency = _ms(started)
            detail["attempts"] = trace
            self.breaker.record_success()
            outcome = "billable" if result.content.strip() else "empty"
            self.stats.record("fetch", outcome)
            if outcome == "billable":
                self.cache.set(key, result)

        body = format_fetch_result(result, max_chars=self.settings.max_fetch_chars)
        content_chars = len(result.content.strip())
        detail["page"] = {
            "title": result.title,
            "content_chars": content_chars,
            "truncated": content_chars > self.settings.max_fetch_chars,
        }
        detail["output_chars"] = len(body)
        self.activity.add(
            "fetch",
            url,
            outcome,
            request_id=rid,
            status=0 if outcome == "cache_hit" else 200,
            latency_ms=latency,
            detail=detail,
        )
        self._log_call("fetch", rid, outcome, url, latency, None, detail)
        head = status_line(
            "fetch",
            rid,
            "缓存" if outcome == "cache_hit" else "成功",
            f"{content_chars} 字" if content_chars else "无正文",
            f"{latency} ms" if latency else "",
        )
        return ToolOutcome(f"{head}\n{body}", request_id=rid)

    def _blocked(
        self,
        kind: str,
        target: str,
        rid: str,
        avail: Availability,
        detail: dict[str, Any],
    ) -> ToolOutcome:
        detail["blocked"] = {"code": avail.code, "message": avail.message}
        self.activity.add(
            kind, target, "blocked", request_id=rid, note=avail.message, detail=detail
        )
        self._log_call(kind, rid, "blocked", target, 0, None, detail)
        label = "联网搜索" if kind == "search" else "网页抓取"
        return ToolOutcome(
            status_line(kind, rid, "未调用", avail.message)
            + f"\nDeepSeek {label}暂不可用：{avail.message}。",
            should_fallback=True,
            reason=avail.code,
            request_id=rid,
        )

    def _on_error(
        self,
        kind: str,
        target: str,
        rid: str,
        exc: DeepSeekToolsError,
        started: float,
        trace: list[dict],
        detail: dict[str, Any],
    ) -> ToolOutcome:
        self.stats.record(kind, "failed")
        opened = self.breaker.record_failure(exc.status, exc.message)
        detail["attempts"] = trace
        detail["error"] = {
            "status": exc.status,
            "message": exc.message,
            "detail": exc.detail,
            "circuit_opened": opened,
        }
        self.activity.add(
            kind,
            target,
            "failed",
            request_id=rid,
            status=exc.status,
            latency_ms=_ms(started),
            note=exc.message + ("（已触发熔断）" if opened else ""),
            detail=detail,
        )
        logger.warning(
            f"[deepdeep] #{rid} {kind} 失败 status={exc.status} "
            f"attempts={exc.attempts} detail={exc.detail!r} circuit_opened={opened}"
        )
        if opened:
            logger.info(
                f"[deepdeep] 熔断已打开：{exc.message}，"
                f"连续失败 {self.breaker.consecutive_failures} 次"
            )
        label = "联网搜索" if kind == "search" else "网页抓取"
        head = status_line(
            kind,
            rid,
            "失败",
            f"HTTP {exc.status}" if exc.status else "网络错误",
            f"已重试 {exc.attempts - 1} 次" if exc.attempts > 1 else "",
        )
        # 搜索词写错（400）换工具也没用；其余情况（包括某个网页抓不到）都值得换备用工具
        return ToolOutcome(
            f"{head}\n{label}失败：{exc.message}",
            should_fallback=exc.status != 400,
            reason=f"http_{exc.status}" if exc.status else "network",
            request_id=rid,
        )

    def _log_call(
        self,
        kind: str,
        rid: str,
        outcome: str,
        target: str,
        latency: int,
        results: int | None,
        detail: dict[str, Any],
    ) -> None:
        parts = [f"[deepdeep] #{rid} {kind} outcome={outcome}"]
        if results is not None:
            parts.append(f"results={results}")
        if latency:
            parts.append(f"latency={latency}ms")
        attempts = detail.get("attempts")
        if attempts:
            parts.append(f"attempts={len(attempts)}")
        # 搜索词属于用户内容，遵循“记录搜索词”开关
        if self.activity.record_targets:
            parts.append(f"target={target[:120]!r}")
        logger.debug(" ".join(parts))

    def stats_report(self, usd_cny_rate: float = 0.0) -> str:
        today = self.stats.day()
        lines = ["DeepDeep · 今日统计"]
        names = {"search": "搜索", "fetch": "抓取"}
        for kind, label in names.items():
            c = today[kind]
            lines.append(
                f"{label}: 计费 {c['billable']} 次 · 空结果 {c['empty']} · "
                f"失败 {c['failed']} · 缓存命中 {c['cache_hit']}"
            )
        cur = self.budget.currency
        b = self.budget.snapshot()

        def fmt(amount: float) -> str:
            return format_money(amount, cur, usd_cny_rate)

        daily = f" / {fmt(b['daily_limit'])}" if b["daily_limit"] else ""
        monthly = f" / {fmt(b['monthly_limit'])}" if b["monthly_limit"] else ""
        lines.append(f"今日预估费用: {fmt(b['today_cost'])}{daily}")
        lines.append(f"本月预估费用: {fmt(b['month_cost'])}{monthly}")
        avail = self.availability()
        lines.append(
            "当前状态: 正常" if avail.ok else f"当前状态: 暂停（{avail.message}）"
        )
        lines.append(
            f"（按每千次 {format_amount(self.stats.price * 1000, cur)} 估算，"
            "以 DeepSeek 平台账单为准）"
        )
        return "\n".join(lines)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
