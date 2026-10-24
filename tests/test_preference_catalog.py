"""搜索偏好（预设、自定义顺序、不用）和「可用的搜索工具」目录。"""

from core import catalog
from core.preference import Preference, format_order, parse_order, resolve
from core.router import TECH_DESC, ToolRouter

NAMES = [
    "deepdeep_web_search",
    "deepdeep_web_fetch",
    "deepdeep_tavily_search",
    "web_search_serpapi",
    "searxng_web_search_general",
    "searxng_web_fetch_url",
    "searxng_web_search_images",
]


def test_parse_format_and_resolve():
    pref = parse_order(
        [
            "general = searxng_*, Deepdeep_web_search, !web_search_serpapi",
            "tech = deepdeep_tavily_search",
            "unknown = x",
            "bad line",
            3,
        ]
    )
    assert pref.order == {
        "general": ["searxng_*", "deepdeep_web_search"],
        "tech": ["deepdeep_tavily_search"],
    }
    assert pref.disabled == {"general": ["web_search_serpapi"]}
    assert format_order(pref.order, pref.disabled) == [
        "general = searxng_*, deepdeep_web_search, !web_search_serpapi",
        "tech = deepdeep_tavily_search",
    ]
    assert resolve("rec") is None and resolve("nonsense") is None
    assert resolve("cheap").order["general"][0] == "searxng_*general*"
    assert resolve("custom", ["read = x"]).order == {"read": ["x"]}


def test_sort_keeps_default_order_for_unlisted():
    pref = Preference({"general": ["b*"]}, {"general": ["c"]})
    assert pref.sort("general", ["a", "c", "b1", "d", "b2"]) == ["b1", "b2", "a", "d"]


def test_recommended_preset_keeps_old_guide():
    old = ToolRouter(lane_rules=None).guide(NAMES)
    assert ToolRouter(lane_rules=None, preference=resolve("rec")).guide(NAMES) == old


def test_custom_order_changes_guide_lanes_and_alternatives():
    pref = parse_order(
        [
            "general = deepdeep_tavily_search, web_search_serpapi, deepdeep_web_search",
            "tech = web_search_serpapi, deepdeep_tavily_search",
            "read = searxng_web_fetch_url, deepdeep_web_fetch",
        ]
    )
    r = ToolRouter("guide_only", lane_rules=None, preference=pref)
    guide = r.guide(NAMES)
    assert (
        "- 新闻时事、中文资料、海外新闻、比赛和最新动态：优先 deepdeep_tavily_search；"
        "没有结果或结果不理想时再用 web_search_serpapi、deepdeep_web_search、searxng_web_search_general"
        in guide
    )
    assert (
        f"- {TECH_DESC}：优先 web_search_serpapi；不理想时再用 deepdeep_tavily_search、deepdeep_web_search"
        in guide
    )
    assert (
        "- 读取网页全文：优先 searxng_web_fetch_url；读取失败时再用 deepdeep_web_fetch"
        in guide
    )
    assert "- 找图片：searxng_web_search_images" in guide
    assert r.alternatives(NAMES, "search")[:2] == [
        "deepdeep_tavily_search",
        "web_search_serpapi",
    ]
    lanes = {lane["key"]: lane for lane in r.lanes(NAMES)}
    assert lanes["search"]["primary"] == "deepdeep_tavily_search"
    assert lanes[f"lane:{TECH_DESC}"]["primary"] == "web_search_serpapi"
    assert lanes["fetch"]["primary"] == "searxng_web_fetch_url"


def test_tech_lane_dropped_when_same_as_general_primary():
    pref = parse_order(
        ["general = deepdeep_tavily_search", "tech = deepdeep_tavily_search"]
    )
    guide = ToolRouter(lane_rules=None, preference=pref).guide(NAMES)
    assert TECH_DESC not in guide and "通用网页搜索（新闻、事实、中文资料等）" in guide


def test_disabled_tools_only_when_off_in_every_scene():
    pref = parse_order(
        [
            "general = !searxng_web_search_general, !web_search_serpapi",
            "tech = !searxng_web_search_general",
            "read = !searxng_web_fetch_url",
        ]
    )
    r = ToolRouter(lane_rules=None, preference=pref)
    # serpapi 只在一般问题里不用，技术问题还会用，所以不拿掉
    assert r.disabled_tools(NAMES) == {
        "searxng_web_search_general",
        "searxng_web_fetch_url",
    }
    guide = r.guide(NAMES)
    assert "searxng_web_search_general" not in guide
    assert "searxng_web_fetch_url" not in guide


def test_deepseek_first_mode_keeps_deepseek_first():
    pref = parse_order(["general = deepdeep_tavily_search, deepdeep_web_search"])
    r = ToolRouter("deepseek_first", lane_rules=None, preference=pref)
    assert r.ordered("general", NAMES)[0] == "deepdeep_web_search"
    assert r.with_preference(None).preference is None and r.preference is pref


def test_presets_order_scenes():
    cheap = ToolRouter(lane_rules=None, preference=resolve("cheap"))
    assert cheap.ordered("general", NAMES)[0] == "searxng_web_search_general"
    assert cheap.ordered("read", NAMES)[0] == "searxng_web_fetch_url"
    zh = ToolRouter(lane_rules=None, preference=resolve("zh"))
    assert zh.ordered("tech", NAMES)[0] == "deepdeep_web_search"


def test_label_for():
    assert catalog.label_for("deepdeep_web_search") == "DeepSeek 搜索"
    assert catalog.label_for("deepdeep_web_fetch") == "DeepSeek 读网页"
    assert catalog.label_for("searxng_web_fetch_url") == "SearXNG 读网页"
    assert catalog.label_for("web_search_serpapi") == "SerpApi（Google 结果）"
    assert catalog.label_for("my_tool") == "my_tool"


def build_catalog(**over):
    args = {
        "deepseek": {"enabled": True, "configured": True},
        "tavily": {
            "enabled": True,
            "configured": True,
            "usable": True,
            "usage_text": "本月 Tavily 已用 3 次",
        },
        "provider_settings": {
            "web_search": True,
            "websearch_provider": "serpapi",
            "websearch_serpapi_key": ["k"],
            "websearch_tavily_key": ["t"],
        },
        "registered": {
            "deepdeep_web_search": True,
            "deepdeep_tavily_search": True,
            "searxng_web_search_general": True,
            "kimi_web_search": False,
            "weird_search": True,
        },
        "request_tools": ["web_search_serpapi"],
        "setup_status": {},
        "health": {
            "searxng_web_search_general": {
                "verdict": "broken",
                "reason": "5 次调用：失败 5",
            }
        },
        "search_tools": ["weird_search", "searxng_web_search_general"],
    }
    args.update(over)
    return {e["key"]: e for e in catalog.build(**args)}, catalog.build(**args)


def test_catalog_statuses_and_order():
    by_key, items = build_catalog()
    assert by_key["deepseek"]["status"] == "ok"
    assert (
        by_key["tavily"]["status"] == "ok"
        and by_key["tavily"]["detail"] == "本月 Tavily 已用 3 次"
    )
    assert by_key["serpapi"]["status"] == "active"
    assert by_key["tavily_builtin"]["status"] == "ready"
    assert "DeepDeep 的 Tavily 正在用这个 Key" in by_key["tavily_builtin"]["detail"]
    assert by_key["bocha"]["status"] == "no_key"
    assert (
        by_key["searxng"]["status"] == "unstable"
        and "失败 5" in by_key["searxng"]["detail"]
    )
    assert by_key["kimi"]["status"] == "disabled"
    assert (
        by_key["grok"]["status"] == "not_installed"
        and "插件市场" in by_key["grok"]["detail"]
    )
    assert by_key["other:weird_search"]["status"] == "ok"
    ranks = [catalog.STATUS[e["status"]][1] for e in items]
    assert ranks == sorted(ranks)
    assert all(e["strengths"] for e in items if not e["key"].startswith("other:"))


def test_catalog_missing_and_off():
    by_key, _ = build_catalog(
        deepseek={"enabled": False},
        tavily={
            "enabled": True,
            "configured": True,
            "usable": False,
            "reason": "本月 Tavily 调用已达上限",
        },
        provider_settings={"web_search": True, "websearch_provider": "bocha"},
        setup_status={"searxng_web_search_general": "missing"},
    )
    assert by_key["deepseek"]["status"] == "off"
    assert (
        by_key["tavily"]["status"] == "paused" and "上限" in by_key["tavily"]["detail"]
    )
    assert by_key["bocha"]["status"] == "missing"
    assert by_key["serpapi"]["status"] == "no_key"
    assert by_key["searxng"]["status"] == "missing"


def test_grouped_merges_one_backends_tools():
    rows = catalog.grouped(
        [
            "deepdeep_web_search",
            "searxng_web_search_news",
            "web_search_serpapi",
            "searxng_web_search_books",
        ],
        False,
    )
    assert [r["name"] for r in rows] == [
        "deepdeep_web_search",
        "searxng_*",
        "web_search_serpapi",
    ]
    sx = rows[1]
    assert sx["tools"] == ["searxng_web_search_news", "searxng_web_search_books"]
    assert sx["note"].startswith("2 个工具 · ") and sx["label"].startswith("SearXNG")
    assert catalog.grouped(["my_tool"], True) == [
        {
            "name": "my_tool",
            "label": "my_tool",
            "note": "",
            "tools": ["my_tool"],
            "disabled": True,
        }
    ]
    # 合成一行后保存的规则能排序它们全部
    pref = parse_order(["general = searxng_*, deepdeep_web_search"])
    assert pref.sort("general", ["deepdeep_web_search", "searxng_a", "searxng_b"]) == [
        "searxng_a",
        "searxng_b",
        "deepdeep_web_search",
    ]


def test_arxiv_mcp_tools_belong_to_one_entry():
    for tool in (
        "list_papers",
        "read_paper_section",
        "list_paper_latex_sections",
        "search_papers",
    ):
        assert catalog.entry_for(tool).key == "arxiv", tool
