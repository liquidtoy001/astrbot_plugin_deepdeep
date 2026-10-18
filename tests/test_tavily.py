"""DeepDeep 自带的 Tavily 搜索和“按问题类型分工”。"""

from core import tavily
from core.router import TAVILY_TOOL, ToolRouter, parse_lane_rules
from core.tavily import TavilyClient, TavilySearch, format_results


def tavily_result(i, **extra):
    item = {
        "title": f"T{i}",
        "url": f"https://docs.example.com/{i}",
        "content": f"内容{i}",
        "score": 0.9 - i / 10,
    }
    item.update(extra)
    return item


def make_search(fake_api, keys=("tvly-a",)):
    client = TavilyClient(lambda: list(keys), api_url=f"{fake_api.base_url}/search")
    return TavilySearch(client)


def test_tool_name_matches_router():
    assert tavily.TOOL_NAME == TAVILY_TOOL


async def test_search_formats_results_and_sends_key(fake_api):
    s = make_search(fake_api)
    fake_api.tavily_queue.append(
        (
            200,
            {
                "results": [
                    tavily_result(1, published_date="2026-10-01"),
                    tavily_result(2),
                ]
            },
            {},
        )
    )
    out = await s.search("Python 3.14 new features", 3)
    await s.client.close()
    req = fake_api.requests[0]
    assert req["auth"] == "Bearer tvly-a"
    assert req["json"] == {
        "query": "Python 3.14 new features",
        "max_results": 3,
        "search_depth": "basic",
    }
    assert out.text.startswith(f"[Tavily 搜索 #{out.request_id}] 成功 · 2 条结果")
    assert (
        "日期: 2026-10-01" in out.text
        and "链接: https://docs.example.com/2" in out.text
    )
    assert not out.should_fallback


async def test_cache_and_count_defaults(fake_api):
    s = make_search(fake_api)
    s.cache = tavily.TTLCache(60)
    fake_api.tavily_queue.append((200, {"results": [tavily_result(1)]}, {}))
    await s.search("q", None)
    second = await s.search("q", 0)
    await s.client.close()
    assert (
        len(fake_api.requests) == 1 and fake_api.requests[0]["json"]["max_results"] == 5
    )
    assert "缓存" in second.text.split("\n", 1)[0]


async def test_bad_key_falls_over_to_next_key(fake_api):
    s = make_search(fake_api, keys=("tvly-bad", "tvly-good"))
    fake_api.tavily_queue += [
        (401, {"detail": "bad"}, {}),
        (200, {"results": [tavily_result(1)]}, {}),
    ]
    out = await s.search("q", 1)
    await s.client.close()
    assert [r["auth"] for r in fake_api.requests] == [
        "Bearer tvly-bad",
        "Bearer tvly-good",
    ]
    assert "成功" in out.text


async def test_quota_error_suggests_fallback(fake_api):
    s = make_search(fake_api)
    fake_api.tavily_queue.append((432, {"detail": "limit"}, {}))
    out = await s.search("q", 1)
    await s.client.close()
    assert out.should_fallback and out.reason == "http_432"
    assert "本月额度已用完" in out.text


async def test_empty_and_no_key(fake_api):
    s = make_search(fake_api)
    fake_api.tavily_queue.append((200, {"results": []}, {}))
    out = await s.search("q", 1)
    assert out.should_fallback and out.reason == "empty"
    none = TavilySearch(TavilyClient(lambda: []))
    out2 = await none.search("q", 1)
    assert out2.should_fallback and "没有可用的 Tavily API Key" in out2.text
    await s.client.close()


def test_format_truncates_long_content():
    text = format_results("q", [tavily.TavilyResult("t", "u", "字" * 2000)])
    assert "…" in text and len(text) < 1000


# ---------- 按问题类型分工 ----------

NAMES = [
    "deepdeep_web_search",
    "deepdeep_web_fetch",
    TAVILY_TOOL,
    "web_search_serpapi",
    "searxng_web_search_general",
    "searxng_web_search_images",
]


def test_parse_lane_rules():
    assert parse_lane_rules(["技术问题 = a, B*", "无效", " = x", "y ="]) == [
        ("技术问题", ["a", "b*"])
    ]
    assert parse_lane_rules("oops")[0][1][0] == TAVILY_TOOL


def test_guide_has_tech_lane_and_tavily_first_backup():
    guide = ToolRouter().guide(NAMES)
    assert (
        "- 新闻时事、中文资料、海外新闻、比赛和最新动态：优先 deepdeep_web_search；"
        f"没有结果或结果不理想时再用 {TAVILY_TOOL}、web_search_serpapi、searxng_web_search_general"
    ) in guide
    assert (
        f"- 编程、软件版本、安全漏洞、官方文档等技术问题：优先 {TAVILY_TOOL}；不理想时再用 deepdeep_web_search"
        in guide
    )


def test_no_lane_tool_keeps_old_guide():
    names = [n for n in NAMES if n != TAVILY_TOOL]
    guide = ToolRouter().guide(names)
    assert "通用网页搜索（新闻、事实、中文资料等）：优先 deepdeep_web_search" in guide
    assert "技术问题" not in guide


def test_lane_primary_not_hidden_in_deepseek_first_but_hidden_in_deepseek_only():
    hidden = ToolRouter("deepseek_first").plan(NAMES, True)
    assert TAVILY_TOOL not in hidden and "web_search_serpapi" in hidden
    assert TAVILY_TOOL in ToolRouter("deepseek_only").plan(NAMES, True)


def test_lanes_and_preview_show_tech_lane():
    r = ToolRouter()
    lanes = r.lanes(NAMES)
    assert [lane["key"] for lane in lanes[:3]] == [
        "search",
        "lane:编程、软件版本、安全漏洞、官方文档等技术问题",
        "fetch",
    ]
    assert lanes[1]["primary"] == TAVILY_TOOL and lanes[1]["backups"] == [
        "deepdeep_web_search"
    ]
    assert lanes[0]["backups"][0] == TAVILY_TOOL
    row = {t["name"]: t for t in r.preview(NAMES)}[TAVILY_TOOL]
    assert row["role"] == "fallback" and row["specialty"].endswith("技术问题首选")
    assert r.search_category(TAVILY_TOOL) == "deepdeep"


def test_lane_line_dropped_when_tavily_is_general_primary():
    r = ToolRouter(own_enabled=False)
    names = [n for n in NAMES if not n.startswith("deepdeep_web")]
    guide = r.guide(names)
    assert f"通用网页搜索（新闻、事实、中文资料等）：优先 {TAVILY_TOOL}" in guide
    assert "技术问题" not in guide
    assert not any(lane["key"].startswith("lane:") for lane in r.lanes(names))


def test_empty_lane_rules_disable_lanes():
    assert "技术问题" not in ToolRouter(lane_rules=[]).guide(NAMES)


def test_tavily_extract_is_not_ranked_as_search():
    r = ToolRouter()
    alts = r.alternatives(["tavily_extract_web_page", "searxng_web_fetch_url"], "fetch")
    assert alts == ["searxng_web_fetch_url", "tavily_extract_web_page"]
