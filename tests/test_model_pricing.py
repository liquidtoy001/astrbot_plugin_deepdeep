import json
import types

import pytest

from core import caps, connectivity, kbtrace, mcp_view
from core.activity import ActivityLog
from core.astrbot_view import AstrBotView
from core.calllog import CallLog
from core.model_pricing import (
    FREE,
    PriceBook,
    is_local,
    parse_model_prices,
    slim_catalog,
)
from core.recorder import ToolCallRecorder

CATALOG = {
    "deepseek": {
        "models": {
            "deepseek-flash": {
                "cost": {"input": 0.1, "output": 0.4, "cache_read": 0.01}
            },
            "no-price": {},
        }
    },
    "relay-a": {"models": {"gpt-x": {"cost": {"input": 2, "output": 8}}}},
    "relay-b": {"models": {"openai/gpt-x": {"cost": {"input": 4, "output": 16}}}},
    "broken": "not a dict",
}


def book(tmp_path=None, **kwargs):
    b = PriceBook(tmp_path / "prices.json" if tmp_path else None, **kwargs)
    b._use(slim_catalog(CATALOG), 1000.0)
    return b


@pytest.mark.parametrize(
    ("api_base", "kind", "expected"),
    [
        ("http://localhost:11434/v1", None, True),
        ("http://127.0.0.1:1234", None, True),
        ("http://192.168.1.5:8000/v1", None, True),
        ("http://10.0.0.2/v1", None, True),
        ("http://ollama:11434", None, True),
        ("http://gpu.local/v1", None, True),
        ("localhost:8000", None, True),
        ("https://api.openai.com/v1", None, False),
        ("https://8.8.8.8/v1", None, False),
        (None, "ollama_chat", True),
        (None, "lm_studio", True),
        (None, "openai_chat_completion", False),
        (None, None, False),
    ],
)
def test_local_models(api_base, kind, expected):
    assert is_local(api_base, kind) is expected


def test_manual_beats_everything():
    b = book()
    b.manual = parse_model_prices(["deepseek-flash = 1 0.5 2 cny"])
    price = b.price_for("deepseek-flash", "http://localhost:1")
    assert (price.input, price.cached, price.output, price.source) == (
        1,
        0.5,
        2,
        "manual",
    )


def test_local_is_free_even_if_the_model_has_a_price():
    assert book().price_for("deepseek-flash", "http://localhost:1") is FREE


def test_official_price_uses_the_host():
    price = book().price_for("deepseek-flash", "https://api.deepseek.com/v1")
    assert price.source == "official"
    assert (price.input, price.cached, price.output) == (0.1, 0.01, 0.4)


def test_reference_price_is_the_median_across_channels():
    price = book().price_for("gpt-x", "https://some-relay.example/v1")
    assert price.source == "reference"
    assert (price.input, price.output) == (3, 12)


def test_unknown_model_has_no_price():
    assert book().price_for("made-up", "https://x.example") is None
    assert book().price_for(None) is None


def test_missing_cache_price_falls_back_to_input():
    assert slim_catalog(CATALOG)["relay-a"]["gpt-x"] == [2, 8, 2]


@pytest.mark.parametrize(
    "line",
    [
        "no equals",
        "m = 1 2 3",
        "m = a b c cny",
        "m = 1 2 3 eur",
        " = 1 2 3 cny",
        "m = -1 2 3 usd",
        7,
    ],
)
def test_bad_price_lines_are_ignored(line):
    assert parse_model_prices([line]) == {}


def test_cost_converts_usd_with_the_configured_rate():
    b = book(usd_rate=lambda: 7.0)
    price = b.price_for("deepseek-flash", "https://api.deepseek.com")
    assert b.cost(price, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(
        (0.1 + 0.01 + 0.4) * 7
    )
    assert b.cost(None, 1) is None


def test_kb_cost_covers_only_embedding_and_rerank():
    b = book(usd_rate=lambda: 1.0)
    b.manual = parse_model_prices(["my-embed = 2 2 0 cny"])
    infos = {
        "chat": {
            "model": "deepseek-flash",
            "api_base": "https://api.deepseek.com",
            "kind": "chat",
        },
        "paid": {"model": "my-embed", "kind": "embedding"},
        "local": {
            "model": "bge",
            "api_base": "http://localhost:1",
            "kind": "embedding",
        },
        "mystery": {"model": "made-up", "kind": "rerank"},
    }
    tokens = {"embedding": {"paid": 1_000_000, "local": 5}, "rerank": {"mystery": 9}}
    out = b.kb_cost(tokens, infos)
    assert out["embedding"] == 2 and out["rerank"] == 0 and out["total"] == 2
    assert out["unpriced"] == ["made-up"]
    assert {r["provider_id"] for r in out["rows"]} == {"paid", "local", "mystery"}
    local = next(r for r in out["rows"] if r["provider_id"] == "local")
    assert local["price"]["source"] == "local" and local["cost_cny"] == 0


def test_kb_cost_lists_models_even_before_any_use():
    out = book().kb_cost({}, {"e": {"model": "made-up", "kind": "embedding"}})
    assert out["rows"][0]["tokens"] == 0 and out["unpriced"] == ["made-up"]


def test_price_cache_survives_a_restart(tmp_path):
    (tmp_path / "prices.json").write_text(
        json.dumps(
            {
                "fetched_at": 5.0,
                "catalog": {"deepseek": {"deepseek-flash": [1, 2, 3]}},
            }
        ),
        encoding="utf-8",
    )
    b = PriceBook(tmp_path / "prices.json")
    assert b.fetched_at == 5.0
    assert b.price_for("deepseek-flash", "https://api.deepseek.com").input == 1


def test_broken_price_cache_is_ignored(tmp_path):
    (tmp_path / "prices.json").write_text("{", encoding="utf-8")
    assert PriceBook(tmp_path / "prices.json").fetched_at == 0.0


async def test_fetch_failure_does_not_raise(monkeypatch):
    b = PriceBook(None, clock=lambda: 1e9)
    monkeypatch.setattr("core.model_pricing.CATALOG_URL", "http://127.0.0.1:9/none")
    await b.ensure_fresh()
    assert b.price_for("anything") is None


# ---------- 精简结果 ----------

RESULTS = {
    "context_text": "x" * 1000,
    "results": [
        {"kb_name": "a", "doc_name": "1", "content": "内容一" * 100, "score": 0.8},
        {"kb_name": "a", "doc_name": "1", "content": "内容一" * 100, "score": 0.7},
        {"kb_name": "b", "doc_name": "2", "content": "内容二", "score": 0.2},
    ],
}


def test_trim_off_returns_the_same_object():
    assert kbtrace.trim_result(RESULTS, 0, 0) == (RESULTS, 0)


def test_trim_clips_dedupes_and_filters():
    out, saved = kbtrace.trim_result(RESULTS, 0.5, 10)
    assert len(out["results"]) == 1
    assert out["results"][0]["content"].endswith("…")
    assert (
        "【知识 1】" in out["context_text"] and "【知识 2】" not in out["context_text"]
    )
    assert saved > 0
    # 原结果不被改动，AstrBot 可能还要用
    assert len(RESULTS["results"]) == 3
    assert len(RESULTS["results"][0]["content"]) == 300


def test_trim_always_keeps_one_result():
    out, _ = kbtrace.trim_result(RESULTS, 0.99, 0)
    assert len(out["results"]) == 1


def test_trim_does_not_filter_by_score_when_rerank_failed():
    out, _ = kbtrace.trim_result(RESULTS, 0.5, 0, scores_reliable=False)
    assert len(out["results"]) == 2


@pytest.mark.parametrize("empty", [None, {}, {"results": []}])
def test_trim_handles_no_results(empty):
    assert kbtrace.trim_result(empty, 0.5, 10) == (empty, 0)


class _Manager:
    async def get_kb_by_name(self, _name):
        return None

    async def retrieve(self, query, kb_names):
        return dict(RESULTS)


async def test_trim_setting_reaches_the_model_and_records_savings():
    manager = _Manager()
    log = CallLog(None)
    rec = ToolCallRecorder(log, ActivityLog(record_targets=True), own_tools=())
    kbtrace.install(manager, rec.on_kb_retrieve, lambda: (0.0, 5))
    out = await manager.retrieve("q", ["a"])
    assert all(len(r["content"]) <= 6 for r in out["results"])
    row = log.get(log.query()[0][0]["id"])
    assert row["kb"]["tokens"]["saved"] > 0


async def test_no_trim_setting_leaves_results_alone():
    manager = _Manager()
    rec = ToolCallRecorder(
        CallLog(None), ActivityLog(record_targets=True), own_tools=()
    )
    kbtrace.install(manager, rec.on_kb_retrieve)
    assert (await manager.retrieve("q", ["a"]))["context_text"] == "x" * 1000


# ---------- 识别工具 ----------


class _Tool:
    def __init__(self, name, description="", module=None):
        self.name = name
        self.description = description
        if module:
            self.handler_module_path = module


class _Result:
    isError = False

    def __init__(self):
        self.content = [type("T", (), {"text": "结果"})()]


async def _record(tool, **kwargs):
    log = CallLog(None)
    rec = ToolCallRecorder(
        log, ActivityLog(record_targets=True), own_tools=("own",), **kwargs
    )
    await rec.on_start(1, "s", tool, {"query": "q"})
    await rec.on_end(1, tool, _Result())
    return log.get(log.query()[0][0]["id"])


async def test_tool_that_lost_its_mcp_info_is_found_by_name():
    # AstrBot 交给钩子的是普通工具对象，没有 mcp_server_name
    plain = type("FunctionTool", (_Tool,), {"__module__": "astrbot.core.agent.tool"})
    tool = plain("search_repositories")
    row = await _record(tool, mcp_server_of=lambda name: "github")
    assert row["source"] == "mcp:github"


async def test_plain_tool_stays_where_it_was():
    row = await _record(_Tool("other"), mcp_server_of=lambda _name: None)
    assert row["source"] != "mcp:github" and row["kb"] is None


async def test_kb_tool_recognised_by_description():
    row = await _record(_Tool("lookup", "从知识库中查找资料"))
    assert row["kb"]["mode"] == "unscored"


async def test_kb_tool_recognised_by_its_plugin():
    row = await _record(
        _Tool("lookup", "查资料", module="x.y"),
        plugin_resolver=lambda _m: "astrbot_plugin_docs",
        kb_plugin=lambda name: name == "astrbot_plugin_docs",
    )
    assert row["source"] == "plugin:astrbot_plugin_docs"
    assert row["kb"]["mode"] == "unscored"


async def test_web_search_mentioning_knowledge_is_not_a_kb():
    row = await _record(
        _Tool("web_search", "search the knowledge base of the internet"),
        is_search=lambda name: name == "web_search",
    )
    assert row["kb"] is None


async def test_own_tool_is_never_a_kb():
    row = await _record(_Tool("own", "知识库"))
    assert row["kb"] is None


# ---------- 调用日志分类 ----------


def _rows(log):
    log.add(tool="astr_kb_search", source="builtin", status="ok")
    log.add(tool="kb_x", source="plugin:p", status="ok", kb={"mode": "unscored"})
    log.add(tool="search_repositories", source="mcp:github", status="ok")
    log.add(tool="web_search", source="plugin:s", status="ok")
    log.add(tool="deepdeep_deepseek_search", source="deepseek", status="ok")
    log.add(tool="play_music", source="plugin:m", status="ok")
    log.add(tool="shell", source="builtin", status="ok")


SEARCH = ["web_search", "deepdeep_deepseek_search"]


def test_category_counts_add_up():
    log = CallLog(None)
    _rows(log)
    assert log.category_counts(None, SEARCH) == {
        "all": 7,
        "search": 2,
        "kb": 2,
        "mcp": 1,
        "plugin": 1,
        "builtin": 1,
    }


@pytest.mark.parametrize(
    ("category", "tools"),
    [
        ("kb", {"astr_kb_search", "kb_x"}),
        ("mcp", {"search_repositories"}),
        ("search", {"web_search", "deepdeep_deepseek_search"}),
        ("plugin", {"play_music"}),
        ("builtin", {"shell"}),
    ],
)
def test_category_filter(category, tools):
    log = CallLog(None)
    _rows(log)
    items, _ = log.query({"category": category, "search_tools": SEARCH})
    assert {r["tool"] for r in items} == tools


def test_category_filter_without_search_tools():
    log = CallLog(None)
    _rows(log)
    assert log.query({"category": "search", "search_tools": []})[1] == 0
    # 没有搜索工具名单时，deepdeep_deepseek_search 也算其他工具，但不会让整类查不到
    assert log.query({"category": "builtin", "search_tools": []})[1] == 2


def test_old_rows_are_moved_to_their_mcp_server():
    log = CallLog(None)
    log.add(tool="search_repositories", source="builtin", status="ok")
    log.add(tool="shell", source="builtin", status="ok")
    assert log.fix_mcp_sources({"search_repositories": "github"}) == 1
    assert log.fix_mcp_sources({"search_repositories": "github"}) == 0
    stats = log.mcp_stats()
    assert [(r["source"], r["tool"]) for r in stats] == [
        ("mcp:github", "search_repositories")
    ]


# ---------- MCP 页 ----------


def _server(state="connected", tools=()):
    return {
        "name": "github",
        "state": state,
        "transport": "http",
        "tools": list(tools),
        "errors": [],
    }


def test_mcp_view_merges_tools_and_calls():
    tools = [
        {"name": "search_code", "description": "搜索代码", "parameters": {"a": 1}},
        {"name": "create_issue", "description": "新建", "parameters": {}},
    ]
    stats = [
        {
            "source": "mcp:github",
            "tool": "search_code",
            "total": 4,
            "errors": 1,
            "avg_ms": 1000,
            "result_chars": 100,
        },
        {
            "source": "mcp:github",
            "tool": "gone_tool",
            "total": 1,
            "errors": 0,
            "avg_ms": 2000,
            "result_chars": 0,
        },
    ]
    [server] = mcp_view.build([_server(tools=tools)], stats)
    assert server["calls"] == 5 and server["errors"] == 1
    assert server["avg_ms"] == 1200
    assert server["result_tokens"] == 50
    assert server["unused"] == 1
    assert server["schema_tokens"] > 0
    assert [t["name"] for t in server["tools"]] == [
        "search_code",
        "gone_tool",
        "create_issue",
    ]
    kinds = {t["name"]: t["kind"] for t in server["tools"]}
    assert kinds["search_code"] == "search" and kinds["create_issue"] == "write"


def test_mcp_view_keeps_removed_servers_that_have_records():
    stats = [
        {
            "source": "mcp:old",
            "tool": "x",
            "total": 2,
            "errors": 0,
            "avg_ms": None,
            "result_chars": 0,
        }
    ]
    [server] = mcp_view.build([], stats)
    assert (
        server["name"] == "old"
        and server["state"] == "removed"
        and server["avg_ms"] is None
    )


def test_mcp_view_lists_idle_and_used_servers_in_order():
    servers = [dict(_server(), name="a"), dict(_server(), name="b")]
    stats = [
        {
            "source": "mcp:b",
            "tool": "x",
            "total": 1,
            "errors": 0,
            "avg_ms": 5,
            "result_chars": 0,
        }
    ]
    assert [s["name"] for s in mcp_view.build(servers, stats)] == ["b", "a"]


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("get_commit", "read"),
        ("search_code", "search"),
        ("create_pull_request", "write"),
        ("merge_pull_request", "write"),
        ("query_knowledge_base", "kb"),
        ("address_lookup", "search"),
    ],
)
def test_tool_kind(name, kind):
    assert mcp_view.tool_kind(name) == kind


def _active(name, state="connected", calls=0, errors=0):
    return {"name": name, "state": state, "calls": calls, "errors": errors}


@pytest.mark.parametrize(
    ("servers", "verdict"),
    [
        ([], "off"),
        ([_active("a", "disabled")], "off"),
        ([_active("a")], "ok"),
        ([_active("a"), _active("b", "error")], "warn"),
        ([_active("a", "error")], "bad"),
        ([_active("a", "connecting"), _active("b", "error")], "bad"),
        ([_active("a", calls=4, errors=2)], "warn"),
        ([_active("a", calls=4, errors=1)], "ok"),
        ([_active("a", calls=1, errors=1)], "ok"),
        ([_active("a"), _active("b", "disabled")], "ok"),
    ],
)
def test_mcp_status(servers, verdict):
    assert mcp_view.assess(servers)["verdict"] == verdict


def test_mcp_status_names_servers_that_are_down():
    out = mcp_view.assess([_active("a"), _active("b", "error")])
    assert out["reasons"] == [{"code": "down", "servers": ["b"]}]
    assert out["connected"] == 1 and out["total"] == 2


# ---------- 开关 ----------


class _Config(dict):
    saved = 0

    def save_config(self):
        self.saved += 1


class _KbManager:
    def __init__(self, names):
        self.kbs = {
            n: types.SimpleNamespace(
                kb=types.SimpleNamespace(kb_name=n, doc_count=2, chunk_count=9),
                init_error=None,
            )
            for n in names
        }

    async def get_kb_by_name(self, name):
        return self.kbs.get(name)

    async def list_kbs(self):
        return [h.kb for h in self.kbs.values()]


def _kb_view(enabled=("a",), names=("a", "b")):
    config = _Config(kb_names=list(enabled), kb_agentic_mode=True)
    context = types.SimpleNamespace(
        kb_manager=_KbManager(names), get_config=lambda umo=None: config
    )
    return AstrBotView(context), config


async def test_kb_switch_changes_only_the_config_list():
    view, config = _kb_view()
    await view.set_kb_enabled("b", True)
    assert config["kb_names"] == ["a", "b"] and config.saved == 1
    await view.set_kb_enabled("b", True)
    assert config["kb_names"] == ["a", "b"]
    await view.set_kb_enabled("a", False)
    assert config["kb_names"] == ["b"]


async def test_kb_switch_rejects_unknown_kb_but_can_remove_it():
    view, config = _kb_view(enabled=("gone",))
    with pytest.raises(ValueError):
        await view.set_kb_enabled("nope", True)
    assert config.saved == 0
    await view.set_kb_enabled("gone", False)
    assert config["kb_names"] == []


async def test_kb_config_lists_all_with_their_state():
    view, _ = _kb_view()
    out = await view.kb_config()
    assert out["all"] == [
        {"name": "a", "enabled": True, "docs": 2},
        {"name": "b", "enabled": False, "docs": 2},
    ]


class _McpManager:
    def __init__(self, active=True, fail=False):
        self.config = {"mcpServers": {"gh": {"url": "u", "active": active}}}
        self.mcp_client_dict = {"gh": object()} if active else {}
        self.fail = fail
        self.calls = []

    def load_mcp_config(self):
        return self.config

    def save_mcp_config(self, config):
        self.config = config
        return True

    async def enable_mcp_server(self, name, config, timeout=None):
        self.calls.append(("enable", name, config["active"]))
        if self.fail:
            raise RuntimeError("连不上")
        self.mcp_client_dict[name] = object()

    async def disable_mcp_server(self, name=None, timeout=10):
        self.calls.append(("disable", name))
        self.mcp_client_dict.pop(name)


def _mcp_view(manager):
    return AstrBotView(types.SimpleNamespace(get_llm_tool_manager=lambda: manager))


async def test_mcp_switch_saves_and_stops_the_server():
    manager = _McpManager()
    await _mcp_view(manager).set_mcp_active("gh", False)
    assert manager.config["mcpServers"]["gh"]["active"] is False
    assert manager.calls == [("disable", "gh")]


async def test_mcp_switch_starts_the_server():
    manager = _McpManager(active=False)
    await _mcp_view(manager).set_mcp_active("gh", True)
    assert manager.calls == [("enable", "gh", True)]
    assert manager.config["mcpServers"]["gh"]["url"] == "u"


async def test_mcp_switch_rolls_back_when_start_fails():
    manager = _McpManager(active=False, fail=True)
    with pytest.raises(RuntimeError):
        await _mcp_view(manager).set_mcp_active("gh", True)
    assert manager.config["mcpServers"]["gh"]["active"] is False


async def test_mcp_switch_rejects_unknown_server():
    with pytest.raises(ValueError):
        await _mcp_view(_McpManager()).set_mcp_active("nope", True)


# ---------- 网络检测 ----------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://a.example/v1?key=secret", "https://a.example"),
        ("https://user:pw@a.example:8443/x", "https://a.example:8443"),
        ("http://searxng:8080", "http://searxng:8080"),
        ("a.example/v1", "https://a.example"),
        ("", None),
        (None, None),
    ],
)
def test_origin_never_keeps_paths_or_passwords(url, origin):
    assert connectivity.origin_of(url) == origin


def _mcp(name, tools=(), **extra):
    return {
        "name": name,
        "state": "connected",
        "tools": [{"name": t} for t in tools],
        **extra,
    }


def test_services_include_mcp_kb_and_unknown_tools():
    rows = connectivity.describe_services(
        ["web_search_x", "search_papers", "deepdeep_deepseek_search"],
        [
            _mcp("arxiv", ["search_papers"]),
            _mcp("remote", ["ask"], endpoint="https://mcp.example"),
            _mcp("files", ["read"]),
            _mcp("off", ["x"], state="disabled"),
        ],
        {
            "e": {
                "kind": "embedding",
                "model": "m",
                "api_base": "https://embed.example/v1",
            },
            "r": {"kind": "rerank", "model": "n", "api_base": "http://localhost:9/v1"},
            "c": {"kind": "chat", "model": "gpt", "api_base": "https://chat.example"},
        },
        True,
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["mcp:arxiv"]["endpoint"] == "https://export.arxiv.org"
    assert by_key["mcp:remote"]["endpoint"] == "https://mcp.example"
    assert by_key["mcp:files"]["kind"] == "unknown"
    assert by_key["mcp:files"]["endpoint"] is None
    assert "mcp:off" not in by_key
    assert by_key["kb:e"]["endpoint"] == "https://embed.example"
    assert by_key["kb:r"]["kind"] == "self_hosted" and by_key["kb:r"]["intranet"]
    assert "kb:c" not in by_key
    assert by_key["other"]["tools"] == ["web_search_x"]


def test_services_skip_kb_when_none_enabled_and_known_backends():
    rows = connectivity.describe_services(
        ["search_code"],
        [_mcp("github", ["search_code"])],
        {"e": {"kind": "embedding", "model": "m", "api_base": "https://e.example"}},
        False,
    )
    assert rows == []


# ---------- 嵌入和重排的价格 ----------


def test_single_price_line():
    price = parse_model_prices(["my-rerank = 0.5 cny"])["my-rerank"]
    assert (price.input, price.cached, price.output) == (0.5, 0.5, 0)
    assert price.currency == "cny" and price.source == "manual"


@pytest.mark.parametrize(
    ("model", "value"),
    [
        ("qwen3.7-text-embedding", 0.5),
        ("qwen3.7-text-rerank", 0.5),
        ("qwen3-vl-embedding", 0.7),
        ("tongyi-embedding-vision-flash-2026-03-06", 0.15),
    ],
)
def test_aliyun_prices_are_built_in(model, value):
    bases = [
        "https://ws-abc.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank",
        "https://dashscope.aliyuncs.com/api/v1",
    ]
    for base in bases:
        price = book().price_for(model, base)
        assert price.input == value and price.currency == "cny"
        assert price.source == "official"


def test_aliyun_price_only_applies_to_aliyun():
    assert book().price_for("qwen3.7-text-embedding", "https://relay.example") is None


def test_manual_price_beats_the_built_in_one():
    b = book()
    b.manual = parse_model_prices(["qwen3.7-text-rerank = 0.1 cny"])
    price = b.price_for("qwen3.7-text-rerank", "https://dashscope.aliyuncs.com/api/v1")
    assert price.input == 0.1 and price.source == "manual"


@pytest.mark.parametrize(
    ("kind", "base"),
    [
        ("ollama_embedding", "http://localhost:11434"),
        ("vllm_rerank", "http://127.0.0.1:8000"),
        ("xinference_rerank", "http://gpu.example:9997"),
        ("tei_rerank", "http://tei.example:8080"),
    ],
)
def test_self_hosted_adapters_are_free(kind, base):
    assert book().price_for("any", base, kind) is FREE


def test_default_address_uses_the_adapter_to_find_the_provider():
    b = PriceBook(None)
    b._use({"google": {"gemini-embedding-001": [0.15, 0, 0.15]}}, 1.0)
    price = b.price_for("gemini-embedding-001", "", "gemini_embedding")
    assert price.source == "official" and price.input == 0.15
    # 填了别的地址就是中转站，只能算参考价
    assert (
        b.price_for(
            "gemini-embedding-001", "https://relay.example", "gemini_embedding"
        ).source
        == "reference"
    )


def test_provider_fields_with_adapter_specific_names():
    class Provider:
        def __init__(self, config):
            self.provider_config = config

    context = types.SimpleNamespace(
        get_all_providers=list,
        get_all_embedding_providers=lambda: [
            Provider(
                {
                    "id": "e",
                    "type": "dashscope_embedding",
                    "embedding_model": "m",
                    "embedding_api_base": "https://a",
                }
            )
        ],
        provider_manager=types.SimpleNamespace(
            rerank_provider_insts=[
                Provider(
                    {
                        "id": "n",
                        "type": "nvidia_rerank",
                        "nvidia_rerank_model": "x",
                        "nvidia_rerank_api_base": "https://b",
                    }
                ),
                Provider(
                    {
                        "id": "b",
                        "type": "bailian_rerank",
                        "rerank_model": "y",
                        "rerank_api_base": "https://c",
                    }
                ),
            ]
        ),
    )
    infos = AstrBotView(context).providers_info()
    assert infos["e"]["model"] == "m" and infos["e"]["api_base"] == "https://a"
    assert infos["n"]["model"] == "x" and infos["n"]["api_base"] == "https://b"
    assert infos["n"]["kind"] == "rerank" and infos["b"]["model"] == "y"


# ---------- SearXNG 重查 ----------


class _Resp:
    status = 200

    def __init__(self, data):
        self.data = data

    async def json(self, content_type=None):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Session:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Resp(self.answers.pop(0))


TIMED_OUT = {
    "results": [],
    "unresponsive_engines": [["bing", "timeout"], ["baidu", "timeout"]],
}
WORKS = {
    "results": [{"title": "DeepSeek", "engines": ["bing"]}],
    "unresponsive_engines": [["baidu", "CAPTCHA"]],
}


async def test_searxng_is_queried_again_when_everything_timed_out():
    session = _Session(TIMED_OUT, WORKS)
    out = await connectivity.check_searxng(session, "http://s")
    assert session.calls == 2 and out["working"] == ["bing"]


async def test_searxng_blocked_engines_are_not_retried():
    blocked = {
        "results": [],
        "unresponsive_engines": [["bing", "CAPTCHA"], ["baidu", "timeout"]],
    }
    session = _Session(blocked)
    out = await connectivity.check_searxng(session, "http://s")
    assert session.calls == 1 and out["working"] == []


async def test_searxng_with_results_is_not_retried():
    session = _Session(WORKS)
    await connectivity.check_searxng(session, "http://s")
    assert session.calls == 1


def test_local_service_without_a_model_name_is_free():
    assert book().price_for(None, "http://127.0.0.1:8080", "tei_rerank") is FREE
    assert book().price_for(None, "https://x.example") is None


# ---------- 按服务的调用上限 ----------


def test_service_pattern_for_one_tool():
    assert caps.service_pattern(["web_search_bocha"]) == "web_search_bocha"


def test_service_pattern_uses_a_shared_prefix():
    tools = ["searxng_web_search_books", "searxng_web_search_news"]
    assert caps.service_pattern(tools, ["other"]) == "searxng_web_search_*"


def test_service_pattern_avoids_catching_other_tools():
    tools = ["search_code", "search_issues"]
    assert (
        caps.service_pattern(tools, ["search_flights"]) == "search_code|search_issues"
    )


def test_service_pattern_without_a_useful_prefix():
    assert (
        caps.service_pattern(["get_commit", "search_code"]) == "get_commit|search_code"
    )


class _Counter:
    def __init__(self, counts):
        self._counts = counts

    def counts(self):
        return self._counts

    def add(self, name):
        self._counts[name] = self._counts.get(name, 0) + 1


def test_tools_joined_with_a_bar_share_one_limit():
    cap = caps.ToolCaps(_Counter({"a": 2, "b": 3, "c": 9}))
    cap.rules = caps.parse_limit_rules(["a|b = 5"])
    assert cap.rule_for("b") == ("a|b", 5)
    assert cap.rule_for("c") is None
    assert cap.used("a|b") == 5
    assert set(cap.blocked(["a", "b", "c"])) == {"a", "b"}


def test_service_row_reads_existing_rules_and_usage():
    cap = caps.ToolCaps(_Counter({"web_x": 2, "web_y": 1}))
    cap.rules = caps.parse_limit_rules(["web_* = 50"])
    row = cap.for_service(["web_x", "web_y"])
    assert row == {"pattern": "web_*", "limit": 50, "used": 3}
    assert caps.ToolCaps(_Counter({})).for_service(["solo"]) == {
        "pattern": "solo",
        "limit": 0,
        "used": 0,
    }


def test_a_service_with_many_tools_can_be_saved():
    from core.web_api import validate_settings

    tools = [f"tool_number_{i}_of_this_server" for i in range(19)]
    line = f"{caps.service_pattern(tools, ['tool_other'])} = 100"
    many = "|".join(tools) + " = 100"
    assert validate_settings({"tool_monthly_limits": [line]})[1] is None
    assert validate_settings({"tool_monthly_limits": [many]})[1] is None
    assert validate_settings({"search_lane_rules": [many]})[1]
