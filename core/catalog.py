"""设置页「可用的搜索工具」：DeepDeep 认识的所有搜索工具、各自擅长什么、花不花钱，以及当前状态。

只读配置和已注册的工具，不发请求。状态的来源：
- DeepDeep 自带的 DeepSeek / Tavily：Key、开关、本月上限、Key 是否失效
- AstrBot 内置网页搜索的各家：Key 填没填、是不是当前选中的提供商（同一时间只能选一家）
- 插件和 MCP：有没有安装（工具是否已注册、是否启用）、配置检查和健康检测的结论
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from .router import OWN_TOOLS, TAVILY_TOOL


@dataclass(frozen=True, slots=True)
class Entry:
    key: str
    name: str
    # own：DeepDeep 自带；builtin：AstrBot 内置网页搜索；plugin：插件；mcp：MCP 服务；kb：知识库
    source: str
    strengths: str
    cost: str
    # 属于这一项的工具名规则
    patterns: tuple[str, ...] = ()
    # 内置网页搜索：提供商 ID 和 Key 的配置项
    provider: str = ""
    key_field: str = ""
    # 没装时怎么装
    install: str = ""


CATALOG: tuple[Entry, ...] = (
    Entry(
        "deepseek",
        "DeepSeek 搜索",
        "own",
        "中文资料覆盖最好，结果带发布日期、相关度和权威度，适合新闻和最新动态",
        "按次计费，每千次 ¥5（国际站 $0.75）",
        ("deepdeep_web_search", "deepdeep_web_fetch"),
    ),
    Entry(
        "tavily",
        "Tavily",
        "own",
        "英文和技术资料最好：编程、软件版本、安全漏洞、官方文档",
        "免费每月 1000 次",
        (TAVILY_TOOL,),
    ),
    Entry(
        "serpapi",
        "SerpApi（Google 结果）",
        "builtin",
        "返回 Google 的结果，国内外网站都覆盖，另有图片、学术、财经搜索",
        "免费每月 250 次",
        ("*serpapi*",),
        "serpapi",
        "websearch_serpapi_key",
    ),
    Entry(
        "bocha",
        "博查搜索",
        "builtin",
        "中文网站为主，接口在国内，国内服务器连接稳定",
        "按次计费",
        ("web_search_bocha",),
        "bocha",
        "websearch_bocha_key",
    ),
    Entry(
        "baidu_ai_search",
        "百度 AI 搜索",
        "builtin",
        "百度的搜索结果，中文网站覆盖广",
        "按次计费（有免费额度）",
        ("web_search_baidu",),
        "baidu_ai_search",
        "websearch_baidu_app_builder_key",
    ),
    Entry(
        "tavily_builtin",
        "Tavily（AstrBot 内置）",
        "builtin",
        "和 DeepDeep 的 Tavily 是同一个服务，另带网页读取",
        "免费每月 1000 次（和 DeepDeep 的 Tavily 共用 Key）",
        ("web_search_tavily", "tavily_extract_web_page"),
        "tavily",
        "websearch_tavily_key",
    ),
    Entry(
        "brave",
        "Brave Search",
        "builtin",
        "独立的搜索索引，英文网站为主，注重隐私",
        "有免费额度",
        ("web_search_brave",),
        "brave",
        "websearch_brave_key",
    ),
    Entry(
        "exa",
        "Exa",
        "builtin",
        "语义搜索：按意思找相似的文章、论文和网站，另带网页读取",
        "有免费额度",
        ("web_search_exa", "exa_get_contents"),
        "exa",
        "websearch_exa_key",
    ),
    Entry(
        "firecrawl",
        "Firecrawl",
        "builtin",
        "搜索的同时抓取网页全文，适合需要读整页内容的场景",
        "有免费额度",
        ("web_search_firecrawl", "firecrawl_extract_web_page"),
        "firecrawl",
        "websearch_firecrawl_key",
    ),
    Entry(
        "anysearch",
        "AnySearch",
        "builtin",
        "聚合多家搜索引擎的结果",
        "按次计费",
        ("web_search_anysearch",),
        "anysearch",
        "websearch_anysearch_key",
    ),
    Entry(
        "deepseek_builtin",
        "DeepSeek（AstrBot 内置）",
        "builtin",
        "AstrBot 4.29.1 起自带的基础版 DeepSeek 搜索，和 DeepDeep 用同一个账户计费",
        "按次计费，和 DeepDeep 共用 DeepSeek 账户",
        ("web_search_deepseek", "deepseek_fetch_web_page"),
        "deepseek",
        "websearch_deepseek_key",
    ),
    Entry(
        "searxng",
        "SearXNG（web_searcher_pro 插件）",
        "plugin",
        "自建、不花钱，聚合多个搜索引擎，另有图片、视频、新闻、学术等分类搜索",
        "免费（需要自建 SearXNG）",
        ("searxng_*",),
        install="在 AstrBot 插件市场搜索 web_searcher_pro 安装，并自建 SearXNG",
    ),
    Entry(
        "kimi",
        "Kimi 联网搜索",
        "plugin",
        "月之暗面的联网搜索，中文资料好",
        "按次计费",
        ("kimi_web_search", "kimi_web_fetch"),
        install="在 AstrBot 插件市场搜索 kimi_web_search 安装",
    ),
    Entry(
        "grok",
        "Grok 搜索",
        "plugin",
        "包含 X（推特）上的实时内容，适合追热点",
        "按次计费",
        ("grok_web_search", "grok_web_fetch"),
        install="在 AstrBot 插件市场搜索 grok_web_search 安装",
    ),
    Entry(
        "gemini",
        "Gemini 搜索",
        "plugin",
        "借 Gemini 用 Google 搜索并总结",
        "有免费额度",
        ("gemini_search",),
        install="在 AstrBot 插件市场搜索 gemini_search 安装",
    ),
    Entry(
        "exa_plugin",
        "Exa（插件）",
        "plugin",
        "Exa 语义搜索的插件版",
        "有免费额度",
        ("exa-search", "web_fetch_exa"),
        install="在 AstrBot 插件市场搜索 exa_web_search 安装",
    ),
    Entry(
        "anysearch_plugin",
        "AnySearch（插件）",
        "plugin",
        "AnySearch 聚合搜索的插件版，支持批量搜索",
        "按次计费",
        ("anysearch_*",),
        install="在 AstrBot 插件市场搜索 anysearch 安装",
    ),
    Entry(
        "github",
        "GitHub 搜索（MCP）",
        "mcp",
        "直接搜 GitHub 上的仓库、代码、Issue 和 PR",
        "免费",
        (
            "search_repositories",
            "search_code",
            "search_issues",
            "search_pull_requests",
            "*github*",
        ),
        install="在 AstrBot「MCP」里添加 GitHub 官方 MCP 服务",
    ),
    Entry(
        "arxiv",
        "arXiv 论文（MCP）",
        "mcp",
        "搜索和下载 arXiv 论文",
        "免费",
        ("*paper*", "*arxiv*"),
        install="在 AstrBot「MCP」里添加 arXiv MCP 服务",
    ),
    Entry(
        "kb",
        "本地知识库",
        "kb",
        "只查 AstrBot 里上传的资料，不联网",
        "免费",
        ("astr_kb_search",),
        install="在 AstrBot「知识库」里创建并给人格启用",
    ),
)

SOURCE_LABELS = {
    "own": "DeepDeep 自带",
    "builtin": "AstrBot 内置网页搜索",
    "plugin": "插件",
    "mcp": "MCP",
    "kb": "知识库",
}

# 状态：(显示文字, 排序)。排前面的先展示
STATUS = {
    "active": ("当前在用", 0),
    "ok": ("可用", 0),
    "unstable": ("不稳定", 1),
    "missing": ("缺 Key", 1),
    "paused": ("暂停", 1),
    "ready": ("已填 Key，未选用", 2),
    "off": ("已关闭", 3),
    "disabled": ("已停用", 3),
    "no_key": ("没填 Key", 4),
    "not_installed": ("未安装", 5),
}

WHERE_BUILTIN = "AstrBot「配置 → 网页搜索」"


def _filled(value: Any) -> bool:
    if isinstance(value, list):
        return any(isinstance(v, str) and v.strip() for v in value)
    return isinstance(value, str) and bool(value.strip())


def entry_for(tool: str) -> Entry | None:
    lowered = tool.lower()
    for entry in CATALOG:
        if any(fnmatchcase(lowered, p) for p in entry.patterns):
            return entry
    return None


def label_for(tool: str) -> str:
    """工具名对应的通俗名字（偏好排序里显示），认不出来时返回工具名本身。"""
    if tool == "deepdeep_web_fetch":
        return "DeepSeek 读网页"
    entry = entry_for(tool)
    if entry is None:
        return tool
    if any(k in tool for k in ("fetch", "extract", "get_contents")):
        return f"{entry.name.split('（')[0]} 读网页"
    return entry.name


def grouped(names: list[str], disabled: bool) -> list[dict[str, Any]]:
    """搜索偏好里的一行行：同一个后端的多个工具合成一行（比如 SearXNG 的几个分类搜索），
    位置按其中排最前的那个；保存时用能匹配它们全部的规则（比如 searxng_*）。"""
    groups: dict[str, list[str]] = {}
    for n in names:
        e = entry_for(n)
        groups.setdefault(e.key if e else f"tool:{n}", []).append(n)
    out: list[dict[str, Any]] = []
    for tools in groups.values():
        e = entry_for(tools[0])
        pattern = None
        if e is not None and len(tools) > 1:
            pattern = next(
                (
                    p
                    for p in e.patterns
                    if all(fnmatchcase(t.lower(), p) for t in tools)
                ),
                None,
            )
        if pattern:
            out.append(
                {
                    "name": pattern,
                    "label": e.name,
                    "note": f"{len(tools)} 个工具 · {e.cost}",
                    "tools": tools,
                    "disabled": disabled,
                }
            )
            continue
        for t in tools:
            te = entry_for(t)
            out.append(
                {
                    "name": t,
                    "label": label_for(t),
                    "note": te.cost if te else "",
                    "tools": [t],
                    "disabled": disabled,
                }
            )
    return out


def _health_status(tools: list[str], health: dict[str, dict[str, Any]]) -> str:
    verdicts = {
        health[t]["verdict"]
        for t in tools
        if t in health and not health[t].get("ignored")
    }
    return "unstable" if verdicts & {"broken", "degraded"} else ""


def build(
    *,
    deepseek: dict[str, Any],
    tavily: dict[str, Any],
    provider_settings: dict[str, Any],
    registered: dict[str, bool],
    request_tools: list[str],
    setup_status: dict[str, str],
    health: dict[str, dict[str, Any]],
    search_tools: list[str] = (),
) -> list[dict[str, Any]]:
    """``deepseek``：{"enabled", "configured"}；``tavily``：WebApi.tavily_status()；
    ``registered``：已注册的工具 {名字: 是否启用}；``request_tools``：会出现在请求里的工具（含内置网页搜索）；
    ``setup_status``：配置检查给出的 {工具名: missing/warn}；``health``：健康检测 {工具名: {...}}；
    ``search_tools``：路由认出的搜索类工具（目录里没有的也列出来）。"""
    ps = provider_settings or {}
    current = str(ps.get("websearch_provider") or "") if ps.get("web_search") else ""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in CATALOG:
        tools = sorted(
            {
                t
                for t in [*registered, *request_tools]
                if any(fnmatchcase(t.lower(), p) for p in e.patterns)
            }
        )
        seen.update(tools)
        status, detail = "", ""
        if e.key == "deepseek":
            if not deepseek.get("enabled"):
                status, detail = "off", "已在设置中关闭"
            elif not deepseek.get("configured"):
                status, detail = (
                    "missing",
                    "没有找到 DeepSeek API Key：在插件配置里填写，或添加 DeepSeek 官方提供商",
                )
            else:
                status = _health_status(tools, health) or "ok"
        elif e.key == "tavily":
            if not tavily.get("enabled"):
                status, detail = "off", "已在设置中关闭"
            elif not tavily.get("configured"):
                status, detail = (
                    "no_key",
                    f"在 {WHERE_BUILTIN}填写 Tavily 的 Key（不用选它为当前提供商）",
                )
            elif not tavily.get("usable"):
                status, detail = "paused", tavily.get("reason") or ""
            else:
                status = _health_status(tools, health) or "ok"
                detail = tavily.get("usage_text") or ""
        elif e.source == "builtin":
            filled = _filled(ps.get(e.key_field))
            if current == e.provider:
                if filled:
                    status = _health_status(tools, health) or "active"
                else:
                    status = "missing"
                    detail = f"当前选中了它，但没有填写 Key，内置搜索一调用就会报错：在 {WHERE_BUILTIN}填写"
            elif filled:
                status = "ready"
                detail = (
                    "DeepDeep 的 Tavily 正在用这个 Key"
                    if e.provider == "tavily" and tavily.get("usable")
                    else f"内置网页搜索同一时间只能选一家，想用它的话在 {WHERE_BUILTIN}切换"
                )
            else:
                status, detail = "no_key", f"在 {WHERE_BUILTIN}选它并填写 Key"
        else:
            installed = [t for t in tools if t in registered or t in request_tools]
            if not installed:
                status, detail = "not_installed", e.install
            elif not any(registered.get(t, True) for t in installed):
                status, detail = "disabled", "工具已在 AstrBot 里停用"
            elif any(setup_status.get(t) == "missing" for t in installed):
                status, detail = "missing", "配置不完整，见概览页的配置检查"
            else:
                status = _health_status(installed, health) or "ok"
        if status == "unstable" and not detail:
            bad = next((health[t] for t in tools if t in health), {})
            detail = bad.get("reason", "")
        label, rank = STATUS[status]
        out.append(
            {
                "key": e.key,
                "name": e.name,
                "source": SOURCE_LABELS[e.source],
                "strengths": e.strengths,
                "cost": e.cost,
                "status": status,
                "status_label": label,
                "detail": detail,
                "tools": tools,
                "_rank": rank,
            }
        )
    # 认不出来的其他搜索工具也列出来，免得漏掉
    for t in sorted(set(search_tools) - seen - set(OWN_TOOLS)):
        status = (
            (
                "disabled"
                if not registered.get(t, True)
                else ("missing" if setup_status.get(t) == "missing" else "")
            )
            or _health_status([t], health)
            or "ok"
        )
        label, rank = STATUS[status]
        out.append(
            {
                "key": f"other:{t}",
                "name": t,
                "source": SOURCE_LABELS["plugin"],
                "strengths": "",
                "cost": "",
                "status": status,
                "status_label": label,
                "detail": "",
                "tools": [t],
                "_rank": rank,
            }
        )
    out.sort(key=lambda x: x["_rank"])
    for x in out:
        del x["_rank"]
    return out
