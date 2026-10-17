"""持久化调用日志（所有工具）与各搜索工具用量。"""

import types

import pytest

from core.activity import ActivityLog
from core.calllog import MAX_RESULT_CHARS, CallLog
from core.recorder import ToolCallRecorder, detect_source, result_to_text

OWN = ("deepdeep_web_search", "deepdeep_web_fetch")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def fake_result(text=None, is_error=False, images=0):
    if text is None and not images:
        return None
    content = (
        [types.SimpleNamespace(type="text", text=text)] if text is not None else []
    )
    content += [types.SimpleNamespace(type="image", data="...") for _ in range(images)]
    return types.SimpleNamespace(content=content, isError=is_error)


def tool(name, module=None, mcp=None):
    t = types.SimpleNamespace(name=name, handler_module_path=module)
    if mcp:
        t.mcp_server_name = mcp
    return t


# ---------- 存储 ----------


def test_store_roundtrip_filters_and_persistence(tmp_path):
    clock = Clock()
    path = tmp_path / "calls.db"
    log = CallLog(path, clock=clock)
    a = log.start(
        tool="web_search_tavily", source="builtin", session="qq:g1", args={"query": "x"}
    )
    log.finish(a, status="ok", duration_ms=120, result="r" * 10)
    clock.t += 10
    b = log.add(
        tool="deepdeep_web_search",
        source="deepseek",
        status="error",
        request_id="abc123",
        http_status=402,
        error="余额不足",
        detail={"error": {"status": 402}},
    )
    log.close()

    log = CallLog(path, clock=clock)  # 重启后还在
    items, total = log.query()
    assert total == 2 and [i["id"] for i in items] == [b, a]
    # 列表不含大字段，只给出是否存在
    assert not {"args", "result", "detail"} & set(items[0])
    assert items[0]["has_detail"] == 1 and items[1]["has_detail"] == 0

    assert log.query({"tool": "web_search_tavily"})[1] == 1
    assert log.query({"source": "deepseek"})[1] == 1
    assert log.query({"problems_only": True})[0][0]["request_id"] == "abc123"
    assert log.query({"q": "abc12"})[1] == 1
    assert log.query({"q": "100%"})[1] == 0  # 通配符被转义
    assert log.query({"since": clock.t - 5})[1] == 1

    full = log.get(b)
    assert full["detail"] == {"error": {"status": 402}} and full["http_status"] == 402
    # 未开启记录内容时，参数和结果都不保存，只保存结果长度
    other = log.get(a)
    assert (
        other["args"] is None
        and other["result"] is None
        and other["result_chars"] == 10
    )
    facets = log.facets()
    assert set(facets["tools"]) == {"deepdeep_web_search", "web_search_tavily"}
    assert set(facets["sources"]) == {"deepseek", "builtin"}
    log.close()


def test_content_recorded_only_when_enabled():
    log = CallLog(None)
    a = log.start(tool="t", source="plugin:x", args={"q": "秘密"}, record_content=True)
    log.finish(
        a, status="ok", result="y" * (MAX_RESULT_CHARS + 50), record_content=True
    )
    item = log.get(a)
    assert item["args"] == {"q": "秘密"} and item["content_recorded"] is True
    assert (
        item["result"].endswith("已截断）")
        and item["result_chars"] == MAX_RESULT_CHARS + 50
    )


def test_retention_and_row_cap():
    clock = Clock()
    log = CallLog(None, retention_days=14, max_rows=3, clock=clock)
    old = log.add(tool="old", source="builtin", status="ok")
    clock.t += 15 * 86400
    for i in range(4):
        log.add(tool=f"t{i}", source="builtin", status="ok")
    log.prune()
    items, total = log.query()
    assert total == 3 and old not in [i["id"] for i in items]
    assert [i["tool"] for i in items] == ["t3", "t2", "t1"]


def test_running_rows_marked_interrupted_after_restart(tmp_path):
    path = tmp_path / "calls.db"
    log = CallLog(path)
    rid = log.start(tool="slow", source="builtin")
    log.close()
    log = CallLog(path)
    assert log.get(rid)["status"] == "interrupted"
    log.close()


def test_clear():
    log = CallLog(None)
    log.add(tool="t", source="builtin", status="ok")
    assert log.clear() == 1 and log.query()[1] == 0


# ---------- 记录器 ----------


def test_detect_source():
    resolver = {
        "data.plugins.web_searcher_pro.main": "astrbot_plugin_web_searcher_pro"
    }.get

    def resolve(m):
        return resolver(m) or (
            "astrbot_plugin_web_searcher_pro"
            if m.startswith("data.plugins.web_searcher_pro.")
            else None
        )

    assert detect_source(tool("deepdeep_web_search"), OWN, resolve) == "deepseek"
    assert detect_source(
        tool("x", "data.plugins.web_searcher_pro.services.a"), OWN, resolve
    ) == ("plugin:astrbot_plugin_web_searcher_pro")
    assert detect_source(tool("read_file", mcp="github"), OWN, resolve) == "mcp:github"
    assert (
        detect_source(tool("web_search_tavily", "astrbot.core.tools.web"), OWN, resolve)
        == "builtin"
    )
    assert detect_source(tool("x", "custom.mod"), OWN, resolve) == "module:custom.mod"


def test_result_to_text():
    assert result_to_text(None) is None
    assert result_to_text(fake_result("hi", images=1)) == "hi\n[SimpleNamespace]"
    assert result_to_text("plain") == "plain"


@pytest.fixture
def recorder():
    activity = ActivityLog()
    clock = Clock(100.0)
    rec = ToolCallRecorder(CallLog(None), activity, own_tools=OWN, clock=clock)
    rec.clock = clock
    return rec


async def test_other_tool_logs_metadata_only_by_default(recorder):
    t = tool("web_search_tavily", "astrbot.core.tools.web")
    await recorder.on_start(1, "qq:g1", t, {"query": "用户隐私"})
    recorder.clock.t += 0.25
    await recorder.on_end(1, t, fake_result("搜索结果"))
    item = recorder.log.get(recorder.log.query()[0][0]["id"])
    assert item["status"] == "ok" and item["duration_ms"] == 250
    assert item["source"] == "builtin" and item["session"] == "qq:g1"
    assert item["args"] is None and item["result"] is None and item["result_chars"] == 4


async def test_other_tool_full_content_when_toggled(recorder):
    recorder.record_other_content = True
    t = tool("kimi_web_search", "data.plugins.kimi.main")
    await recorder.on_start(1, None, t, {"query": "q"})
    await recorder.on_end(1, t, fake_result("boom", is_error=True))
    item = recorder.log.get(recorder.log.query()[0][0]["id"])
    assert item["status"] == "error" and item["error"] == "boom"
    assert item["args"] == {"query": "q"} and item["result"] == "boom"


async def test_none_result_and_exception_without_end_hook(recorder):
    t1 = tool("send_message_to_user")
    await recorder.on_start(7, None, t1, {})
    await recorder.on_end(7, t1, None)
    t2 = tool("flaky")
    await recorder.on_start(7, None, t2, {})  # 抛异常，AstrBot 不会触发 on_end
    assert await recorder.on_agent_done(7) == 1
    items = {i["tool"]: i for i in recorder.log.query()[0]}
    assert items["send_message_to_user"]["note"].startswith("无返回值")
    assert items["flaky"]["status"] == "error" and "抛出异常" in items["flaky"]["error"]


async def test_parallel_calls_same_tool_pair_fifo(recorder):
    t = tool("web_search_tavily")
    await recorder.on_start(1, None, t, {"query": "a"})
    recorder.clock.t += 1
    await recorder.on_start(1, None, t, {"query": "b"})
    recorder.clock.t += 1
    await recorder.on_end(1, t, fake_result("A"))
    await recorder.on_end(1, t, fake_result("B"))
    durations = sorted(i["duration_ms"] for i in recorder.log.query()[0])
    assert durations == [1000, 2000]


async def test_own_tool_enriched_from_activity(recorder):
    recorder.activity.add(
        "search",
        "q",
        "failed",
        request_id="abc123",
        status=402,
        note="余额不足",
        detail={
            "error": {"status": 402, "message": "DeepSeek 账户余额不足"},
            "fallback": {"restored_tools": ["x"]},
        },
    )
    t = tool("deepdeep_web_search")
    await recorder.on_start(1, None, t, {"query": "q"})
    await recorder.on_end(
        1, t, fake_result("[DeepSeek 搜索 #abc123] 失败 · HTTP 402\n联网搜索失败")
    )
    item = recorder.log.get(recorder.log.query()[0][0]["id"])
    assert item["status"] == "error" and item["request_id"] == "abc123"
    assert item["http_status"] == 402 and item["error"] == "DeepSeek 账户余额不足"
    assert item["detail"]["fallback"] == {"restored_tools": ["x"]}
    assert item["args"] == {"query": "q"}  # 自己的工具遵循“记录搜索词”开关（默认开）


async def test_disabled_recorder_records_nothing(recorder):
    recorder.enabled = False
    t = tool("x")
    await recorder.on_start(1, None, t, {})
    await recorder.on_end(1, t, None)
    assert recorder.log.query()[1] == 0


# ---------- 各搜索工具用量 ----------


def test_tool_stats_aggregates_per_tool():
    clock = Clock()
    log = CallLog(None, clock=clock)
    for status, ms in (("ok", 100), ("ok", 300), ("error", 50), ("blocked", None)):
        rid = log.start(tool="web_search_tavily", source="builtin")
        log.finish(rid, status=status, duration_ms=ms)
    log.add(tool="deepdeep_web_search", source="deepseek", status="cache_hit")
    clock.t += 3600
    log.add(tool="set_user_nickname", source="plugin:x", status="ok", duration_ms=5)
    rows = {r["tool"]: r for r in log.tool_stats()}
    t = rows["web_search_tavily"]
    assert (t["total"], t["ok"], t["error"], t["blocked"]) == (4, 2, 1, 1)
    assert t["avg_ms"] == 150  # 只算真正发出去的请求：(100 + 300 + 50) / 3
    assert rows["deepdeep_web_search"]["cache_hit"] == 1
    # 时间范围
    assert {r["tool"] for r in log.tool_stats(since=clock.t - 10)} == {
        "set_user_nickname"
    }
