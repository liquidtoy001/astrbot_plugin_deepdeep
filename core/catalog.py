"""设置页「可用的搜索工具」：DeepDeep 认识的所有搜索工具、各自擅长什么、花不花钱，以及当前状态。

只读配置和已注册的工具，不发请求。状态的来源：
- DeepDeep 自带的 DeepSeek / Tavily：Key、开关、本月上限、Key 是否失效
- AstrBot 内置网页搜索的各家：Key 填没填、是不是当前选中的提供商（同一时间只能选一家）
- 插件和 MCP：有没有安装（工具是否已注册、是否启用）、配置检查和健康检测的结论
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from . import providers
from .providers import CATALOG, FETCH_TOOL, OWN_TOOLS
from .providers import Listing as Entry

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
    if tool == FETCH_TOOL:
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


def _price_field(
    key: str, prices: dict[str, tuple[float, str]], billing: str
) -> dict[str, Any]:
    """表里没有价格的服务：用户可以自己填每千次价格。"""
    amount, cur = prices.get(key, (None, billing))
    return {"key": key, "amount": amount, "currency": cur}


def _service_fields(
    e: Entry,
    deepseek: dict[str, Any],
    tavily: dict[str, Any],
    extra: dict[str, dict[str, Any]],
    prices: dict[str, tuple[float, str]],
    billing: str,
) -> dict[str, Any]:
    """设置页服务卡片要的东西：注册地址、评测成绩，DeepDeep 自带的服务还有开关和填 Key 的地方。"""
    provider = providers.of_listing(e.key)
    out: dict[str, Any] = {
        "signup_url": provider.signup_url,
        # 设置页的筛选：免费 / 付费、国内 / 国外
        "free": provider.free,
        "free_quota": bool(provider.pricing and provider.pricing.free_monthly),
        "region": provider.networks[0].region if provider.networks else "",
    }
    if not (provider.free or provider.pricing or provider.key == "deepseek"):
        out["price"] = _price_field(provider.key, prices, billing)
    if e.source != "own":
        return out
    state = {"deepseek": deepseek, "tavily": tavily, **extra}.get(e.key, {})
    # 还没有在真实接口上跑过
    out["untested"] = provider.api is not None and not provider.api.tested
    out["enabled_field"] = provider.enabled_field
    out["enabled"] = bool(state.get("enabled"))
    out["key_input"] = {
        "service": provider.key,
        "masked": state.get("masked") or "",
        "source": state.get("source") or "",
        # 只有存在 DeepDeep 自己配置里的 Key 才能在这里清除；复用 AstrBot 的不归这里管
        "own": state.get("source") == "插件配置",
    }
    return out


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
    extra: dict[str, dict[str, Any]] | None = None,
    prices: dict[str, tuple[float, str]] | None = None,
    billing: str = "cny",
) -> list[dict[str, Any]]:
    """``deepseek``：{"enabled", "configured"}；``tavily``：TavilyGate.status()；
    ``registered``：已注册的工具 {名字: 是否启用}；``request_tools``：会出现在请求里的工具（含内置网页搜索）；
    ``setup_status``：配置检查给出的 {工具名: missing/warn}；``health``：健康检测 {工具名: {...}}；
    ``search_tools``：路由认出的搜索类工具（目录里没有的也列出来）；
    ``extra``：ExtraSearch.status()。"""
    extra = extra or {}
    prices = prices or {}
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
        if not tools and not e.show_when_missing:
            continue
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
        elif e.source == "own":
            state = extra.get(e.key, {})
            failure = state.get("last_failure")
            if not state.get("configured"):
                status, detail = "no_key", "在这里填写 Key 就能用"
            elif not state.get("enabled"):
                status, detail = "off", "已填 Key，打开开关后才提供给模型"
            elif failure:
                status = "unstable"
                detail = (
                    f"最近一次调用失败：{failure['label']}（HTTP {failure['status']}）"
                )
            else:
                status = _health_status(tools, health) or "ok"
        elif e.source == "builtin":
            filled = _filled(ps.get(e.builtin.key_field))
            if current == e.builtin.id:
                if filled:
                    status = _health_status(tools, health) or "active"
                else:
                    status = "missing"
                    detail = f"当前选中了它，但没有填写 Key，内置搜索一调用就会报错：在 {WHERE_BUILTIN}填写"
            elif filled:
                status = "ready"
                detail = (
                    "DeepDeep 的 Tavily 正在用这个 Key"
                    if e.builtin.id == "tavily" and tavily.get("usable")
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
                **_service_fields(e, deepseek, tavily, extra, prices, billing),
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
                "price": _price_field(f"tool:{t}", prices, billing),
            }
        )
    out.sort(key=lambda x: x["_rank"])
    for x in out:
        del x["_rank"]
    return out
