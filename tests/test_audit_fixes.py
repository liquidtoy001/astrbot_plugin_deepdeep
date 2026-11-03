"""2026-11 全面检查发现的问题：每条对应一个修好之前会失败的场景。"""
# ruff: noqa: F811

import asyncio
import gc
import json
import threading
import time
import types
import weakref
from pathlib import Path

import pytest
from test_plugin import (
    CURRENT_REQUEST,
    FakeConfig,
    FakeContext,
    FakeEvent,
    FakeToolSet,
    api,
    make_plugin,
    plugin_module,  # noqa: F401 - pytest 夹具
)

from core import kbtrace
from core.activity import ActivityLog
from core.astrbot_view import AstrBotView
from core.calllog import CallLog
from core.caps import ToolCaps, parse_limit_rules
from core.client import DeepSeekToolsClient, SearchResult
from core.guard import BudgetGuard
from core.model_pricing import PriceBook, parse_model_prices
from core.pluginkb import PluginKbWatch
from core.providers import KB_TOOL
from core.recorder import ERROR_NOT_RECORDED, ToolCallRecorder
from core.service import SearchService, Settings, cfg_int
from core.stats import MonthlyCounter, UsageStats
from core.tavily import TavilyClient, TavilySearch


def _request():
    return types.SimpleNamespace(
        prompt="问题", system_prompt="人设", contexts=[], extra_user_content_parts=[]
    )


def _recorder(log=None, **kwargs):
    log = log or CallLog(None)
    return log, ToolCallRecorder(log, ActivityLog(), own_tools=(), **kwargs)


def _retrieved(query, marker):
    return {
        "query": query,
        "results": [{"kb_name": marker, "doc_name": "d", "content": "c", "score": 0.9}],
        "context_text": "c",
        "kb_count": 1,
        "embedding_calls": 1,
        "rerank_calls": 0,
        "duration_ms": 1,
        "error": None,
    }


# ---------- 内容记录开关 ----------


@pytest.mark.asyncio
async def test_error_text_follows_the_content_switch():
    log, recorder = _recorder()
    tool = types.SimpleNamespace(name="memory_query")
    result = types.SimpleNamespace(
        isError=True, content=[types.SimpleNamespace(text="私密的记忆片段")]
    )
    await recorder.on_start(1, "s", tool, {"query": "问题"})
    await recorder.on_end(1, tool, result)
    assert log.get(1)["error"] == ERROR_NOT_RECORDED
    assert log.get(1)["status"] == "error"

    recorder.record_other_content = True
    await recorder.on_start(1, "s", tool, {"query": "问题"})
    await recorder.on_end(1, tool, result)
    assert log.get(2)["error"] == "私密的记忆片段"


@pytest.mark.asyncio
async def test_plugin_and_kb_errors_keep_only_the_type_when_not_recording():
    log, recorder = _recorder()

    async def hook(event, req):
        raise ValueError("私密内容")

    meta = types.SimpleNamespace(handler=hook)
    watch = PluginKbWatch(
        kb_plugins=lambda: [{"name": "kb"}],
        providers=list,
        on_inject=recorder.on_plugin_inject,
        handlers=lambda: [(meta, "kb")],
    )
    watch.sync()
    with pytest.raises(ValueError):
        await meta.handler(types.SimpleNamespace(), _request())
    assert log.get(1)["error"] == "ValueError"

    fields = _retrieved("问题", "kb")
    fields.update(results=None, context_text=None, error="RuntimeError: 私密内容")
    await recorder.on_kb_retrieve(**fields)
    row = log.get(2)
    assert row["error"] == "RuntimeError"
    assert row["kb"]["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_diagnostics_never_carry_url_credentials(plugin_module, monkeypatch):
    from astrbot_plugin_deepdeep.core import connectivity

    plugin = make_plugin(plugin_module)
    url = "http://user:SECRET@searxng.example.com:8080/search?api_key=SECRET"
    plugin.web.search_tool_names = lambda: ["searxng_web_search_general"]
    plugin.web.searxng_url = lambda: url
    used = {}

    async def run_check(groups, searxng_url):
        used["url"] = searxng_url
        used["groups"] = groups
        return {
            "server_domestic_ok": True,
            "server_foreign_ok": True,
            "endpoints": {g["endpoint"]: {"ok": True} for g in groups},
        }

    monkeypatch.setattr(connectivity, "run_check", run_check)
    await plugin.web.network_check()
    # 检测时还是用完整地址连接
    assert used["url"] == url
    shown = json.dumps(
        [plugin.web.network_data(), plugin.web.diagnostics_data()], ensure_ascii=False
    )
    assert "SECRET" not in shown and "user:" not in shown
    assert "http://searxng.example.com:8080" in shown
    await plugin.terminate()


def test_diagnostics_leave_out_other_tools_error_text(plugin_module):
    plugin = make_plugin(plugin_module)
    plugin.call_log.add(tool="t", source="builtin", status="error", error="私密内容")
    plugin.call_log.add(
        tool="deepdeep_deepseek_search",
        source="deepseek",
        status="error",
        error="连接超时",
    )
    problems = plugin.web.diagnostics_data()["recent_problems"]
    assert "连接超时" in json.dumps(problems, ensure_ascii=False)
    assert "私密内容" not in json.dumps(problems, ensure_ascii=False)


# ---------- 调用日志 ----------


def test_startup_uses_the_configured_retention(plugin_module):
    data = plugin_module.StarTools.get_data_dir()
    old = CallLog(data / "calls.db", retention_days=365)
    row = old.start(tool="t", source="builtin", ts=time.time() - 30 * 86400)
    old.finish(row, status="ok")
    old.close()
    plugin = plugin_module.DeepSeekSearchPlugin(
        FakeContext(), FakeConfig(call_log_retention_days=365)
    )
    assert plugin.call_log.summary()["total"] == 1


@pytest.mark.asyncio
async def test_same_query_in_two_sessions_stays_apart():
    log, recorder = _recorder()
    tool = types.SimpleNamespace(name=KB_TOOL)
    both_started = asyncio.Event()
    started = []

    async def session(key, marker, wait=None):
        await recorder.on_start(key, f"s{key}", tool, {"query": "同样的问题"})
        started.append(key)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        if wait is not None:
            await wait.wait()
        await recorder.on_kb_retrieve(**_retrieved("同样的问题", marker))
        await recorder.on_end(key, tool, f"结果 {marker}")

    second_done = asyncio.Event()

    async def second():
        await session(2, "kb_b")
        second_done.set()

    await asyncio.gather(session(1, "kb_a", second_done), second())
    rows = {r["session"]: r["id"] for r in log.query()[0]}
    assert log.get(rows["s1"])["kb"]["hits"][0]["kb"] == "kb_a"
    assert log.get(rows["s2"])["kb"]["hits"][0]["kb"] == "kb_b"
    assert log.summary()["total"] == 2


@pytest.mark.asyncio
async def test_parallel_calls_of_one_tool_keep_their_own_results():
    log, recorder = _recorder()
    recorder.record_other_content = True
    tool = types.SimpleNamespace(name="search")
    first_may_finish = asyncio.Event()

    async def call(query, wait=None):
        await recorder.on_start(1, "s", tool, {"query": query})
        await asyncio.sleep(0)
        if wait is not None:
            await wait.wait()
        await recorder.on_end(1, tool, f"{query} 的结果")
        if wait is None:
            first_may_finish.set()

    await asyncio.gather(call("甲", first_may_finish), call("乙"))
    for row in log.query()[0]:
        full = log.get(row["id"])
        assert full["result"] == f"{full['args']['query']} 的结果"


@pytest.mark.asyncio
async def test_one_model_round_is_counted_once():
    log, recorder = _recorder()
    tool = types.SimpleNamespace(name=KB_TOOL)
    for query in ("甲", "乙"):
        await recorder.on_start(1, "s", tool, {"query": query}, context_tokens=1000)
        await recorder.on_kb_retrieve(**_retrieved(query, "kb"))
        await recorder.on_end(1, tool, "结果")
    await recorder.on_agent_done(
        1, {"input_other": 900, "input_cached": 100, "output": 50}
    )
    tokens = log.kb_stats()["tokens"]
    assert tokens["final_rounds"] == 1 and tokens["final_other"] == 900
    assert tokens["next_rounds"] == 1 and tokens["next_input"] == 1000
    # 每条检索的详情还能看到这一轮的用量
    assert all(
        log.get(r["id"])["kb"]["tokens"]["final"]["input_other"] == 900
        for r in log.query()[0]
    )


def test_rows_from_before_the_fix_still_add_up():
    log = CallLog(None)
    for _ in range(2):
        log.add(
            tool=KB_TOOL,
            source="builtin",
            status="ok",
            kb={
                "mode": "tool",
                "tokens": {"next_input": 100, "final": {"input_other": 5}},
            },
        )
    tokens = log.kb_stats()["tokens"]
    assert tokens["next_rounds"] == 2 and tokens["final_other"] == 10


def test_community_success_does_not_hide_builtin_failure():
    log = CallLog(None)
    log.add(
        tool="astr_kb_direct", source="builtin", status="error", kb={"mode": "direct"}
    )
    for source in ("plugin:memory", "plugin:memory", "mcp:docs"):
        log.add(tool="x", source=source, status="ok", kb={"mode": "plugin_inject"})
    recent = log.kb_health(0)
    assert recent["total"] == 4 and recent["builtin_total"] == 1
    config = {"names": ["a"], "problems": []}
    assert kbtrace.assess_status(config, recent)["verdict"] == "bad"


def test_category_counts_are_one_snapshot():
    log = CallLog(None)
    original = log._where
    built = threading.Event()

    def writer():
        built.wait(3)
        log.add(tool="search", source="builtin", status="ok")

    worker = threading.Thread(target=writer)
    worker.start()

    def where(filters):
        if filters.get("category") == "builtin":
            built.set()
            time.sleep(0.05)
        return original(filters)

    log._where = where
    counts = log.category_counts(None, ["search"])
    worker.join(3)
    assert counts["all"] == sum(v for k, v in counts.items() if k != "all")


@pytest.mark.asyncio
async def test_tavily_failures_are_not_logged_as_success():
    log, recorder = _recorder()
    tool = types.SimpleNamespace(name="deepdeep_tavily_search")
    cases = [
        (
            "[Tavily 搜索 #aabbcc] 失败 · HTTP 401\nTavily 搜索失败：Key 无效",
            "error",
            401,
        ),
        ("[Tavily 搜索 #aabbcc] 失败 · 网络错误\nTavily 请求超时", "error", None),
        ("[Tavily 搜索 #aabbcc] 未调用 · 本月已达上限", "blocked", None),
        (
            "本月 Tavily 调用已达上限（本月 Tavily 已用 1 / 1 次），请改用 x",
            "blocked",
            None,
        ),
        ("Tavily 搜索暂不可用：没有 Key。请改用其他搜索工具。", "blocked", None),
        ("[Tavily 搜索 #aabbcc] 成功 · 0 条结果 · 10 ms\n没有找到", "empty", None),
        ("[Tavily 搜索 #aabbcc] 缓存 · 3 条结果\n结果", "cache_hit", None),
        ("[Tavily 搜索 #aabbcc] 成功 · 3 条结果 · 10 ms\n结果", "ok", None),
    ]
    for i, (text, status, http) in enumerate(cases, 1):
        await recorder.on_start(1, "s", tool, {"query": "q"})
        await recorder.on_end(1, tool, text)
        row = log.get(i)
        assert (row["status"], row["http_status"]) == (status, http), text
    assert log.get(1)["error"] == "Tavily 搜索失败：Key 无效"


# ---------- 上限 ----------


class _Client:
    def __init__(self, result):
        self.calls = 0
        self.result = result

    def has_key(self):
        return True

    async def search(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.01)
        return self.result


@pytest.mark.asyncio
async def test_budget_holds_against_concurrent_searches():
    client = _Client([SearchResult(title="t", url="https://e.invalid", rerank_score=2)])
    stats = UsageStats(None, price=1)
    budget = BudgetGuard(stats, daily_limit=1, monthly_limit=1)
    svc = SearchService(client, Settings(), stats=stats, budget=budget)
    outcomes = await asyncio.gather(*(svc.search(f"q{i}") for i in range(6)))
    assert client.calls == 1 and stats.month_cost() == 1
    assert sum(o.reason == "budget_daily" for o in outcomes) == 5
    assert budget.in_flight == 0


@pytest.mark.asyncio
async def test_failed_search_gives_its_budget_back():
    class Failing(_Client):
        async def search(self, *args, **kwargs):
            raise asyncio.CancelledError

    stats = UsageStats(None, price=1)
    budget = BudgetGuard(stats, monthly_limit=1)
    svc = SearchService(Failing([]), Settings(), stats=stats, budget=budget)
    with pytest.raises(asyncio.CancelledError):
        await svc.search("q")
    assert budget.in_flight == 0


@pytest.mark.asyncio
async def test_tavily_limit_holds_against_concurrent_searches():
    client = _Client([])
    tavily = TavilySearch(client)
    tavily.monthly_limit = 1
    await asyncio.gather(*(tavily.search(f"q{i}") for i in range(6)))
    assert client.calls == 1 and tavily.month_used() == 1
    assert tavily._in_flight == 0


def test_caps_reservation_is_given_back():
    caps = ToolCaps(MonthlyCounter(None))
    caps.rules = [("search_*", 1)]
    release = caps.reserve("search_a")
    assert caps.reserve("search_b") is None
    release()
    assert caps.reserve("search_b") is not None
    assert caps.reserve("other")() is None


@pytest.mark.asyncio
async def test_extra_tool_cap_is_checked_at_every_call(plugin_module):
    from astrbot_plugin_deepdeep.core.providers import EXTRA_PROVIDERS

    provider = EXTRA_PROVIDERS[0]
    plugin = make_plugin(
        plugin_module,
        routing_mode="guide_only",
        tool_monthly_limits=[f"{provider.api.tool} = 1"],
        **{provider.key_field: "KEY", provider.enabled_field: True},
    )
    client = _Client([])
    plugin.extra_search.client = types.SimpleNamespace(
        search=client.search, close=lambda: asyncio.sleep(0)
    )
    results = [
        await plugin.extra_search.search(provider, "服务", f"q{i}") for i in range(3)
    ]
    assert client.calls == 1
    assert [is_error for _text, is_error in results] == [False, True, True]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_other_plugin_tool_is_dropped_when_it_reaches_the_cap(plugin_module):
    plugin = make_plugin(
        plugin_module, routing_mode="guide_only", tool_monthly_limits=["searxng_x = 1"]
    )
    event = FakeEvent()
    toolset = FakeToolSet(["searxng_x", "deepdeep_deepseek_search"])
    await plugin.route_search_tools(
        event, types.SimpleNamespace(func_tool=toolset, system_prompt="")
    )
    assert "searxng_x" in toolset.names()
    tool = types.SimpleNamespace(name="searxng_x")
    await plugin.record_tool_end(
        event, tool, {}, types.SimpleNamespace(content=[], isError=False)
    )
    assert "searxng_x" not in toolset.names()
    assert "deepdeep_deepseek_search" in toolset.names()
    await plugin.terminate()


def test_bad_limit_rule_is_ignored():
    assert parse_limit_rules(["a = 1e309", "b = nan", "c = 3"]) == [("c", 3)]
    assert cfg_int({"k": "1e309"}, "k", 5, 0, 10) == 5


@pytest.mark.asyncio
async def test_settings_that_cannot_apply_are_not_saved(plugin_module):
    plugin = make_plugin(plugin_module, panel_language="zh")
    original = plugin.apply_config
    calls = {"n": 0}

    def fail_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("坏设置")
        original()

    plugin.apply_config = fail_once
    saved = plugin.config.saved
    CURRENT_REQUEST.body = {"panel_language": "en"}
    response = await api(plugin, "settings", "POST")()
    assert "坏设置" in json.dumps(response, ensure_ascii=False, default=str)
    assert plugin.config["panel_language"] == "zh"
    assert plugin.config.saved == saved
    await plugin.terminate()


# ---------- 服务返回的内容不对 ----------


@pytest.mark.asyncio
async def test_invalid_json_goes_through_failure_handling(fake_api):
    client = DeepSeekToolsClient(
        lambda: "KEY", base_url=fake_api.base_url, max_retries=0, qps=0
    )
    svc = SearchService(client, Settings())
    fake_api.search_queue.append((200, "<html>代理页面</html>", {}))
    outcome = await svc.search("q")
    assert outcome.should_fallback and outcome.reason == "http_502"
    assert svc.stats.day()["search"]["failed"] == 1
    await client.close()

    tavily_client = TavilyClient(lambda: ["KEY"], api_url=f"{fake_api.base_url}/search")
    for body in ("<html>代理页面</html>", "[1, 2]"):
        fake_api.tavily_queue.extend([(200, body, {}), (200, body, {})])
        outcome = await TavilySearch(tavily_client).search("q")
        assert outcome.should_fallback and "无法解析" in outcome.text
    await tavily_client.close()


def test_missing_exchange_rate_is_not_free():
    book = PriceBook(None, usd_rate=lambda: 0)
    book.manual = parse_model_prices(["paid = 2 usd", "local = 2 cny"])
    providers = {
        "a": {"kind": "embedding", "model": "paid", "api_base": "https://e.invalid"},
        "b": {"kind": "rerank", "model": "local", "api_base": "https://e.invalid"},
    }
    tokens = {"embedding": {"a": 1_000_000}, "rerank": {"b": 1_000_000}}
    cost = book.kb_cost(tokens, providers)
    assert cost["unpriced"] == ["paid"]
    assert cost["total"] == 2.0


# ---------- 社区知识库插件 ----------


def _watch(meta=(), providers=(), recorder=None, plugins=("kb",), clock=None):
    return PluginKbWatch(
        kb_plugins=lambda: [{"name": p} for p in plugins],
        providers=lambda: providers,
        on_inject=recorder.on_plugin_inject if recorder else (lambda **_kw: None),
        handlers=lambda: [(m, "kb") for m in meta],
        **({"clock": clock} if clock else {}),
    )


@pytest.mark.asyncio
async def test_restore_puts_back_an_existing_instance_method():
    class Provider:
        provider_config = {"id": "p"}  # noqa: RUF012

        async def get_embedding(self, text):
            return "类上的方法"

    provider = Provider()

    async def adapter(text):
        return "别的插件的包装"

    provider.get_embedding = adapter
    watch = _watch(providers=[("embedding", provider)])
    watch.sync()
    assert provider.get_embedding is not adapter
    watch.restore()
    assert provider.get_embedding is adapter


@pytest.mark.asyncio
async def test_cancelled_hook_is_not_a_success():
    log, recorder = _recorder()

    async def hook(event, req):
        req.system_prompt += "写了一半"
        raise asyncio.CancelledError

    meta = types.SimpleNamespace(handler=hook)
    _watch([meta], recorder=recorder).sync()
    with pytest.raises(asyncio.CancelledError):
        await meta.handler(types.SimpleNamespace(), _request())
    assert log.get(1)["status"] == "interrupted"
    assert log.kb_stats()["by_plugin"][0]["error"] == 1


def test_replaced_providers_can_be_collected():
    class Provider:
        provider_config = {"id": "p"}  # noqa: RUF012

        async def get_embedding(self, text):
            return []

    current = []
    now = [0.0]
    watch = PluginKbWatch(
        kb_plugins=lambda: [{"name": "kb"}],
        providers=lambda: [("embedding", p) for p in current],
        on_inject=lambda **_kw: None,
        handlers=list,
        clock=lambda: now[0],
    )
    refs = []
    for i in range(4):
        provider = Provider()
        refs.append(weakref.ref(provider))
        current[:] = [provider]
        now[0] = i * 31.0
        watch.sync()
    del provider
    gc.collect()
    assert [r() is not None for r in refs] == [False, False, False, True]
    assert len(watch._restores) == 1


def test_wraps_are_removed_when_the_kb_plugin_goes_away():
    async def hook(event, req):
        return None

    meta = types.SimpleNamespace(handler=hook)
    names = ["kb"]
    now = [0.0]
    watch = PluginKbWatch(
        kb_plugins=lambda: [{"name": n} for n in names],
        providers=list,
        on_inject=lambda **_kw: None,
        handlers=lambda: [(meta, "kb")],
        clock=lambda: now[0],
    )
    watch.sync()
    assert meta.handler is not hook
    names.clear()
    now[0] = 31.0
    watch.sync()
    assert meta.handler is hook


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "change", "expected"),
    [
        (
            "人设",
            lambda m: m.__setitem__("content", m["content"] + " 新记忆"),
            " 新记忆",
        ),
        (
            [{"type": "text", "text": "人设"}],
            lambda m: m["content"].append({"type": "text", "text": "新记忆"}),
            "新记忆",
        ),
    ],
)
async def test_in_place_context_edit_is_seen(before, change, expected):
    log, recorder = _recorder()
    recorder.record_other_content = True

    async def hook(event, req):
        change(req.contexts[0])

    meta = types.SimpleNamespace(handler=hook)
    _watch([meta], recorder=recorder).sync()
    req = _request()
    req.contexts = [
        {"role": "system", "content": before},
        {"role": "user", "content": "旧"},
    ]
    await meta.handler(types.SimpleNamespace(), req)
    assert log.get(1)["result"] == expected


@pytest.mark.asyncio
async def test_extra_part_list_replaced_like_a_memory_plugin():
    """记忆插件换一个新列表，并把新内容放在 extra_user_content_parts 里。"""
    log, recorder = _recorder()
    recorder.record_other_content = True
    old_part = types.SimpleNamespace(text="旧")

    async def hook(event, req):
        req.extra_user_content_parts = [
            *req.extra_user_content_parts,
            types.SimpleNamespace(text="记忆片段"),
        ]

    meta = types.SimpleNamespace(handler=hook)
    _watch([meta], recorder=recorder).sync()
    req = _request()
    req.extra_user_content_parts = [old_part]
    await meta.handler(types.SimpleNamespace(), req)
    assert log.get(1)["result"] == "记忆片段"


@pytest.mark.asyncio
async def test_usage_is_kept_per_provider():
    log, recorder = _recorder()

    class Provider:
        def __init__(self, name):
            self.provider_config = {"id": name}

        async def get_embedding(self, text):
            return []

    a, b = Provider("a"), Provider("b")

    async def hook(event, req):
        await a.get_embedding("x" * 100)
        await b.get_embedding("x" * 100)
        req.system_prompt += "记忆"

    meta = types.SimpleNamespace(handler=hook)
    _watch([meta], [("embedding", a), ("embedding", b)], recorder).sync()
    await meta.handler(types.SimpleNamespace(), _request())
    assert log.kb_stats()["billing"]["embedding"] == {"a": 30, "b": 30}


@pytest.mark.asyncio
async def test_builtin_retrieval_splits_usage_by_embedding_provider():
    helpers = {
        "甲": types.SimpleNamespace(
            kb=types.SimpleNamespace(embedding_provider_id="a"), init_error=None
        ),
        "乙": types.SimpleNamespace(
            kb=types.SimpleNamespace(embedding_provider_id="b"), init_error=None
        ),
    }

    class Manager:
        async def retrieve(self, query, kb_names):
            return {"results": [], "context_text": ""}

        async def get_kb_by_name(self, name):
            return helpers[name]

    seen = {}

    async def on_retrieve(**fields):
        seen.update(fields)

    manager = Manager()
    kbtrace.install(manager, on_retrieve)
    await manager.retrieve("x" * 100, ["甲", "乙"])
    assert seen["billing"]["embedding_by"] == {"a": 30, "b": 30}


def test_low_score_fallback_still_obeys_the_chunk_limit():
    raw = {
        "context_text": "x" * 10000,
        "results": [{"content": "x" * 10000, "score": 0.1}],
    }
    result, saved = kbtrace.trim_result(raw, 0.8, 100)
    assert len(result["results"][0]["content"]) == 101
    assert saved > 0


def test_kb_plugin_recognition_end_to_end():
    def star(name, desc):
        return types.SimpleNamespace(
            name=name,
            display_name=name,
            desc=desc,
            short_desc="",
            activated=True,
            module_path=f"data.plugins.{name}.main",
        )

    stars = [
        star("astrbot_plugin_memory_usage", "Show host RAM usage"),
        star("astrbot_plugin_kb", "Local document vector retrieval"),
        star("astrbot_plugin_memory_garden", "长期记忆召回"),
    ]
    context = types.SimpleNamespace(
        get_all_stars=lambda: stars,
        get_llm_tool_manager=lambda: types.SimpleNamespace(func_list=[]),
    )
    assert [p["name"] for p in AstrBotView(context).kb_plugins()] == [
        "astrbot_plugin_kb",
        "astrbot_plugin_memory_garden",
    ]


# ---------- 卸载和导出 ----------


@pytest.mark.asyncio
async def test_terminate_stops_routes_and_price_task(plugin_module, monkeypatch):
    from astrbot_plugin_deepdeep.core import model_pricing

    plugin = make_plugin(plugin_module)
    plugin.context.registered_web_apis = [
        ("/astrbot_plugin_deepdeep/status", None, ["GET"], ""),
        ("/other_plugin/status", None, ["GET"], ""),
    ]
    monkeypatch.setattr(model_pricing, "WAIT_FOR_FETCH", 0)

    async def slow_fetch():
        await asyncio.Event().wait()

    plugin.pricing._fetch = slow_fetch
    await plugin.pricing.ensure_fresh()
    task = plugin.pricing._task
    await plugin.terminate()
    assert task.cancelled()
    assert [r[0] for r in plugin.context.registered_web_apis] == [
        "/other_plugin/status"
    ]


@pytest.mark.asyncio
async def test_exports_in_the_same_second_do_not_share_a_file(
    plugin_module, monkeypatch
):
    import copy

    from astrbot_plugin_deepdeep.core import web_api

    plugin = make_plugin(plugin_module)
    plugin.call_log.add(tool="甲", source="builtin", status="ok")
    plugin.call_log.add(tool="乙", source="builtin", status="ok")
    a, b = copy.copy(plugin.web), copy.copy(plugin.web)
    a.filters_with_tools = lambda: {"tool": "甲"}
    b.filters_with_tools = lambda: {"tool": "乙"}
    monkeypatch.setattr(web_api.time, "strftime", lambda *_args: "同一秒")
    first = await a.call_export()
    second = await b.call_export()
    assert first["file"] != second["file"]
    tools = [
        json.loads(line)["tool"]
        for line in Path(first["file"]).read_text(encoding="utf-8").splitlines()
    ]
    assert tools == ["甲"]
    await plugin.terminate()


# ---------- 统计文件 ----------


@pytest.mark.asyncio
async def test_saving_stats_does_not_block_the_event_loop(tmp_path, monkeypatch):
    stats = UsageStats(tmp_path / "usage.json")
    original = Path.write_text

    def slow_write(path, *args, **kwargs):
        time.sleep(0.15)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", slow_write)
    started = time.monotonic()
    stats.record("search", "billable")
    stats.record("search", "billable")
    assert time.monotonic() - started < 0.1
    stats.flush()
    # 连着保存时最后写下的是最新的内容
    assert UsageStats(tmp_path / "usage.json").billable() == 2


def test_saving_without_an_event_loop_is_immediate(tmp_path):
    counter = MonthlyCounter(tmp_path / "count.json")
    counter.add()
    assert MonthlyCounter(tmp_path / "count.json").count() == 1


def test_kb_retrievals_without_usage_are_counted_not_priced_as_zero(tmp_path):
    """旧版本的记录只有调用次数，没有 token：页面要说“无法估算”，不能当成花了 0 元。"""
    log = CallLog(tmp_path / "calls.db")
    old = {"mode": "tool", "embedding_calls": 8, "rerank_calls": 1, "tokens": {}}
    new = {
        **old,
        "billing": {"embedding_provider": "emb", "embedding_tokens": 40},
    }
    plugin = {"mode": "plugin_inject", "embedding_calls": 0, "rerank_calls": 0}
    for kb in (old, old, new, plugin):
        log.add(tool="astr_kb_search", source="builtin", status="ok", kb=kb)
    stats = log.kb_stats()
    assert stats["unbilled"] == 2
    assert stats["billing"]["embedding"] == {"emb": 40}
    log.close()
