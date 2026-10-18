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
import re
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp


@dataclass(frozen=True, slots=True)
class Backend:
    key: str
    label: str
    patterns: tuple[str, ...]
    # cloud：云端 API；self_hosted：自建服务（请求从服务器发出）；local：不联网；server：从服务器直接访问网页
    kind: str
    endpoint: str | None
    # 服务商所在地：domestic / foreign / self / none
    region: str
    domestic: str
    foreign: str
    note: str = ""


# 按顺序匹配，第一个命中的生效
BACKENDS: tuple[Backend, ...] = (
    Backend(
        "deepdeep_fetch",
        "DeepSeek 网页抓取",
        ("deepdeep_web_fetch", "deepseek_fetch_web_page"),
        "cloud",
        "https://api.deepseek.com",
        "domestic",
        "能读",
        "能读",
        "由 DeepSeek 的服务器去抓网页，服务器自己打不开的网站也能读",
    ),
    Backend(
        "deepseek",
        "DeepSeek 搜索",
        ("deepdeep_web_search", "web_search_deepseek", "*deepseek*"),
        "cloud",
        "https://api.deepseek.com",
        "domestic",
        "好",
        "好",
        "搜索在 DeepSeek 的服务器上完成，中文内容覆盖最好；服务器只需能连上 api.deepseek.com",
    ),
    Backend(
        "github",
        "GitHub API",
        (
            "*github*",
            "search_repositories",
            "search_code",
            "search_issues",
            "search_pull_requests",
        ),
        "cloud",
        "https://api.github.com",
        "foreign",
        "—",
        "好",
        "只搜 GitHub；国内服务器访问 GitHub 可能不稳定",
    ),
    Backend(
        "searxng_fetch",
        "SearXNG 插件 · 网页抓取",
        ("searxng_web_fetch_url",),
        "server",
        None,
        "self",
        "取决于服务器",
        "取决于服务器",
        "从你的服务器直接打开网页，服务器访问不了的网站读不到",
    ),
    Backend(
        "searxng",
        "SearXNG（自建）",
        ("searxng_*",),
        "self_hosted",
        None,
        "self",
        "取决于引擎",
        "取决于引擎",
        "SearXNG 从你的服务器向各搜索引擎发请求：服务器 IP 被拦截（验证码、限流）的引擎会没有结果，"
        "只剩少数引擎时结果可能完全不相关。点“检测”可以看到哪些引擎还能用",
    ),
    Backend(
        "serpapi",
        "SerpApi（Google 结果）",
        ("*serpapi*",),
        "cloud",
        "https://serpapi.com",
        "foreign",
        "较好",
        "好",
        "返回 Google 的搜索结果，国内外网站都覆盖；接口在国外",
    ),
    Backend(
        "tavily",
        "Tavily",
        ("*tavily*",),
        "cloud",
        "https://api.tavily.com",
        "foreign",
        "一般",
        "好",
        "以英文网站为主，中文网站覆盖一般；接口在国外。2026-10 评测里技术类问题（编程、版本、漏洞、文档）表现最好，但不带发布日期",
    ),
    Backend(
        "brave",
        "Brave Search",
        ("*brave*",),
        "cloud",
        "https://api.search.brave.com",
        "foreign",
        "一般",
        "好",
        "以英文网站为主；接口在国外",
    ),
    Backend(
        "exa",
        "Exa",
        ("*_exa", "exa_*", "exa-*", "web_fetch_exa"),
        "cloud",
        "https://api.exa.ai",
        "foreign",
        "一般",
        "好",
        "语义搜索，以英文网站为主；接口在国外",
    ),
    Backend(
        "firecrawl",
        "Firecrawl",
        ("*firecrawl*",),
        "cloud",
        "https://api.firecrawl.dev",
        "foreign",
        "一般",
        "好",
        "由 Firecrawl 抓取网页；接口在国外",
    ),
    Backend(
        "anysearch",
        "AnySearch",
        ("*anysearch*",),
        "cloud",
        "https://api.anysearch.com",
        "foreign",
        "一般",
        "好",
        "",
    ),
    Backend(
        "bocha",
        "博查搜索",
        ("*bocha*",),
        "cloud",
        "https://api.bochaai.com",
        "domestic",
        "好",
        "一般",
        "以中文网站为主；接口在国内",
    ),
    Backend(
        "baidu",
        "百度 AI 搜索",
        ("*baidu*",),
        "cloud",
        "https://qianfan.baidubce.com",
        "domestic",
        "好",
        "一般",
        "以中文网站为主；接口在国内",
    ),
    Backend(
        "kimi",
        "Kimi（月之暗面）",
        ("*kimi*",),
        "cloud",
        "https://api.moonshot.cn",
        "domestic",
        "好",
        "较好",
        "",
    ),
    Backend(
        "grok",
        "Grok（xAI）",
        ("*grok*",),
        "cloud",
        "https://api.x.ai",
        "foreign",
        "一般",
        "好",
        "包含 X（推特）上的实时内容；接口在国外，插件里可以配置代理或中转地址",
    ),
    Backend(
        "gemini",
        "Gemini（Google）",
        ("*gemini*",),
        "cloud",
        "https://generativelanguage.googleapis.com",
        "foreign",
        "较好",
        "好",
        "接口在国外，国内服务器通常无法直连",
    ),
    Backend(
        "minimax",
        "MiniMax",
        ("mmx_*",),
        "cloud",
        "https://api.minimaxi.com",
        "domestic",
        "好",
        "一般",
        "",
    ),
    Backend(
        "kb",
        "本地知识库",
        ("astr_kb_search",),
        "local",
        None,
        "none",
        "—",
        "—",
        "只查 AstrBot 里的知识库，不联网",
    ),
)

KIND_LABELS = {
    "cloud": "云端 API",
    "self_hosted": "自建服务（内网）",
    "server": "服务器直连",
    "local": "本机",
}
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
                endpoint = searxng_url
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
    async with aiohttp.ClientSession(timeout=timeout) as session:
        site_tasks = {
            (region, label): _probe(session, url)
            for region, sites in PROBE_SITES.items()
            for label, url in sites
        }
        endpoints = sorted({g["endpoint"] for g in groups if g.get("endpoint")})
        ep_tasks = {ep: _probe(session, ep) for ep in endpoints}
        searx_task = check_searxng(session, searxng_url) if searxng_url else None
        site_res = await asyncio.gather(*site_tasks.values())
        ep_res = await asyncio.gather(*ep_tasks.values())
        searx = await searx_task if searx_task else None
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
        "endpoints": dict(zip(ep_tasks, ep_res, strict=True)),
        "searxng": searx,
    }
