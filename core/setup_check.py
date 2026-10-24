"""配置检查：各搜索工具有没有配好 Key，没配好时怎么补。

只读配置，不发请求。结果用在面板顶部和 /deepdeep stats，帮新用户发现“工具在列表里，
但一调用就报错”的情况（比如 Grok 插件没填 Key、内置网页搜索选了 SerpApi 却没填 SerpApi 的 Key）。

状态：ok 已配置；missing 缺少必需的配置，调用会失败；warn 建议补上（可选功能用不了）；info 说明。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# AstrBot 内置网页搜索：提供商 -> (显示名, 读取 Key 的配置项)。每家各读各的，不会互相复用
BUILTIN_PROVIDERS: dict[str, tuple[str, str]] = {
    "tavily": ("Tavily", "websearch_tavily_key"),
    "bocha": ("博查", "websearch_bocha_key"),
    "brave": ("Brave", "websearch_brave_key"),
    "firecrawl": ("Firecrawl", "websearch_firecrawl_key"),
    "baidu_ai_search": ("百度 AI 搜索", "websearch_baidu_app_builder_key"),
    "exa": ("Exa", "websearch_exa_key"),
    "anysearch": ("AnySearch", "websearch_anysearch_key"),
    "deepseek": ("DeepSeek", "websearch_deepseek_key"),
    "serpapi": ("SerpApi", "websearch_serpapi_key"),
}

# 插件配置里代表“主要 API Key”的字段名；全部为空时视为没配好
PRIMARY_KEY_RE = re.compile(r"^(api_?key|apikey|key|access_?key)$", re.IGNORECASE)
# 其他凭据字段：为空只提醒，不算错误（往往只影响某个可选功能）
SECONDARY_KEY_RE = re.compile(r"(api_?key|token|secret|cookie)$", re.IGNORECASE)
SKIP_FIELD_RE = re.compile(r"(interval|ttl|minutes|seconds|limit|count)", re.IGNORECASE)
# 必需的服务地址（如 SearXNG 地址）
SERVICE_URL_RE = re.compile(r"searxng.*url|url.*searxng", re.IGNORECASE)

TAVILY_SIGNUP_URL = "https://app.tavily.com"

STATUS_ORDER = {"missing": 0, "warn": 1, "info": 2, "ok": 3}


def _filled(value: Any) -> bool:
    if isinstance(value, list):
        return any(isinstance(v, str) and v.strip() for v in value)
    return isinstance(value, str) and bool(value.strip())


def scan_fields(config: Any, prefix: str = "") -> dict[str, list[tuple[str, bool]]]:
    """找出配置里的凭据和服务地址字段。只记录“填了没有”，不读取具体的值。"""
    found: dict[str, list[tuple[str, bool]]] = {
        "primary": [],
        "secondary": [],
        "url": [],
    }
    if not isinstance(config, dict):
        return found
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            sub = scan_fields(value, path)
            for k in found:
                found[k] += sub[k]
            continue
        name = str(key)
        if not isinstance(value, (str, list)) or SKIP_FIELD_RE.search(name):
            continue
        if SERVICE_URL_RE.search(name):
            found["url"].append((path, _filled(value)))
        elif PRIMARY_KEY_RE.match(name):
            found["primary"].append((path, _filled(value)))
        elif SECONDARY_KEY_RE.search(name):
            found["secondary"].append((path, _filled(value)))
    return found


def check_plugin(plugin: str, tools: list[str], config: Any) -> dict[str, Any]:
    label = plugin.removeprefix("astrbot_plugin_")
    item = {"key": f"plugin:{plugin}", "title": f"插件 {label}", "tools": sorted(tools)}
    if config is None:
        return {**item, "status": "info", "detail": "没有找到插件配置文件，无法检查"}
    f = scan_fields(config)
    missing_urls = [p for p, ok in f["url"] if not ok]
    if missing_urls:
        return {
            **item,
            "status": "missing",
            "detail": f"没有填写服务地址：{'、'.join(missing_urls)}",
            "fix": f"在 AstrBot「插件」页打开 {label} 的配置填写",
        }
    if f["primary"] and not any(ok for _, ok in f["primary"]):
        return {
            **item,
            "status": "missing",
            "detail": "没有填写 API Key，这些工具一调用就会报错",
            "fix": f"在 AstrBot「插件」页打开 {label} 的配置填写 Key；暂时不用的话可以在 AstrBot 里关掉这些工具",
        }
    empty = [p.rsplit(".", 1)[-1] for p, ok in f["secondary"] if not ok]
    if empty:
        names = "、".join(dict.fromkeys(empty))
        return {
            **item,
            "status": "info",
            "detail": f"可选的凭据没有填写：{names}（只影响相关的可选功能）",
        }
    return {**item, "status": "ok", "detail": "已配置"}


def check_builtin(
    ps: dict[str, Any], tools_by_provider: dict[str, list[str]]
) -> dict[str, Any]:
    item = {"key": "builtin", "title": "AstrBot 内置网页搜索"}
    if not ps.get("web_search"):
        return {
            **item,
            "status": "info",
            "detail": "未开启（可选）",
            "fix": "需要时在 AstrBot「配置 → 网页搜索」开启，并选一家（Tavily、SerpApi、博查等）",
            "tools": [],
        }
    provider = str(ps.get("websearch_provider") or "")
    label, field = BUILTIN_PROVIDERS.get(provider, (provider or "未知", ""))
    tools = tools_by_provider.get(provider, [])
    if not field:
        return {
            **item,
            "status": "info",
            "detail": f"当前提供商：{label}",
            "tools": tools,
        }
    if _filled(ps.get(field)):
        return {
            **item,
            "status": "ok",
            "detail": f"当前提供商：{label}，Key 已填写",
            "tools": tools,
        }
    return {
        **item,
        "status": "missing",
        "detail": f"当前提供商是 {label}，但没有填写 {label} 的 Key，内置搜索工具一调用就会报错",
        "fix": f"在 AstrBot「配置 → 网页搜索」填写 {label} 的 Key",
        "tools": tools,
    }


def check_deepseek(
    key_source: tuple[str | None, str], own_enabled: bool
) -> dict[str, Any]:
    item = {
        "key": "deepseek",
        "title": "DeepDeep · DeepSeek 搜索和抓取",
        "tools": ["deepdeep_web_search", "deepdeep_web_fetch"],
    }
    if not own_enabled:
        return {**item, "status": "info", "detail": "已在设置中关闭"}
    key, source = key_source
    if key:
        return {**item, "status": "ok", "detail": f"Key 来源：{source}"}
    return {
        **item,
        "status": "missing",
        "detail": "没有找到 DeepSeek API Key，DeepDeep 的搜索不可用（会自动引导模型改用其他搜索）",
        "fix": "在插件配置里填写 DeepSeek API Key，或在 AstrBot 里添加 api_base 为 https://api.deepseek.com 的 DeepSeek 提供商（会自动复用它的 Key）",
    }


def check_tavily(status: dict[str, Any]) -> dict[str, Any]:
    item = {
        "key": "tavily",
        "title": "DeepDeep · Tavily 搜索",
        "tools": ["deepdeep_tavily_search"],
    }
    if not status.get("enabled"):
        return {**item, "status": "info", "detail": "已在设置中关闭"}
    if not status.get("configured"):
        return {
            **item,
            "status": "warn",
            "detail": "没有找到 Tavily Key：技术类问题会改由 DeepSeek 等其他工具负责",
            "fix": "在 AstrBot「配置 → 网页搜索」选 Tavily 并填写 Key（填完可以切回原来的提供商，Key 会保留），"
            "或在 AstrBot 插件配置页给 DeepDeep 单独填写「Tavily API Key」。Tavily 免费额度每月 1000 次",
        }
    failure = status.get("last_failure")
    if failure:
        code = failure.get("status")
        which = (
            f"第 {failure['position']} 个 Key（{failure.get('masked', '')}）"
            if (failure.get("total") or 1) > 1
            else ""
        )
        fix = (
            f"到 {TAVILY_SIGNUP_URL} 登录，复制有效的 Key，"
            "填到 AstrBot「配置 → 网页搜索」的 Tavily Key，或插件配置里的「Tavily API Key」"
            if code == 401
            else f"等下个月额度重置，或到 {TAVILY_SIGNUP_URL} 升级套餐、换一个 Key。"
            "可以在「高级设置 → Tavily 每月调用上限」里设一个比额度小的数，用完前自动停用"
        )
        return {
            **item,
            # 按失败原因区分，忽略“Key 无效”后，“额度用完”仍会提醒
            "key": f"tavily:http_{code}",
            "status": "warn",
            "detail": f"最近一次调用失败：{which}{failure.get('label')}（HTTP {code}）。"
            + (
                status.get("reason") or "已暂停提供 Tavily，技术问题改由其他搜索负责"
                if failure.get("all_failed")
                else "还有其他 Key 可用，暂时不影响搜索"
            ),
            "fix": fix,
            "url": TAVILY_SIGNUP_URL,
        }
    if not status.get("usable"):
        return {
            **item,
            "status": "info",
            "detail": status.get("reason") or "本次不提供",
        }
    detail = f"Key 来源：{status.get('source')}"
    if status.get("usage_text"):
        detail += f"；{status['usage_text']}"
    return {**item, "status": "ok", "detail": detail}


def check_model_search(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对话模型内置的联网搜索（只看已启用的提供商）。"""
    out = []
    for m in models:
        if not m.get("enabled", True):
            continue
        pid = m.get("provider_id") or "?"
        item = {
            "key": f"model:{pid}",
            "title": f"对话模型 {pid} · 内置联网搜索",
            "tools": [],
            "url": "",
        }
        where = f"在 AstrBot「提供商」里编辑 {pid}"
        if m.get("tools_broken"):
            out.append(
                {
                    **item,
                    "status": "warn",
                    "detail": f"开启了{m.get('label')}。AstrBot 的说明是开启后函数工具会失效，"
                    "DeepDeep 和其他搜索工具可能都调用不了",
                    "fix": f"{where}，关闭「启用原生搜索功能」，联网搜索交给 DeepDeep；"
                    "想保留 Gemini 自己搜索的话，可以关掉 DeepDeep 和其他搜索工具，免得白占上下文",
                }
            )
            continue
        if m.get("method") == "gemini_url":
            detail = (
                f"开启了{m.get('label')}：模型能自己读取网页，和网页读取工具作用重复"
            )
            fix = f"一般不用处理；想统一由 DeepDeep 读取网页的话，{where}，关闭「启用 URL 上下文」"
        else:
            detail = (
                f"开启了{m.get('label')}：模型会自己联网搜索，和搜索工具作用重复"
                + ("，而且会额外计费" if m.get("extra_cost") else "")
                + "。这部分搜索 DeepDeep 看不到，也不计入费用和调用日志"
            )
            fix = f"想统一由 DeepDeep 管理搜索的话，{where}，关掉内置搜索；两边都开也能用，只是可能搜两遍"
        out.append({**item, "status": "info", "detail": detail, "fix": fix})
    return out


def load_plugin_config(config_dir: Path | None, plugin: str) -> Any:
    if config_dir is None:
        return None
    path = config_dir / f"{plugin}_config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def build(
    *,
    deepseek: dict[str, Any],
    tavily: dict[str, Any],
    builtin: dict[str, Any],
    plugins: list[dict[str, Any]],
    models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = [deepseek, tavily, builtin, *plugins, *(models or [])]
    items.sort(key=lambda x: STATUS_ORDER.get(x["status"], 9))
    tool_status: dict[str, str] = {}
    for it in items:
        if it["status"] in ("missing", "warn"):
            for t in it.get("tools", []):
                tool_status.setdefault(t, it["status"])
    return {
        "items": items,
        "tool_status": tool_status,
        "missing": sum(1 for it in items if it["status"] == "missing"),
        "warn": sum(1 for it in items if it["status"] == "warn"),
    }
