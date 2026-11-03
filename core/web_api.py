"""插件 WebUI（pages/dashboard）的后端接口。

页面通过 window.AstrBotPluginView（4.26–4.28 为 AstrBotPluginPage）bridge 调用，路由形如 /<插件名>/status，
只有登录了 AstrBot 管理面板的用户才能访问。
"""

from __future__ import annotations

import asyncio
import json
import platform
import secrets
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

from . import (
    catalog,
    connectivity,
    kbtrace,
    mcp_view,
    preference,
    route_view,
    setup_check,
)
from .credentials import mask_key
from .guide import TECH_DESC
from .health import VERDICT_LABELS, assess
from .model_pricing import PriceBook
from .pricing import CURRENCIES
from .providers import (
    BUILTIN_PROVIDER_TOOLS,
    BUILTIN_WEB_SEARCH_TOOLS,
    EXTRA_PROVIDERS,
    EXTRA_TOOLS,
    KB_TOOL,
    KEY_FIELDS,
    OWN_TOOLS,
    TAVILY_TOOL,
)
from .providers import get as get_provider
from .router import SELECTABLE_MODES, ToolRouter
from .uistate import UiState

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .astrbot_view import AstrBotView
    from .calllog import CallLog
    from .caps import ToolCaps
    from .credentials import Credentials, TavilyGate
    from .extra_search import ExtraSearch
    from .service import SearchService
    from .spending import Spending

# WebUI 允许修改的配置项及校验规则
EDITABLE_FIELDS: dict[str, tuple[type, Any, Any]] = {
    "own_tools_enabled": (bool, None, None),
    "tavily_enabled": (bool, None, None),
    "tavily_monthly_limit": (int, 0, 1000000),
    "tool_monthly_limits": (list, None, None),
    "custom_prices": (list, None, None),
    "panel_language": (str, None, None),
    "search_preference": (str, None, None),
    "search_order": (list, None, None),
    "search_lane_rules": (list, None, None),
    "routing_mode": (str, None, None),
    "billing_currency": (str, None, None),
    "daily_budget": (float, 0, 100000),
    "monthly_budget": (float, 0, 1000000),
    "exchange_rate": (float, 0, 100),
    "fallback_tool_patterns": (list, None, None),
    "specialist_rules": (list, None, None),
    "inject_search_guide": (bool, None, None),
    "record_queries": (bool, None, None),
    "circuit_threshold": (int, 1, 50),
    "circuit_cooldown_seconds": (int, 10, 86400),
    "call_log_enabled": (bool, None, None),
    "call_log_record_other_content": (bool, None, None),
    "kb_trim_min_score": (float, 0, 1),
    "kb_trim_chunk_chars": (int, 0, 20000),
    "model_prices": (list, None, None),
    "call_log_retention_days": (int, 1, 365),
    "default_count": (int, 1, 50),
    "max_count": (int, 1, 50),
    "min_rerank_score": (float, 0, 3),
    "include_content": (bool, None, None),
    "content_chars_per_result": (int, 0, 20000),
    "max_search_chars": (int, 500, 100000),
    "max_fetch_chars": (int, 500, 200000),
    "send_original_question": (bool, None, None),
    "cache_ttl_seconds": (int, 0, 86400),
    "request_timeout": (int, 3, 120),
    "max_retries": (int, 0, 5),
    "qps_limit": (float, 0.1, 5),
    **{p.enabled_field: (bool, None, None) for p in EXTRA_PROVIDERS},
}

MAX_KEY_LENGTH = 512

# 字符串配置项的可选值
STR_CHOICES = {
    "routing_mode": SELECTABLE_MODES,
    "billing_currency": tuple(CURRENCIES),
    "panel_language": ("auto", "zh", "en"),
    "search_preference": tuple(preference.PRESET_LABELS),
}

# 调用日志列表的时间范围（小时）
CALL_CATEGORIES = {"search", "kb", "mcp", "plugin", "builtin"}
RANGE_HOURS = {"1h": 1, "24h": 24, "7d": 168, "14d": 336, "30d": 720}
CALL_STATUSES = {
    "ok",
    "error",
    "blocked",
    "empty",
    "cache_hit",
    "interrupted",
    "running",
}

# 导出的文件留这么久，够浏览器下载完
EXPORT_KEEP_SECONDS = 3600


def _split_lines(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [v.strip() for v in value.splitlines() if v.strip()][:50]


def validate_settings(payload: Any) -> tuple[dict[str, Any], str | None]:
    """返回 (清洗后的配置, 错误信息)。未知字段直接忽略。"""
    if not isinstance(payload, dict):
        return {}, "请求体必须是 JSON 对象"
    clean: dict[str, Any] = {}
    for key, (typ, lo, hi) in EDITABLE_FIELDS.items():
        if key not in payload:
            continue
        value = payload[key]
        if typ is bool:
            if not isinstance(value, bool):
                return {}, f"{key} 必须是布尔值"
            clean[key] = value
        elif typ is str:
            allowed = STR_CHOICES[key]
            if value not in allowed:
                return {}, f"{key} 只能是 {', '.join(allowed)}"
            clean[key] = value
        elif typ is list:
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                return {}, f"{key} 必须是字符串列表"
            items = [v.strip() for v in value if v.strip()]
            # 一家服务的调用上限可能要列出它的所有工具
            longest = 4000 if key == "tool_monthly_limits" else 200
            if len(items) > 80 or any(len(v) > longest for v in items):
                return {}, f"{key} 条目过多或过长"
            clean[key] = items
        else:
            if isinstance(value, bool):
                return {}, f"{key} 必须是数字"
            try:
                num = typ(value)
            except (TypeError, ValueError, OverflowError):
                return {}, f"{key} 必须是数字"
            if not (lo <= num <= hi):
                return {}, f"{key} 需在 {lo} 到 {hi} 之间"
            clean[key] = num
    return clean, None


class WebHost(Protocol):
    """WebApi 对插件的全部依赖，都是公开成员。router 在保存设置后会换新，所以每次现取。"""

    config: Mapping[str, Any]
    data_dir: Path | None
    router: ToolRouter
    service: SearchService
    call_log: CallLog
    tool_caps: ToolCaps
    astrbot: AstrBotView
    credentials: Credentials
    tavily_gate: TavilyGate
    extra_search: ExtraSearch
    spending: Spending
    pricing: PriceBook

    @property
    def logging_enabled(self) -> bool: ...
    def settings_view(self) -> dict[str, Any]: ...
    async def update_settings(self, clean: dict[str, Any]) -> None: ...
    async def run_service_test(self, service: str) -> dict: ...
    def resolve_plugin(self, module: str) -> str | None: ...


class WebApi:
    def __init__(self, host: WebHost, plugin_name: str, version: str):
        self.host = host
        self.name = plugin_name
        self.version = version
        # 最近一次连通性检测的结果（只在内存里）
        self.last_network_check: dict[str, Any] | None = None
        data_dir = getattr(host, "data_dir", None)
        self.ui = UiState(Path(data_dir) / "ui_state.json" if data_dir else None)

    def register(self, context: Any) -> None:
        routes = [
            ("status", self.status, ["GET"], "插件状态"),
            ("usage", self.usage, ["GET"], "逐日用量"),
            ("activity", self.activity, ["GET"], "最近调用"),
            ("tools", self.tools, ["GET"], "搜索工具路由预览"),
            ("settings", self.save_settings, ["POST"], "保存设置"),
            ("keys", self.save_key, ["POST"], "保存或清除 Key"),
            ("test", self.test, ["POST"], "连接测试"),
            ("reset-circuit", self.reset_circuit, ["POST"], "解除暂停"),
            ("diagnostics", self.diagnostics, ["GET"], "诊断信息"),
            ("search-stats", self.search_stats, ["GET"], "各搜索工具用量"),
            ("overview", self.overview, ["GET"], "主力搜索工具和健康状况"),
            ("search-tools", self.search_tools, ["GET"], "可用的搜索工具和搜索偏好"),
            ("network", self.network, ["GET"], "各搜索工具的网络说明"),
            ("network-check", self.network_check, ["POST"], "检测连通性"),
            ("ignore", self.ignore, ["POST"], "忽略提醒"),
            ("unignore", self.unignore, ["POST"], "恢复提醒"),
            ("disable-tools", self.disable_tools, ["POST"], "停用工具"),
            ("enable-tools", self.enable_tools, ["POST"], "重新启用工具"),
            ("calls", self.calls, ["GET"], "调用日志列表"),
            ("call/<call_id>", self.call_detail, ["GET"], "调用日志详情"),
            ("call-facets", self.call_facets, ["GET"], "调用日志筛选项"),
            ("call-export", self.call_export, ["GET"], "导出调用日志"),
            ("call-clear", self.call_clear, ["POST"], "清空调用日志"),
            ("call-feedback", self.call_feedback, ["POST"], "标记知识库检索有没有用"),
            ("kb-stats", self.kb_stats, ["GET"], "知识库检索统计"),
            ("kb-status", self.kb_status, ["GET"], "知识库是否正常"),
            ("mcp", self.mcp, ["GET"], "MCP 服务和用量"),
            ("kb-toggle", self.kb_toggle, ["POST"], "开关知识库"),
            ("mcp-toggle", self.mcp_toggle, ["POST"], "开关 MCP 服务"),
            ("mcp-tool-toggle", self.mcp_tool_toggle, ["POST"], "开关 MCP 工具"),
            ("call-categories", self.call_categories, ["GET"], "调用日志分类"),
        ]
        for path, handler, methods, desc in routes:
            context.register_web_api(f"/{self.name}/{path}", handler, methods, desc)

    def unregister(self, context: Any) -> None:
        """AstrBot 卸载插件时不会清掉这些路由，留着的话旧面板还能改设置。"""
        routes = getattr(context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [r for r in routes if not r[0].startswith(f"/{self.name}/")]

    # ---------- 读取 ----------

    def today_tools(self) -> dict[str, Any]:
        """今天所有搜索工具的调用次数（来自调用日志），其中多少是 DeepSeek。读网页和搜索分开，
        本地知识库不算联网搜索。调用日志关闭时没有数据，面板改为只显示 DeepSeek 自己的统计。"""
        router = self.host.router
        out: dict[str, Any] = {
            kind: {"total": 0, "deepseek": 0, "cache_hit": 0, "empty": 0, "failed": 0}
            for kind in ("search", "fetch")
        }
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for item in self.host.call_log.tool_stats(midnight.timestamp()):
            tool = item["tool"]
            if router.search_category(tool) is None or tool == KB_TOOL:
                continue
            row = out["fetch" if router.is_fetch_like(tool) else "search"]
            # 不算被拦下的和还在进行的
            done = item["ok"] + item["empty"] + item["cache_hit"] + item["error"]
            row["total"] += done
            row["cache_hit"] += item["cache_hit"]
            row["empty"] += item["empty"]
            row["failed"] += item["error"]
            if tool in OWN_TOOLS:
                row["deepseek"] += done
        return {**out, "logging_enabled": self.host.logging_enabled}

    def spending_data(self) -> dict[str, Any]:
        """本月各付费服务的估算花费。没有 Key 时自带工具不算在用。"""
        names = self.search_tool_names()
        if not self.host.credentials.deepseek_key():
            names = [n for n in names if n not in OWN_TOOLS]
        return self.host.spending.report(names)

    def status_data(self) -> dict[str, Any]:
        svc = self.host.service
        key, source = self.host.credentials.deepseek_key_source()
        avail = svc.availability("search")
        return {
            "version": self.version,
            "available": avail.ok,
            "unavailable_code": avail.code,
            "unavailable_message": avail.message,
            "key": {"configured": bool(key), "masked": mask_key(key), "source": source},
            "tavily": self.host.tavily_gate.status(),
            "budget": svc.budget.snapshot(),
            "circuit": svc.breaker.snapshot(),
            "today": svc.stats.day(),
            "today_tools": self.today_tools(),
            "spending": self.spending_data(),
            "tool_caps": self.host.tool_caps.snapshot(),
            "settings": self.host.settings_view(),
        }

    async def status(self):
        return json_response(await asyncio.to_thread(self.status_data))

    async def usage(self):
        days = request.query.get("days", 30, type=int) or 30
        days = max(1, min(62, days))
        return json_response({"days": await asyncio.to_thread(self.usage_days, days)})

    def usage_days(self, days: int) -> list[dict[str, Any]]:
        """每天的搜索和抓取次数。DeepSeek 用它自己的计费统计（一直保留），其他工具来自调用日志
        （按保留天数）；两边不重复，调用日志里 DeepDeep 的 DeepSeek 工具不算。"""
        series = self.host.service.stats.series(days)
        by_day = {d["date"]: d for d in series}
        for d in series:
            d["other_search"] = d["other_fetch"] = 0
        router = self.host.router
        since = time.time() - days * 86400
        for row in self.host.call_log.daily_tool_counts(since):
            tool = row["tool"]
            day = by_day.get(row["day"])
            if (
                day is None
                or tool in OWN_TOOLS
                or tool == KB_TOOL
                or router.search_category(tool) is None
            ):
                continue
            key = "other_fetch" if router.is_fetch_like(tool) else "other_search"
            day[key] += row["n"]
        return series

    async def activity(self):
        limit = request.query.get("limit", 50, type=int) or 50
        return json_response(
            {"items": self.host.service.activity.recent(max(1, min(100, limit)))}
        )

    async def tools(self):
        # 预览可以带上尚未保存的匹配规则，方便边改边看
        # 规则用换行分隔，避免依赖 bridge 对数组参数的序列化方式
        router = self.host.router
        fb = _split_lines(request.query.get("fallback"))
        rules = _split_lines(request.query.get("specialists"))
        if fb or rules:
            router = ToolRouter(
                router.mode,
                fb or router.fallback_patterns,
                rules or [f"{p} = {d}" for p, d in router.specialist_rules],
                own_enabled=router.own_enabled,
                lane_rules=[f"{d} = {', '.join(p)}" for d, p in router.lane_rules],
            )
        return json_response(
            {
                "scenarios": await asyncio.to_thread(self.route_preview, router),
                "tools": router.preview(self.host.astrbot.current_tool_names()),
                "builtin_known": BUILTIN_WEB_SEARCH_TOOLS,
            }
        )

    def route_preview(self, router: ToolRouter) -> dict[str, Any]:
        """工具路由预览：正常、DeepSeek 故障、超出上限三种状态下，每类问题用什么。
        都是真实路由算出来的，不按模式写死。"""
        # 预览的“正常”和“超出上限”都是假设，不管现在是不是已经超了
        names = self.search_tool_names(ignore_spending=True)
        names = [n for n in names if router.search_category(n) is not None]
        current = self.host.astrbot.current_tool_names()
        others = len({n for n in current if router.search_category(n) is None})
        setup, health = self.problems("24h")
        status = route_view.ToolStatus(health, setup["tool_status"])
        has_key = bool(self.host.credentials.deepseek_key())
        over = frozenset(self.host.spending.blocked(names, assume_over=True))

        def build(available: bool, reason: str, removed: frozenset[str]) -> dict:
            s = router.scenario(
                names, available=available, reason=reason, removed=removed
            )
            own_failing = not available and any(n in OWN_TOOLS for n in s["kept"])
            lanes, missing = route_view.arrange(s["lanes"])
            rows = []
            for row in lanes:
                failed = own_failing and row["names"][0] in OWN_TOOLS
                kind = "fetch" if row["key"] == "fetch" else "search"
                advice = status.items(s["advice"][kind] if failed else [])
                used = {x["label"] for x in advice}
                groups = status.items(row["names"])
                rows.append(
                    {
                        "key": row["key"],
                        "plain": row["plain"],
                        "group": row["group"],
                        "first": groups[:1],
                        "failed": failed,
                        "advice": advice,
                        "rest": [
                            x
                            for x in groups[1:]
                            if x["label"] not in used
                            and not (own_failing and set(x["tools"]) & set(OWN_TOOLS))
                        ],
                    }
                )
            kept_own = any(n in OWN_TOOLS for n in s["kept"])
            hidden_own = any(n in OWN_TOOLS for n in s["hidden"])
            effect = (
                "advice"
                if kept_own and s["advice"]["search"]
                else "no_backup"
                if kept_own
                else "swap"
                if hidden_own
                else "no_own"
            )
            return {
                "rows": rows,
                "effect": effect,
                "missing": missing,
                "advice": status.items(s["advice"]["search"]),
                "removed": status.items(sorted(removed & set(names))),
            }

        states = {
            "ok": build(has_key, "" if has_key else "no_key", frozenset()),
            "down": build(
                False, "no_key" if not has_key else "circuit_open", frozenset()
            ),
            "budget": build(False, "no_key" if not has_key else "budget_monthly", over),
        }
        base = {r["key"]: r for r in states["ok"]["rows"]}
        fields = ("first", "failed", "advice", "rest")
        for name in ("down", "budget"):
            rows = states[name]["rows"]
            changed = [
                r
                for r in rows
                if r["key"] not in base
                or any(r[f] != base[r["key"]][f] for f in fields)
            ]
            states[name]["rows"] = changed
            states[name]["unchanged"] = len(rows) - len(changed)
            # 正常时有、这个状态下整行没有了
            states[name]["gone"] = [
                base[k]["plain"] for k in base if k not in {r["key"] for r in rows}
            ]
        return {
            "mode": router.mode,
            "states": states,
            "other_tools": others,
            "own_in_use": router.own_enabled and has_key,
        }

    def search_tool_names(
        self, include_pref_disabled: bool = False, ignore_spending: bool = False
    ) -> list[str]:
        """当前会出现在请求里的搜索类工具（关闭自带搜索时不含 DeepDeep 自己的）。
        ``include_pref_disabled`` 为 True 时保留搜索偏好里设为“不用”的（给偏好设置用）。"""
        router = self.host.router
        names = self.host.astrbot.current_tool_names()
        if not router.own_enabled:
            names = [n for n in names if n not in OWN_TOOLS]
        if TAVILY_TOOL in names and not self.host.tavily_gate.usable(names)[0]:
            names = [n for n in names if n != TAVILY_TOOL]
        # 与实际请求一致：没有 DeepSeek Key 且有别的工具时，自带工具不会给模型
        if self.host.service.availability("search").code == "no_key":
            hidden = router.plan(names, False, "no_key")
            names = [n for n in names if n not in OWN_TOOLS or n not in hidden]
        blocked = set(self.host.tool_caps.blocked(names))
        if not ignore_spending:
            blocked |= self.host.spending.blocked(names)
        if not include_pref_disabled:
            blocked |= router.disabled_tools(names)
        return sorted(
            {
                n
                for n in names
                if router.search_category(n) is not None and n not in blocked
            }
        )

    def health_data(self, range_key: str = "24h") -> dict[str, dict[str, Any]]:
        """各搜索工具的健康结论：{工具名: {...}}。只做提示，不影响路由。"""
        hours = RANGE_HOURS.get(range_key)
        since = time.time() - hours * 3600 if hours else None
        router = self.host.router
        out: dict[str, dict[str, Any]] = {}
        for row in self.host.call_log.tool_health(since):
            if router.search_category(row["tool"]) is None:
                continue
            # 知识库没查到内容、字面不重合都不算故障，只看调用有没有出错
            if row["tool"] == KB_TOOL:
                row = {**row, "empty": 0, "irrelevant": 0, "bad_streak": 0}
            item = {**row, **assess(row)}
            # 最近连续 3 次都有问题，即使整体比例不高也提醒
            if item["bad_streak"] >= 3 and item["verdict"] in ("ok", "unknown"):
                item["verdict"] = "degraded"
                item["reason"] += f"；最近连续 {item['bad_streak']} 次有问题"
            item["verdict_label"] = VERDICT_LABELS[item["verdict"]]
            prev = out.get(row["tool"])
            if prev is None or prev["total"] < item["total"]:
                out[row["tool"]] = item
        return out

    def plugin_search_tools(self) -> dict[str, list[str]]:
        """其他插件提供的搜索类工具：{插件名: [工具名, ...]}。"""
        out: dict[str, list[str]] = {}
        router = self.host.router
        tools = self.host.astrbot.llm_tools()
        for t in tools:
            name = getattr(t, "name", "")
            if not getattr(t, "active", True) or router.search_category(name) is None:
                continue
            module = getattr(t, "handler_module_path", None) or ""
            plugin = self.host.resolve_plugin(module) if module else None
            if plugin and plugin != self.name:
                out.setdefault(plugin, []).append(name)
        return out

    def config_dir(self) -> Path | None:
        data_dir = self.host.data_dir
        return Path(data_dir).parent.parent / "config" if data_dir else None

    def setup_data(self) -> dict[str, Any]:
        """各搜索工具的配置检查（只读配置，不发请求）。"""
        cdir = self.config_dir()
        plugins = [
            setup_check.check_plugin(p, tools, setup_check.load_plugin_config(cdir, p))
            for p, tools in sorted(self.plugin_search_tools().items())
        ]
        return setup_check.build(
            deepseek=setup_check.check_deepseek(
                self.host.credentials.deepseek_key_source(),
                self.host.router.own_enabled,
                bool(self.host.router.alternatives(self.search_tool_names(), "search")),
            ),
            tavily=setup_check.check_tavily(self.host.tavily_gate.status()),
            builtin=setup_check.check_builtin(
                self.host.astrbot.web_search_settings(), BUILTIN_PROVIDER_TOOLS
            ),
            plugins=[
                *plugins,
                *setup_check.check_extra(self.host.extra_search.status()),
            ],
            models=setup_check.check_model_search(self.host.astrbot.model_searches()),
        )

    def fixable(self, tools: list[str], registered: dict[str, bool]) -> list[str]:
        """可以一键停用的工具：已注册、正在启用、不是 DeepDeep 自己的（它们有自己的开关）。"""
        own = {*OWN_TOOLS, TAVILY_TOOL, *EXTRA_TOOLS}
        return [t for t in tools if registered.get(t) and t not in own]

    def problems(
        self, range_key: str = "24h"
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """配置检查和健康检测，并按“忽略”记录标出已忽略的项。"""
        setup = self.setup_data()
        health = self.health_data(range_key)
        registered = self.host.astrbot.registered_tools()
        current: dict[str, str] = {}
        for it in setup["items"]:
            if it["status"] in ("missing", "warn"):
                current[f"setup:{it['key']}"] = it["status"]
        for tool, h in health.items():
            if h["verdict"] in ("broken", "degraded"):
                current[f"health:{tool}"] = "bad"
        self.ui.prune(current)
        for it in setup["items"]:
            key = f"setup:{it['key']}"
            it["ignore_key"] = key
            it["ignored"] = key in current and self.ui.is_ignored(key, current[key])
            it["fixable_tools"] = self.fixable(it.get("tools", []), registered)
        for tool, h in health.items():
            key = f"health:{tool}"
            h["ignore_key"] = key
            h["ignored"] = key in current and self.ui.is_ignored(key, current[key])
            h["fixable"] = bool(self.fixable([tool], registered))
        active = [it for it in setup["items"] if not it["ignored"]]
        setup["missing"] = sum(1 for it in active if it["status"] == "missing")
        setup["warn"] = sum(1 for it in active if it["status"] == "warn")
        setup["tool_status"] = {
            t: s
            for t, s in setup["tool_status"].items()
            if any(
                t in it.get("tools", [])
                for it in active
                if it["status"] in ("missing", "warn")
            )
        }
        return setup, health

    def handled_data(self) -> dict[str, Any]:
        registered = self.host.astrbot.registered_tools()
        return {
            "disabled": [
                {
                    "tool": tool,
                    "ts": info.get("ts"),
                    "reason": info.get("reason", ""),
                    "still_disabled": registered.get(tool) is False,
                }
                for tool, info in sorted(self.ui.disabled_tools.items())
            ],
        }

    def catalog_data(self) -> list[dict[str, Any]]:
        """设置页「可用的搜索工具」：所有认识的搜索工具、擅长什么、当前状态。"""
        router = self.host.router
        current = self.host.astrbot.current_tool_names()
        key, source = self.host.credentials.deepseek_key_source()
        entries = catalog.build(
            deepseek={
                "enabled": router.own_enabled,
                "configured": bool(key),
                "masked": mask_key(key),
                "source": source,
            },
            tavily=self.host.tavily_gate.status(),
            provider_settings=self.host.astrbot.web_search_settings(),
            registered=self.host.astrbot.registered_tools(),
            request_tools=current,
            setup_status=self.setup_data()["tool_status"],
            health=self.health_data("24h"),
            search_tools=[n for n in current if router.search_category(n)],
            extra=self.host.extra_search.status(),
            prices=self.host.spending.custom_prices,
            billing=self.host.service.budget.currency.code,
        )
        # 设置页按服务填调用上限，不用用户自己找工具名
        known = list(self.host.astrbot.registered_tools())
        for entry in entries:
            if entry.get("tools"):
                entry["cap"] = self.host.tool_caps.for_service(entry["tools"], known)
        return entries

    def preference_data(self) -> dict[str, Any]:
        """设置页「搜索偏好」：各个预设下每类场景的工具顺序，切换预设时不用再请求。"""
        router = self.host.router
        names = self.search_tool_names(include_pref_disabled=True)

        def lists(pref: preference.Preference | None) -> dict[str, list[dict]]:
            r = router.with_preference(pref)
            out = {}
            for scene in preference.SCENES:
                kept = r.ordered(scene, names)
                if pref is None and scene == "tech":
                    # “推荐”下技术问题按分工规则（默认 Tavily 优先）
                    lane = dict(r.lane_assignments(names)).get(TECH_DESC)
                    if lane in kept:
                        kept.remove(lane)
                        kept.insert(0, lane)
                full = r.with_preference(None).ordered(scene, names)
                out[scene] = catalog.grouped(kept, False) + catalog.grouped(
                    [n for n in full if n not in kept], True
                )
            return out

        active = preference.normalize_preset(
            self.host.config.get("search_preference", preference.DEFAULT_PRESET)
        )
        orders = {
            key: lists(preference.resolve(key))
            for key in preference.PRESET_LABELS
            if key != "custom"
        }
        lines = self.host.config.get("search_order") or []
        # 还没自定义过时，“自定义”从当前生效的顺序开始改
        orders["custom"] = (
            lists(preference.resolve("custom", lines))
            if lines
            else orders.get(active, orders["rec"])
        )
        note = ""
        if router.mode in ("deepseek_first", "deepseek_only"):
            note = "当前路由模式下，一般问题和读取网页固定先用 DeepSeek；想完全按偏好排，把路由模式改成“只引导”"
        return {
            "preset": active,
            "presets": preference.PRESET_LABELS,
            "scenes": preference.SCENE_LABELS,
            "orders": orders,
            "mode_note": note,
        }

    async def search_tools(self):
        def build() -> dict[str, Any]:
            return {
                "catalog": self.catalog_data(),
                "preferences": self.preference_data(),
            }

        return json_response(await asyncio.to_thread(build))

    def search_ready(self, names: list[str]) -> bool:
        """有没有真正能用的通用搜索。没有 Key 时请求里保留的自带工具不算。"""
        router = self.host.router
        has_key = bool(self.host.credentials.deepseek_key_source()[0])
        return (router.own_enabled and has_key) or bool(
            router.alternatives(names, "search")
        )

    @staticmethod
    def key_services() -> list[dict[str, str]]:
        """能在面板里直接填 Key 的服务（快速开始用）。"""
        out = []
        for key in KEY_FIELDS:
            provider = get_provider(key)
            entry = next(x for x in provider.listings if x.source == "own")
            out.append(
                {
                    "service": key,
                    "name": entry.name,
                    "strengths": entry.strengths,
                    "cost": entry.cost,
                    "signup_url": provider.signup_url,
                }
            )
        return out

    def overview_data(self, range_key: str = "24h") -> dict[str, Any]:
        names = self.search_tool_names()
        setup, health = self.problems(range_key)
        status = route_view.ToolStatus(health, setup["tool_status"])
        rows, missing = route_view.arrange(self.host.router.lanes(names))
        lanes = []
        for row in rows:
            groups = status.items(row["names"])
            lanes.append({**row, "first": groups[:1], "rest": groups[1:]})
        return {
            "range": range_key,
            "own_enabled": self.host.router.own_enabled,
            "lanes": lanes,
            "missing_categories": missing,
            "search_ready": self.search_ready(names),
            "key_services": self.key_services(),
            "health": health,
            "backends": {
                n: {"label": b.label, "kind": b.kind, "region": b.region}
                for n in names
                if (b := connectivity.match_backend(n)) is not None
            },
            "logging_enabled": self.host.logging_enabled,
            "setup": setup,
            "handled": self.handled_data(),
            "model_search": self.host.astrbot.model_searches(),
            # 本月已达调用上限、这个月不再提供的工具
            "capped": self.host.tool_caps.blocked(
                self.host.astrbot.current_tool_names()
            ),
        }

    async def overview(self):
        range_key = request.query.get("range") or "24h"
        if range_key not in RANGE_HOURS:
            range_key = "24h"
        return json_response(await asyncio.to_thread(self.overview_data, range_key))

    def searxng_url(self) -> str | None:
        try:
            return connectivity.find_searxng_url(self.config_dir())
        except Exception as exc:  # noqa: BLE001 - 读不到只是不显示地址
            logger.debug(f"[deepdeep] 查找 SearXNG 地址失败: {exc}")
            return None

    def network_data(self) -> dict[str, Any]:
        url = self.searxng_url()
        names = self.search_tool_names()
        astrbot = self.host.astrbot
        return {
            "groups": [
                *connectivity.describe(names, url),
                *connectivity.describe_services(
                    names,
                    astrbot.mcp_servers(),
                    astrbot.providers_info(),
                    bool(astrbot.config().get("kb_names")),
                ),
            ],
            # 只显示地址的主机部分：完整地址里可能有账号密码或 Key
            "searxng_url": connectivity.origin_of(url),
            "last_check": self.last_network_check,
        }

    async def network(self):
        return json_response(await asyncio.to_thread(self.network_data))

    async def network_check(self):
        data = await asyncio.to_thread(self.network_data)
        searxng_url = await asyncio.to_thread(self.searxng_url)
        result = await connectivity.run_check(data["groups"], searxng_url)
        self.last_network_check = result
        logger.info(
            "[deepdeep] 连通性检测：国内网站 "
            f"{'可访问' if result['server_domestic_ok'] else '不可访问'}，国外网站 "
            f"{'可访问' if result['server_foreign_ok'] else '不可访问'}"
        )
        return json_response({**data, "last_check": result})

    def diagnostics_data(self) -> dict[str, Any]:
        """反馈问题时附上的诊断信息：版本、配置摘要、状态、最近失败。不含 API Key。"""
        try:
            from astrbot.core.config.default import VERSION as astrbot_version
        except Exception:  # noqa: BLE001 - 取不到版本不影响其他信息
            astrbot_version = "unknown"
        svc = self.host.service
        s = svc.settings
        status = self.status_data()
        problems, _ = self.host.call_log.query({"problems_only": True}, limit=10)
        # 诊断信息可能发给别人：去掉会话（含用户 ID）和结果内容。
        # 其他工具的错误文字可能带用户内容，DeepDeep 自己的工具只有固定的说明
        recent_problems = [
            {
                k: v
                for k, v in row.items()
                if k not in ("session", "top_hit")
                and (k != "error" or row["source"] == "deepseek")
            }
            for row in problems
        ]
        tools = [
            t
            for t in self.host.router.preview(self.host.astrbot.current_tool_names())
            if t["role"] != "other"
        ]
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "plugin_version": self.version,
            "astrbot_version": astrbot_version,
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
            "key": status["key"],
            "available": status["available"],
            "unavailable_code": status["unavailable_code"],
            "settings": {
                **status["settings"],
                "default_count": s.default_count,
                "max_count": s.max_count,
                "min_rerank_score": s.min_rerank_score,
                "include_content": s.include_content,
                "max_search_chars": s.max_search_chars,
                "max_fetch_chars": s.max_fetch_chars,
                "send_original_question": s.send_original_question,
                "cache_ttl_seconds": self.host.config.get("cache_ttl_seconds"),
                "request_timeout": self.host.config.get("request_timeout"),
                "max_retries": self.host.config.get("max_retries"),
                "qps_limit": self.host.config.get("qps_limit"),
            },
            "budget": status["budget"],
            "circuit": status["circuit"],
            "today": status["today"],
            "tavily": status["tavily"],
            "tool_caps": status["tool_caps"],
            "model_search": self.host.astrbot.model_searches(),
            "search_tools": tools,
            "search_tool_health": {
                k: {"verdict": v["verdict"], "reason": v["reason"]}
                for k, v in self.health_data("7d").items()
            },
            "network_check": self.last_network_check,
            "setup": [
                {k: it.get(k) for k in ("key", "status", "detail", "tools")}
                for it in self.setup_data()["items"]
            ],
            "recent_problems": recent_problems,
            "call_log": self.host.call_log.summary(),
        }

    async def diagnostics(self):
        data = await asyncio.to_thread(self.diagnostics_data)
        data["knowledge_base"] = await self.diagnostics_kb()
        data["mcp"] = await asyncio.to_thread(self.diagnostics_mcp)
        return json_response(data)

    async def diagnostics_kb(self) -> dict[str, Any]:
        config = await self.host.astrbot.kb_config()
        recent = await asyncio.to_thread(
            self.host.call_log.kb_health, time.time() - 86400
        )
        return {
            **kbtrace.assess_status(config, recent),
            "enabled_count": len(config["names"]),
            "agentic": config["agentic"],
            "recent_24h": recent,
            "plugins": self.host.astrbot.kb_plugins(),
        }

    def diagnostics_mcp(self) -> dict[str, Any]:
        self.fix_mcp_sources()
        servers = mcp_view.build(
            self.host.astrbot.mcp_servers(),
            self.host.call_log.mcp_stats(time.time() - 7 * 86400),
        )
        # 不带错误输出：里面可能有服务地址或 Key
        return {
            **mcp_view.assess(servers),
            "servers": [
                {
                    k: s[k]
                    for k in ("name", "state", "transport", "calls", "errors", "avg_ms")
                }
                | {"tools": len(s["tools"])}
                for s in servers
            ],
        }

    # ---------- 调用日志 ----------

    @staticmethod
    def call_filters() -> dict[str, Any]:
        q = request.query
        filters: dict[str, Any] = {}
        for key in ("tool", "source", "session", "request_id"):
            value = (q.get(key) or "").strip()
            if value:
                filters[key] = value[:200]
        status = (q.get("status") or "").strip()
        if status == "problems":
            filters["problems_only"] = True
        elif status in CALL_STATUSES:
            filters["status"] = status
        keyword = (q.get("q") or "").strip()
        if keyword:
            filters["q"] = keyword[:200]
        category = q.get("category") or ""
        if category in CALL_CATEGORIES:
            filters["category"] = category
        hours = RANGE_HOURS.get(q.get("range") or "")
        if hours:
            filters["since"] = time.time() - hours * 3600
        return filters

    def search_stats_data(self, range_key: str = "24h") -> dict[str, Any]:
        hours = RANGE_HOURS.get(range_key)
        since = time.time() - hours * 3600 if hours else None
        router = self.host.router
        rows = []
        for item in self.host.call_log.tool_stats(since):
            category = router.search_category(item["tool"])
            if category is None:
                continue
            rows.append(
                {
                    **item,
                    "category": category,
                    "same_backend": router.is_same_backend(item["tool"]),
                }
            )
        return {
            "range": range_key,
            "rows": rows,
            "total": sum(r["total"] for r in rows),
            # 只算联网搜索：读网页和本地知识库不算
            "consecutive_search": self.host.call_log.consecutive_searches(
                (
                    r["tool"]
                    for r in rows
                    if not router.is_fetch_like(r["tool"]) and r["tool"] != KB_TOOL
                ),
                since,
            ),
            "logging_enabled": self.host.logging_enabled,
        }

    async def search_stats(self):
        range_key = request.query.get("range") or "24h"
        if range_key not in RANGE_HOURS and range_key != "all":
            range_key = "24h"
        return json_response(await asyncio.to_thread(self.search_stats_data, range_key))

    def search_tool_names_logged(self) -> list[str]:
        """日志里出现过的搜索和读网页工具，分类筛选用。"""
        router = self.host.router
        return [
            tool
            for tool in self.host.call_log.facets()["tools"]
            if tool != KB_TOOL and router.search_category(tool) is not None
        ]

    def filters_with_tools(self) -> dict[str, Any]:
        filters = self.call_filters()
        if filters.get("category"):
            filters["search_tools"] = self.search_tool_names_logged()
        return filters

    def fix_mcp_sources(self) -> None:
        self.host.call_log.fix_mcp_sources(self.host.astrbot.mcp_tool_servers())

    async def call_categories(self):
        hours = RANGE_HOURS.get(request.query.get("range") or "")
        since = time.time() - hours * 3600 if hours else None

        def work() -> dict[str, int]:
            self.fix_mcp_sources()
            return self.host.call_log.category_counts(
                since, self.search_tool_names_logged()
            )

        return json_response(await asyncio.to_thread(work))

    async def mcp(self):
        hours = RANGE_HOURS.get(request.query.get("range") or "7d")
        since = time.time() - hours * 3600 if hours else None

        def work() -> list[dict[str, Any]]:
            self.fix_mcp_sources()
            return mcp_view.build(
                self.host.astrbot.mcp_servers(), self.host.call_log.mcp_stats(since)
            )

        servers = await asyncio.to_thread(work)
        return json_response(
            {
                "servers": servers,
                "status": mcp_view.assess(servers),
                "logging_enabled": self.host.logging_enabled,
            }
        )

    async def calls(self):
        limit = request.query.get("limit", 50, type=int) or 50
        offset = request.query.get("offset", 0, type=int) or 0
        log = self.host.call_log
        filters = await asyncio.to_thread(self.filters_with_tools)
        items, total = await asyncio.to_thread(
            log.query, filters, limit=limit, offset=offset
        )
        summary = await asyncio.to_thread(log.summary)
        return json_response({"items": items, "total": total, "summary": summary})

    async def call_detail(self, call_id: str):
        try:
            row_id = int(call_id)
        except ValueError:
            return error_response("无效的记录 ID", status_code=400)
        item = await asyncio.to_thread(self.host.call_log.get, row_id)
        if item is None:
            return error_response("记录不存在或已被清理", status_code=404)
        return json_response(item)

    async def call_facets(self):
        return json_response(await asyncio.to_thread(self.host.call_log.facets))

    async def call_export(self):
        filters = await asyncio.to_thread(self.filters_with_tools)
        export_dir = (
            Path(self.host.data_dir) / "exports"
            if self.host.data_dir
            else Path(tempfile.gettempdir()) / "astrbot_deepdeep_exports"
        )

        stamp = time.strftime("%Y%m%d-%H%M%S")

        def _write() -> Path:
            export_dir.mkdir(parents=True, exist_ok=True)
            # 刚生成的可能还在下载，过一会儿才清
            cutoff = time.time() - EXPORT_KEEP_SECONDS
            for old in export_dir.glob("calls-*.jsonl"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
            # 同一秒的几次导出不能写到同一个文件
            path = export_dir / f"calls-{stamp}-{secrets.token_hex(4)}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for item in self.host.call_log.iter_all(filters):
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            return path

        path = await asyncio.to_thread(_write)
        return file_response(
            path, filename=f"calls-{stamp}.jsonl", content_type="application/x-ndjson"
        )

    async def call_clear(self):
        removed = await asyncio.to_thread(self.host.call_log.clear)
        logger.info(f"[deepdeep] WebUI 清空了调用日志（{removed} 条）")
        return json_response({"removed": removed})

    async def call_feedback(self):
        body = await self._body()
        feedback = body.get("feedback")
        if feedback not in ("good", "bad", None):
            return error_response("无效的标记", status_code=400)
        try:
            row_id = int(body.get("id"))
        except (TypeError, ValueError):
            return error_response("无效的记录 ID", status_code=400)
        found = await asyncio.to_thread(
            self.host.call_log.set_feedback, row_id, feedback
        )
        if not found:
            return error_response("记录不存在或已被清理", status_code=404)
        return json_response({"id": row_id, "feedback": feedback})

    async def kb_stats(self):
        hours = RANGE_HOURS.get(request.query.get("range") or "")
        since = time.time() - hours * 3600 if hours else None
        stats = await asyncio.to_thread(self.host.call_log.kb_stats, since)
        names = {p["name"]: p["display_name"] for p in self.host.astrbot.kb_plugins()}
        for row in stats["by_plugin"]:
            row["display_name"] = names.get(row["plugin"]) or row["plugin"]
        await self.host.pricing.ensure_fresh()
        stats["cost"] = self.host.pricing.kb_cost(
            stats.pop("billing"), self.host.astrbot.providers_info()
        )
        stats["manual_prices"] = self.host.config.get("model_prices") or []
        config = self.host.astrbot.config()
        stats["settings"] = {
            "trim_min_score": self.host.config.get("kb_trim_min_score", 0),
            "trim_chunk_chars": self.host.config.get("kb_trim_chunk_chars", 0),
            "final_top_k": config.get("kb_final_top_k"),
        }
        # 没有记录来源时无从比较，不返回
        idle: dict[str, list] = {"idle_kbs": [], "idle_docs": []}
        if stats["by_kb"]:
            idle = await self.host.astrbot.idle_kb(
                {r["kb"] for r in stats["by_kb"]},
                {(r["kb"], r["doc"]) for r in stats["by_doc"]},
            )
        return json_response(
            {**stats, **idle, "logging_enabled": self.host.logging_enabled}
        )

    async def _switch(self, name: Any, active: Any, action: Any):
        """开关类接口的公共部分：校验参数，出错时把原因告诉页面。"""
        if not isinstance(name, str) or not name or not isinstance(active, bool):
            return error_response("参数不对", status_code=400)
        try:
            await action(name, active)
        except Exception as exc:  # noqa: BLE001 - 包括 AstrBot 启动 MCP 服务时的各种错误
            return error_response(f"操作失败：{exc}", status_code=400)
        logger.info(f"[deepdeep] WebUI 把 {name} 设为 {'开启' if active else '关闭'}")
        return None

    async def kb_toggle(self):
        body = await self._body()
        failed = await self._switch(
            body.get("name"), body.get("enabled"), self.host.astrbot.set_kb_enabled
        )
        return failed or await self.kb_status()

    async def mcp_toggle(self):
        body = await self._body()
        failed = await self._switch(
            body.get("name"), body.get("active"), self.host.astrbot.set_mcp_active
        )
        return failed or json_response({"ok": True})

    async def mcp_tool_toggle(self):
        body = await self._body()
        name = body.get("name")
        if name not in self.host.astrbot.mcp_tool_servers():
            return error_response("不是 MCP 工具", status_code=400)
        failed = await self._switch(
            name, body.get("active"), self.host.astrbot.set_tool_active
        )
        return failed or json_response({"ok": True})

    async def kb_status(self):
        config = await self.host.astrbot.kb_config()
        recent = await asyncio.to_thread(
            self.host.call_log.kb_health, time.time() - 86400
        )
        return json_response(
            {
                **kbtrace.assess_status(config, recent),
                "plugins": self.host.astrbot.kb_plugins(),
                "config": config,
                "recent": recent,
                "logging_enabled": self.host.logging_enabled,
            }
        )

    # ---------- 写入 ----------

    async def save_settings(self):
        clean, err = validate_settings(await request.json(default={}))
        if err:
            return error_response(err, status_code=400)
        try:
            await self.host.update_settings(clean)
        except Exception as exc:  # noqa: BLE001 - 设置已经退回原样，把原因告诉页面
            logger.warning(f"[deepdeep] 应用设置失败: {exc}")
            return error_response(f"设置没有保存：{exc}", status_code=400)
        # 切换语言很常见，不值得留在日志里
        log = logger.debug if set(clean) == {"panel_language"} else logger.info
        log(f"[deepdeep] WebUI 更新设置: {sorted(clean)}")
        return json_response(await asyncio.to_thread(self.status_data))

    async def save_key(self):
        """保存或清除 DeepDeep 自己的 Key。空提交不会清掉已保存的 Key，清除要明确带上 clear。
        只写插件自己的配置，不写 AstrBot 的；返回和日志里都只有打码后的 Key。"""
        body = await self._body()
        service = str(body.get("service") or "")
        field = KEY_FIELDS.get(service)
        if field is None:
            return error_response(
                "这个服务的 Key 不在 DeepDeep 里保存", status_code=400
            )
        enabled_field = get_provider(service).enabled_field
        if body.get("clear") is True:
            await self.host.update_settings({field: ""})
            logger.info(f"[deepdeep] WebUI 清除了 {service} 的 Key")
            changed = True
        else:
            key = body.get("key")
            key = key.strip() if isinstance(key, str) else ""
            if len(key) > MAX_KEY_LENGTH or any(c.isspace() for c in key):
                return error_response("Key 的格式不对", status_code=400)
            changed = bool(key)
            if changed:
                # 快速开始里选了一家就是要用它，默认关着的服务也一起打开
                turn_on = {enabled_field: True} if body.get("enable") is True else {}
                await self.host.update_settings({field: key, **turn_on})
                logger.info(
                    f"[deepdeep] WebUI 更新了 {service} 的 Key（{mask_key(key)}）"
                )
        status = await asyncio.to_thread(self.status_data)
        return json_response({"changed": changed, "status": status})

    async def _body(self) -> dict[str, Any]:
        body = await request.json(default={})
        return body if isinstance(body, dict) else {}

    async def ignore(self):
        key = str((await self._body()).get("key") or "")
        setup, health = await asyncio.to_thread(self.problems)
        fps = {
            it["ignore_key"]: it["status"]
            for it in setup["items"]
            if it["status"] in ("missing", "warn")
        }
        fps.update(
            {
                h["ignore_key"]: "bad"
                for h in health.values()
                if h["verdict"] in ("broken", "degraded")
            }
        )
        if key not in fps:
            return error_response("这条提醒已经不存在了，刷新看看", status_code=400)
        self.ui.ignore(key, fps[key])
        logger.info(f"[deepdeep] WebUI 忽略提醒: {key}")
        return json_response(await asyncio.to_thread(self.overview_data))

    async def unignore(self):
        key = str((await self._body()).get("key") or "")
        self.ui.unignore(key)
        return json_response(await asyncio.to_thread(self.overview_data))

    @staticmethod
    def _tool_list(body: dict[str, Any]) -> list[str]:
        tools = body.get("tools")
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, str) and t][:50]

    async def disable_tools(self):
        body = await self._body()
        tools = self._tool_list(body)
        allowed = set(self.fixable(tools, self.host.astrbot.registered_tools()))
        reason = str(body.get("reason") or "")[:200]
        done, failed = [], {}
        for name in tools:
            if name not in allowed:
                failed[name] = "不能从这里停用（不是已注册的插件工具，或已经停用）"
                continue
            try:
                await self.host.astrbot.set_tool_active(name, False)
            except Exception as exc:  # noqa: BLE001 - 逐个报告，不影响其他工具
                failed[name] = str(exc)
                continue
            self.ui.mark_disabled(name, reason)
            done.append(name)
        if done:
            logger.info(f"[deepdeep] WebUI 停用了工具: {done}（{reason}）")
        data = await asyncio.to_thread(self.overview_data)
        return json_response({**data, "done": done, "failed": failed})

    async def enable_tools(self):
        tools = self._tool_list(await self._body())
        registered = self.host.astrbot.registered_tools()
        done, failed = [], {}
        for name in tools:
            if name not in registered:
                self.ui.mark_enabled(name)
                failed[name] = "这个工具已经不存在（插件可能被卸载了），已从列表移除"
                continue
            try:
                await self.host.astrbot.set_tool_active(name, True)
            except Exception as exc:  # noqa: BLE001
                failed[name] = str(exc)
                continue
            self.ui.mark_enabled(name)
            done.append(name)
        if done:
            logger.info(f"[deepdeep] WebUI 重新启用了工具: {done}")
        data = await asyncio.to_thread(self.overview_data)
        return json_response({**data, "done": done, "failed": failed})

    async def test(self):
        service = str((await self._body()).get("service") or "deepseek")
        return json_response(await self.host.run_service_test(service))

    async def reset_circuit(self):
        self.host.service.breaker.reset()
        return json_response(await asyncio.to_thread(self.status_data))
