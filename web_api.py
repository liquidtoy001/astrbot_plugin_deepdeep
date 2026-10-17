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

from .core.pricing import CURRENCIES
from .core.router import BUILTIN_WEB_SEARCH_TOOLS, ROUTING_MODES, ToolRouter

if TYPE_CHECKING:
    from .main import DeepSeekSearchPlugin

# AstrBot 设置页的网页搜索提供商 -> 会加入请求的内置工具名
BUILTIN_PROVIDER_TOOLS = {
    "tavily": ["web_search_tavily", "tavily_extract_web_page"],
    "bocha": ["web_search_bocha"],
    "brave": ["web_search_brave"],
    "firecrawl": ["web_search_firecrawl", "firecrawl_extract_web_page"],
    "baidu_ai_search": ["web_search_baidu"],
    "exa": ["web_search_exa", "exa_get_contents"],
    "anysearch": ["web_search_anysearch"],
}

# WebUI 允许修改的配置项及校验规则
EDITABLE_FIELDS: dict[str, tuple[type, Any, Any]] = {
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
            "budget": svc.budget.snapshot(),
            "circuit": svc.breaker.snapshot(),
            "today": svc.stats.day(),
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
            )
        return json_response(
            {
                "tools": router.preview(self.current_tool_names()),
                "builtin_known": BUILTIN_WEB_SEARCH_TOOLS,
            }
        )

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
            "search_tools": tools,
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

    async def test(self):
        return json_response(await self.plugin.run_connectivity_test())

    async def reset_circuit(self):
        self.plugin.service.breaker.reset()
        return json_response(self.status_data())
