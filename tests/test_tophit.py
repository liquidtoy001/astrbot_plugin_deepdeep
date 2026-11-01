import json

import pytest

from core import tophit
from core.activity import ActivityLog
from core.calllog import CallLog
from core.formatter import format_fetch_result
from core.recorder import ToolCallRecorder


def test_own_tool_format():
    text = (
        "[Tavily 搜索 #abc123] 成功 · 2 条结果 · 10 ms\n"
        "「x」的 Tavily 搜索结果（共 2 条，按相关度排序）：\n\n"
        "[1] 标题一\n日期: 2026-10-01 | 相关度: 0.90\n链接: https://a.example\n摘要: 这是摘要\n\n"
        "[2] 标题二\n链接: https://b.example\n摘要: 别取我"
    )
    assert tophit.extract(text) == "标题一 — 这是摘要"


@pytest.mark.parametrize(
    "data",
    [
        {"results": [{"title": "T", "url": "u", "snippet": "S"}, {"title": "no"}]},
        {"organic_results": [{"title": "T", "description": "S"}]},
        {"web": {"results": [{"title": "T", "content": "S"}]}},
        [{"title": "T", "summary": "S"}],
    ],
)
def test_json_formats(data):
    assert tophit.extract(json.dumps(data)) == "T — S"


def test_plugin_text_without_snippet_label():
    text = "搜索结果：\n1. 标题\nhttps://a.example\n一段说明\n\n2. 其他"
    assert tophit.extract(text) == "标题 — 一段说明"


def test_fetch_uses_page_title():
    assert (
        tophit.extract("标题: 某个页面\n链接: https://a.example\n\n正文") == "某个页面"
    )


def test_snippet_repeating_the_title_is_not_shown_twice():
    assert tophit.extract("[1] 标题\n摘要: 标题") == "标题"
    assert tophit.extract("[1] 标题\n摘要: 标题：正文") == "标题 — 正文"


def test_long_text_is_clipped():
    found = tophit.extract(json.dumps({"results": [{"title": "字" * 500}]}))
    assert len(found) == tophit.MAX_CHARS and found.endswith("…")


@pytest.mark.parametrize(
    "text",
    [None, "", "没有找到相关网页。", '{"results": []}', '{"ok": true}', "错误：连不上"],
)
def test_unrecognised_gives_nothing(text):
    assert tophit.extract(text) is None


class _Tool:
    def __init__(self, name):
        self.name = name


class _Result:
    isError = False

    def __init__(self, text):
        self.content = [type("T", (), {"text": text})()]


async def _run(tool, *, record_targets=True):
    log = CallLog(None)
    rec = ToolCallRecorder(
        log,
        ActivityLog(record_targets=record_targets),
        own_tools=("deepdeep_web_search",),
        is_search=lambda name: name == "web_search_x",
    )
    await rec.on_start(1, None, _Tool(tool), {"query": "q"})
    await rec.on_end(
        1, _Tool(tool), _Result('{"results": [{"title": "T", "snippet": "S"}]}')
    )
    items, _ = log.query()
    return items[0]


async def test_search_tool_records_top_hit_but_not_full_result():
    row = await _run("web_search_x")
    assert row["top_hit"] == "T — S"
    assert not row["content_recorded"]


async def test_other_tool_records_no_top_hit():
    assert (await _run("get_weather"))["top_hit"] is None


async def test_switch_off_records_no_top_hit():
    assert (await _run("web_search_x", record_targets=False))["top_hit"] is None


def test_real_fetch_format():
    from core.client import FetchResult

    text = format_fetch_result(
        FetchResult(url="https://a.example", title="页", content="正文")
    )
    assert tophit.extract(text) == "页"


def test_web_search_pro_plugin_format():
    text = "1. 标题\nLink: https://a.example\nContent: 内容\n\n2. 其他\nLink: b\nContent: c"
    assert tophit.extract(text) == "标题 — 内容"
