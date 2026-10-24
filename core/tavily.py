"""Tavily 搜索：给技术类问题（编程、软件版本、安全漏洞、官方文档）用的第二个通用搜索。

2026-10 的对比评测里，Tavily 在技术类问题上明显好于 DeepSeek，但不带发布日期、时效类问题较弱，
所以只作为“按问题类型分工”里技术问题的首选和 DeepDeep 的第一备用。

Key 默认复用 AstrBot「网页搜索」里配置的 Tavily Key（provider_settings.websearch_tavily_key），
所以即使内置网页搜索选的是别家（比如 SerpApi），也能同时用 Tavily。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .cache import TTLCache
from .stats import MonthlyCounter

API_URL = "https://api.tavily.com/search"
TOOL_NAME = "deepdeep_tavily_search"
MAX_QUERY_CHARS = 400
SNIPPET_CHARS = 600
RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})

_STATUS_MESSAGES = {
    400: "搜索请求参数无效",
    401: "Tavily API Key 无效",
    429: "Tavily 请求过于频繁或本月额度已用完",
    432: "Tavily 本月额度已用完",
    433: "Tavily 账户额度不足",
}

# 这些状态码说明 Key 本身有问题：按 Key 记下来（只在内存里），显示在配置检查里；
# 所有 Key 都有问题时暂停提供 Tavily。这个 Key 再次调用成功、或重启后清除
KEY_FAILURES = {401: "Key 无效", 432: "额度用完", 433: "额度用完"}
# 额度用完后暂停多久再试（Tavily 按账户的计费周期重置额度，不一定是每月 1 号）
QUOTA_PAUSE_SECONDS = 24 * 3600


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def mask(key: str) -> str:
    return f"{key[:4]}****{key[-4:]}" if len(key) > 10 else "****"


class TavilyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(slots=True)
class TavilyResult:
    title: str
    url: str
    content: str
    score: float = 0.0
    date: str = ""


@dataclass(slots=True)
class TavilyOutcome:
    text: str
    should_fallback: bool = False
    reason: str = ""
    request_id: str = ""
    results: list[TavilyResult] = field(default_factory=list)


class TavilyClient:
    """``key_provider`` 每次调用时返回当前可用的 Key 列表（第一个优先，失败时换下一个）。"""

    def __init__(
        self,
        key_provider: Callable[[], list[str]],
        *,
        timeout: float = 20,
        session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        api_url: str = API_URL,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.key_provider = key_provider
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._session_factory = session_factory
        self.api_url = api_url
        self._wall = wall_clock
        # key_id -> {"status", "label", "message", "at"}
        self.key_failures: dict[str, dict[str, Any]] = {}

    def keys(self) -> list[str]:
        try:
            return [
                k.strip()
                for k in self.key_provider()
                if isinstance(k, str) and k.strip()
            ]
        except Exception:  # noqa: BLE001 - 读配置失败时视为没有 Key
            return []

    def has_key(self) -> bool:
        return bool(self.keys())

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = (
                self._session_factory()
                if self._session_factory
                else aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                )
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def search(
        self, query: str, count: int, keys: list[str] | None = None
    ) -> list[TavilyResult]:
        """``keys`` 不传时用 key_provider；按会话读取配置时由调用方传入。"""
        keys = self.keys() if keys is None else [k for k in keys if k]
        if not keys:
            raise TavilyError(0, "没有可用的 Tavily API Key")
        body = {
            "query": query[:MAX_QUERY_CHARS],
            "max_results": max(1, min(20, count)),
            "search_depth": "basic",
        }
        last: TavilyError | None = None
        for key in keys:
            for attempt in range(2):
                try:
                    async with self._get_session().post(
                        self.api_url,
                        json=body,
                        headers={"Authorization": f"Bearer {key}"},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            self.key_failures.pop(key_id(key), None)
                            return [
                                _parse(x)
                                for x in data.get("results") or []
                                if isinstance(x, dict) and x.get("url")
                            ]
                        last = TavilyError(
                            resp.status,
                            _STATUS_MESSAGES.get(
                                resp.status, f"Tavily 返回 HTTP {resp.status}"
                            ),
                        )
                except asyncio.TimeoutError:
                    last = TavilyError(0, "Tavily 请求超时")
                except aiohttp.ClientError as exc:
                    last = TavilyError(0, f"无法连接 Tavily（{type(exc).__name__}）")
                # Key 无效或额度用完：记下来，换下一个 Key；限流、超时、服务端错误：同一个 Key 再试一次
                if last.status in KEY_FAILURES:
                    self.key_failures[key_id(key)] = {
                        "status": last.status,
                        "label": KEY_FAILURES[last.status],
                        "message": last.message,
                        "at": self._wall(),
                    }
                    break
                if attempt == 0 and (last.status == 0 or last.status in RETRYABLE):
                    await asyncio.sleep(0.5)
                    continue
                break
        assert last is not None
        raise last


def _parse(x: dict[str, Any]) -> TavilyResult:
    try:
        score = float(x.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return TavilyResult(
        title=str(x.get("title") or ""),
        url=str(x.get("url") or ""),
        content=str(x.get("content") or ""),
        score=score,
        date=str(x.get("published_date") or "")[:10],
    )


def format_results(
    query: str, results: list[TavilyResult], max_chars: int = 6000
) -> str:
    if not results:
        return f"Tavily 没有找到与「{query}」相关的网页。"
    header = f"「{query}」的 Tavily 搜索结果（共 {len(results)} 条，按相关度排序）："
    blocks: list[str] = []
    used = len(header)
    for i, r in enumerate(results, 1):
        meta = [f"相关度: {r.score:.2f}"]
        if r.date:
            meta.insert(0, f"日期: {r.date}")
        # Tavily 的摘要常带 Markdown 标题和大量换行，压成一段，省上下文
        content = re.sub(r"\s+", " ", re.sub(r"#{1,6}\s*", "", r.content)).strip()
        if len(content) > SNIPPET_CHARS:
            content = content[:SNIPPET_CHARS] + "…"
        block = "\n".join(
            [
                f"[{i}] {r.title or '(无标题)'}",
                " | ".join(meta),
                f"链接: {r.url}",
                f"摘要: {content}",
            ]
        )
        if blocks and used + len(block) + 2 > max_chars:
            blocks.append(
                f"（其余 {len(results) - len(blocks)} 条结果因长度限制已省略）"
            )
            break
        blocks.append(block)
        used += len(block) + 2
    footer = "结果大多不带发布日期；问最新动态时请以 deepdeep_web_search 的结果和日期为准。回答时请注明来源链接。"
    return "\n\n".join([header, *blocks, footer])


class TavilySearch:
    """工具调用的完整流程：校验、缓存、请求、整理结果、出错时给出下一步建议。"""

    def __init__(
        self,
        client: TavilyClient,
        cache: TTLCache[Any] | None = None,
        usage: MonthlyCounter | None = None,
    ) -> None:
        self.client = client
        self.cache: TTLCache[Any] = cache if cache is not None else TTLCache(0)
        self.default_count = 5
        self.max_count = 10
        self.max_chars = 6000
        # 本月成功的计费调用次数（HTTP 200，含无结果；缓存命中不算）
        self.usage = usage if usage is not None else MonthlyCounter(None)
        self.monthly_limit = 0  # 0 表示不限制

    def month_used(self) -> int:
        return self.usage.count()

    def limit_reached(self) -> bool:
        return self.monthly_limit > 0 and self.month_used() >= self.monthly_limit

    def usage_text(self) -> str:
        used = self.month_used()
        if self.monthly_limit > 0:
            return f"本月 Tavily 已用 {used} / {self.monthly_limit} 次"
        return f"本月 Tavily 已用 {used} 次"

    def _active(self, failure: dict[str, Any]) -> bool:
        """额度用完的记录过了暂停时间就不再算数（到时候放行一次试探）。"""
        if failure["status"] == 401:
            return True
        return self.client._wall() - failure["at"] < QUOTA_PAUSE_SECONDS

    def failures(self, keys: list[str] | None = None) -> list[dict[str, Any]]:
        """当前这些 Key 里有问题的，按 Key 的顺序。换掉的 Key 的旧记录不再出现。"""
        keys = self.client.keys() if keys is None else keys
        out = []
        for i, key in enumerate(keys):
            f = self.client.key_failures.get(key_id(key))
            if f is not None:
                out.append(
                    {**f, "position": i + 1, "total": len(keys), "masked": mask(key)}
                )
        return out

    def last_failure(self, keys: list[str] | None = None) -> dict[str, Any] | None:
        """最近一次 Key 无效 / 额度用完的记录；all_failed 表示所有 Key 都有问题。"""
        found = self.failures(keys)
        if not found:
            return None
        latest = max(found, key=lambda f: f["at"])
        return {**latest, "all_failed": len(found) == latest["total"]}

    def paused(self, keys: list[str] | None = None) -> str:
        """所有 Key 都无效或额度用完时，返回暂停的原因；否则返回空字符串。"""
        keys = self.client.keys() if keys is None else keys
        found = self.failures(keys)
        if not keys or len(found) < len(keys) or not all(map(self._active, found)):
            return ""
        if all(f["status"] == 401 for f in found):
            return "Tavily Key 无效，换成有效的 Key 后自动恢复"
        quota = [f for f in found if f["status"] != 401]
        resume = max(f["at"] for f in quota) + QUOTA_PAUSE_SECONDS
        hours = max(1, round((resume - self.client._wall()) / 3600))
        return f"Tavily 额度用完，约 {hours} 小时后自动重试"

    def _count(self, value: Any) -> int:
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            n = self.default_count
        return min(n, self.max_count)

    async def search(
        self, query: str, count: Any = None, keys: list[str] | None = None
    ) -> TavilyOutcome:
        rid = secrets.token_hex(3)
        query = (query or "").strip()
        if not query:
            return TavilyOutcome(
                f"[Tavily 搜索 #{rid}] 未调用 · 搜索关键词为空\n请提供要搜索的内容。",
                request_id=rid,
            )
        n = self._count(count)
        key = ("tavily", query, n)
        cached = self.cache.get(key)
        if cached is not None:
            return TavilyOutcome(
                f"[Tavily 搜索 #{rid}] 缓存 · {len(cached)} 条结果\n{format_results(query, cached, self.max_chars)}",
                request_id=rid,
                results=cached,
            )
        # 缓存不花钱，所以先查缓存再看上限
        if self.limit_reached():
            return TavilyOutcome(
                f"[Tavily 搜索 #{rid}] 未调用 · 本月 Tavily 调用已达上限（{self.month_used()} / {self.monthly_limit} 次）",
                True,
                "monthly_limit",
                rid,
            )
        started = time.monotonic()
        try:
            results = await self.client.search(query, n, keys)
        except TavilyError as exc:
            head = f"[Tavily 搜索 #{rid}] 失败 · " + (
                f"HTTP {exc.status}" if exc.status else "网络错误"
            )
            return TavilyOutcome(
                f"{head}\nTavily 搜索失败：{exc.message}",
                True,
                f"http_{exc.status}" if exc.status else "network",
                rid,
            )
        ms = int((time.monotonic() - started) * 1000)
        self.usage.add()
        if results:
            self.cache.set(key, results)
        text = f"[Tavily 搜索 #{rid}] 成功 · {len(results)} 条结果 · {ms} ms\n{format_results(query, results, self.max_chars)}"
        return TavilyOutcome(
            text, not results, "" if results else "empty", rid, results
        )
