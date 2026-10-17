"""DeepSeek Search & Fetch API 客户端。

文档：https://api-docs.deepseek.com/guides/search
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

MAX_QUERY_CHARS = 128
MAX_ORI_QUESTION_CHARS = 128
MAX_URL_CHARS = 8192
MAX_COUNT = 50

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# 给模型看的错误说明：要短、要明确告诉它下一步该怎么做，避免它反复重试。
_STATUS_MESSAGES = {
    400: "搜索请求参数无效",
    401: "DeepSeek API Key 无效或缺失，请联系管理员检查插件配置",
    402: "DeepSeek 账户余额不足，暂时无法联网搜索，请告知用户并直接根据已有知识回答",
    403: "该网页被 DeepSeek 安全策略拒绝抓取，请换一个链接或改用搜索",
    404: "无法获取该网页的可用内容（页面不存在、被站点屏蔽或没有可提取的正文），请换一个链接",
    408: "请求超时，请稍后再试",
    413: "请求内容过大",
    429: "联网搜索请求过于频繁，请稍后再试",
    500: "DeepSeek 搜索服务内部错误，请稍后再试",
    504: "DeepSeek 搜索服务上游超时，请稍后再试",
}


class DeepSeekToolsError(Exception):
    """调用失败。``status`` 为 0 表示网络层错误或本地校验失败。"""

    def __init__(self, status: int, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail
        self.attempts = 1

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUSES


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    summary: str = ""
    content: str = ""
    site_name: str | None = None
    date: str | None = None
    rerank_score: float = 0.0
    authority: int = -1

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> SearchResult:
        def _num(value: Any, default: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return cls(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            snippet=str(item.get("snippet") or ""),
            summary=str(item.get("summary") or ""),
            content=str(item.get("content") or ""),
            site_name=item.get("site_name") or None,
            date=item.get("date") or None,
            rerank_score=_num(item.get("rerank_score"), 0.0),
            authority=int(_num(item.get("authority"), -1)),
        )


@dataclass(slots=True)
class FetchResult:
    url: str
    title: str
    content: str


class RateLimiter:
    """把请求均匀间隔开，保证不超过 ``qps``。"""

    def __init__(
        self,
        qps: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await self._sleep(wait)


def validate_fetch_url(url: str) -> str:
    """按官方限制在本地先校验 URL，省一次无效请求。返回去掉首尾空白的 URL。"""
    url = (url or "").strip()
    if not url:
        raise DeepSeekToolsError(0, "链接为空")
    if len(url) > MAX_URL_CHARS:
        raise DeepSeekToolsError(0, f"链接过长（超过 {MAX_URL_CHARS} 字符）")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DeepSeekToolsError(0, "只能抓取以 http:// 或 https:// 开头的完整网页链接")
    if parts.username or parts.password:
        raise DeepSeekToolsError(0, "链接中不能包含用户名或密码")
    return url


def clamp_text(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:limit]


class DeepSeekToolsClient:
    def __init__(
        self,
        api_key_getter: Callable[[], str | None],
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20,
        max_retries: int = 2,
        qps: float = 5,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        backoff_base: float = 1.0,
    ) -> None:
        self._api_key_getter = api_key_getter
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._backoff_base = backoff_base
        # 官方对 Search 和 Fetch 分别限流，这里也分开
        self._search_limiter = RateLimiter(qps, sleep=sleep)
        self._fetch_limiter = RateLimiter(qps, sleep=sleep)
        self._session: aiohttp.ClientSession | None = None

    def configure(self, *, timeout: float, max_retries: int, qps: float) -> None:
        """WebUI 修改配置后热更新，不用重载插件。"""
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max(0, max_retries)
        self._search_limiter = RateLimiter(qps, sleep=self._sleep)
        self._fetch_limiter = RateLimiter(qps, sleep=self._sleep)

    def has_key(self) -> bool:
        return bool((self._api_key_getter() or "").strip())

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def search(
        self,
        query: str,
        *,
        count: int = 5,
        ori_question: str | None = None,
        need_content: bool = False,
        trace: list[dict] | None = None,
    ) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            raise DeepSeekToolsError(0, "搜索关键词为空")
        payload: dict[str, Any] = {
            "query": query[:MAX_QUERY_CHARS],
            "count": max(1, min(MAX_COUNT, int(count))),
            "need_content": bool(need_content),
        }
        ori = clamp_text(ori_question, MAX_ORI_QUESTION_CHARS)
        if ori:
            payload["ori_question"] = ori
        data = await self._post(SEARCH_PATH, payload, self._search_limiter, trace)
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise DeepSeekToolsError(500, "DeepSeek 搜索返回了无法解析的结果")
        return [SearchResult.from_api(i) for i in items if isinstance(i, dict)]

    async def fetch(self, url: str, *, trace: list[dict] | None = None) -> FetchResult:
        url = validate_fetch_url(url)
        data = await self._post(FETCH_PATH, {"url": url}, self._fetch_limiter, trace)
        if not isinstance(data, dict):
            raise DeepSeekToolsError(500, "DeepSeek 抓取返回了无法解析的结果")
        return FetchResult(
            url=url,
            title=str(data.get("title") or ""),
            content=str(data.get("content") or ""),
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        limiter: RateLimiter,
        trace: list[dict] | None = None,
    ) -> Any:
        """发送请求，按需重试。``trace`` 不为 None 时逐次记录每次尝试，便于排查。"""
        api_key = (self._api_key_getter() or "").strip()
        if not api_key:
            raise DeepSeekToolsError(
                401,
                "未配置 DeepSeek API Key，请联系管理员在插件配置中填写，或添加 DeepSeek 官方提供商",
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = self._base_url + path
        attempt = 0
        while True:
            await limiter.acquire()
            retry_after: float | None = None
            started = time.monotonic()
            try:
                session = self._get_session()
                async with session.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        _trace(trace, attempt, 200, started)
                        return data
                    error = await self._build_error(resp)
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            except (TimeoutError, asyncio.TimeoutError):
                error = DeepSeekToolsError(0, "连接 DeepSeek 超时，请稍后再试")
            except aiohttp.ClientError as exc:
                error = DeepSeekToolsError(
                    0, "无法连接 DeepSeek 搜索服务，请稍后再试", detail=repr(exc)
                )

            give_up = not error.retryable or attempt >= self._max_retries
            delay = self._backoff_base * (2**attempt)
            if retry_after is not None:
                delay = max(delay, min(retry_after, 30.0))
            _trace(
                trace,
                attempt,
                error.status,
                started,
                error=error.detail or error.message,
                retry_in=None if give_up else delay,
            )
            if give_up:
                error.attempts = attempt + 1
                raise error
            attempt += 1
            await self._sleep(delay)

    @staticmethod
    async def _build_error(resp: aiohttp.ClientResponse) -> DeepSeekToolsError:
        detail = ""
        try:
            body = await resp.json(content_type=None)
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                detail = str(body["error"].get("message") or "")
        except (ValueError, aiohttp.ClientError):
            try:
                detail = (await resp.text())[:300]
            except (UnicodeDecodeError, aiohttp.ClientError):
                detail = ""
        message = _STATUS_MESSAGES.get(resp.status)
        if message is None:
            message = (
                "DeepSeek 搜索服务暂时不可用，请稍后再试"
                if resp.status >= 500
                else f"DeepSeek 搜索请求失败（HTTP {resp.status}）"
            )
        if resp.status == 400 and detail:
            message = f"{message}：{detail}"
        return DeepSeekToolsError(resp.status, message, detail=detail)


def _trace(
    trace: list[dict] | None,
    attempt: int,
    status: int,
    started: float,
    *,
    error: str = "",
    retry_in: float | None = None,
) -> None:
    if trace is None:
        return
    entry: dict[str, Any] = {
        "attempt": attempt + 1,
        "status": status,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
    if error:
        entry["error"] = error[:300]
    if retry_in is not None:
        entry["retry_in_s"] = round(retry_in, 2)
    trace.append(entry)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
