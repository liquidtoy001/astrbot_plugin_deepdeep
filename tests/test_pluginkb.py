import inspect
import types
from typing import ClassVar

import pytest

from core import kbtrace, pluginkb
from core.activity import ActivityLog
from core.astrbot_view import looks_like_kb_plugin
from core.calllog import CallLog
from core.pluginkb import PluginKbWatch, added_text, snapshot
from core.recorder import ToolCallRecorder


class _Req:
    def __init__(self, prompt="问题", system="人设", contexts=None):
        self.prompt = prompt
        self.system_prompt = system
        self.contexts = contexts or []
        self.extra_user_content_parts = []
        self.func_tool = None


class _Embedding:
    provider_config: ClassVar[dict] = {"id": "emb"}

    async def get_embedding(self, text):
        # 有的服务商内部一个方法调另一个
        return (await self.get_embeddings([text]))[0]

    async def get_embeddings(self, text):
        return [[0.0] for _ in text]


class _Rerank:
    provider_config: ClassVar[dict] = {"id": "rr"}

    async def rerank(self, query, documents, top_n=None):
        return documents


def _handler(fn):
    return types.SimpleNamespace(handler=fn)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _setup(meta, *, plugins=("kb_plugin",), providers=(), record_other=False):
    log = CallLog(None)
    activity = ActivityLog()
    activity.record_targets = True
    recorder = ToolCallRecorder(log, activity, own_tools=())
    recorder.record_other_content = record_other
    clock = _Clock()
    watch = PluginKbWatch(
        kb_plugins=lambda: [{"name": p} for p in plugins],
        providers=lambda: providers,
        on_inject=recorder.on_plugin_inject,
        handlers=lambda: meta,
        clock=clock,
    )
    return log, recorder, watch, clock


def test_added_text_finds_what_a_plugin_added():
    old = {"role": "user", "content": "旧消息"}
    req = _Req(contexts=[old])
    before = snapshot(req)
    req.system_prompt = "人设\n\n知识：咖啡"
    req.prompt = "资料：茶\n问题"
    req.contexts.insert(
        0, {"role": "system", "content": [{"type": "text", "text": "上下文"}]}
    )
    text = added_text(before, req)
    assert "知识：咖啡" in text and "资料：茶" in text and "上下文" in text
    assert "人设" not in text and "旧消息" not in text


def test_added_text_ignores_a_shorter_rewrite():
    req = _Req(prompt="一个很长的问题")
    before = snapshot(req)
    req.prompt = "短问题"
    assert added_text(before, req) == ""


@pytest.mark.asyncio
async def test_injection_is_recorded_without_changing_the_request():
    embedding, rerank = _Embedding(), _Rerank()

    async def hook(event, req):
        await embedding.get_embedding("问题")
        await rerank.rerank("问题", ["候选一", "候选二"])
        req.system_prompt += "\n知识内容"

    meta = _handler(hook)
    log, _, watch, _ = _setup(
        [(meta, "kb_plugin")],
        providers=[("embedding", embedding), ("rerank", rerank)],
    )
    watch.sync()
    assert inspect.iscoroutinefunction(meta.handler)
    req = _Req()
    event = types.SimpleNamespace(unified_msg_origin="s1")
    await meta.handler(event, req)

    assert req.system_prompt == "人设\n知识内容"
    row = log.get(log.query({"kb_only": True})[0][0]["id"])
    assert row["tool"] == kbtrace.KB_PLUGIN_INJECT_TOOL
    assert row["source"] == "plugin:kb_plugin"
    assert row["status"] == "ok"
    assert row["session"] == "s1"
    kb = row["kb"]
    assert kb["mode"] == kbtrace.MODE_PLUGIN_INJECT
    # get_embedding 内部调 get_embeddings，只算一次
    assert kb["embedding_calls"] == 1
    assert kb["rerank_calls"] == 1
    assert kb["billing"]["embedding_provider"] == "emb"
    assert kb["billing"]["rerank_tokens"] > 0
    assert kb["tokens"]["result"] > 0
    # 默认不保存内容和问题
    assert not row.get("result")
    assert not row.get("args")


@pytest.mark.asyncio
async def test_content_follows_the_record_switch():
    async def hook(event, req):
        req.system_prompt += "\n知识内容"

    meta = _handler(hook)
    log, _, watch, _ = _setup([(meta, "kb_plugin")], record_other=True)
    watch.sync()
    await meta.handler(types.SimpleNamespace(), _Req())
    row = log.get(log.query({"kb_only": True})[0][0]["id"])
    assert "知识内容" in row["result"]


@pytest.mark.asyncio
async def test_idle_hook_is_not_a_retrieval():
    async def hook(event, req):
        return None

    meta = _handler(hook)
    log, _, watch, _ = _setup([(meta, "kb_plugin")])
    watch.sync()
    await meta.handler(types.SimpleNamespace(), _Req())
    assert log.query({"kb_only": True})[1] == 0


@pytest.mark.asyncio
async def test_retrieval_with_nothing_to_inject_is_empty():
    embedding = _Embedding()

    async def hook(event, req):
        await embedding.get_embedding("问题")

    meta = _handler(hook)
    log, _, watch, _ = _setup(
        [(meta, "kb_plugin")], providers=[("embedding", embedding)]
    )
    watch.sync()
    await meta.handler(types.SimpleNamespace(), _Req())
    assert log.query({"kb_only": True})[0][0]["status"] == "empty"


@pytest.mark.asyncio
async def test_plugin_error_is_recorded_and_still_raised():
    async def hook(event, req):
        raise ValueError("boom")

    meta = _handler(hook)
    log, _, watch, _ = _setup([(meta, "kb_plugin")])
    watch.sync()
    with pytest.raises(ValueError):
        await meta.handler(types.SimpleNamespace(), _Req())
    assert log.query({"kb_only": True})[0][0]["status"] == "error"


@pytest.mark.asyncio
async def test_recording_failure_does_not_break_the_request():
    called = []

    async def hook(event, req):
        called.append(1)
        req.system_prompt += "x"

    async def broken(**_kwargs):
        raise RuntimeError("db")

    meta = _handler(hook)
    watch = PluginKbWatch(
        kb_plugins=lambda: [{"name": "kb_plugin"}],
        providers=lambda: (),
        on_inject=broken,
        handlers=lambda: [(meta, "kb_plugin")],
    )
    watch.sync()
    await meta.handler(types.SimpleNamespace(), _Req())
    assert called == [1]


@pytest.mark.asyncio
async def test_only_kb_plugins_are_wrapped_and_restore_works():
    async def hook(event, req):
        return None

    mine, other = _handler(hook), _handler(hook)
    embedding = _Embedding()
    original = embedding.get_embedding
    _, _, watch, clock = _setup(
        [(mine, "kb_plugin"), (other, "chat_plugin")],
        providers=[("embedding", embedding)],
    )
    watch.sync()
    assert mine.handler is not hook
    assert other.handler is hook
    wrapped = mine.handler
    clock.now += pluginkb.SYNC_INTERVAL
    watch.sync()
    # 不会包两层
    assert mine.handler is wrapped
    watch.restore()
    assert mine.handler is hook
    assert embedding.get_embedding == original


def test_nothing_is_wrapped_without_a_kb_plugin():
    async def hook(event, req):
        return None

    meta = _handler(hook)
    embedding = _Embedding()
    _, _, watch, _ = _setup(
        [(meta, "chat_plugin")], plugins=(), providers=[("embedding", embedding)]
    )
    watch.sync()
    assert meta.handler is hook
    assert "get_embedding" not in embedding.__dict__


@pytest.mark.asyncio
async def test_provider_outside_a_plugin_hook_is_not_counted():
    embedding = _Embedding()
    _, _, watch, _ = _setup([], providers=[("embedding", embedding)])
    watch.sync()
    assert await embedding.get_embedding("问题") == [0.0]


def test_interface_change_only_warns_once():
    def broken():
        raise ImportError("moved")

    watch = PluginKbWatch(
        kb_plugins=lambda: [{"name": "a"}],
        providers=lambda: (),
        on_inject=None,
        handlers=broken,
    )
    watch.sync()
    assert watch._warned


@pytest.mark.asyncio
async def test_stats_keep_plugins_apart_from_scored_retrievals():
    log = CallLog(None)
    for status, tokens in (("ok", 100), ("ok", 300), ("empty", 0), ("error", 0)):
        log.add(
            tool=kbtrace.KB_PLUGIN_INJECT_TOOL,
            source="plugin:kb_plugin",
            status=status,
            duration_ms=100,
            kb={
                "mode": kbtrace.MODE_PLUGIN_INJECT,
                "embedding_calls": 1,
                "rerank_calls": 0,
                "tokens": {"result": tokens},
                "billing": {"embedding_provider": "emb", "embedding_tokens": 5},
            },
        )
    stats = log.kb_stats()
    assert stats["total"] == 4
    assert stats["unscored"] == 4
    assert stats["empty"] == 0
    assert stats["error"] == 0
    assert stats["embedding_calls"] == 4
    assert stats["billing"]["embedding"] == {"emb": 20}
    assert stats["by_plugin"] == [
        {
            "plugin": "kb_plugin",
            "total": 4,
            "empty": 1,
            "error": 1,
            "avg_tokens": 200,
            "avg_ms": 100,
        }
    ]
    # 平均耗时和内容量只看带分数的检索
    assert stats["tokens"]["result"] == 0
    assert stats["avg_ms"] is None
    # 插件失败只提醒，不算 AstrBot 知识库失败
    health = log.kb_health(0)
    assert health["error"] == 0
    assert health["plugin_error"] == 1
    verdict = kbtrace.assess_status({"names": ["a"], "problems": []}, health)
    assert verdict["verdict"] == "warn"
    assert verdict["reasons"] == [{"code": "plugin_errors", "n": 1}]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("astrbot_plugin_knowledge_base", True),
        ("astrbot_plugin_kb Local document retrieval", True),
        ("astrbot_plugin_memory_usage Show host RAM usage", False),
        ("astrbot_plugin_image_tools 压缩到 100 KB 以内", False),
        ("astrbot_plugin_ragflow_adapter", True),
        ("astrbot_plugin_rag_pro", True),
        ("astrbot_plugin_livingmemory", True),
        ("记忆花园 长期记忆召回", True),
        ("meme_manager 表情包管理", False),
        ("storage fragment", False),
    ],
)
def test_kb_plugin_recognition(text, expected):
    assert looks_like_kb_plugin(text.split()[0], text) is expected
