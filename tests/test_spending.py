"""所有付费搜索服务的花费估算、每月上限，以及路由预览。"""

import types

import pytest
from test_plugin import (
    CURRENT_REQUEST,
    FakeConfig,
    FakeContext,
    FakeEvent,
    FakeToolSet,
    api,
    plugin_module,  # noqa: F401 - pytest 夹具
)

OWN = ["deepdeep_deepseek_search", "deepdeep_deepseek_fetch", "deepdeep_tavily_search"]


def make(module, tools=(), provider=None, **config):
    ctx = FakeContext(tool_names=[*OWN, *tools], web_search_provider=provider)
    return module.DeepSeekSearchPlugin(ctx, FakeConfig({"exchange_rate": 7, **config}))


def use(plugin, tool, n):
    plugin.tool_caps.counter.add(tool, n)


def service(report, key):
    return next(x for x in report["services"] if x["key"] == key)


def report(plugin):
    return plugin.web.spending_data()


async def routed(plugin, names):
    toolset = FakeToolSet(names)
    req = types.SimpleNamespace(func_tool=toolset, system_prompt="")
    await plugin.route_search_tools(FakeEvent(), req)
    return toolset.names(), req.system_prompt


# ---------- 估算 ----------


def test_cost_is_calls_times_price_in_billing_currency(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    use(plugin, "web_search_brave", 1000)  # $5 × 7
    r = report(plugin)
    assert service(r, "brave")["cost"] == pytest.approx(35)
    assert (
        r["total"] == pytest.approx(35)
        and r["estimated"] is True
        and r["currency"] == "cny"
    )
    assert service(r, "brave")["share"] == 1

    # 同一个数据目录：重启后次数还在，这里只换了币种
    usd = make(plugin_module, billing_currency="usd")
    assert report(usd)["total"] == pytest.approx(5)


def test_monthly_free_quota_is_deducted_and_shared(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    plugin.tavily.usage.add("total", 700)  # DeepDeep 的 Tavily 工具
    use(plugin, "web_search_tavily", 500)  # 内置工具，同一个账户
    s = service(report(plugin), "tavily")
    assert s["calls"] == 1200 and s["free_quota"] == 1000 and s["free_used"] == 1000
    assert s["cost"] == pytest.approx(200 * 0.008 * 7)

    use(plugin, "web_search_serpapi", 250)
    assert service(report(plugin), "serpapi")["cost"] == 0
    use(plugin, "image_search_serpapi", 10)  # 同一家的工具一起算
    assert service(report(plugin), "serpapi")["cost"] == pytest.approx(10 * 0.025 * 7)


def test_one_time_quota_counts_as_paid_and_says_so(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    use(plugin, "deepdeep_serper_search", 100)
    s = service(report(plugin), "serper")
    assert s["cost"] == pytest.approx(100 * 0.001 * 7) and s["free_quota"] == 0
    assert "一次性" in s["note"]


def test_unknown_price_is_listed_not_counted_as_zero(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    use(plugin, "web_search_bocha", 50)
    use(plugin, "my_web_search", 3)  # 认不出是哪一家
    r = report(plugin)
    assert r["total"] == 0 and not r["services"]
    assert {(x["name"], x["calls"]) for x in r["excluded"]} == {
        ("博查搜索", 50),
        ("my_web_search", 3),
    }


def test_free_services_are_not_paid_rows(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    use(plugin, "searxng_web_search_general", 500)
    use(plugin, "keenable_web_search", 500)
    r = report(plugin)
    assert not r["services"] and not r["excluded"]


def test_zero_exchange_rate_cannot_convert_so_not_counted(plugin_module):  # noqa: F811
    plugin = make(plugin_module, exchange_rate=0)
    use(plugin, "web_search_brave", 1000)
    r = report(plugin)
    assert r["total"] == 0 and [x["key"] for x in r["excluded"]] == ["brave"]


def test_deepseek_uses_its_billable_count_plus_builtin_tools(plugin_module):  # noqa: F811
    plugin = make(plugin_module, api_key="sk-test-12345678")
    for _ in range(200):
        plugin.service.stats.record("search", "billable")
    plugin.service.stats.record("search", "failed")  # 失败的不算
    plugin.service.stats.record("search", "cache_hit")  # 缓存不算
    use(plugin, "web_search_deepseek", 100)
    s = service(report(plugin), "deepseek")
    assert s["calls"] == 300
    assert s["cost"] == pytest.approx(300 * 0.005)


def test_extra_search_cache_hits_are_not_billed(plugin_module):  # noqa: F811
    plugin = make(plugin_module, perplexity_api_key="pplx-secret-123456")
    tool = "deepdeep_perplexity_search"
    # 工具钩子不记 DeepDeep 自己的工具，只有真正发出去的请求才算
    plugin._count_tool_use(types.SimpleNamespace(name=tool), types.SimpleNamespace())
    assert plugin.tool_caps.counter.count(tool) == 0
    plugin.extra_search.on_billed(tool)
    assert plugin.tool_caps.counter.count(tool) == 1


def test_inactive_key_means_no_deepseek_row(plugin_module):  # noqa: F811
    assert not report(make(plugin_module))["services"]
    with_key = report(make(plugin_module, api_key="sk-test-12345678"))
    assert service(with_key, "deepseek")["cost"] == 0


# ---------- 上限 ----------


async def test_over_cap_removes_paid_tools_keeps_free_and_quota_left(plugin_module):  # noqa: F811
    plugin = make(plugin_module, monthly_budget=10)
    names = [
        "web_search_brave",
        "web_search_serpapi",  # 免费额度还没用完
        "searxng_web_search_general",  # 免费
        "web_search_bocha",  # 不知道价格
        "set_user_nickname",
    ]
    before, _ = await routed(plugin, names)
    assert "web_search_brave" in before
    use(plugin, "web_search_brave", 1000)  # 35 元，超了
    after, prompt = await routed(plugin, names)
    assert "web_search_brave" not in after
    assert {
        "web_search_serpapi",
        "searxng_web_search_general",
        "web_search_bocha",
        "set_user_nickname",
    } <= set(after)
    assert "web_search_brave" not in prompt
    r = report(plugin)
    assert r["over"] is True
    # AstrBot 的工具开关不动
    assert all(t.active for t in plugin.context._tools.func_list)
    await plugin.terminate()


async def test_over_cap_also_makes_deepseek_unavailable(plugin_module):  # noqa: F811
    plugin = make(plugin_module, api_key="sk-test-12345678", monthly_budget=10)
    assert plugin.service.availability("search").ok
    use(plugin, "web_search_brave", 1000)
    avail = plugin.service.availability("search")
    assert not avail.ok and avail.code == "budget_monthly"
    await plugin.terminate()


async def test_month_rollover_restores_tools(plugin_module):  # noqa: F811
    plugin = make(plugin_module, monthly_budget=10)
    use(plugin, "web_search_brave", 1000)
    assert "web_search_brave" not in (await routed(plugin, ["web_search_brave"]))[0]
    plugin.tool_caps.counter._months.clear()
    assert "web_search_brave" in (await routed(plugin, ["web_search_brave"]))[0]
    await plugin.terminate()


def test_daily_limit_still_counts_deepseek_only(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module, api_key="sk-test-12345678", daily_budget=1, monthly_budget=0
    )
    use(plugin, "web_search_brave", 1000)
    assert plugin.service.availability("search").ok
    assert not report(plugin)["over"]  # 0 表示不限制


async def test_per_tool_limit_and_total_cap_are_independent(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        monthly_budget=1000,
        tool_monthly_limits=["web_search_brave = 10"],
    )
    use(plugin, "web_search_brave", 10)
    names, _ = await routed(plugin, ["web_search_brave", "web_search_exa"])
    assert names == ["web_search_exa"]  # 单个工具的上限先到
    assert not report(plugin)["over"]
    await plugin.terminate()


async def test_zero_spend_leaves_prompt_untouched(plugin_module):  # noqa: F811
    plugin = make(plugin_module, monthly_budget=0.001)
    names = ["web_search_brave", "deepdeep_deepseek_search"]
    kept, _ = await routed(plugin, names)
    assert "web_search_brave" in kept
    await plugin.terminate()


# ---------- 自己填的单价 ----------


def test_custom_price_counts_and_converts(plugin_module):  # noqa: F811
    plugin = make(plugin_module, custom_prices=["bocha = 36 cny"])
    use(plugin, "web_search_bocha", 100)
    r = report(plugin)
    s = service(r, "bocha")
    assert s["cost"] == pytest.approx(3.6) and "单价" in s["note"]
    assert not r["excluded"] and r["total"] == pytest.approx(3.6)
    # 换成美元计费时按汇率换算
    usd = make(plugin_module, custom_prices=["bocha = 36 cny"], billing_currency="usd")
    assert report(usd)["total"] == pytest.approx(3.6 / 7)
    # 汇率是 0 时换算不了，回到未计入
    zero = make(
        plugin_module,
        custom_prices=["bocha = 36 cny"],
        billing_currency="usd",
        exchange_rate=0,
    )
    assert [x["key"] for x in report(zero)["excluded"]] == ["bocha"]


async def test_custom_priced_service_is_removed_over_cap(plugin_module):  # noqa: F811
    plugin = make(plugin_module, custom_prices=["bocha = 36 cny"], monthly_budget=3)
    names = ["web_search_bocha", "web_search_baidu"]
    assert (await routed(plugin, names))[0] == names
    use(plugin, "web_search_bocha", 100)
    # 博查填了单价，超限后拿掉；百度没填，留着
    assert (await routed(plugin, names))[0] == ["web_search_baidu"]
    await plugin.terminate()


def test_custom_price_for_unrecognised_tool(plugin_module):  # noqa: F811
    plugin = make(plugin_module, custom_prices=["tool:my_web_search = 10 cny"])
    use(plugin, "my_web_search", 100)
    r = report(plugin)
    assert service(r, "tool:my_web_search")["cost"] == pytest.approx(1)
    assert plugin.spending.blocked(["my_web_search"], assume_over=True) == {
        "my_web_search"
    }


def test_price_zero_means_free_and_stays(plugin_module):  # noqa: F811
    plugin = make(plugin_module, custom_prices=["bocha = 0 cny"], monthly_budget=1)
    use(plugin, "web_search_bocha", 100)
    use(plugin, "web_search_brave", 1000)
    assert plugin.spending.blocked(["web_search_bocha", "web_search_brave"]) == {
        "web_search_brave"
    }


def test_bad_price_lines_are_ignored():
    from core.spending import format_price_rules, parse_price_rules

    rules = parse_price_rules(
        ["bocha = 36 cny", "x", "a = 1", "b = -1 cny", "c = 1 eur", "d = y cny", 3]
    )
    assert rules == {"bocha": (36.0, "cny")}
    assert format_price_rules(rules) == ["bocha = 36 cny"]
    assert parse_price_rules(None) == {}


async def test_price_is_saved_from_the_panel_and_shown_on_the_card(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    rows = {e["key"]: e for e in (await api(plugin, "search-tools")())["catalog"]}
    assert rows["bocha"]["price"] == {"key": "bocha", "amount": None, "currency": "cny"}
    # 有官方价格的、免费的、DeepSeek 都不用填
    for key in ("brave", "searxng", "deepseek"):
        assert "price" not in rows[key], key
    CURRENT_REQUEST.body = {"custom_prices": ["bocha = 36 cny"]}
    status = await api(plugin, "settings", "POST")()
    assert status["settings"]["custom_prices"] == ["bocha = 36 cny"]
    rows = {e["key"]: e for e in (await api(plugin, "search-tools")())["catalog"]}
    assert rows["bocha"]["price"]["amount"] == 36
    await plugin.terminate()


# ---------- 路由预览 ----------


def states(plugin):
    return plugin.web.route_preview(plugin.router)


def tools_with(plugin, names):
    plugin.context._tools.func_list.extend(types.SimpleNamespace(name=n) for n in names)


def test_preview_guide_only_says_nothing_is_hidden(plugin_module):  # noqa: F811
    plugin = make(plugin_module, tools=["web_search_brave"], api_key="sk-test-12345678")
    data = states(plugin)
    ok = {r["key"]: r for r in data["states"]["ok"]["rows"]}
    assert ok["search"]["plain"] == "一般问题"
    assert ok["search"]["first"][0]["label"] == "DeepSeek 搜索"
    assert not ok["search"]["failed"]
    down = data["states"]["down"]
    assert down["effect"] == "advice"
    assert [x["label"] for x in down["advice"]] == ["Brave Search"]
    row = next(r for r in down["rows"] if r["key"] == "search")
    assert row["failed"] and row["advice"][0]["label"] == "Brave Search"


def test_preview_hide_mode_swaps_and_only_lists_changes(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        tools=["web_search_brave"],
        api_key="sk-test-12345678",
        routing_mode="deepseek_first",
    )
    data = states(plugin)
    assert data["states"]["down"]["effect"] == "swap"
    row = next(r for r in data["states"]["down"]["rows"] if r["key"] == "search")
    assert row["first"][0]["label"] == "Brave Search" and not row["failed"]
    # 正常时 Brave 被隐藏
    ok = next(r for r in data["states"]["ok"]["rows"] if r["key"] == "search")
    assert ok["first"][0]["label"] == "DeepSeek 搜索" and not ok["rest"]


def test_preview_deepseek_only_has_no_backup(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        tools=["web_search_brave"],
        api_key="sk-test-12345678",
        routing_mode="deepseek_only",
    )
    assert states(plugin)["states"]["down"]["effect"] == "no_backup"


def test_preview_without_deepseek_nothing_changes(plugin_module):  # noqa: F811
    plugin = make(plugin_module, tools=["web_search_brave"])
    down = states(plugin)["states"]["down"]
    assert down["effect"] == "no_own" and not down["rows"]


def test_preview_over_cap_removes_paid_tools(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        tools=["web_search_brave", "searxng_web_search_general"],
        api_key="sk-test-12345678",
    )
    budget = states(plugin)["states"]["budget"]
    assert [x["label"] for x in budget["removed"]] == ["Brave Search"]
    labels = [x["label"] for x in budget["advice"]]
    assert "Brave Search" not in labels and labels


def test_preview_still_lists_removed_tools_when_already_over(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        tools=["web_search_brave"],
        api_key="sk-test-12345678",
        monthly_budget=10,
    )
    use(plugin, "web_search_brave", 1000)
    assert "web_search_brave" not in plugin.web.search_tool_names()
    data = states(plugin)["states"]
    assert [x["label"] for x in data["budget"]["removed"]] == ["Brave Search"]
    ok = next(r for r in data["ok"]["rows"] if r["key"] == "search")
    assert [x["label"] for x in ok["rest"]] == ["Brave Search"]


def test_preview_counts_other_tools_and_groups_one_backend(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        tools=[
            "set_user_nickname",
            "searxng_web_search_general",
            "searxng_web_search_news",
        ],
        api_key="sk-test-12345678",
    )
    data = states(plugin)
    assert data["other_tools"] == 1
    row = next(r for r in data["states"]["ok"]["rows"] if r["key"] == "search")
    assert len(row["rest"]) == 2 or row["rest"][0]["tools"]


async def test_preview_comes_with_the_tools_route(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    data = await api(plugin, "tools")()
    assert set(data["scenarios"]["states"]) == {"ok", "down", "budget"}
    assert "tools" in data  # 老字段还在
    await plugin.terminate()


async def test_catalog_rows_carry_filter_fields(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    rows = {e["key"]: e for e in (await api(plugin, "search-tools")())["catalog"]}
    assert rows["bocha"]["region"] == "domestic" and rows["bocha"]["free"] is False
    assert rows["brave"]["region"] == "foreign"
    assert rows["searxng"]["free"] is True
    assert rows["tavily"]["free_quota"] is True
    await plugin.terminate()
