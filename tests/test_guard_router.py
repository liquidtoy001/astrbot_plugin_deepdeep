from datetime import date

from conftest import make_result

from core.activity import ActivityLog
from core.cache import TTLCache
from core.client import DeepSeekToolsClient
from core.guard import BudgetGuard, CircuitBreaker
from core.router import ToolRouter
from core.service import SearchService, Settings
from core.stats import UsageStats


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def stats_on(day):
    return UsageStats(None, today=lambda: day)


# ---------- 费用上限 ----------


def test_budget_daily_and_monthly_limits():
    stats = stats_on(date(2026, 10, 17))
    # 默认按人民币计费，每次 ¥0.005；上限 ¥0.0175 允许 3 次
    guard = BudgetGuard(stats, daily_limit=0.0175, monthly_limit=0)
    for _ in range(3):
        assert guard.check("search").ok
        stats.record("search", "billable")
    blocked = guard.check("search")
    assert not blocked.ok and blocked.code == "budget_daily"

    guard = BudgetGuard(stats, daily_limit=0, monthly_limit=0.0175)
    assert guard.check("fetch").code == "budget_monthly"
    assert BudgetGuard(stats).check("search").ok  # 0 = 不限制


def test_month_cost_resets_on_new_month():
    day = [date(2026, 10, 31)]
    stats = UsageStats(None, today=lambda: day[0])
    for _ in range(4):
        stats.record("search", "billable")
    assert stats.month_cost() > 0
    day[0] = date(2026, 11, 1)
    assert stats.month_cost() == 0
    assert stats.cost(2) > 0  # 滚动窗口仍能看到昨天


def test_budget_snapshot_warning():
    stats = stats_on(date(2026, 10, 17))
    for _ in range(9):
        stats.record("search", "billable")
    snap = BudgetGuard(stats, daily_limit=0.05).snapshot()
    assert snap["warning"] is True and 0.8 < snap["daily_ratio"] < 1


def test_series_shape():
    stats = stats_on(date(2026, 10, 17))
    stats.record("fetch", "billable")
    series = stats.series(3)
    assert [d["date"] for d in series] == ["2026-10-15", "2026-10-16", "2026-10-17"]
    assert series[-1]["fetch"]["billable"] == 1 and series[-1]["cost"] > 0


# ---------- 熔断 ----------


def test_breaker_opens_after_threshold_and_half_opens():
    clock = Clock()
    b = CircuitBreaker(threshold=3, cooldown_seconds=60, clock=clock)
    assert not b.record_failure(500, "e")
    assert not b.record_failure(0, "network")
    assert b.record_failure(503, "e")  # 第三次打开
    assert not b.check().ok and b.check().code == "circuit_open"
    clock.t += 61
    assert b.check().ok  # 半开放行
    assert b.record_failure(500, "still down")  # 试探失败立即重新打开
    assert not b.check().ok
    b.record_success()
    assert b.check().ok and b.consecutive_failures == 0


def test_breaker_hard_failures_and_request_specific_errors():
    clock = Clock()
    b = CircuitBreaker(
        threshold=3, cooldown_seconds=60, hard_cooldown_seconds=1800, clock=clock
    )
    for status in (400, 403, 404, 404, 404):
        assert not b.record_failure(status, "page")
    assert b.check().ok and b.consecutive_failures == 0
    assert b.record_failure(402, "余额不足")
    clock.t += 120
    assert not b.check().ok  # 402 冷却更久
    clock.t += 1800
    assert b.check().ok


def test_success_resets_consecutive_count():
    b = CircuitBreaker(threshold=2)
    b.record_failure(500, "e")
    b.record_success()
    assert not b.record_failure(500, "e")


# ---------- 路由 ----------

NAMES = [
    "deepdeep_web_search",
    "deepdeep_web_fetch",
    "web_search_tavily",
    "tavily_extract_web_page",
    "searxng_web_search_general",
    "searxng_web_search_images",
    "searxng_github_search",
    "searxng_web_fetch_url",
    "astrbot_execute_shell",
]
FALLBACK = {
    "web_search_tavily",
    "tavily_extract_web_page",
    "searxng_web_search_general",
    "searxng_web_fetch_url",
}
OWN = {"deepdeep_web_search", "deepdeep_web_fetch"}


def test_default_patterns_classify_known_tools():
    r = ToolRouter("deepseek_first")
    assert {n for n in NAMES if r.is_fallback(n)} == FALLBACK


def test_plan_per_mode():
    assert ToolRouter("deepseek_first").plan(NAMES, True) == FALLBACK
    assert ToolRouter("deepseek_first").plan(NAMES, False) == OWN
    assert ToolRouter("deepseek_only").plan(NAMES, False) == FALLBACK
    assert ToolRouter("fallback_only").plan(NAMES, True) == OWN
    assert ToolRouter("coexist").plan(NAMES, False) == set()
    assert ToolRouter("bogus").mode == "guide_only"  # 非法值回退到默认模式


def test_deepseek_first_without_fallback_keeps_own_tools():
    # 没有备用工具时不藏自己，模型才能拿到“暂不可用”的说明
    assert (
        ToolRouter("deepseek_first").plan([*OWN, "astrbot_execute_shell"], False)
        == set()
    )


def test_custom_patterns_and_case_insensitivity():
    r = ToolRouter(fallback_patterns=["My_Search*"], specialist_rules=[])
    assert r.is_fallback("my_search_tool") and not r.is_fallback("web_search_tavily")
    # 非法配置回退到默认
    assert (
        ToolRouter(fallback_patterns="oops").fallback_patterns
        == ToolRouter("deepseek_first").fallback_patterns
    )


# ---------- 服务层与守卫的配合 ----------


def make_service(fake_api, no_sleep, **kw):
    stats = UsageStats(None)
    client = DeepSeekToolsClient(
        lambda: "k", base_url=fake_api.base_url, qps=0, sleep=no_sleep, max_retries=0
    )
    return SearchService(
        client,
        Settings(),
        cache=TTLCache(600),
        stats=stats,
        budget=BudgetGuard(stats, **kw),
        breaker=CircuitBreaker(threshold=2),
        activity=ActivityLog(),
    )


async def test_budget_blocks_before_request_but_cache_still_served(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep, daily_limit=0.005)
    fake_api.search_queue.append((200, {"results": [make_result(1)]}, {}))
    first = await svc.search("q", None)
    assert not first.should_fallback
    cached = await svc.search("q", None)  # 缓存免费，不受预算限制
    assert cached.text.split("\n", 1)[1] == first.text.split("\n", 1)[1]
    blocked = await svc.search("other", None)
    await svc.client.close()
    assert blocked.should_fallback and blocked.reason == "budget_daily"
    assert len(fake_api.requests) == 1
    assert [e["outcome"] for e in svc.activity.recent()] == [
        "blocked",
        "cache_hit",
        "billable",
    ]


async def test_breaker_opens_and_blocks(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    fake_api.search_queue += [(500, {"error": {"message": "x"}}, {})] * 2
    a = await svc.search("a", None)
    b = await svc.search("b", None)
    c = await svc.search("c", None)
    await svc.client.close()
    assert a.should_fallback and b.should_fallback
    assert c.reason == "circuit_open"
    assert len(fake_api.requests) == 2
    assert "已触发熔断" in svc.activity.recent()[1]["note"]


async def test_bad_query_does_not_suggest_fallback(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    fake_api.search_queue.append((400, {"error": {"message": "query too long"}}, {}))
    out = await svc.search("q", None)
    await svc.client.close()
    assert not out.should_fallback and "query too long" in out.text


async def test_no_key_is_reported(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    svc.client._api_key_getter = lambda: ""
    out = await svc.search("q", None)
    await svc.client.close()
    assert out.reason == "no_key" and fake_api.requests == []


def test_activity_log_respects_privacy_switch():
    log = ActivityLog(max_entries=2, record_targets=False)
    log.add("search", "秘密问题", "billable")
    log.record_targets = True
    log.add("search", "公开问题", "billable")
    log.add("fetch", "https://x", "failed", status=404)
    items = log.recent()
    assert [i["target"] for i in items] == [
        "https://x",
        "公开问题",
    ]  # 最多 2 条，新的在前


# ---------- 路由边界情况 ----------


class _TS:
    """最小工具集，按对象身份区分两个工具集。"""

    def __init__(self, names):
        self.tools = [type("T", (), {"name": n})() for n in names]

    def names(self):
        return [t.name for t in self.tools]

    def get_tool(self, name):
        return next((t for t in self.tools if t.name == name), None)

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]

    def add_tool(self, tool):
        self.remove_tool(tool.name)
        self.tools.append(tool)


def test_no_deepseek_tools_in_request_means_no_change():
    # 例如人格只启用了 SearXNG，或插件在当前会话被禁用
    names = ["web_search_tavily", "searxng_web_search_general", "astrbot_execute_shell"]
    for mode in ("deepseek_first", "deepseek_only", "fallback_only"):
        assert ToolRouter(mode).plan(names, True) == set()
        assert ToolRouter(mode).plan(names, False) == set()


def test_only_hides_the_kind_deepseek_can_replace():
    names = [
        "deepdeep_web_search",
        "web_search_tavily",
        "tavily_extract_web_page",
        "searxng_web_fetch_url",
    ]
    assert ToolRouter("deepseek_first").plan(names, True) == {
        "web_search_tavily"
    }  # 没有 deepdeep_web_fetch，抓取工具保留
    names = ["deepdeep_web_fetch", "web_search_tavily", "searxng_web_fetch_url"]
    assert ToolRouter("deepseek_first").plan(names, True) == {"searxng_web_fetch_url"}


def test_restore_also_updates_replaced_request_toolset():
    # skills_like 模式：AstrBot 在钩子之后把 req.func_tool 换成精简版工具集
    router = ToolRouter("deepseek_first")
    raw = _TS(
        ["deepdeep_web_search", "deepdeep_web_fetch", "web_search_tavily", "other"]
    )
    req = type("Req", (), {})()
    req.func_tool = raw
    state = router.apply(raw, True, req)
    light = _TS(raw.names())
    req.func_tool = light
    restored = router.restore_fallback(state, drop_own=True)
    assert restored == ["web_search_tavily"]
    for ts in (raw, light):
        assert "web_search_tavily" in ts.names()
        assert "deepdeep_web_search" not in ts.names()
    # 重复调用是幂等的
    assert router.restore_fallback(state, drop_own=True) == ["web_search_tavily"]
    assert light.names().count("web_search_tavily") == 1


# ---------- 金额显示 ----------


def test_money_formatting():
    from core.pricing import CURRENCIES, format_money

    cny, usd = CURRENCIES["cny"], CURRENCIES["usd"]
    assert format_money(0.021, cny) == "¥0.021"
    assert format_money(0.021, cny, 7.1) == "¥0.021（$0.0030）"
    assert format_money(5, usd, 7.1) == "$5.00（¥35.50）"
    assert format_money(0.0412, usd) == "$0.0412"


def test_stats_report_uses_billing_currency():
    from core.pricing import CURRENCIES

    stats = stats_on(date(2026, 10, 17))
    for _ in range(4):
        stats.record("search", "billable")
    svc = SearchService(
        DeepSeekToolsClient(lambda: "k"),
        Settings(),
        stats=stats,
        budget=BudgetGuard(stats, monthly_limit=30),
    )
    report = svc.stats_report(7.1)
    assert "¥0.020（$0.0028）" in report and "¥30.00（$4.23）" in report
    assert "每千次 ¥5.00" in report
    assert "$" not in svc.stats_report(0)

    # 切换到美元计费：单价和显示都跟着变
    stats.price = CURRENCIES["usd"].price
    svc.budget.currency = CURRENCIES["usd"]
    svc.budget.monthly_limit = 5
    report = svc.stats_report(7.1)
    assert "$0.0030（¥0.021）" in report and "$5.00（¥35.50）" in report


# ---------- 超出预算时不降级到同一 DeepSeek 账户计费的工具 ----------

BUDGET_NAMES = [
    "deepdeep_web_search",
    "deepdeep_web_fetch",
    "web_search_deepseek",  # AstrBot 4.29.1 内置的 DeepSeek 搜索
    "deepseek_fetch_web_page",
    "web_search_tavily",
    "searxng_web_fetch_url",
]


def test_budget_cap_keeps_same_backend_hidden():
    r = ToolRouter("deepseek_first")
    assert r.is_same_backend("web_search_deepseek") and not r.is_same_backend(
        "deepdeep_web_search"
    )
    # 故障：所有备用工具都可以用
    assert r.plan(BUDGET_NAMES, False, "circuit_open") == {
        "deepdeep_web_search",
        "deepdeep_web_fetch",
    }
    # 超出预算：DeepSeek 计费的备用工具也藏起来
    assert r.plan(BUDGET_NAMES, False, "budget_daily") == {
        "deepdeep_web_search",
        "deepdeep_web_fetch",
        "web_search_deepseek",
        "deepseek_fetch_web_page",
    }


def test_budget_cap_with_only_same_backend_fallback():
    # 唯一的备用就是内置 DeepSeek：藏起它，保留自己的工具让模型拿到“超出预算”的说明
    names = ["deepdeep_web_search", "web_search_deepseek"]
    assert ToolRouter("deepseek_first").plan(names, False, "budget_monthly") == {
        "web_search_deepseek"
    }


def test_restore_skips_same_backend_on_budget():
    r = ToolRouter("deepseek_first")
    ts = _TS(BUDGET_NAMES)
    state = r.apply(ts, True)
    restored = r.restore_fallback(state, drop_own=True, reason="budget_daily")
    assert set(restored) == {"web_search_tavily", "searxng_web_fetch_url"}
    assert "web_search_deepseek" not in ts.names()
    # 其他原因的失败可以用内置 DeepSeek 兜底
    ts2 = _TS(BUDGET_NAMES)
    state2 = r.apply(ts2, True)
    assert "web_search_deepseek" in r.restore_fallback(
        state2, drop_own=True, reason="http_500"
    )


def test_preview_has_budget_column():
    rows = {t["name"]: t for t in ToolRouter("deepseek_first").preview(BUDGET_NAMES)}
    assert rows["web_search_deepseek"]["visible_when_down"] is True
    assert rows["web_search_deepseek"]["visible_when_budget"] is False
    assert rows["web_search_tavily"]["visible_when_budget"] is True
    assert rows["web_search_deepseek"]["same_backend"] is True


# ---------- 搜索分工 ----------


def test_specialist_rules_parse_and_match():
    r = ToolRouter(
        specialist_rules=["*image* = 找图片", "search_papers = 论文", "  ", 3, "*map*"]
    )
    assert r.specialty("searxng_web_search_images") == "找图片"
    assert r.specialty("search_papers") == "论文"
    assert r.specialty("searxng_web_search_map") == "专用搜索"  # 没写说明时的默认文案
    assert r.specialty("deepdeep_web_search") is None
    # 专用工具不算备用，即使匹配了备用规则
    assert not r.is_fallback("searxng_web_search_images")
    assert r.search_category("searxng_web_search_images") == "special"
    # 非法配置回退到默认规则
    assert ToolRouter(specialist_rules="oops").specialty("x_image_y") == "找图片"


def test_guide_lists_only_present_tools_in_rule_order():
    r = ToolRouter("deepseek_first")
    names = [
        "deepdeep_web_search",
        "deepdeep_web_fetch",
        "searxng_web_search_videos",
        "searxng_web_search_images",
        "search_papers",
        "searxng_web_search_academic",
        "set_user_nickname",
    ]
    lines = r.guide(names).splitlines()
    assert lines[0].startswith("【搜索工具分工】")
    assert lines[1:] == [
        "- 通用网页搜索（新闻、事实、中文资料等）：deepdeep_web_search",
        "- 读取网页全文：deepdeep_web_fetch",
        "- 找图片：searxng_web_search_images",
        "- 找视频（B 站、YouTube 等视频平台）：searxng_web_search_videos",
        "- 学术论文和期刊：searxng_web_search_academic",
        "- 学术论文（arXiv 等）：search_papers",
    ]


def test_guide_uses_fallback_tools_when_deepseek_hidden():
    r = ToolRouter("deepseek_first")
    guide = r.guide(
        ["web_search_tavily", "tavily_extract_web_page", "searxng_web_search_images"]
    )
    assert "通用网页搜索（新闻、事实、中文资料等）：web_search_tavily" in guide
    assert "读取网页全文：tavily_extract_web_page" in guide


def test_no_guide_without_specialists():
    assert (
        ToolRouter("deepseek_first").guide(
            ["deepdeep_web_search", "deepdeep_web_fetch"]
        )
        is None
    )


def test_guide_caps_long_groups():
    r = ToolRouter("deepseek_first")
    papers = [
        "download_paper",
        "get_paper_latex",
        "list_papers",
        "read_paper",
        "search_paper_text",
        "search_papers",
    ]
    guide = r.guide(["deepdeep_web_search", *papers])
    line = next(x for x in guide.splitlines() if x.startswith("- 学术论文（arXiv 等）"))
    # 带 search 的排在前面，最多列 3 个，其余用“等 N 个”概括
    assert (
        line
        == "- 学术论文（arXiv 等）：search_paper_text、search_papers、download_paper 等 6 个"
    )


def test_alternatives_rank_general_engines_first():
    r = ToolRouter("guide_only")
    names = [
        "searxng_web_search_books",
        "searxng_web_search_files",
        "searxng_web_search_general",
        "searxng_web_search_news",
        "web_search_tavily",
        "web_search_deepseek",
    ]
    assert r.alternatives(names, "search") == [
        "searxng_web_search_general",
        "web_search_tavily",
        "searxng_web_search_news",
        "searxng_web_search_books",
        "searxng_web_search_files",
        "web_search_deepseek",
    ]
