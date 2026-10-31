"""面板上的大类、状态灯，以及用量图的数据。"""

from test_plugin import (
    FakeConfig,
    FakeContext,
    api,
    plugin_module,  # noqa: F401 - pytest 夹具
)

from core.route_view import ToolStatus, arrange, category_of
from core.router import ToolRouter

OWN = ["deepdeep_web_search", "deepdeep_web_fetch"]
SPECIAL = [
    "image_search_serpapi",
    "searxng_web_search_videos",
    "searxng_web_search_music",
    "searxng_web_search_academic",
    "scholar_search_serpapi",
    "search_papers",
    "search_code",
    "search_issues",
    "searxng_github_search",
    "finance_search_serpapi",
    "searxng_web_search_map",
]


def rows_for(names):
    rows, missing = arrange(ToolRouter().lanes(names))
    return {r["key"]: r for r in rows}, missing


def test_media_and_academic_are_one_category_each():
    rows, _ = rows_for([*OWN, *SPECIAL])
    assert set(rows["cat:media"]["names"]) == {
        "image_search_serpapi",
        "searxng_web_search_videos",
        "searxng_web_search_music",
    }
    assert set(rows["cat:academic"]["names"]) == {
        "searxng_web_search_academic",
        "scholar_search_serpapi",
        "search_papers",
    }
    assert sum(1 for k in rows if "academic" in k or "paper" in k) == 1
    assert not any(k.startswith("special:") for k in rows)


def test_categories_with_no_tool_are_named():
    _, missing = rows_for(OWN)
    for title in ("新闻时事", "法律", "医学健康", "游戏", "财经行情"):
        assert title in missing, title
    _, missing = rows_for([*OWN, "finance_search_serpapi"])
    assert "财经行情" not in missing


def test_news_row_shares_the_general_first_choice():
    names = [*OWN, "searxng_web_search_news", "searxng_web_search_general"]
    rows, missing = rows_for(names)
    assert "新闻时事" not in missing
    news = rows["cat:news"]
    assert news["names"] == ["deepdeep_web_search", "searxng_web_search_news"]
    assert "searxng_web_search_news" not in rows["search"]["names"]
    # 路由的数据不变
    assert "searxng_web_search_news" in rows["search"]["backups"]


def test_user_rule_outside_the_categories_keeps_its_own_row():
    router = ToolRouter(specialist_rules=["*weather* = 查天气", "*legal* = 查法条"])
    rows, missing = arrange(router.lanes([*OWN, "get_weather", "legal_lookup"]))
    keys = {r["key"]: r for r in rows}
    assert keys["special:查天气"]["plain"] == "查天气"
    assert keys["cat:law"]["plain"] == "法律"
    assert "法律" not in missing


def test_category_patterns_do_not_catch_unrelated_names():
    assert category_of("lawn_care_search") is None
    assert category_of("search_pubmed").key == "medical"
    assert category_of("astr_kb_search").key == "kb"


def test_lamp_is_green_when_unused_and_shows_problems():
    health = {
        "a": {"verdict": "broken", "total": 5},
        "b": {"verdict": "degraded", "total": 2},
        "c": {"verdict": "unknown", "total": 1},
        "d": {"verdict": "broken", "total": 5, "ignored": True},
    }
    status = ToolStatus(health, {"e": "missing", "f": "warn"})
    assert status.lamp(["never_called"]) == "ok"
    assert status.lamp(["c"]) == "ok"
    assert status.lamp(["d"]) == "ok"
    assert status.lamp(["a", "c"]) == "bad"
    assert status.lamp(["b"]) == "warn"
    assert status.lamp(["e"]) == "bad"
    assert status.lamp(["f"]) == "warn"
    assert status.calls(["a", "b", "never_called"]) == 7


def test_one_service_is_listed_once():
    status = ToolStatus({}, {})
    items = status.items(["search_code", "search_issues", "searxng_github_search"])
    labels = [x["label"] for x in items]
    assert len(labels) == len(set(labels))
    github = next(x for x in items if "GitHub" in x["label"])
    assert set(github["tools"]) == {"search_code", "search_issues"}


async def test_overview_lanes_have_lamps_and_plain_names(plugin_module):  # noqa: F811
    ctx = FakeContext(
        tool_names=[*OWN, "search_code", "search_issues", "search_papers"]
    )
    plugin = plugin_module.DeepSeekSearchPlugin(ctx, FakeConfig({"api_key": "sk-x"}))
    data = await api(plugin, "overview")()
    rows = {r["key"]: r for r in data["lanes"]}
    assert rows["search"]["first"][0]["lamp"] == "ok"
    assert rows["cat:code"]["group"] == "special"
    assert (
        len(rows["cat:code"]["first"][0]["tools"]) == 2 and not rows["cat:code"]["rest"]
    )
    assert "法律" in data["missing_categories"]
    await plugin.terminate()


async def test_overview_lamp_red_without_key(plugin_module):  # noqa: F811
    plugin = plugin_module.DeepSeekSearchPlugin(
        FakeContext(tool_names=OWN), FakeConfig({})
    )
    data = await api(plugin, "overview")()
    rows = {r["key"]: r for r in data["lanes"]}
    assert rows["search"]["first"][0]["lamp"] == "bad"
    await plugin.terminate()


async def test_usage_counts_other_tools_without_double_counting(plugin_module):  # noqa: F811
    plugin = plugin_module.DeepSeekSearchPlugin(FakeContext(), FakeConfig({}))
    for tool, status in (
        ("web_search_bocha", "ok"),
        ("web_search_bocha", "error"),
        ("web_search_bocha", "blocked"),  # 被拦下的不算
        ("searxng_web_fetch_url", "ok"),
        ("deepdeep_web_search", "ok"),  # 用 DeepSeek 自己的计费统计，不重复算
        ("astr_kb_search", "ok"),  # 不算联网搜索
        ("set_user_nickname", "ok"),  # 不是搜索工具
    ):
        plugin.call_log.add(tool=tool, source="x", status=status)
    plugin.service.stats.record("search", "billable")
    days = (await api(plugin, "usage")())["days"]
    today = days[-1]
    assert today["other_search"] == 2
    assert today["other_fetch"] == 1
    assert today["search"]["billable"] == 1
    await plugin.terminate()
