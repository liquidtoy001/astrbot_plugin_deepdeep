import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .core.activity import ActivityLog
from .core.cache import TTLCache
from .core.calllog import CallLog
from .core.client import DeepSeekToolsClient, DeepSeekToolsError
from .core.guard import BudgetGuard, CircuitBreaker
from .core.pricing import DEFAULT_CURRENCY, get_currency
from .core.recorder import ToolCallRecorder
from .core.router import DEFAULT_MODE, OWN_TOOLS, TAVILY_TOOL, ToolRouter
from .core.service import (
    SearchService,
    Settings,
    ToolOutcome,
    cfg_float,
    cfg_int,
    new_request_id,
)
from .core.stats import UsageStats
from .core.tavily import TavilyClient, TavilySearch
from .web_api import WebApi

PLUGIN_NAME = "astrbot_plugin_deepdeep"
PLUGIN_VERSION = "v1.0.1"
DEEPSEEK_HOST = "api.deepseek.com"
ROUTING_EXTRA_KEY = "deepdeep_routing"

# 这些情况不是 DeepSeek 故障（某个网页抓不到、没搜到、结果相关度低）：
# 建议换工具再试，但不把 DeepDeep 的工具撤掉
SOFT_REASONS = {"http_403", "http_404", "empty", "low_quality"}


@register(
    PLUGIN_NAME,
    "Turino",
    "AstrBot 搜索管理器：分工协作、自动降级、费用上限与调用日志",
    PLUGIN_VERSION,
)
class DeepSeekSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = None
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001 - 数据目录不可用时降级为不持久化
            logger.warning(
                f"[deepdeep] 无法获取数据目录，统计和调用日志不会持久化: {exc}"
            )
        self.data_dir = data_dir

        stats = UsageStats(data_dir / "usage.json" if data_dir else None)
        self.service = SearchService(
            DeepSeekToolsClient(self._resolve_api_key),
            Settings(),
            stats=stats,
            budget=BudgetGuard(stats),
            breaker=CircuitBreaker(),
            activity=ActivityLog(),
        )
        self.router = ToolRouter()
        self.tavily = TavilySearch(TavilyClient(self.tavily_keys))
        self.tavily_enabled = True
        self.call_log = self._open_call_log(data_dir)
        self.recorder = ToolCallRecorder(
            self.call_log,
            self.service.activity,
            own_tools=OWN_TOOLS,
            plugin_resolver=_resolve_plugin_name,
        )
        self._cache_ttl: float | None = None
        self.apply_config()

        self.web = WebApi(self, PLUGIN_NAME, PLUGIN_VERSION)
        self.web.register(context)

    @staticmethod
    def _open_call_log(data_dir) -> CallLog:
        if data_dir is not None:
            try:
                return CallLog(data_dir / "calls.db")
            except Exception as exc:  # noqa: BLE001 - 数据库打不开时退回内存，不影响搜索
                logger.warning(
                    f"[deepdeep] 调用日志数据库打开失败，改用内存（重启后清空）: {exc}"
                )
        return CallLog(None)

    # ---------- 配置 ----------

    def apply_config(self) -> None:
        """根据当前配置刷新各组件。启动时和 WebUI 保存设置后调用。"""
        c = self.config
        svc = self.service
        svc.settings = Settings.from_config(c)
        svc.client.configure(
            timeout=cfg_float(c, "request_timeout", 20, 3, 120),
            max_retries=cfg_int(c, "max_retries", 2, 0, 5),
            qps=cfg_float(c, "qps_limit", 5, 0.1, 5),
        )
        ttl = cfg_float(c, "cache_ttl_seconds", 600, 0, 86400)
        if ttl != self._cache_ttl:
            svc.cache = TTLCache(ttl)
            self.tavily.cache = TTLCache(ttl)
            self._cache_ttl = ttl
        self.tavily_enabled = bool(c.get("tavily_enabled", True))
        self.tavily.client.timeout = cfg_float(c, "request_timeout", 20, 3, 120)
        self.tavily.default_count = svc.settings.default_count
        self.tavily.max_count = svc.settings.max_count
        self.tavily.max_chars = svc.settings.max_search_chars
        currency = get_currency(str(c.get("billing_currency", DEFAULT_CURRENCY)))
        svc.stats.price = currency.price
        svc.budget.currency = currency
        svc.budget.daily_limit = cfg_float(c, "daily_budget", 0, 0, 100000)
        svc.budget.monthly_limit = cfg_float(c, "monthly_budget", 30, 0, 1000000)
        svc.breaker.threshold = cfg_int(c, "circuit_threshold", 3, 1, 50)
        svc.breaker.cooldown = cfg_float(c, "circuit_cooldown_seconds", 300, 10, 86400)
        svc.breaker.hard_cooldown = max(svc.breaker.cooldown, 1800)
        svc.activity.record_targets = bool(c.get("record_queries", True))
        self.usd_cny_rate = cfg_float(c, "exchange_rate", 7.1, 0, 100)
        self.recorder.enabled = bool(c.get("call_log_enabled", True))
        self.recorder.record_other_content = bool(
            c.get("call_log_record_other_content", False)
        )
        self.call_log.retention_days = cfg_float(
            c, "call_log_retention_days", 14, 1, 365
        )
        self.router = ToolRouter(
            str(c.get("routing_mode", DEFAULT_MODE)),
            c.get("fallback_tool_patterns"),
            c.get("specialist_rules"),
            own_enabled=bool(c.get("own_tools_enabled", True)),
            lane_rules=c.get("search_lane_rules"),
        )
        self.inject_guide = bool(c.get("inject_search_guide", True))

    def resolve_key_source(self) -> tuple[str | None, str]:
        """返回 (key, 来源说明)。"""
        key = str(self.config.get("api_key", "") or "").strip()
        if key:
            return key, "插件配置"
        try:
            providers = self.context.get_all_providers()
        except Exception:  # noqa: BLE001 - 框架内部异常不应影响搜索
            return None, ""
        for provider in providers:
            pc = getattr(provider, "provider_config", None) or {}
            if DEEPSEEK_HOST not in str(pc.get("api_base", "")):
                continue
            for k in provider.get_keys():
                if k and k.strip():
                    return k.strip(), f"复用提供商 {pc.get('id', '')}".strip()
        return None, ""

    def _resolve_api_key(self) -> str | None:
        return self.resolve_key_source()[0]

    @staticmethod
    def resolve_plugin(module: str) -> str | None:
        return _resolve_plugin_name(module)

    def _builtin_web_search(self) -> dict:
        try:
            return self.context.get_config().get("provider_settings", {}) or {}
        except Exception:  # noqa: BLE001 - 读不到配置时当作没有
            return {}

    def tavily_key_source(self) -> tuple[list[str], str]:
        """返回 (Key 列表, 来源说明)。插件配置优先，否则复用 AstrBot 网页搜索里的 Tavily Key。"""
        own = str(self.config.get("tavily_api_key", "") or "").strip()
        if own:
            return [own], "插件配置"
        keys = self._builtin_web_search().get("websearch_tavily_key") or []
        if isinstance(keys, str):
            keys = [keys]
        keys = [k.strip() for k in keys if isinstance(k, str) and k.strip()]
        return keys, ("复用 AstrBot 网页搜索的 Tavily Key" if keys else "")

    def tavily_keys(self) -> list[str]:
        return self.tavily_key_source()[0]

    def tavily_usable(self, names: list[str] | None = None) -> tuple[bool, str]:
        """本次请求要不要提供 deepdeep_tavily_search，以及不提供的原因。"""
        if not self.tavily_enabled:
            return False, "已在插件设置中关闭"
        if not self.tavily.client.has_key():
            return False, "没有找到 Tavily API Key"
        ps = self._builtin_web_search()
        builtin = ps.get("web_search") and ps.get("websearch_provider") == "tavily"
        if builtin or (names and "web_search_tavily" in names):
            return False, "AstrBot 内置网页搜索已经是 Tavily，避免重复"
        return True, ""

    # ---------- 工具路由 ----------

    # 优先级设为最低，在其他插件的 on_llm_request 钩子之后运行，
    # 这样它们追加的搜索工具也能被正确路由
    @filter.on_llm_request(priority=-100)
    async def route_search_tools(self, event: AstrMessageEvent, req: ProviderRequest):
        """按路由模式增删本次请求的搜索工具，并加上分工说明。

        关闭“启用自带搜索”时，把 DeepDeep 自己的工具从本次请求里拿掉，其余照常。
        """
        toolset = getattr(req, "func_tool", None)
        if not toolset:
            return
        names = toolset.names()
        managed = any(n in OWN_TOOLS or n == TAVILY_TOOL for n in names)
        # Tavily 不可用（关闭、没有 Key、内置网页搜索已经是 Tavily）时只在本次请求里拿掉
        if TAVILY_TOOL in names and not self.tavily_usable(names)[0]:
            toolset.remove_tool(TAVILY_TOOL)
        avail = self.service.availability("search")
        state = self.router.apply(toolset, avail.ok, req, avail.code)
        event.set_extra(ROUTING_EXTRA_KEY, state)
        # 分工提示：只在本插件参与的请求里加，内容随工具集固定，不破坏提示词缓存
        if managed and self.inject_guide:
            guide = self.router.guide(toolset.names())
            if guide:
                base = getattr(req, "system_prompt", None) or ""
                req.system_prompt = f"{base}\n\n{guide}\n"
        if state.hidden_own or state.hidden_fallback:
            logger.debug(
                f"[deepdeep] 路由 mode={self.router.mode} available={avail.ok} "
                f"hidden_own={state.hidden_own} "
                f"hidden_fallback={[t.name for t in state.hidden_fallback]}"
            )

    def _disabled_text(self, kind: str) -> str | None:
        """关闭了自带搜索，但模型仍调用了（比如其他插件发起的请求没经过路由）。"""
        if self.router.own_enabled:
            return None
        label = "联网搜索" if kind == "search" else "网页抓取"
        return f"DeepDeep 自带的{label}已在插件设置中关闭，请改用其他搜索工具。"

    def _finish(
        self, event: AstrMessageEvent, outcome: ToolOutcome, kind: str = "search"
    ) -> str:
        self._trace(event, outcome)
        if not outcome.should_fallback:
            return outcome.text
        state = event.get_extra(ROUTING_EXTRA_KEY)
        soft = outcome.reason in SOFT_REASONS
        budget = outcome.reason.startswith("budget")
        retry = "再读取一次" if kind == "fetch" else "再搜一次"

        if self.router.mode == "guide_only":
            # 不改工具列表（保持提示词缓存），只在工具结果里建议换哪个工具
            names = state.toolset.names() if state is not None else []
            alts = self.router.alternatives(names, kind, outcome.reason)
            self.service.activity.annotate(
                outcome.request_id,
                fallback={"mode": self.router.mode, "suggested_tools": alts},
            )
            if not alts:
                if soft:
                    return outcome.text
                return f"{outcome.text}\n请告知用户联网搜索暂时不可用，并根据已有知识谨慎回答。"
            listed = "、".join(alts[:3])
            if soft:
                return f"{outcome.text}\n如果结果不够好，可以改用 {listed} {retry}。"
            extra = "；不要改用其他同样走 DeepSeek 计费的工具" if budget else ""
            return f"{outcome.text}\n请改用 {listed} 继续完成任务{extra}。"

        drop_own = not soft
        restored = self.router.restore_fallback(
            state, drop_own=drop_own, reason=outcome.reason
        )
        self.service.activity.annotate(
            outcome.request_id,
            fallback={
                "mode": self.router.mode,
                "restored_tools": restored,
                "deepseek_tools_removed": bool(restored) and drop_own,
            },
        )
        if restored:
            logger.info(
                f"[deepdeep] #{outcome.request_id} 降级：已放出备用工具 {restored}"
            )
            # 只推荐同一类工具：抓取失败推荐抓取工具，搜索不理想推荐搜索工具
            same_kind = self.router.alternatives(restored, kind, outcome.reason)
            if soft:
                if not same_kind:
                    return outcome.text
                return (
                    f"{outcome.text}\n如果结果不够好，可以改用 {'、'.join(same_kind[:3])} {retry}"
                    "（已为本次对话启用）。"
                )
            return (
                f"{outcome.text}\n已为本次对话启用备用搜索工具：{'、'.join(restored)}。"
                "请改用这些工具继续完成任务。"
            )
        if soft:
            return outcome.text
        if self.router.mode == "deepseek_only":
            return f"{outcome.text}\n请告知用户联网搜索暂时不可用，并根据已有知识谨慎回答。"
        return (
            f"{outcome.text}\n当前没有可用的备用搜索工具，"
            "请告知用户联网搜索暂时不可用，并根据已有知识谨慎回答。"
        )

    @staticmethod
    def _trace(event: AstrMessageEvent, outcome: ToolOutcome) -> None:
        """同时写一条到 AstrBot 官方 Trace（WebUI 的“追踪”页面，需在设置中开启）。"""
        trace = getattr(event, "trace", None)
        if trace is None:
            return
        try:
            trace.record(
                "deepdeep",
                request_id=outcome.request_id,
                status=outcome.text.split("\n", 1)[0],
                fallback=outcome.should_fallback,
                reason=outcome.reason,
            )
        except Exception as exc:  # noqa: BLE001 - Trace 失败不影响工具结果
            logger.debug(f"[deepdeep] 写入 Trace 失败: {exc}")

    # ---------- 调用日志：记录所有工具的调用 ----------

    @filter.on_using_llm_tool()
    async def record_tool_start(self, event: AstrMessageEvent, tool, tool_args):
        try:
            await self.recorder.on_start(
                id(event), getattr(event, "unified_msg_origin", None), tool, tool_args
            )
        except Exception as exc:  # noqa: BLE001 - 记录失败绝不能影响工具调用
            logger.warning(f"[deepdeep] 记录工具调用失败: {exc}")

    @filter.on_llm_tool_respond()
    async def record_tool_end(
        self, event: AstrMessageEvent, tool, tool_args, tool_result
    ):
        try:
            await self.recorder.on_end(id(event), tool, tool_result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[deepdeep] 记录工具结果失败: {exc}")

    @filter.on_agent_done()
    async def record_agent_done(self, event: AstrMessageEvent, run_context, response):
        try:
            await self.recorder.on_agent_done(id(event))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[deepdeep] 收尾调用日志失败: {exc}")

    @filter.llm_tool(name=OWN_TOOLS[0])
    async def deepdeep_web_search(
        self, event: AstrMessageEvent, query: str, count: int = 0
    ) -> str:
        """联网搜索网页，获取实时信息（新闻、事实核查、价格、版本、天气、赛事、人物近况等）。每条结果带长摘要、链接、发布日期、相关度和权威度。

        查询写法：用简洁的关键词，不要整句提问。支持以下语法，可组合使用：
        site:github.com 只搜某网站（多个用 OR 连接：site:a.com OR site:b.com）；-site:x.com 排除网站；
        "精确短语"；-关键词 排除词；lang:zh,en 限定语言；after:2026-01-01 / before:2026-06 限定发布时间。
        结果按相关度而不是时间排序：问“最新、最近、今天”这类问题时，务必用 after: 限定为今天往前几天的日期，
        并以结果里的日期为准，不要把较早的内容当成最新。B 站等视频平台的页面收录有限，可改搜相关报道。
        摘要不够详细时，再用 deepdeep_web_fetch 读取具体网页。

        Args:
            query(string): 搜索关键词，最多 128 字，可包含上面的语法
            count(number): 返回结果数，可不填，默认 5
        """
        disabled = self._disabled_text("search")
        if disabled:
            return disabled
        outcome = await self.service.search(query, count, event.message_str)
        return self._finish(event, outcome)

    @filter.llm_tool(name=OWN_TOOLS[1])
    async def deepdeep_web_fetch(self, event: AstrMessageEvent, url: str) -> str:
        """读取指定网页的完整内容（Markdown 格式）。用于用户发来链接、或搜索结果摘要不够详细时。

        Args:
            url(string): 完整的网页链接，以 http:// 或 https:// 开头
        """
        disabled = self._disabled_text("fetch")
        if disabled:
            return disabled
        outcome = await self.service.fetch(url)
        return self._finish(event, outcome, "fetch")

    @filter.llm_tool(name=TAVILY_TOOL)
    async def deepdeep_tavily_search(
        self, event: AstrMessageEvent, query: str, count: int = 0
    ) -> str:
        """用 Tavily 联网搜索，擅长英文网页和技术资料：编程、软件版本号、安全漏洞（CVE）、官方文档、开源项目等。

        结果大多不带发布日期；问新闻、比赛、“最新、最近”这类时效问题时优先用 deepdeep_web_search。

        Args:
            query(string): 搜索关键词，用简洁的关键词，技术问题建议用英文
            count(number): 返回结果数，可不填，默认 5
        """
        ok, why = self.tavily_usable()
        if not ok:
            return f"Tavily 搜索暂不可用：{why}。请改用其他搜索工具。"
        outcome = await self.tavily.search(query, count)
        if not outcome.should_fallback:
            return outcome.text
        state = event.get_extra(ROUTING_EXTRA_KEY)
        names = state.toolset.names() if state is not None else []
        alts = [n for n in names if n == OWN_TOOLS[0] and self.router.own_enabled]
        alts += [
            n for n in self.router.alternatives(names, "search") if n != TAVILY_TOOL
        ]
        if not alts:
            return outcome.text
        listed = "、".join(alts[:3])
        if outcome.reason == "empty":
            return f"{outcome.text}\n可以改用 {listed} 再搜一次。"
        return f"{outcome.text}\n请改用 {listed} 继续完成任务。"

    # ---------- 管理命令 ----------

    async def run_connectivity_test(self) -> dict:
        """真实请求一次（绕过缓存和预算检查），供命令和 WebUI 使用。"""
        if not self.service.client.has_key():
            return {
                "ok": False,
                "message": "未找到 DeepSeek API Key：请在插件配置中填写，"
                "或添加 api_base 为 https://api.deepseek.com 的提供商。",
            }
        svc = self.service
        rid = new_request_id()
        trace: list[dict] = []
        try:
            results = await svc.client.search("DeepSeek", count=1, trace=trace)
        except DeepSeekToolsError as exc:
            svc.stats.record("search", "failed")
            svc.activity.add(
                "search",
                "[连接测试]",
                "failed",
                request_id=rid,
                status=exc.status,
                note=exc.message,
                detail={
                    "attempts": trace,
                    "error": {
                        "status": exc.status,
                        "message": exc.message,
                        "detail": exc.detail,
                    },
                },
            )
            logger.warning(
                f"[deepdeep] #{rid} 连接测试失败 status={exc.status} detail={exc.detail!r}"
            )
            await self._log_test(rid, "error", trace, exc.status, exc.message)
            return {
                "ok": False,
                "message": exc.message,
                "detail": exc.detail,
                "request_id": rid,
            }
        svc.breaker.record_success()
        outcome = "billable" if results else "empty"
        svc.stats.record("search", outcome)
        svc.activity.add(
            "search",
            "[连接测试]",
            outcome,
            request_id=rid,
            status=200,
            latency_ms=sum(a["latency_ms"] for a in trace),
            results=len(results),
            detail={"attempts": trace},
        )
        logger.info(f"[deepdeep] #{rid} 连接测试成功")
        await self._log_test(rid, "ok" if results else "empty", trace, 200, None)
        first = f"{results[0].title} {results[0].url}" if results else ""
        return {
            "ok": True,
            "message": "连接正常（本次测试计费 1 次）",
            "first_result": first,
            "request_id": rid,
        }

    async def _log_test(self, rid, status, trace, http_status, error) -> None:
        try:
            await asyncio.to_thread(
                self.call_log.add,
                tool="[连接测试]",
                source="deepseek",
                status=status,
                request_id=rid,
                http_status=http_status,
                error=error,
                duration_ms=sum(a["latency_ms"] for a in trace),
                detail={"attempts": trace},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[deepdeep] 记录连接测试失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("deepdeep")
    async def deepdeep_command(self, event: AstrMessageEvent, action: str = "stats"):
        """DeepSeek 搜索管理：/deepdeep stats 查看用量，/deepdeep test 测试连通性，/deepdeep reset 解除熔断"""
        action = (action or "stats").strip().lower()
        if action == "test":
            r = await self.run_connectivity_test()
            prefix = "✅" if r["ok"] else "❌"
            extra = f"\n首条结果：{r['first_result']}" if r.get("first_result") else ""
            yield event.plain_result(f"{prefix} {r['message']}{extra}")
        elif action == "stats":
            report = self.service.stats_report(self.usd_cny_rate)
            data = await asyncio.to_thread(self.web.search_stats_data, "24h")
            if data["rows"]:
                parts = [
                    f"{r['tool']} {r['total']} 次"
                    + (f"（失败 {r['error']}）" if r["error"] else "")
                    for r in data["rows"][:8]
                ]
                report += "\n近 24 小时各搜索工具调用：" + "、".join(parts)
            # 面板上点了“忽略”的提醒这里也不再列出
            setup, health = await asyncio.to_thread(self.web.problems, "24h")
            flagged = [
                f"{tool}（{h['verdict_label']}：{h['reason']}）"
                for tool, h in health.items()
                if h["verdict"] in ("broken", "degraded") and not h["ignored"]
            ]
            if flagged:
                report += "\n⚠ 可能有问题的搜索工具：\n" + "\n".join(flagged[:6])
            missing = [
                it
                for it in setup["items"]
                if it["status"] == "missing" and not it["ignored"]
            ]
            if missing:
                report += "\n⚠ 配置不完整：\n" + "\n".join(
                    f"{it['title']}：{it['detail']}" for it in missing[:6]
                )
            yield event.plain_result(report)
        elif action == "reset":
            self.service.breaker.reset()
            yield event.plain_result("已解除熔断，下次搜索会重新尝试 DeepSeek。")
        else:
            yield event.plain_result(
                "用法：/deepdeep stats 查看用量；/deepdeep test 测试连通性；/deepdeep reset 解除熔断"
            )

    async def terminate(self):
        await self.service.client.close()
        await self.tavily.client.close()
        try:
            self.call_log.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[deepdeep] 关闭调用日志失败: {exc}")


def _resolve_plugin_name(module: str) -> str | None:
    """根据工具处理函数所在模块找到所属插件名（与 AstrBot 自己的解析方式一致）。"""
    try:
        from astrbot.core.star.star import star_map
    except Exception:  # noqa: BLE001 - 内部接口变化时只是显示不出插件名
        return None
    for module_path, metadata in list(star_map.items()):
        if not module_path or not getattr(metadata, "name", None):
            continue
        package = module_path.rpartition(".")[0]
        if module == module_path or module.startswith(package + "."):
            return metadata.name
    return None
