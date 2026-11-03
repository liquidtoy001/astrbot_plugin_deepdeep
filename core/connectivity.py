"""各搜索工具走哪条网络、能搜到国内还是国外网站，以及从服务器实测的连通性。

搜索工具分两种：
- 云端 API（DeepSeek、Tavily、博查……）：搜索在服务商的服务器上完成，你的服务器只需要连得上它的接口
- 自建或本机服务（SearXNG、直接抓网页的工具）：请求从你的服务器发出，
  服务器访问不了的网站搜不到、读不了；服务器 IP 也可能被搜索引擎拦截（验证码、限流）

检测只发免费请求（打开网站首页、访问接口地址、查一次 SearXNG），不调用任何计费接口。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp

from .providers import BACKENDS
from .providers import Network as Backend

KIND_LABELS = {
    "cloud": "云端 API",
    "self_hosted": "自建服务（内网）",
    "server": "服务器直连",
    "local": "本机",
    "unknown": "本机进程或未知",
}
# 在服务器本机运行、但已知会访问哪里的 MCP 服务
KNOWN_MCP_ENDPOINTS = {"arxiv": "https://export.arxiv.org"}
REGION_LABELS = {"domestic": "国内", "foreign": "国外", "self": "自建", "none": "—"}

# 判断服务器网络环境的参考网站
PROBE_SITES = {
    "domestic": [
        ("百度", "https://www.baidu.com"),
        ("哔哩哔哩", "https://www.bilibili.com"),
    ],
    "foreign": [
        ("Google", "https://www.google.com"),
        ("GitHub", "https://github.com"),
        ("Wikipedia", "https://en.wikipedia.org"),
    ],
}
PROBE_TIMEOUT = 6
SEARXNG_TIMEOUT = 20


def match_backend(name: str) -> Backend | None:
    lowered = name.lower()
    for b in BACKENDS:
        if any(fnmatchcase(lowered, p) for p in b.patterns):
            return b
    return None


def origin_of(url: Any) -> str | None:
    """只留协议、主机和端口。路径和参数里可能有 Key，账号密码也不带。"""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url if "//" in url else f"//{url}")
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return f"{parts.scheme or 'https'}://{parts.hostname}{port}"


def is_private_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost",) or "." not in host:
        # docker 服务名（searxng）、localhost
        return True
    if re.match(r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return True
    return host.endswith((".local", ".lan", ".internal"))


def find_searxng_url(config_dir: Path | None) -> str | None:
    """在其他插件的配置里找 SearXNG 地址（例如 web_search_pro 的 searxng_api_url）。"""
    if config_dir is None or not config_dir.is_dir():
        return None
    for path in sorted(config_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        found = _search_key(data)
        if found:
            return found
    return None


def _search_key(data: Any, depth: int = 0) -> str | None:
    if depth > 3 or not isinstance(data, dict):
        return None
    for key, value in data.items():
        if (
            "searxng" in str(key).lower()
            and isinstance(value, str)
            and value.startswith(("http://", "https://"))
        ):
            return value.rstrip("/")
        found = _search_key(value, depth + 1)
        if found:
            return found
    return None


def describe(names: list[str], searxng_url: str | None) -> list[dict[str, Any]]:
    """把本次可用的搜索工具按后端分组，附上网络说明。"""
    groups: dict[str, dict[str, Any]] = {}
    for name in sorted(set(names)):
        b = match_backend(name)
        if b is None:
            continue
        g = groups.get(b.key)
        if g is None:
            endpoint = b.endpoint
            kind = b.kind
            if b.key == "searxng" and searxng_url:
                # 这个地址会显示出来、写进诊断信息，不能带账号密码和参数
                endpoint = origin_of(searxng_url) or b.endpoint
                if not is_private_host(searxng_url):
                    kind = "cloud"
            g = groups[b.key] = {
                "key": b.key,
                "label": b.label,
                "kind": kind,
                "kind_label": KIND_LABELS.get(kind, kind),
                "endpoint": endpoint,
                "intranet": bool(endpoint and is_private_host(endpoint)),
                "region": b.region,
                "region_label": REGION_LABELS.get(b.region, b.region),
                "domestic": b.domestic,
                "foreign": b.foreign,
                "note": b.note,
                "tools": [],
            }
        g["tools"].append(name)
    order = {b.key: i for i, b in enumerate(BACKENDS)}
    return sorted(groups.values(), key=lambda g: order.get(g["key"], 99))


def _service_row(
    key: str, label: str, endpoint: str | None, tools: list[str], note: str
) -> dict[str, Any]:
    intranet = bool(endpoint and is_private_host(endpoint))
    kind = "unknown" if not endpoint else "self_hosted" if intranet else "cloud"
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "endpoint": endpoint,
        "intranet": intranet,
        "region": "none",
        "region_label": REGION_LABELS["none"],
        "domestic": "—",
        "foreign": "—",
        "note": note,
        "tools": tools,
    }


def describe_services(
    names: list[str],
    mcp_servers: list[dict[str, Any]],
    providers: dict[str, dict[str, Any]],
    kb_enabled: bool,
) -> list[dict[str, Any]]:
    """搜索后端以外还要联网的服务：MCP、知识库的嵌入和重排、认不出后端的搜索工具。"""
    rows = []
    mcp_tools: set[str] = set()
    for server in mcp_servers:
        tools = [t["name"] for t in server.get("tools") or []]
        mcp_tools.update(tools)
        # 停用的不用测；已经按后端列出来的（比如 GitHub API）不重复
        if server.get("state") == "disabled" or any(match_backend(t) for t in tools):
            continue
        name = server["name"]
        endpoint = server.get("endpoint") or next(
            (url for key, url in KNOWN_MCP_ENDPOINTS.items() if key in name.lower()),
            None,
        )
        note = (
            "MCP 服务，这里只检测服务器能不能连上它的接口"
            if endpoint
            else "在服务器本机运行的 MCP 服务，不知道它要访问哪里，看它的调用记录是否失败"
        )
        rows.append(
            _service_row(f"mcp:{name}", f"{name}（MCP）", endpoint, tools, note)
        )
    if kb_enabled:
        for provider_id, info in providers.items():
            kind = info.get("kind")
            if kind not in ("embedding", "rerank"):
                continue
            title = "知识库嵌入模型" if kind == "embedding" else "知识库重排模型"
            rows.append(
                _service_row(
                    f"kb:{provider_id}",
                    title,
                    origin_of(info.get("api_base")),
                    [info.get("model") or provider_id],
                    "知识库每次检索都要用，连不上时结果会变差",
                )
            )
    others = sorted(
        n for n in set(names) if not match_backend(n) and n not in mcp_tools
    )
    if others:
        rows.append(
            _service_row(
                "other",
                "其他搜索工具",
                None,
                others,
                "DeepDeep 不知道这些工具访问哪里，看它们的调用记录是否失败",
            )
        )
    return rows


async def _probe(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    """能连上就算通：任何 HTTP 状态码（401、404……）都说明网络是通的。"""
    started = time.monotonic()
    try:
        async with session.get(url, allow_redirects=False) as resp:
            status = resp.status
        return {
            "ok": True,
            "status": status,
            "ms": int((time.monotonic() - started) * 1000),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": "超时",
            "ms": int((time.monotonic() - started) * 1000),
        }
    except aiohttp.ClientError as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "ms": int((time.monotonic() - started) * 1000),
        }


async def check_searxng(
    session: aiohttp.ClientSession, base_url: str
) -> dict[str, Any]:
    """查 SearXNG。闲置后第一次查询可能所有引擎都超时，这时再查一次，免得误报。"""
    first = await _query_searxng(session, base_url)
    if first.get("ok") and not first["results"] and _all_timed_out(first["failed"]):
        return await _query_searxng(session, base_url)
    return first


def _all_timed_out(failed: list[dict[str, str]]) -> bool:
    return bool(failed) and all("timeout" in x["reason"].lower() for x in failed)


async def _query_searxng(
    session: aiohttp.ClientSession, base_url: str
) -> dict[str, Any]:
    """真实查一次 SearXNG（免费），看哪些引擎还能用。"""
    url = f"{base_url}/search?" + urlencode({"q": "DeepSeek", "format": "json"})
    started = time.monotonic()
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=SEARXNG_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                hint = (
                    "（检查 SearXNG 是否开启了 JSON 格式）"
                    if resp.status == 403
                    else ""
                )
                return {"ok": False, "error": f"HTTP {resp.status}{hint}"}
            data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "超时"}
    except (aiohttp.ClientError, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    if not isinstance(data, dict):
        return {"ok": False, "error": "返回的不是 JSON 对象"}
    results = data.get("results") or []
    working: dict[str, int] = {}
    for r in results:
        for e in r.get("engines") or [r.get("engine")]:
            if e:
                working[e] = working.get(e, 0) + 1
    failed = [
        {"engine": str(item[0]), "reason": str(item[1]) if len(item) > 1 else ""}
        for item in data.get("unresponsive_engines") or []
        if isinstance(item, (list, tuple)) and item
    ]
    # 测试词是 DeepSeek：结果里连这个词都没有，说明剩下的引擎在乱给结果
    relevant = sum(
        1
        for r in results
        if "deepseek"
        in f"{r.get('title', '')} {r.get('content', '')} {r.get('url', '')}".lower()
    )
    return {
        "ok": True,
        "ms": int((time.monotonic() - started) * 1000),
        "results": len(results),
        "relevant": relevant,
        "working": sorted(working, key=lambda e: -working[e]),
        "failed": failed,
    }


async def run_check(
    groups: list[dict[str, Any]], searxng_url: str | None
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
    # 和搜索请求走同一条路（跟随 AstrBot 的代理）；SearXNG 是自建的，直接连
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        site_tasks = {
            (region, label): _probe(session, url)
            for region, sites in PROBE_SITES.items()
            for label, url in sites
        }
        endpoints = sorted({g["endpoint"] for g in groups if g.get("endpoint")})
        ep_tasks = {ep: _probe(session, ep) for ep in endpoints}
        site_res = await asyncio.gather(*site_tasks.values())
        ep_res = await asyncio.gather(*ep_tasks.values())
    searx = None
    if searxng_url:
        async with aiohttp.ClientSession(timeout=timeout) as direct:
            searx = await check_searxng(direct, searxng_url)
    sites: dict[str, list[dict[str, Any]]] = {"domestic": [], "foreign": []}
    for (region, label), res in zip(site_tasks, site_res, strict=True):
        sites[region].append({"label": label, **res})
    summary = {
        region: sum(1 for s in items if s["ok"]) for region, items in sites.items()
    }
    return {
        "checked_at": time.time(),
        "sites": sites,
        "server_domestic_ok": summary["domestic"] > 0,
        "server_foreign_ok": summary["foreign"] > 0,
        "proxy": bool(os.environ.get("https_proxy") or os.environ.get("http_proxy")),
        "endpoints": dict(zip(ep_tasks, ep_res, strict=True)),
        "searxng": searx,
    }
