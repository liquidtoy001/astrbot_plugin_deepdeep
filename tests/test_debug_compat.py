"""调试信息规范化、社区搜索插件兼容、“最新”类查询的结果过滤。"""

import re

from conftest import make_result

from core.activity import ActivityLog
from core.cache import TTLCache
from core.client import DeepSeekToolsClient, SearchResult
from core.formatter import format_search_results, has_date_filter, wants_recent
from core.guard import BudgetGuard, CircuitBreaker
from core.router import ToolRouter
from core.service import SearchService, Settings, status_line
from core.stats import UsageStats

STATUS_RE = re.compile(r"^\[DeepSeek (搜索|抓取) #[0-9a-f]{6}\] ")


def make_service(fake_api, no_sleep, record=True, **settings):
    stats = UsageStats(None)
    client = DeepSeekToolsClient(
        lambda: "k", base_url=fake_api.base_url, qps=0, sleep=no_sleep, max_retries=1
    )
    return SearchService(
        client,
        Settings(**settings),
        cache=TTLCache(600),
        stats=stats,
        budget=BudgetGuard(stats),
        breaker=CircuitBreaker(threshold=5),
        activity=ActivityLog(record_targets=record),
    )


# ---------- 社区搜索插件兼容 ----------

# 2026-10 插件市场中会和 DeepSeek 重复的搜索工具
COMMUNITY_FALLBACK = {
    "web_search_tavily": False,
    "tavily_extract_web_page": True,
    "searxng_web_search_general": False,
    "searxng_web_fetch_url": True,
    "kimi_web_search": False,
    "kimi_web_fetch": True,
    "grok_web_search": False,
    "grok_web_fetch": True,
    "mmx_web_search": False,
    "anysearch_search": False,
    "anysearch_batch_search": False,
    "anysearch_extract": True,
    "gemini_search": False,
    "web_fetch": True,
    "exa-search": False,
    "web_fetch_exa": True,
    # 同类 DeepSeek 搜索插件：和本插件同时安装时由本插件接管
    "deepseek_web_search": False,
    "web_search_deepseek": False,
    "deepseek_fetch_web_page": True,
}
# 专用工具或无关工具，不能被当成备用搜索
NOT_FALLBACK = [
    "searxng_web_search_images",
    "searxng_web_search_videos",
    "searxng_github_search",
    "webshot_analyze",
    "webshot_send",
    "exa-code-context",
    "cf_browser_markdown",
    "cf_browser_crawl_start",
    "mmx_generate_image",
    "search_memes",
    "set_user_nickname",
    "harness_submit_code",
    "deepdeep_web_search",
    "deepdeep_web_fetch",
]


def test_community_search_tools_are_recognized():
    r = ToolRouter("deepseek_first", lane_rules=[])
    for name, fetch_like in COMMUNITY_FALLBACK.items():
        assert r.is_fallback(name), name
        assert r.is_fetch_like(name) is fetch_like, name
    for name in NOT_FALLBACK:
        assert not r.is_fallback(name), name


def test_kimi_and_grok_hidden_while_deepseek_available():
    names = [
        "deepdeep_web_search",
        "deepdeep_web_fetch",
        *COMMUNITY_FALLBACK,
        *NOT_FALLBACK,
    ]
    hidden = ToolRouter("deepseek_first", lane_rules=[]).plan(names, True)
    assert hidden == set(COMMUNITY_FALLBACK)


# ---------- 状态行与请求 ID ----------


def test_status_line_format():
    assert status_line("search", "abc123", "成功", "", "5 条结果") == (
        "[DeepSeek 搜索 #abc123] 成功 · 5 条结果"
    )


async def test_every_tool_result_starts_with_status_line(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    fake_api.search_queue += [
        (200, {"results": [make_result(1)]}, {}),
        (503, {"error": {"message": "busy"}}, {}),
        (503, {"error": {"message": "busy"}}, {}),
    ]
    fake_api.fetch_queue.append((200, {"title": "T", "content": "abc"}, {}))
    outs = [
        await svc.search("ok", None),
        await svc.search("ok", None),  # 缓存
        await svc.search("bad", None),  # 503 重试 1 次后失败
        await svc.search("  ", None),  # 本地拦截
        await svc.fetch("https://example.com/x"),
        await svc.fetch("nope"),
    ]
    await svc.client.close()
    for out in outs:
        assert STATUS_RE.match(out.text), out.text
        assert f"#{out.request_id}]" in out.text.splitlines()[0]
    assert "] 成功 · 1 条结果" in outs[0].text  # 本地模拟服务耗时可能为 0，省略耗时
    assert "] 缓存 · 1 条结果" in outs[1].text
    assert "] 失败 · HTTP 503 · 已重试 1 次" in outs[2].text
    assert "] 未调用 · 搜索关键词为空" in outs[3].text
    assert "] 成功 · 3 字" in outs[4].text
    assert "] 未调用 · 链接无效" in outs[5].text


async def test_activity_detail_records_request_attempts_and_results(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    fake_api.search_queue += [
        (429, {"error": {"message": "slow"}}, {}),
        (200, {"results": [make_result(1, score=0.4), make_result(2)]}, {}),
    ]
    out = await svc.search("q", 3, "用户问的话")
    await svc.client.close()
    entry = svc.activity.recent(1)[0]
    assert entry["request_id"] == out.request_id
    d = entry["detail"]
    assert d["request"] == {
        "query": "q",
        "count": 3,
        "api_count": 10,
        "need_content": False,
        "ori_question": "用户问的话",
    }
    assert [a["status"] for a in d["attempts"]] == [429, 200]
    assert d["attempts"][0]["retry_in_s"] == 1.0
    assert [r["score"] for r in d["results"]] == [0.4, 2.0]
    assert d["output_chars"] == len(out.text.split("\n", 1)[1])


async def test_activity_detail_respects_privacy_switch(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep, record=False)
    fake_api.search_queue.append((200, {"results": [make_result(1)]}, {}))
    await svc.search("秘密问题", None, "我的隐私")
    await svc.client.close()
    entry = svc.activity.recent(1)[0]
    text = repr(entry)
    assert (
        "秘密问题" not in text and "我的隐私" not in text and "example.com" not in text
    )
    assert entry["detail"]["request"]["count"] == 5  # 非隐私字段保留


async def test_error_detail_and_annotate(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    fake_api.search_queue.append(
        (402, {"error": {"message": "Insufficient Balance"}}, {})
    )
    out = await svc.search("q", None)
    await svc.client.close()
    assert svc.activity.annotate(out.request_id, fallback={"restored_tools": ["x"]})
    assert not svc.activity.annotate("nope", fallback={})
    d = svc.activity.recent(1)[0]["detail"]
    assert (
        d["error"]["status"] == 402 and d["error"]["detail"] == "Insufficient Balance"
    )
    assert d["error"]["circuit_opened"] is True
    assert d["fallback"] == {"restored_tools": ["x"]}


# ---------- “最新”类查询 ----------


def _res(score, date):
    return SearchResult.from_api(make_result(int(score * 100), score=score, date=date))


def test_query_intent_detection():
    assert has_date_filter("极客湾 after:2026-10-12")
    assert has_date_filter("before:2026 x")
    assert not has_date_filter("aftermath")
    assert wants_recent("极客湾 最新视频")
    assert wants_recent("latest RTX news")
    assert not wants_recent("极客湾 最新视频 after:2026-10-12")
    assert not wants_recent("麒麟 9050 评测")


def test_date_filtered_query_keeps_low_score_fresh_results():
    # 复现：after: 查询下，10/14–10/15 的正确结果分数只有 0.4–0.8
    results = [
        _res(1.24, "2026-10-15"),
        _res(0.82, "2026-10-15"),
        _res(0.39, "2026-10-14"),
    ]
    text = format_search_results("极客湾 after:2026-10-12", results, min_score=1.0)
    assert "共 3 条" in text and "2026-10-14" in text


def test_recent_query_keeps_all_and_warns_about_ordering():
    results = [_res(1.6, "2026-10-01"), _res(0.5, "2026-10-15")]
    text = format_search_results("极客湾 最新视频", results, min_score=1.0)
    assert "共 2 条" in text
    assert "按相关度而不是发布时间排序" in text
    assert "本次最新一条发布于 2026-10-15" in text
    assert "after:YYYY-MM-DD" in text


def test_plain_query_still_filtered():
    results = [_res(s, "2026-10-01") for s in (1.6, 1.2, 0.9, 0.8, 0.3)]
    text = format_search_results("麒麟 9050 评测", results, count=5, min_score=0.5)
    assert "共 4 条" in text and "按相关度而不是发布时间" not in text
    assert "相关度: 0.3" not in text


def test_recent_query_newest_date_counts_all_results_and_appends_newest():
    # 复现：前 5 条最新的是 10/04，但 API 返回的第 8 条是 10/16
    dates = [
        "2026-10-01",
        "2026-09-20",
        "2026-10-04",
        "2026-08-01",
        "2026-09-01",
        "2026-07-01",
        "2026-06-01",
        "2026-10-16",
        "2026-10-10",
        "2026-05-01",
    ]
    results = [_res(2.0 - i * 0.1, d) for i, d in enumerate(dates)]
    text = format_search_results("极客湾 最新视频", results, count=5, min_score=0.5)
    assert "本次最新一条发布于 2026-10-16" in text
    assert "共 7 条" in text
    assert "以下是日期较新、但相关度排名靠后的结果" in text
    assert text.index("2026-10-16") < text.index("2026-10-10")


def test_recent_query_no_extra_when_top_results_are_newest():
    results = [
        _res(1.6, "2026-10-16"),
        _res(1.0, "2026-10-01"),
        _res(0.9, "2026-09-01"),
    ]
    text = format_search_results("极客湾 最新视频", results, count=2, min_score=0.5)
    assert "共 2 条" in text and "日期较新" not in text
