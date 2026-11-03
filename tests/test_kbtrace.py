import contextlib
import types

import pytest

from core import kbtrace
from core.activity import ActivityLog
from core.calllog import CallLog
from core.recorder import ToolCallRecorder


class _Vec:
    rerank_provider = object()


class _Helper:
    init_error = None
    vec_db = _Vec()


class _Manager:
    def __init__(self, results=None, error=None):
        self.results = results
        self.error = error

    async def get_kb_by_name(self, name):
        return None if name == "missing" else _Helper()

    async def retrieve(self, query, kb_names, top_k_fusion=20, top_m_final=5):
        if self.error:
            raise self.error
        if not self.results:
            return None
        return {"context_text": "正文" * 10, "results": self.results}


HITS = [
    {
        "kb_name": "梗",
        "doc_name": "a.md",
        "chunk_index": 3,
        "content": "内容" * 100,
        "score": 0.36,
    },
    {"kb_name": "论文", "doc_name": "b.pdf", "content": "别的", "score": 0.2},
]


class _Tool:
    name = "astr_kb_search"


class _Result:
    isError = False

    def __init__(self):
        self.content = [type("T", (), {"text": "以下是相关的知识库内容"})()]


def _setup(manager, *, record_targets=True, record_other=True):
    log = CallLog(None)
    rec = ToolCallRecorder(
        log, ActivityLog(record_targets=record_targets), own_tools=("own",)
    )
    rec.record_other_content = record_other
    restore = kbtrace.install(manager, rec.on_kb_retrieve)
    return log, rec, restore


async def test_tool_call_gets_scores_in_its_own_row():
    manager = _Manager(HITS)
    log, rec, _ = _setup(manager)
    await rec.on_start(1, "s", _Tool(), {"query": "q"})
    await manager.retrieve(query="q", kb_names=["梗", "论文", "missing"])
    await rec.on_end(1, _Tool(), _Result())
    items, total = log.query()
    assert total == 1
    row = items[0]
    assert row["tool"] == "astr_kb_search"
    assert row["top_hit"] == "梗 / a.md"
    assert row["kb_top_score"] == 0.36
    assert row["kb_count"] == 2
    kb = log.get(row["id"])["kb"]
    assert kb["mode"] == "tool"
    assert kb["kb_count"] == 3
    assert kb["embedding_calls"] == 2
    assert kb["rerank_calls"] == 1
    assert len(kb["hits"][0]["snippet"]) == kbtrace.SNIPPET_CHARS


async def test_low_word_overlap_is_not_marked_irrelevant():
    manager = _Manager(HITS)
    log, rec, _ = _setup(manager)
    await rec.on_start(1, "s", _Tool(), {"query": "完全不同的词"})
    await manager.retrieve("完全不同的词", ["梗"])
    await rec.on_end(1, _Tool(), _Result())
    row = log.get(log.query()[0][0]["id"])
    assert row["quality"] is None and row["status"] == "ok"


async def test_tool_with_no_results_is_empty():
    manager = _Manager(None)
    log, rec, _ = _setup(manager)
    await rec.on_start(1, "s", _Tool(), {"query": "q"})
    await manager.retrieve("q", ["梗"])
    await rec.on_end(1, _Tool(), _Result())
    row = log.query()[0][0]
    assert row["status"] == "empty" and row["kb_count"] == 0
    assert log.get(row["id"])["kb"]["rerank_calls"] == 0


async def test_failed_tool_keeps_the_real_error():
    manager = _Manager(error=ValueError("库不可用"))
    log, rec, _ = _setup(manager)
    await rec.on_start(1, "s", _Tool(), {"query": "q"})
    with pytest.raises(ValueError):
        await manager.retrieve("q", ["梗"])
    await rec.on_agent_done(1)
    row = log.query()[0][0]
    assert row["status"] == "error" and "库不可用" in row["error"]


async def test_direct_retrieval_is_logged_on_its_own():
    manager = _Manager(HITS)
    log, _rec, _ = _setup(manager)
    await manager.retrieve("q", ["梗"])
    row = log.query()[0][0]
    assert row["tool"] == kbtrace.KB_DIRECT_TOOL
    full = log.get(row["id"])
    assert full["kb"]["mode"] == "direct"
    assert full["args"] == {"query": "q"}
    assert full["result_chars"] == 20


async def test_injection_is_recognised_by_the_prompt():
    manager = _Manager(HITS)
    log, rec, _ = _setup(manager)
    await manager.retrieve("用户的问题", ["梗"])
    await rec.on_llm_request("umo", "别的内容")
    assert log.query()[0][0]["tool"] == kbtrace.KB_DIRECT_TOOL
    await rec.on_llm_request("umo", "用户的问题")
    row = log.get(log.query()[0][0]["id"])
    assert row["tool"] == kbtrace.KB_INJECT_TOOL
    assert row["session"] == "umo"
    assert row["kb"]["mode"] == "inject"
    assert row["kb_count"] == 2


async def test_switches_limit_what_is_recorded():
    manager = _Manager(HITS)
    log, _rec, _ = _setup(manager, record_targets=False, record_other=False)
    await manager.retrieve("秘密", ["梗"])
    row = log.get(log.query()[0][0]["id"])
    assert row["args"] is None and row["result"] is None and row["top_hit"] is None
    assert row["kb"]["hits"] == [{"score": 0.36}, {"score": 0.2}]
    assert row["kb_top_score"] == 0.36


async def test_disabled_log_records_nothing():
    manager = _Manager(HITS)
    log, rec, _ = _setup(manager)
    rec.enabled = False
    await manager.retrieve("q", ["梗"])
    assert log.query()[1] == 0


async def test_recording_failure_does_not_touch_retrieval():
    manager = _Manager(HITS)

    async def broken(**_kwargs):
        raise RuntimeError("写不进去")

    kbtrace.install(manager, broken)
    assert (await manager.retrieve("q", ["梗"]))["results"] == HITS


async def test_restore_and_reinstall_do_not_stack():
    manager = _Manager(HITS)
    original = manager.retrieve
    calls = []

    async def record(**kwargs):
        calls.append(kwargs)

    kbtrace.install(manager, record)
    restore = kbtrace.install(manager, record)
    await manager.retrieve("q", ["梗"])
    assert len(calls) == 1
    restore()
    assert manager.retrieve == original


def test_no_retrieve_interface():
    assert kbtrace.install(object(), lambda **_: None) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("kb_search", True),
        ("search_knowledge", True),
        ("rag-query", True),
        ("知识库查询", True),
        ("web_search", False),
        ("drag_item", False),
        ("thinkbox", False),
    ],
)
def test_plugin_kb_tool_names(name, expected):
    assert kbtrace.is_plugin_kb_tool(name) is expected


async def test_plugin_kb_tool_has_no_scores():
    log = CallLog(None)
    rec = ToolCallRecorder(log, ActivityLog(record_targets=True), own_tools=())

    class Tool:
        name = "kb_search"

    await rec.on_start(1, "s", Tool(), {"query": "完全不同"})
    await rec.on_end(1, Tool(), type("R", (), {"isError": False, "content": []})())
    row = log.get(log.query({"kb_only": True})[0][0]["id"])
    assert row["kb"]["mode"] == "unscored"
    assert row["quality"] != "irrelevant"


def _stat_rows(log):
    for score, feedback, status, hits in (
        (0.4, "good", "ok", [("梗", "a"), ("梗", "b")]),
        (0.2, "bad", "ok", [("论文", "c"), ("梗", "a")]),
        (None, None, "empty", []),
    ):
        row = log.add(
            tool="astr_kb_search",
            source="builtin",
            status=status,
            kb={
                "mode": "tool",
                "embedding_calls": 2,
                "rerank_calls": 1 if hits else 0,
                "count": len(hits),
                "top_score": score,
                "hits": [{"kb": k, "doc": d, "score": score} for k, d in hits],
            },
        )
        if feedback:
            assert log.set_feedback(row, feedback)
    log.add(tool="kb_search", source="plugin:x", status="ok", kb={"mode": "unscored"})
    log.add(tool="other", source="builtin", status="ok")


def test_stats():
    log = CallLog(None)
    _stat_rows(log)
    stats = log.kb_stats()
    assert stats["total"] == 4
    assert stats["unscored"] == 1
    assert stats["empty"] == 1
    assert stats["multi_kb"] == 1
    assert stats["embedding_calls"] == 6
    assert stats["rerank_calls"] == 2
    assert stats["avg_top_score"] == 0.3
    assert stats["feedback"]["good"] == {"count": 1, "avg_top_score": 0.4}
    assert stats["feedback"]["bad"] == {"count": 1, "avg_top_score": 0.2}
    assert stats["by_kb"][0] == {"kb": "梗", "hits": 3, "first": 1, "avg_score": 0.333}
    assert stats["by_doc"][0] == {"kb": "梗", "doc": "a", "hits": 2}
    assert stats["modes"] == {"tool": 3, "unscored": 1}


def test_feedback_only_on_kb_rows():
    log = CallLog(None)
    row = log.add(tool="other", source="builtin", status="ok")
    assert not log.set_feedback(row, "good")


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "calls.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "duration_ms INTEGER, tool TEXT NOT NULL, source TEXT NOT NULL, session TEXT, "
        "status TEXT NOT NULL, request_id TEXT, http_status INTEGER, args TEXT, result TEXT, "
        "result_chars INTEGER, error TEXT, note TEXT, detail TEXT, "
        "content_recorded INTEGER NOT NULL DEFAULT 0, quality TEXT, relevance REAL, top_hit TEXT)"
    )
    conn.commit()
    conn.close()
    log = CallLog(path)
    row = log.add(
        tool="astr_kb_search",
        source="builtin",
        status="ok",
        kb={"mode": "tool", "count": 1, "top_score": 0.5, "hits": []},
    )
    assert log.get(row)["kb_top_score"] == 0.5
    log.close()


class _Inner:
    """和 AstrBot 的 RetrievalManager 一样吞掉向量和重排的异常。"""

    def __init__(self, vec_dbs, rerank_error=None):
        self.vec_dbs = vec_dbs
        self.rerank_error = rerank_error

    async def _dense_retrieve(self, query, kb_ids, kb_options):
        for kb_id in kb_ids:
            with contextlib.suppress(Exception):
                await kb_options[kb_id]["vec_db"].retrieve(query=query)
        return []

    async def _rerank(self, query, results, top_k, rerank_provider):
        if self.rerank_error:
            raise self.rerank_error
        return results

    async def retrieve(self, query):
        options = {k: {"vec_db": v} for k, v in self.vec_dbs.items()}
        await self._dense_retrieve(
            query=query, kb_ids=list(options), kb_options=options
        )
        with contextlib.suppress(Exception):
            await self._rerank(query=query, results=[], top_k=5, rerank_provider=None)


class _GoodVec:
    async def retrieve(self, **_kwargs):
        return []


class _BadVec:
    async def retrieve(self, **_kwargs):
        raise RuntimeError("向量接口超时")


class _InnerManager(_Manager):
    def __init__(self, inner):
        super().__init__(HITS)
        self.retrieval_manager = inner

    async def get_kb(self, kb_id):
        helper = _Helper()
        helper.kb = type("K", (), {"kb_name": f"库{kb_id}"})()
        return helper

    async def retrieve(self, query, kb_names, top_k_fusion=20, top_m_final=5):
        await self.retrieval_manager.retrieve(query)
        return {"context_text": "正文", "results": self.results}


async def test_silent_failures_are_marked_degraded():
    inner = _Inner(
        {"a": _GoodVec(), "b": _BadVec()}, rerank_error=ValueError("重排挂了")
    )
    manager = _InnerManager(inner)
    log, _rec, restore = _setup(manager)
    await manager.retrieve("q", ["a", "b"])
    row = log.query()[0][0]
    assert row["status"] == "ok" and row["kb_degraded"] == 1
    kb = log.get(row["id"])["kb"]
    assert kb["degraded"] == {"dense_failed": ["库b"], "rerank_failed": True}
    # 实际发生的次数，不是推算的
    assert kb["embedding_calls"] == 2 and kb["rerank_calls"] == 1
    restore()
    assert (
        "_dense_retrieve" not in inner.__dict__
        or inner._dense_retrieve.__name__ == "_dense_retrieve"
    )


async def test_healthy_retrieval_is_not_degraded():
    manager = _InnerManager(_Inner({"a": _GoodVec()}))
    log, _rec, _ = _setup(manager)
    await manager.retrieve("q", ["a"])
    row = log.query()[0][0]
    assert row["kb_degraded"] == 0
    assert "degraded" not in log.get(row["id"])["kb"]


async def test_watching_does_not_stack_on_reinstall():
    inner = _Inner({"a": _GoodVec()})
    manager = _InnerManager(inner)
    log, rec, _ = _setup(manager)
    kbtrace.install(manager, rec.on_kb_retrieve)
    await manager.retrieve("q", ["a"])
    assert log.get(log.query()[0][0]["id"])["kb"]["embedding_calls"] == 1


async def test_model_usage_of_the_round_after_retrieval():
    manager = _Manager(HITS)
    log, rec, _ = _setup(manager)

    class Web:
        name = "web_search"

    await rec.on_start(1, "s", _Tool(), {"query": "q"}, context_tokens=90000)
    await manager.retrieve("q", ["梗"])
    await rec.on_end(1, _Tool(), _Result())
    await rec.on_start(1, "s", Web(), {"query": "w"}, context_tokens=90000)
    await rec.on_end(1, Web(), _Result())
    usage = {"input_other": 3000, "input_cached": 88000, "output": 200}
    await rec.on_agent_done(1, usage)
    row = next(r for r in log.query()[0] if r["tool"] == "astr_kb_search")
    tokens = log.get(row["id"])["kb"]["tokens"]
    assert tokens["next_input"] == 90000
    assert tokens["final"] == usage
    assert tokens["shared_tools"] == 2
    assert tokens["result"] > 0


async def test_last_round_usage_is_not_given_to_earlier_retrievals():
    manager = _Manager(HITS)
    log, rec, _ = _setup(manager)

    class Web:
        name = "web_search"

    await rec.on_start(1, "s", _Tool(), {"query": "q"}, context_tokens=90000)
    await manager.retrieve("q", ["梗"])
    await rec.on_end(1, _Tool(), _Result())
    await rec.on_start(1, "s", Web(), {"query": "w"}, context_tokens=95000)
    await rec.on_end(1, Web(), _Result())
    await rec.on_agent_done(1, {"input_other": 1, "input_cached": 2, "output": 3})
    row = next(r for r in log.query()[0] if r["tool"] == "astr_kb_search")
    tokens = log.get(row["id"])["kb"]["tokens"]
    assert tokens["next_input"] == 90000
    assert "final" not in tokens


def test_usage_fields():
    usage = type("U", (), {"input_other": 1, "input_cached": 2, "output": 3})()
    response = type("R", (), {"usage": usage})()
    assert kbtrace.usage_fields(response) == {
        "input_other": 1,
        "input_cached": 2,
        "output": 3,
    }
    assert kbtrace.usage_fields(object()) is None


def test_estimate_tokens():
    assert kbtrace.estimate_tokens(None) == 0
    assert kbtrace.estimate_tokens("知识库") == 1
    assert kbtrace.estimate_tokens("a" * 10) == 3


def _recent(total=0, error=0, degraded=0):
    return {"total": total, "error": error, "degraded": degraded, "last_ts": None}


@pytest.mark.parametrize(
    ("names", "problems", "recent", "verdict"),
    [
        ([], [], _recent(), "off"),
        (["a"], [], _recent(), "idle"),
        (["a"], [], _recent(5), "ok"),
        ([], [], _recent(5), "ok"),
        (["a"], [{"code": "empty", "kb": "a"}], _recent(5), "warn"),
        (["a"], [{"code": "missing", "kb": "a"}], _recent(5), "bad"),
        (["a"], [{"code": "init_error", "kb": "a"}], _recent(), "bad"),
        (["a"], [], _recent(10, error=1), "warn"),
        (["a"], [], _recent(10, error=5), "bad"),
        (["a"], [], _recent(10, degraded=1), "warn"),
    ],
)
def test_status_verdict(names, problems, recent, verdict):
    config = {"names": names, "problems": problems}
    assert kbtrace.assess_status(config, recent)["verdict"] == verdict


def test_scores_do_not_affect_status():
    config = {"names": ["a"], "problems": []}
    assert kbtrace.assess_status(config, _recent(50))["reasons"] == []


def test_marked_rows_survive_pruning():
    now = [1_000_000.0]
    log = CallLog(None, retention_days=1, max_rows=2, clock=lambda: now[0])
    kept = log.add(tool="k", source="b", status="ok", kb={"mode": "tool", "count": 1})
    gone = log.add(tool="k", source="b", status="ok", kb={"mode": "tool", "count": 1})
    assert log.set_feedback(kept, "good")
    now[0] += 3 * 86400
    log.prune()
    assert log.get(kept) is not None
    assert log.get(gone) is None
    assert log.clear() == 1


def test_health_and_stats_count_degraded_and_tokens():
    log = CallLog(None)
    log.add(
        tool="astr_kb_search",
        source="builtin",
        status="ok",
        kb={
            "mode": "tool",
            "count": 1,
            "top_score": 0.5,
            "hits": [],
            "degraded": {"dense_failed": ["a"], "rerank_failed": False},
            "tokens": {
                "result": 10,
                "next_input": 100,
                "final": {"input_other": 1, "input_cached": 2, "output": 3},
            },
        },
    )
    log.add(
        tool="astr_kb_search", source="builtin", status="error", kb={"mode": "tool"}
    )
    health = log.kb_health(0)
    assert health["total"] == 2 and health["error"] == 1 and health["degraded"] == 1
    stats = log.kb_stats()
    assert stats["degraded"] == 1
    assert stats["tokens"]["result"] == 10
    assert stats["tokens"]["next_input"] == 100
    assert stats["tokens"]["final_cached"] == 2
    assert stats["tokens"]["final_rounds"] == 1


class _RerankInner(_Inner):
    """重排时带上候选内容，和 AstrBot 一样用关键字参数。"""

    async def retrieve(self, query):
        await super().retrieve(query)
        await self._rerank(
            query=query,
            results=[types.SimpleNamespace(content="候选内容" * 25)],
            top_k=5,
            rerank_provider=None,
        )


async def test_watching_starts_when_retrieval_manager_appears_later():
    """AstrBot 先加载插件、后初始化知识库：装上的时候还没有检索管理器。"""
    manager = _InnerManager(_RerankInner({"a": _GoodVec(), "b": _BadVec()}))
    inner = manager.retrieval_manager
    del manager.retrieval_manager
    log, _rec, restore = _setup(manager)
    manager.retrieval_manager = inner
    await manager.retrieve("q", ["a", "b"])
    kb = log.get(log.query()[0][0]["id"])["kb"]
    assert kb["degraded"] == {"dense_failed": ["库b"], "rerank_failed": False}
    assert kb["billing"]["rerank_tokens"] > 0
    # 换了一个新的检索管理器也能跟上
    manager.retrieval_manager = _RerankInner({"c": _BadVec()})
    await manager.retrieve("q2", ["c"])
    kb = log.get(log.query()[0][0]["id"])["kb"]
    assert kb["degraded"]["dense_failed"] == ["库c"]
    restore()
    assert manager.retrieval_manager._rerank.__name__ == "_rerank"
