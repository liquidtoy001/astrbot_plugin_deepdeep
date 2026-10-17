from datetime import date

from conftest import make_result

from core.cache import TTLCache
from core.client import DeepSeekToolsClient, FetchResult, SearchResult
from core.formatter import format_fetch_result, format_search_results
from core.service import SearchService, Settings, coerce_count
from core.stats import UsageStats


def body(text):
    """去掉第一行状态行。"""
    return text.split("\n", 1)[1]


def make_service(fake_api, no_sleep, tmp_path=None, ttl=600, **settings):
    client = DeepSeekToolsClient(
        lambda: "k", base_url=fake_api.base_url, qps=0, sleep=no_sleep
    )
    stats = UsageStats(tmp_path / "usage.json" if tmp_path else None)
    return SearchService(client, Settings(**settings), cache=TTLCache(ttl), stats=stats)


# ---------- service ----------


async def test_search_formats_and_caches(fake_api, no_sleep, tmp_path):
    fake_api.search_queue.append(
        (200, {"results": [make_result(1), make_result(2)]}, {})
    )
    svc = make_service(fake_api, no_sleep, tmp_path)

    first = (await svc.search("DeepSeek", 0, "DeepSeek 最近有什么新闻")).text
    second = (await svc.search("DeepSeek", 0, "另一个人问同样的事")).text
    await svc.client.close()

    # 第一行是状态行（含请求 ID），正文应一致
    assert body(first) == body(second)
    assert "] 成功 · 2 条结果" in first.splitlines()[0]
    assert "] 缓存 · 2 条结果" in second.splitlines()[0]
    assert "[1] 标题1" in first and "https://example.com/2" in first
    assert len(fake_api.requests) == 1  # 第二次走缓存
    assert fake_api.requests[0]["json"]["ori_question"] == "DeepSeek 最近有什么新闻"
    today = svc.stats.day()["search"]
    assert today["billable"] == 1 and today["cache_hit"] == 1


async def test_original_question_can_be_disabled(fake_api, no_sleep):
    fake_api.search_queue.append((200, {"results": [make_result(1)]}, {}))
    svc = make_service(fake_api, no_sleep, send_original_question=False)
    await svc.search("q", None, "原话")
    await svc.client.close()
    assert "ori_question" not in fake_api.requests[0]["json"]


async def test_empty_results_not_cached_or_billed(fake_api, no_sleep):
    fake_api.search_queue += [(200, {"results": []}, {}), (200, {"results": []}, {})]
    svc = make_service(fake_api, no_sleep)
    text = (await svc.search("xyz", None)).text
    await svc.search("xyz", None)
    await svc.client.close()
    assert "没有找到" in text
    assert len(fake_api.requests) == 2
    assert svc.stats.day()["search"] == {
        "billable": 0,
        "empty": 2,
        "failed": 0,
        "cache_hit": 0,
    }


async def test_search_error_becomes_friendly_text(fake_api, no_sleep):
    fake_api.search_queue.append(
        (402, {"error": {"message": "Insufficient Balance"}}, {})
    )
    svc = make_service(fake_api, no_sleep)
    text = (await svc.search("q", None)).text
    await svc.client.close()
    assert "] 失败 · HTTP 402" in text.splitlines()[0]
    assert body(text).startswith("联网搜索失败：") and "余额不足" in text
    assert svc.stats.day()["search"]["failed"] == 1


async def test_blank_query_does_not_call_api(fake_api, no_sleep):
    svc = make_service(fake_api, no_sleep)
    assert "为空" in (await svc.search("   ", None)).text
    await svc.client.close()
    assert fake_api.requests == []


async def test_fetch_flow(fake_api, no_sleep):
    fake_api.fetch_queue += [
        (200, {"title": "T", "content": "x" * 50}, {}),
        (404, {"error": {"message": "no content"}}, {}),
    ]
    svc = make_service(fake_api, no_sleep, max_fetch_chars=500)
    ok = (await svc.fetch("https://example.com/a")).text
    cached = (await svc.fetch("https://example.com/a")).text
    bad = (await svc.fetch("https://example.com/b")).text
    invalid = (await svc.fetch("not a url")).text
    await svc.client.close()

    assert body(ok) == body(cached) and "标题: T" in ok
    assert body(bad).startswith("网页抓取失败：") and "换一个链接" in bad
    assert body(invalid).startswith("网页抓取失败：") and "] 未调用" in invalid
    assert len(fake_api.requests) == 2  # 缓存命中和本地校验失败都不发请求
    assert svc.stats.day()["fetch"] == {
        "billable": 1,
        "empty": 0,
        "failed": 1,
        "cache_hit": 1,
    }


async def test_cache_disabled_when_ttl_zero(fake_api, no_sleep):
    fake_api.search_queue += [(200, {"results": [make_result(1)]}, {})] * 2
    svc = make_service(fake_api, no_sleep, ttl=0)
    await svc.search("q", None)
    await svc.search("q", None)
    await svc.client.close()
    assert len(fake_api.requests) == 2


def test_coerce_count():
    s = Settings(default_count=5, max_count=10)
    assert coerce_count(None, s) == 5
    assert coerce_count(0, s) == 5
    assert coerce_count("3", s) == 3
    assert coerce_count(7.9, s) == 7
    assert coerce_count(99, s) == 10
    assert coerce_count("abc", s) == 5


def test_settings_from_config_clamps_bad_values():
    s = Settings.from_config(
        {
            "default_count": 30,
            "max_count": 8,
            "min_rerank_score": 9,
            "max_search_chars": "x",
        }
    )
    assert s.max_count == 8 and s.default_count == 8
    assert s.min_rerank_score == 3.0
    assert s.max_search_chars == 6000


# ---------- formatter ----------


def _results(*scores):
    return [
        SearchResult.from_api(make_result(i, score=s)) for i, s in enumerate(scores, 1)
    ]


def test_low_score_results_are_filtered():
    text = format_search_results("q", _results(2.5, 0.3, 1.6), min_score=1.0)
    assert "标题1" in text and "标题3" in text and "标题2" not in text
    assert "共 2 条" in text


def test_all_low_score_keeps_top_two_with_warning():
    text = format_search_results("q", _results(0.5, 0.4, 0.3), min_score=1.0)
    assert "标题1" in text and "标题2" in text and "标题3" not in text
    assert "相关度都较低" in text


def test_max_chars_truncates_by_entry():
    results = [
        SearchResult.from_api(make_result(i, summary="摘" * 300)) for i in range(1, 11)
    ]
    text = format_search_results("q", results, max_chars=1200)
    assert "[1]" in text and "[10]" not in text
    assert "因长度限制已省略" in text
    assert len(text) < 1500


def test_content_only_when_enabled():
    r = _results(2.0)
    assert "正文节选" not in format_search_results("q", r)
    with_content = format_search_results("q", r, include_content=True, content_chars=20)
    assert "正文节选" in with_content and "…" in with_content


def test_falls_back_to_snippet_and_hides_unknown_authority():
    r = [SearchResult.from_api(make_result(1, summary="", authority=-1))]
    text = format_search_results("q", r)
    assert "摘要: 短摘要1" in text and "权威度" not in text


def test_format_fetch_truncates():
    text = format_fetch_result(
        FetchResult("https://a.com", "", "y" * 1000), max_chars=100
    )
    assert "(无标题)" in text and "已截断" in text
    assert "没有可提取的正文" in format_fetch_result(FetchResult("u", "t", "  "))


# ---------- cache & stats ----------


def test_ttl_cache_expiry_and_lru():
    now = [0.0]
    cache = TTLCache(10, max_entries=2, clock=lambda: now[0])
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # a 变成最近使用
    cache.set("c", 3)  # 淘汰 b
    assert cache.get("b") is None and cache.get("a") == 1
    now[0] = 11
    assert cache.get("a") is None


def test_stats_persist_and_cost(tmp_path):
    path = tmp_path / "usage.json"
    day = [date(2026, 10, 17)]
    stats = UsageStats(path, today=lambda: day[0])
    for _ in range(4):
        stats.record("search", "billable")
    stats.record("fetch", "billable")
    stats.record("fetch", "failed")

    reloaded = UsageStats(path, today=lambda: day[0])
    assert reloaded.day()["search"]["billable"] == 4
    assert abs(reloaded.cost() - 5 * 0.005) < 1e-9  # 默认人民币，每次 ¥0.005

    day[0] = date(2026, 10, 18)
    reloaded.record("search", "billable")
    assert reloaded.billable(1) == 1 and reloaded.billable(30) == 6

    day[0] = date(2027, 1, 15)  # 超过 62 天的数据被清理
    reloaded.record("search", "empty")
    assert reloaded.billable(60) == 0


def test_stats_survive_corrupt_file(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{not json", encoding="utf-8")
    stats = UsageStats(path)
    stats.record("search", "billable")
    assert stats.day()["search"]["billable"] == 1
