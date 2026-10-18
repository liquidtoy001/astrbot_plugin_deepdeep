"""搜索工具健康检测、调用日志迁移和连通性说明。"""

import sqlite3
import types

import pytest

from core import connectivity
from core.activity import ActivityLog
from core.calllog import CallLog
from core.health import assess, classify, extract_query, query_terms, relevance
from core.recorder import ToolCallRecorder

OWN = ("deepdeep_web_search", "deepdeep_web_fetch")


# ---------- 单次结果的判断 ----------


def test_query_terms_split_words_numbers_and_chinese():
    assert query_terms("CVE-2024-3094") >= {"cve", "2024", "3094"}
    assert query_terms("极客湾 最新视频 site:bilibili.com") == {
        "极客",
        "客湾",
        "最新",
        "新视",
        "视频",
    }
    assert query_terms("site:a.com") == set()


def test_relevance_ignores_echoed_query():
    # SearXNG 只剩 Bing 时的真实情况：结果和搜索词毫无关系，只在开头回显了搜索词
    junk = "CVE-2024-3094 的搜索结果\n1. MySkillsFuture\nLink: https://www.myskillsfuture.gov.sg\nContent: Courses"
    assert relevance("CVE-2024-3094", junk) == 0.0
    good = "1. CVE-2024-3094 xz backdoor\nContent: CVE-2024-3094 affects xz 5.6.0"
    assert relevance("CVE-2024-3094", good) == 1.0
    echoed_json = '{"query": "CVE-2024-3094", "results": [{"title": "SkillsFuture"}]}'
    assert relevance("CVE-2024-3094", echoed_json) == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, "empty"),
        ("   ", "empty"),
        ("No music found for your query.", "empty"),
        ("没有找到图片，请稍后再试。", "empty"),
        (
            "SearXNG returned 403 Forbidden. Please check the /search JSON interface.",
            "error",
        ),
        ("Error: request timed out", "error"),
        ("1. Python 3.14 发布\nContent: Python 3.14 新特性包括……" * 3, None),
    ],
)
def test_classify(text, expected):
    assert classify("Python 3.14 新特性", text)[0] == expected


def test_classify_irrelevant_and_tool_error_flag():
    quality, score = classify(
        "Falcons CS2 latest match", "AliExpress robotic arm 6 axis " * 20
    )
    assert quality == "irrelevant" and score < 0.3
    assert classify("q", "whatever", is_error=True) == ("error", None)
    # 没有搜索词（例如抓取工具）时只判断失败和空结果
    assert classify(None, "正常的网页正文" * 50) == (None, None)


def test_extract_query():
    assert extract_query({"query": " q "}) == "q"
    assert extract_query({"keywords": ["a", "b"]}) == "a b"
    assert extract_query({"url": "https://a.com"}) is None
    assert extract_query(None) is None


def test_assess_thresholds():
    assert assess({"total": 2, "error": 2})["verdict"] == "unknown"
    broken = assess({"total": 9, "empty": 1, "irrelevant": 7, "avg_relevance": 0.08})
    assert broken["verdict"] == "broken"
    assert broken["reason"] == "9 次调用：空结果 1、结果不相关 7；平均相关度 0.08"
    assert assess({"total": 10, "error": 4})["verdict"] == "degraded"
    assert assess({"total": 10, "error": 1})["verdict"] == "ok"


# ---------- 调用日志 ----------


def test_v1_database_is_migrated(tmp_path):
    path = tmp_path / "calls.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, duration_ms INTEGER,"
        " tool TEXT NOT NULL, source TEXT NOT NULL, session TEXT, status TEXT NOT NULL, request_id TEXT,"
        " http_status INTEGER, args TEXT, result TEXT, result_chars INTEGER, error TEXT, note TEXT,"
        " detail TEXT, content_recorded INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO calls (ts, tool, source, status) VALUES (1, 'old_tool', 'builtin', 'ok');"
        "PRAGMA user_version=1;"
    )
    conn.close()
    log = CallLog(path, retention_days=0)
    rid = log.start(tool="t", source="builtin")
    log.finish(rid, status="ok", quality="irrelevant", relevance=0.1)
    item = log.get(rid)
    assert item["quality"] == "irrelevant" and item["relevance"] == 0.1
    assert log.query()[1] == 2
    log.close()


def test_tool_health_counts_and_streak():
    log = CallLog(None, retention_days=0)

    def add(tool, status="ok", quality=None, relevance=None):
        rid = log.start(tool=tool, source="plugin:x")
        log.finish(rid, status=status, quality=quality, relevance=relevance)

    add("searxng_web_search_general", relevance=0.9)
    for _ in range(3):
        add("searxng_web_search_general", quality="irrelevant", relevance=0.0)
    add("searxng_web_search_general", status="empty")
    add("grok_web_search", status="error")
    add("grok_web_search", status="ok", quality="error")
    add("grok_web_search", status="blocked")  # 被拦截的不算
    rows = {r["tool"]: r for r in log.tool_health()}
    g = rows["searxng_web_search_general"]
    assert (g["total"], g["irrelevant"], g["empty"], g["error"]) == (5, 3, 1, 0)
    assert g["bad_streak"] == 4 and g["avg_relevance"] == 0.23
    k = rows["grok_web_search"]
    assert (k["total"], k["error"], k["bad_streak"]) == (2, 2, 2)


async def test_recorder_scores_results_without_storing_content():
    clock = types.SimpleNamespace(t=0.0)
    rec = ToolCallRecorder(
        CallLog(None), ActivityLog(), own_tools=OWN, clock=lambda: clock.t
    )
    t = types.SimpleNamespace(
        name="searxng_web_search_general", handler_module_path="data.plugins.x.main"
    )
    await rec.on_start(1, None, t, {"query": "Falcons CS2 latest match"})
    res = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(text="1. AliExpress robot arm\nContent: cheap " * 10)
        ],
        isError=False,
    )
    await rec.on_end(1, t, res)
    item = rec.log.get(rec.log.query()[0][0]["id"])
    assert item["status"] == "ok" and item["quality"] == "irrelevant"
    assert item["result"] is None and item["args"] is None  # 内容没有保存


# ---------- 连通性说明 ----------


def test_describe_groups_tools_by_backend():
    names = [
        "deepdeep_web_search",
        "deepdeep_web_fetch",
        "web_search_deepseek",
        "searxng_web_search_general",
        "searxng_web_search_videos",
        "searxng_github_search",
        "searxng_web_fetch_url",
        "web_search_serpapi",
        "astr_kb_search",
    ]
    groups = {g["key"]: g for g in connectivity.describe(names, "http://searxng:8080")}
    assert groups["deepseek"]["tools"] == ["deepdeep_web_search", "web_search_deepseek"]
    assert groups["deepdeep_fetch"]["kind"] == "cloud"
    sx = groups["searxng"]
    assert (
        sx["endpoint"] == "http://searxng:8080"
        and sx["intranet"]
        and sx["kind"] == "self_hosted"
    )
    assert groups["github"]["tools"] == ["searxng_github_search"]
    assert groups["searxng_fetch"]["kind"] == "server"
    assert groups["kb"]["kind"] == "local"
    assert groups["serpapi"]["region"] == "foreign"


def test_public_searxng_counts_as_cloud():
    g = connectivity.describe(
        ["searxng_web_search_general"], "https://searx.example.org"
    )[0]
    assert g["kind"] == "cloud" and not g["intranet"]


def test_find_searxng_url_from_other_plugin_config(tmp_path):
    (tmp_path / "a.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / "astrbot_plugin_web_search_pro_config.json").write_text(
        '{"searxng_api_url": "http://searxng:8080/", "github_token": "secret"}',
        encoding="utf-8",
    )
    (tmp_path / "broken.json").write_text("{nope", encoding="utf-8")
    assert connectivity.find_searxng_url(tmp_path) == "http://searxng:8080"
    assert connectivity.find_searxng_url(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("url", "private"),
    [
        ("http://searxng:8080", True),
        ("http://127.0.0.1:8888", True),
        ("http://192.168.1.5", True),
        ("http://172.20.0.3:8080", True),
        ("https://api.deepseek.com", False),
        ("http://172.40.0.1", False),
    ],
)
def test_is_private_host(url, private):
    assert connectivity.is_private_host(url) is private
