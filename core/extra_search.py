"""表驱动的搜索客户端：Perplexity、Serper、Jina、TinyFish 这类只要发一个 HTTP 请求的搜索服务。

每家的请求和返回格式写在 providers.py 的 SearchApi 里，加一家不用改这里。
只有填了 Key 且开着的服务才注册工具，其他用户的工具列表不变。它们只作为备用，不改默认首选。

新工具走现有的机制：调用日志和健康检查靠 AstrBot 的工具钩子，每月上限靠 tool_monthly_limits。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from astrbot.api import logger

from .cache import TTLCache
from .credentials import mask_key
from .providers import EXTRA_PROVIDERS, Provider, SearchApi

if TYPE_CHECKING:
    from .astrbot_view import AstrBotView

MAX_QUERY_CHARS = 400
SNIPPET_CHARS = 600
RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})
# 这些状态码说明 Key 有问题，显示在配置检查里，重试也没用
KEY_FAILURES = {401: "Key 无效", 402: "额度用完", 403: "Key 无效或没有权限"}

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词，用简洁的关键词"},
        "count": {"type": "number", "description": "返回结果数，可不填，默认 5"},
    },
    "required": ["query"],
}


class ExtraSearchError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(slots=True)
class ExtraResult:
    title: str
    url: str
    snippet: str
    date: str = ""


def _first_text(item: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def parse_results(api: SearchApi, data: Any) -> list[ExtraResult]:
    """按记录里写的字段名取结果。格式不对时返回空，不抛异常。"""
    rows = data.get(api.results_field) if isinstance(data, dict) else None
    out = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        url = _first_text(item, api.url_fields)
        if url:
            out.append(
                ExtraResult(
                    title=_first_text(item, api.title_fields),
                    url=url,
                    snippet=_first_text(item, api.snippet_fields),
                    date=_first_text(item, api.date_fields)[:10],
                )
            )
    return out


def format_results(
    label: str, query: str, results: list[ExtraResult], max_chars: int = 6000
) -> str:
    if not results:
        return f"{label} 没有找到与「{query}」相关的网页。"
    header = f"「{query}」的 {label} 搜索结果（共 {len(results)} 条）："
    blocks: list[str] = []
    used = len(header)
    for i, r in enumerate(results, 1):
        snippet = re.sub(r"\s+", " ", r.snippet).strip()
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS] + "…"
        lines = [f"[{i}] {r.title or '(无标题)'}"]
        if r.date:
            lines.append(f"日期: {r.date}")
        lines += [f"链接: {r.url}", f"摘要: {snippet}"]
        block = "\n".join(lines)
        if blocks and used + len(block) + 2 > max_chars:
            blocks.append(
                f"（其余 {len(results) - len(blocks)} 条结果因长度限制已省略）"
            )
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join([header, *blocks, "回答时请注明来源链接。"])


class ExtraClient:
    def __init__(self, timeout: float = 20) -> None:
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @staticmethod
    def url_for(api: SearchApi) -> str:
        return api.url

    async def search(
        self, label: str, api: SearchApi, key: str, query: str, count: int
    ) -> list[ExtraResult]:
        fields: dict[str, Any] = {api.query_field: query[:MAX_QUERY_CHARS]}
        if api.count_field:
            fields[api.count_field] = max(1, min(api.max_count, count))
        headers = {
            api.auth_header: f"{api.auth_prefix}{key}",
            "Accept": "application/json",
        }
        place = {"params": fields} if api.method == "GET" else {"json": fields}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        last = ExtraSearchError(0, f"{label} 请求失败")
        for attempt in range(2):
            try:
                async with self._get_session().request(
                    api.method,
                    self.url_for(api),
                    headers=headers,
                    timeout=timeout,
                    **place,
                ) as resp:
                    if resp.status == 200:
                        results = parse_results(api, await resp.json(content_type=None))
                        # 有的服务（Jina、TinyFish）不收条数参数，这里截断
                        return results[: max(1, count)]
                    last = ExtraSearchError(
                        resp.status, f"{label} 返回 HTTP {resp.status}"
                    )
            except asyncio.TimeoutError:
                last = ExtraSearchError(0, f"{label} 请求超时")
            except aiohttp.ClientError as exc:
                last = ExtraSearchError(0, f"无法连接 {label}（{type(exc).__name__}）")
            except ValueError:
                last = ExtraSearchError(0, f"{label} 返回的内容无法解析")
            if attempt == 0 and (last.status == 0 or last.status in RETRYABLE):
                await asyncio.sleep(0.5)
                continue
            break
        raise last


# 工具对象可能被 AstrBot 复制，所以工具上不存回调，按工具名在这里查
_RUNNERS: dict[str, Any] = {}


def make_tool(api: SearchApi, description: str) -> Any:
    """AstrBot 的 FunctionTool。放在函数里导入：内部接口变了只影响这几个工具，
    weekly compat（.github/scripts/compat_probe.py）会检查。"""
    from astrbot.core.agent.tool import FunctionTool
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    @pydantic_dataclass
    class ExtraSearchTool(FunctionTool):
        async def call(self, context: Any, **kwargs: Any) -> Any:
            text, is_error = await _RUNNERS[self.name](
                str(kwargs.get("query") or ""), kwargs.get("count") or 0
            )
            return error_result(text) if is_error else text

    return ExtraSearchTool(
        name=api.tool, description=description, parameters=dict(TOOL_PARAMETERS)
    )


def error_result(text: str) -> Any:
    """isError 让调用日志和健康检查知道这次失败了。"""
    import mcp

    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)], isError=True
    )


class ExtraSearch:
    def __init__(self, astrbot: AstrBotView, config: Mapping[str, Any]) -> None:
        self._astrbot = astrbot
        self._config = config
        self.client = ExtraClient()
        self.cache: TTLCache[Any] = TTLCache(0)
        self.default_count = 5
        self.max_count = 10
        self.max_chars = 6000
        self._registered: set[str] = set()
        # 最近一次 Key 的问题（只在内存里）：{服务: {"status", "label", "at"}}
        self.failures: dict[str, dict[str, Any]] = {}
        # 服务商真正收费的调用才记一次，缓存命中和失败的不算（每月上限和花费估算都用它）
        self.on_billed: Any = lambda _tool: None

    # ---------- 状态 ----------

    def key(self, provider: Provider) -> str:
        return str(self._config.get(provider.key_field, "") or "").strip()

    def enabled(self, provider: Provider) -> bool:
        return bool(self._config.get(provider.enabled_field, not provider.opt_in))

    def ready(self, provider: Provider) -> bool:
        return self.enabled(provider) and bool(self.key(provider))

    def status(self) -> dict[str, dict[str, Any]]:
        """设置页和配置检查用：{Listing 的 key: {...}}。Key 只有打码后的。"""
        out = {}
        for p in EXTRA_PROVIDERS:
            key = self.key(p)
            entry = next(x for x in p.listings if x.source == "own")
            out[entry.key] = {
                "service": p.key,
                "name": entry.name,
                "tool": p.api.tool,
                "enabled": self.enabled(p),
                "configured": bool(key),
                "masked": mask_key(key),
                "source": "插件配置" if key else "",
                "tested": p.api.tested,
                "last_failure": self.failures.get(p.key),
            }
        return out

    # ---------- 工具注册 ----------

    def sync_tools(self) -> None:
        """设置变了之后重新看一遍：该有的注册，不该有的拿掉。"""
        for p in EXTRA_PROVIDERS:
            tool = p.api.tool
            want = self.ready(p)
            if want and tool not in self._registered:
                entry = next(x for x in p.listings if x.source == "own")
                _RUNNERS[tool] = self._runner(p, entry.name)
                try:
                    self._astrbot.add_tool(make_tool(p.api, p.api.description))
                except Exception as exc:  # noqa: BLE001 - 内部接口变了也不能影响其他功能
                    logger.warning(f"[deepdeep] 注册 {tool} 失败: {exc}")
                    continue
                self._registered.add(tool)
            elif not want and tool in self._registered:
                self.remove_tool(tool)

    def remove_tool(self, tool: str) -> None:
        try:
            self._astrbot.remove_tool(tool)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[deepdeep] 移除 {tool} 失败: {exc}")
        self._registered.discard(tool)

    def remove_all(self) -> None:
        for tool in list(self._registered):
            self.remove_tool(tool)

    def _runner(self, provider: Provider, label: str) -> Any:
        async def run(query: str, count: Any) -> tuple[str, bool]:
            return await self.search(provider, label, query, count)

        return run

    # ---------- 搜索 ----------

    def _count(self, count: Any) -> int:
        try:
            value = int(count)
        except (TypeError, ValueError):
            value = 0
        return min(value if value > 0 else self.default_count, self.max_count)

    async def search(
        self, provider: Provider, label: str, query: str, count: Any = 0
    ) -> tuple[str, bool]:
        """返回 (给模型看的文字, 是否失败)。"""
        query = query.strip()
        if not query:
            return "搜索词不能为空。", True
        key = self.key(provider)
        if not key:
            return f"{label} 暂不可用：没有配置 Key。请改用其他搜索工具。", True
        n = self._count(count)
        cache_key = (provider.key, query, n)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached, False
        try:
            results = await self.client.search(label, provider.api, key, query, n)
        except ExtraSearchError as exc:
            if exc.status in KEY_FAILURES:
                self.failures[provider.key] = {
                    "status": exc.status,
                    "label": KEY_FAILURES[exc.status],
                    "at": time.time(),
                }
            logger.warning(f"[deepdeep] {label} 搜索失败 status={exc.status}")
            return f"{exc.message}。请改用其他搜索工具。", True
        self.failures.pop(provider.key, None)
        self.on_billed(provider.api.tool)
        text = format_results(label, query, results, self.max_chars)
        if results:
            self.cache.set(cache_key, text)
        return text, False

    async def test(self, provider: Provider) -> dict[str, Any]:
        """面板上的“测试”：真实请求一次，算一次调用。"""
        if not self.key(provider):
            return {"ok": False, "message": "还没有填写 Key"}
        entry = next(x for x in provider.listings if x.source == "own")
        try:
            await self.client.search(
                entry.name, provider.api, self.key(provider), "DeepSeek", 1
            )
        except ExtraSearchError as exc:
            if exc.status in KEY_FAILURES:
                self.failures[provider.key] = {
                    "status": exc.status,
                    "label": KEY_FAILURES[exc.status],
                    "at": time.time(),
                }
            return {"ok": False, "message": exc.message}
        self.failures.pop(provider.key, None)
        self.on_billed(provider.api.tool)
        return {"ok": True, "message": "连接正常（本次测试算 1 次调用）"}
