"""插件 WebUI（pages/dashboard）的后端接口。

页面通过 window.AstrBotPluginView（4.26–4.28 为 AstrBotPluginPage）bridge 调用，路由形如 /<插件名>/status，
只有登录了 AstrBot 管理面板的用户才能访问。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

from . import catalog, connectivity, preference, setup_check
from . import router as router_mod
from .caps import format_limit_rules
from .health import VERDICT_LABELS, assess
from .pricing import CURRENCIES
from .router import (
    BUILTIN_WEB_SEARCH_TOOLS,
    OWN_TOOLS,
    ROUTING_MODES,
    TAVILY_TOOL,
    ToolRouter,
)
from .uistate import UiState

if TYPE_CHECKING:
    from ..main import DeepSeekSearchPlugin

# AstrBot 设置页的网页搜索提供商 -> 会加入请求的内置工具名
BUILTIN_PROVIDER_TOOLS = {
    "tavily": ["web_search_tavily", "tavily_extract_web_page"],
    "bocha": ["web_search_bocha"],
    "brave": ["web_search_brave"],
    "firecrawl": ["web_search_firecrawl", "firecrawl_extract_web_page"],
    "baidu_ai_search": ["web_search_baidu"],
    "exa": ["web_search_exa", "exa_get_contents"],
    "anysearch": ["web_search_anysearch"],
    "serpapi": [
        "web_search_serpapi",
        "image_search_serpapi",
        "finance_search_serpapi",
        "scholar_search_serpapi",
    ],
    "deepseek": ["web_search_deepseek", "deepseek_fetch_web_page"],
}

# WebUI 允许修改的配置项及校验规则
EDITABLE_FIELDS: dict[str, tuple[type, Any, Any]] = {
    "own_tools_enabled": (bool, None, None),
    "tavily_enabled": (bool, None, None),
    "tavily_monthly_limit": (int, 0, 1000000),
    "tool_monthly_limits": (list, None, None),
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
    "call_log_retention_days": (int, 1, 365),
}

# 字符串配置项的可选值
STR_CHOICES = {
    "routing_mode": tuple(ROUTING_MODES),
    "billing_currency": tuple(CURRENCIES),
    "panel_language": ("auto", "zh", "en"),
    "search_preference": tuple(preference.PRESET_LABELS),
}

# 调用日志列表的时间范围（小时）
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


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    return f"{key[:3]}****{key[-4:]}" if len(key) > 10 else "****"


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
            if len(items) > 80 or any(len(v) > 200 for v in items):
                return {}, f"{key} 条目过多或过长"
            clean[key] = items
        else:
            if isinstance(value, bool):
                return {}, f"{key} 必须是数字"
            try:
                num = typ(value)
            except (TypeError, ValueError):
                return {}, f"{key} 必须是数字"
            if not (lo <= num <= hi):
                return {}, f"{key} 需在 {lo} 到 {hi} 之间"
            clean[key] = num
    return clean, None


class WebApi:
    def __init__(self, plugin: DeepSeekSearchPlugin, plugin_name: str, version: str):
        self.plugin = plugin
        self.name = plugin_name
        self.version = version
        # 最近一次连通性检测的结果（只在内存里）
        self.last_network_check: dict[str, Any] | None = None
        data_dir = getattr(plugin, "data_dir", None)
        self.ui = UiState(Path(data_dir) / "ui_state.json" if data_dir else None)

    def register(self, context: Any) -> None:
        routes = [
            ("status", self.status, ["GET"], "插件状态"),
            ("usage", self.usage, ["GET"], "逐日用量"),
            ("activity", self.activity, ["GET"], "最近调用"),
            ("tools", self.tools, ["GET"], "搜索工具路由预览"),
            ("settings", self.save_settings, ["POST"], "保存设置"),
            ("test", self.test, ["POST"], "连接测试"),
            ("reset-circuit", self.reset_circuit, ["POST"], "解除熔断"),
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
        ]
        for path, handler, methods, desc in routes:
            context.register_web_api(f"/{self.name}/{path}", handler, methods, desc)

    # ---------- 读取 ----------

    def settings_view(self) -> dict[str, Any]:
        c = self.plugin.config
        r = self.plugin.router
        return {
            "own_tools_enabled": r.own_enabled,
            "tavily_enabled": self.plugin.tavily_enabled,
            "tavily_monthly_limit": self.plugin.tavily.monthly_limit,
            "tool_monthly_limits": format_limit_rules(self.plugin.tool_caps.rules),
            "panel_language": self.plugin.panel_language,
            "search_preference": self._preset(),
            "search_order": [
                str(x) for x in (c.get("search_order") or []) if isinstance(x, str)
            ],
            "search_lane_rules": [
                f"{desc} = {', '.join(pats)}" for desc, pats in r.lane_rules
            ],
            "routing_mode": r.mode,
            "billing_currency": self.plugin.service.budget.currency.code,
            "daily_budget": self.plugin.service.budget.daily_limit,
            "monthly_budget": self.plugin.service.budget.monthly_limit,
            "exchange_rate": self.plugin.usd_cny_rate,
            "fallback_tool_patterns": r.fallback_patterns,
            "specialist_rules": [
                f"{p} = {d}" if d else p for p, d in r.specialist_rules
            ],
            "inject_search_guide": self.plugin.inject_guide,
            "record_queries": bool(c.get("record_queries", True)),
            "call_log_enabled": self.plugin.recorder.enabled,
            "call_log_record_other_content": self.plugin.recorder.record_other_content,
            "call_log_retention_days": int(self.plugin.call_log.retention_days),
            "circuit_threshold": self.plugin.service.breaker.threshold,
            "circuit_cooldown_seconds": int(self.plugin.service.breaker.cooldown),
        }

    def tavily_status(self) -> dict[str, Any]:
        keys, source = self.plugin.tavily_key_source()
        usable, why = self.plugin.tavily_usable()
        return {
            "enabled": self.plugin.tavily_enabled,
            "configured": bool(keys),
            "masked": mask_key(keys[0]) if keys else "",
            "source": source,
            "usable": usable,
            "reason": why,
            "month_used": self.plugin.tavily.month_used(),
            "monthly_limit": self.plugin.tavily.monthly_limit,
            "limit_reached": self.plugin.tavily.limit_reached(),
            "usage_text": self.plugin.tavily.usage_text(),
            "last_failure": self.plugin.tavily.last_failure(),
        }

    def status_data(self) -> dict[str, Any]:
        svc = self.plugin.service
        key, source = self.plugin.resolve_key_source()
        avail = svc.availability("search")
        return {
            "version": self.version,
            "available": avail.ok,
            "unavailable_code": avail.code,
            "unavailable_message": avail.message,
            "key": {"configured": bool(key), "masked": mask_key(key), "source": source},
            "tavily": self.tavily_status(),
            "budget": svc.budget.snapshot(),
            "circuit": svc.breaker.snapshot(),
            "today": svc.stats.day(),
            "tool_caps": self.plugin.tool_caps.snapshot(),
            "settings": self.settings_view(),
        }

    async def status(self):
        return json_response(self.status_data())

    async def usage(self):
        days = request.query.get("days", 30, type=int) or 30
        days = max(1, min(62, days))
        return json_response({"days": self.plugin.service.stats.series(days)})

    async def activity(self):
        limit = request.query.get("limit", 50, type=int) or 50
        return json_response(
            {"items": self.plugin.service.activity.recent(max(1, min(100, limit)))}
        )

    def current_tool_names(self) -> list[str]:
        names: list[str] = []
        try:
            mgr = self.plugin.context.get_llm_tool_manager()
            names += [t.name for t in mgr.func_list if getattr(t, "active", True)]
        except Exception as exc:  # noqa: BLE001 - 预览失败不影响其他功能
            logger.debug(f"[deepdeep] 读取工具列表失败: {exc}")
        try:
            ps = self.plugin.context.get_config().get("provider_settings", {})
            if ps.get("web_search"):
                names += BUILTIN_PROVIDER_TOOLS.get(
                    ps.get("websearch_provider", ""), []
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[deepdeep] 读取网页搜索配置失败: {exc}")
        return names

    async def tools(self):
        # 预览可以带上尚未保存的匹配规则，方便边改边看
        # 规则用换行分隔，避免依赖 bridge 对数组参数的序列化方式
        router = self.plugin.router
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
                "tools": router.preview(self.current_tool_names()),
                "builtin_known": BUILTIN_WEB_SEARCH_TOOLS,
            }
        )

    def _preset(self) -> str:
        value = self.plugin.config.get("search_preference", preference.DEFAULT_PRESET)
        return value if value in preference.PRESET_LABELS else preference.DEFAULT_PRESET

    def search_tool_names(self, include_pref_disabled: bool = False) -> list[str]:
        """当前会出现在请求里的搜索类工具（关闭自带搜索时不含 DeepDeep 自己的）。
        ``include_pref_disabled`` 为 True 时保留搜索偏好里设为“不用”的（给偏好设置用）。"""
        router = self.plugin.router
        names = self.current_tool_names()
        if not router.own_enabled:
            names = [n for n in names if n not in OWN_TOOLS]
        if TAVILY_TOOL in names and not self.plugin.tavily_usable(names)[0]:
            names = [n for n in names if n != TAVILY_TOOL]
        blocked = set(self.plugin.tool_caps.blocked(names))
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
        router = self.plugin.router
        out: dict[str, dict[str, Any]] = {}
        for row in self.plugin.call_log.tool_health(since):
            if router.search_category(row["tool"]) is None:
                continue
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
        router = self.plugin.router
        try:
            tools = self.plugin.context.get_llm_tool_manager().func_list
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[deepdeep] 读取工具列表失败: {exc}")
            return out
        for t in tools:
            name = getattr(t, "name", "")
            if not getattr(t, "active", True) or router.search_category(name) is None:
                continue
            module = getattr(t, "handler_module_path", None) or ""
            plugin = self.plugin.resolve_plugin(module) if module else None
            if plugin and plugin != self.name:
                out.setdefault(plugin, []).append(name)
        return out

    def config_dir(self) -> Path | None:
        data_dir = self.plugin.data_dir
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
                self.plugin.resolve_key_source(), self.plugin.router.own_enabled
            ),
            tavily=setup_check.check_tavily(self.tavily_status()),
            builtin=setup_check.check_builtin(
                self.plugin._builtin_web_search(), BUILTIN_PROVIDER_TOOLS
            ),
            plugins=plugins,
            models=setup_check.check_model_search(self.plugin.model_searches()),
        )

    def registered_tools(self) -> dict[str, bool]:
        """已注册的工具（插件、MCP）：{工具名: 是否启用}。内置网页搜索的工具每次请求才加入，不在这里。"""
        try:
            return {
                t.name: bool(getattr(t, "active", True))
                for t in self.plugin.context.get_llm_tool_manager().func_list
            }
        except Exception:  # noqa: BLE001
            return {}

    def fixable(self, tools: list[str], registered: dict[str, bool]) -> list[str]:
        """可以一键停用的工具：已注册、正在启用、不是 DeepDeep 自己的（它们有自己的开关）。"""
        own = {*OWN_TOOLS, TAVILY_TOOL}
        return [t for t in tools if registered.get(t) and t not in own]

    def problems(
        self, range_key: str = "24h"
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """配置检查和健康检测，并按“忽略”记录标出已忽略的项。"""
        setup = self.setup_data()
        health = self.health_data(range_key)
        registered = self.registered_tools()
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
        registered = self.registered_tools()
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
        router = self.plugin.router
        current = self.current_tool_names()
        return catalog.build(
            deepseek={
                "enabled": router.own_enabled,
                "configured": bool(self.plugin.resolve_key_source()[0]),
            },
            tavily=self.tavily_status(),
            provider_settings=self.plugin._builtin_web_search(),
            registered=self.registered_tools(),
            request_tools=current,
            setup_status=self.setup_data()["tool_status"],
            health=self.health_data("24h"),
            search_tools=[n for n in current if router.search_category(n)],
        )

    def preference_data(self) -> dict[str, Any]:
        """设置页「搜索偏好」：各个预设下每类场景的工具顺序，切换预设时不用再请求。"""
        router = self.plugin.router
        names = self.search_tool_names(include_pref_disabled=True)

        def lists(pref: preference.Preference | None) -> dict[str, list[dict]]:
            r = router.with_preference(pref)
            out = {}
            for scene in preference.SCENES:
                kept = r.ordered(scene, names)
                if pref is None and scene == "tech":
                    # “推荐”下技术问题按分工规则（默认 Tavily 优先）
                    lane = dict(r.lane_assignments(names)).get(router_mod.TECH_DESC)
                    if lane in kept:
                        kept.remove(lane)
                        kept.insert(0, lane)
                full = r.with_preference(None).ordered(scene, names)
                out[scene] = catalog.grouped(kept, False) + catalog.grouped(
                    [n for n in full if n not in kept], True
                )
            return out

        active = self._preset()
        orders = {
            key: lists(preference.resolve(key))
            for key in preference.PRESET_LABELS
            if key != "custom"
        }
        lines = self.plugin.config.get("search_order") or []
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

    def overview_data(self, range_key: str = "24h") -> dict[str, Any]:
        names = self.search_tool_names()
        setup, health = self.problems(range_key)
        return {
            "range": range_key,
            "own_enabled": self.plugin.router.own_enabled,
            "lanes": self.plugin.router.lanes(names),
            "health": health,
            "backends": {
                n: {"label": b.label, "kind": b.kind, "region": b.region}
                for n in names
                if (b := connectivity.match_backend(n)) is not None
            },
            "logging_enabled": self.plugin.recorder.enabled,
            "setup": setup,
            "handled": self.handled_data(),
            "model_search": self.plugin.model_searches(),
            # 本月已达调用上限、这个月不再提供的工具
            "capped": self.plugin.tool_caps.blocked(self.current_tool_names()),
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
        return {
            "groups": connectivity.describe(self.search_tool_names(), url),
            "searxng_url": url,
            "last_check": self.last_network_check,
        }

    async def network(self):
        return json_response(await asyncio.to_thread(self.network_data))

    async def network_check(self):
        data = await asyncio.to_thread(self.network_data)
        result = await connectivity.run_check(data["groups"], data["searxng_url"])
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
        svc = self.plugin.service
        s = svc.settings
        status = self.status_data()
        recent_problems, _ = self.plugin.call_log.query(
            {"problems_only": True}, limit=10
        )
        tools = [
            t
            for t in self.plugin.router.preview(self.current_tool_names())
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
                "cache_ttl_seconds": self.plugin.config.get("cache_ttl_seconds"),
                "request_timeout": self.plugin.config.get("request_timeout"),
                "max_retries": self.plugin.config.get("max_retries"),
                "qps_limit": self.plugin.config.get("qps_limit"),
            },
            "budget": status["budget"],
            "circuit": status["circuit"],
            "today": status["today"],
            "tavily": status["tavily"],
            "tool_caps": status["tool_caps"],
            "model_search": self.plugin.model_searches(),
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
            "call_log": self.plugin.call_log.summary(),
        }

    async def diagnostics(self):
        return json_response(await asyncio.to_thread(self.diagnostics_data))

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
        hours = RANGE_HOURS.get(q.get("range") or "")
        if hours:
            filters["since"] = time.time() - hours * 3600
        return filters

    def search_stats_data(self, range_key: str = "24h") -> dict[str, Any]:
        hours = RANGE_HOURS.get(range_key)
        since = time.time() - hours * 3600 if hours else None
        router = self.plugin.router
        rows = []
        for item in self.plugin.call_log.tool_stats(since):
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
            "logging_enabled": self.plugin.recorder.enabled,
        }

    async def search_stats(self):
        range_key = request.query.get("range") or "24h"
        if range_key not in RANGE_HOURS and range_key != "all":
            range_key = "24h"
        return json_response(await asyncio.to_thread(self.search_stats_data, range_key))

    async def calls(self):
        limit = request.query.get("limit", 50, type=int) or 50
        offset = request.query.get("offset", 0, type=int) or 0
        log = self.plugin.call_log
        items, total = await asyncio.to_thread(
            log.query, self.call_filters(), limit=limit, offset=offset
        )
        summary = await asyncio.to_thread(log.summary)
        return json_response({"items": items, "total": total, "summary": summary})

    async def call_detail(self, call_id: str):
        try:
            row_id = int(call_id)
        except ValueError:
            return error_response("无效的记录 ID", status_code=400)
        item = await asyncio.to_thread(self.plugin.call_log.get, row_id)
        if item is None:
            return error_response("记录不存在或已被清理", status_code=404)
        return json_response(item)

    async def call_facets(self):
        return json_response(await asyncio.to_thread(self.plugin.call_log.facets))

    async def call_export(self):
        filters = self.call_filters()
        export_dir = (
            Path(self.plugin.data_dir) / "exports"
            if self.plugin.data_dir
            else Path(tempfile.gettempdir()) / "astrbot_deepdeep_exports"
        )

        def _write() -> Path:
            export_dir.mkdir(parents=True, exist_ok=True)
            for old in export_dir.glob("calls-*.jsonl"):
                old.unlink(missing_ok=True)
            path = export_dir / f"calls-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for item in self.plugin.call_log.iter_all(filters):
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            return path

        path = await asyncio.to_thread(_write)
        return file_response(
            path, filename=path.name, content_type="application/x-ndjson"
        )

    async def call_clear(self):
        removed = await asyncio.to_thread(self.plugin.call_log.clear)
        logger.info(f"[deepdeep] WebUI 清空了调用日志（{removed} 条）")
        return json_response({"removed": removed})

    # ---------- 写入 ----------

    async def save_settings(self):
        clean, err = validate_settings(await request.json(default={}))
        if err:
            return error_response(err, status_code=400)
        config = self.plugin.config
        for k, v in clean.items():
            config[k] = v
        saver = getattr(config, "save_config_async", None) or getattr(
            config, "save_config", None
        )
        if saver is not None:
            result = saver()
            if inspect.isawaitable(result):
                await result
        self.plugin.apply_config()
        logger.info(f"[deepdeep] WebUI 更新设置: {sorted(clean)}")
        return json_response(self.status_data())

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

    async def _set_tool_active(self, name: str, active: bool) -> None:
        ctx = self.plugin.context
        method = "activate_llm_tool" if active else "deactivate_llm_tool"
        fn = getattr(ctx, f"{method}_async", None)
        if fn is not None:
            ok = await fn(name)
        else:
            ok = getattr(ctx, method)(name)
        if ok is False:
            raise ValueError("找不到这个工具")

    async def disable_tools(self):
        body = await self._body()
        tools = self._tool_list(body)
        allowed = set(self.fixable(tools, self.registered_tools()))
        reason = str(body.get("reason") or "")[:200]
        done, failed = [], {}
        for name in tools:
            if name not in allowed:
                failed[name] = "不能从这里停用（不是已注册的插件工具，或已经停用）"
                continue
            try:
                await self._set_tool_active(name, False)
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
        registered = self.registered_tools()
        done, failed = [], {}
        for name in tools:
            if name not in registered:
                self.ui.mark_enabled(name)
                failed[name] = "这个工具已经不存在（插件可能被卸载了），已从列表移除"
                continue
            try:
                await self._set_tool_active(name, True)
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
        return json_response(await self.plugin.run_connectivity_test())

    async def reset_circuit(self):
        self.plugin.service.breaker.reset()
        return json_response(self.status_data())
