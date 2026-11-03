"""把 AstrBot 的工具调用钩子转成调用日志记录。

AstrBot 在每次工具调用前后分别触发 on_using_llm_tool / on_llm_tool_respond。注意：
- 工具抛出异常时不会触发“调用后”钩子，所以在 Agent 结束时（on_agent_done）把没收尾的记录标记为异常
- 前后两个钩子拿到的参数可能不同（前者已过滤掉工具不接受的参数），所以按“事件 + 工具名”配对，而不是按参数
"""

from __future__ import annotations

import asyncio
import contextvars
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import kbtrace
from .activity import ActivityLog
from .calllog import CallLog
from .health import classify, extract_query
from .providers import KB_TOOL, TAVILY_TOOL
from .tophit import extract as extract_top_hit

# 钩子跑了这么久，就算没放内容也是查过了
PLUGIN_BUSY_MS = 200
REQUEST_ID_RE = re.compile(r"#([0-9a-f]{6})\]")
TAVILY_HEAD_RE = re.compile(r"^\[Tavily 搜索 #[0-9a-f]{6}\] (\S+)(?: · (.*))?$")
HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
# 关闭内容记录时，错误只留这句：工具的错误文字里可能有问题原文或记忆片段
ERROR_NOT_RECORDED = "工具返回了错误（内容没有记录）"
# AstrBot 在同一个任务里先调“调用前”钩子再执行工具，所以工具里能知道自己是哪一条记录。
# 不同会话、并行的工具调用各有各的值，比按搜索词或先后顺序猜可靠
_CURRENT_CALL: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "deepdeep_current_call", default=None
)

# DeepSeek 工具在 ActivityLog 里的结果 -> 调用日志状态
OUTCOME_TO_STATUS = {
    "billable": "ok",
    "empty": "empty",
    "cache_hit": "cache_hit",
    "failed": "error",
    "blocked": "blocked",
}

KB_DIRECT_REMEMBERED = 20
# 自动注入的检索在同一条消息发起 LLM 请求之前完成，隔得不会太久
KB_INJECT_WINDOW = 120.0


def result_to_text(tool_result: Any) -> str | None:
    """把 mcp.types.CallToolResult 转成纯文本；None 表示工具没有返回值。"""
    if tool_result is None:
        return None
    content = getattr(tool_result, "content", None)
    if content is None:
        return str(tool_result)
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        parts.append(text if isinstance(text, str) else f"[{type(item).__name__}]")
    return "\n".join(parts)


def _tavily_fields(text: str | None) -> dict[str, Any]:
    """Tavily 的结果是一段文字，第一行写着这次怎么样。"""
    lines = (text or "").split("\n")
    match = TAVILY_HEAD_RE.match(lines[0])
    if match is None:
        # 工具入口自己拦下的：到了上限或暂时不能用
        blocked = lines[0].startswith(
            ("本月 Tavily 调用已达上限", "Tavily 搜索暂不可用")
        )
        return {"status": "blocked" if blocked else "ok"}
    outcome, facts = match.group(1), match.group(2) or ""
    if outcome == "失败":
        http = HTTP_STATUS_RE.search(facts)
        return {
            "status": "error",
            # 这里只有 Tavily 客户端写的固定说明，不含搜索内容
            "error": lines[1] if len(lines) > 1 else lines[0],
            "http_status": int(http.group(1)) if http else None,
        }
    if outcome == "未调用":
        return {"status": "blocked", "note": facts or None}
    if outcome == "缓存":
        return {"status": "cache_hit"}
    return {"status": "empty" if facts.startswith("0 条") else "ok"}


def detect_source(
    tool: Any, own_tools: tuple[str, ...], plugin_resolver: Callable[[str], str | None]
) -> str:
    """工具来自哪里：deepseek / builtin / plugin:<插件名> / mcp:<服务名> / unknown。"""
    name = getattr(tool, "name", "")
    if name in own_tools:
        return "deepseek"
    if type(tool).__name__ == "MCPTool" or hasattr(tool, "mcp_server_name"):
        return f"mcp:{getattr(tool, 'mcp_server_name', '') or '?'}"
    module = getattr(tool, "handler_module_path", None) or ""
    if module:
        plugin = plugin_resolver(module)
        if plugin:
            return f"plugin:{plugin}"
        if module.startswith("astrbot."):
            return "builtin"
        return f"module:{module}"
    if type(tool).__module__.startswith("astrbot."):
        return "builtin"
    return "unknown"


@dataclass
class _Round:
    """模型的一轮回复。同一轮的工具调用，看到的“上一轮 token 数”相同。"""

    tokens: int | None
    tools: int = 0
    kb_rows: list[int] = field(default_factory=list)


class ToolCallRecorder:
    def __init__(
        self,
        log: CallLog,
        activity: ActivityLog,
        *,
        own_tools: tuple[str, ...],
        plugin_resolver: Callable[[str], str | None] = lambda _m: None,
        is_search: Callable[[str], bool] = lambda _name: False,
        mcp_server_of: Callable[[str], str | None] = lambda _name: None,
        kb_plugin: Callable[[str], bool] = lambda _name: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log = log
        self.activity = activity
        self.own_tools = own_tools
        self.plugin_resolver = plugin_resolver
        self.is_search = is_search
        self.mcp_server_of = mcp_server_of
        self.kb_plugin = kb_plugin
        self.enabled = True
        # 其他工具的参数和结果默认不记录，只记工具名、耗时和状态
        self.record_other_content = False
        self._clock = clock
        # (记录 ID, 开始时间, 搜索词)；搜索词只留在内存里，用来算结果的相关度
        self._pending: dict[tuple[int, str], deque[tuple[int, float, str | None]]] = (
            defaultdict(deque)
        )
        # astr_kb_search 进行中时，retrieve 记下的结果，等“调用后”钩子写进同一条记录
        self._kb_results: dict[int, dict[str, Any]] = {}
        # 不是工具发起的检索：(记录 ID, 搜索词, 结果, 时间)，等请求钩子确认是不是自动注入
        self._kb_direct: deque[tuple[int, str, dict[str, Any], float]] = deque(
            maxlen=KB_DIRECT_REMEMBERED
        )
        self._rounds: dict[int, _Round] = {}

    def _source(self, tool: Any) -> str:
        source = detect_source(tool, self.own_tools, self.plugin_resolver)
        # AstrBot 交给钩子的工具对象可能丢了 MCP 信息，按工具名再查一次
        if source in ("builtin", "unknown"):
            server = self.mcp_server_of(getattr(tool, "name", ""))
            if server:
                return f"mcp:{server}"
        return source

    def _is_kb(self, tool: Any, source: str) -> bool:
        """AstrBot 的知识库工具，或工具名、说明、所属插件表明是知识库的工具。"""
        name = getattr(tool, "name", "")
        if name == KB_TOOL:
            return True
        if name in self.own_tools or self.is_search(name):
            return kbtrace.is_plugin_kb_tool(name) and name not in self.own_tools
        if kbtrace.is_plugin_kb_text(name, getattr(tool, "description", "") or ""):
            return True
        return source.startswith("plugin:") and self.kb_plugin(source[7:])

    def _record_content(self, source: str) -> bool:
        # DeepSeek 工具的内容遵循“记录搜索词”开关
        if source == "deepseek":
            return self.activity.record_targets
        return self.record_other_content

    async def on_start(
        self,
        event_key: int,
        session: str | None,
        tool: Any,
        args: Any,
        context_tokens: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        name = getattr(tool, "name", "unknown")
        source = self._source(tool)
        row_id = await asyncio.to_thread(
            self.log.start,
            tool=name,
            source=source,
            session=session,
            args=args,
            record_content=self._record_content(source),
        )
        self._pending[(event_key, name)].append(
            (row_id, self._clock(), extract_query(args))
        )
        _CURRENT_CALL.set(row_id)
        current = self._rounds.get(event_key)
        if current is None or current.tokens != context_tokens:
            current = self._rounds[event_key] = _Round(context_tokens)
        current.tools += 1
        if self._is_kb(tool, source):
            current.kb_rows.append(row_id)

    async def on_end(self, event_key: int, tool: Any, tool_result: Any) -> None:
        name = getattr(tool, "name", "unknown")
        queue = self._pending.get((event_key, name))
        if not queue:
            return
        current = _CURRENT_CALL.get()
        entry = next((e for e in queue if e[0] == current), queue[0])
        queue.remove(entry)
        row_id, started, query = entry
        if not queue:
            self._pending.pop((event_key, name), None)
        duration = int((self._clock() - started) * 1000)
        text = result_to_text(tool_result)

        if name in self.own_tools:
            fields = self._own_fields(text)
            record = self.activity.record_targets
        elif name == TAVILY_TOOL:
            fields = _tavily_fields(text)
            record = self.record_other_content
        else:
            is_error = bool(getattr(tool_result, "isError", False))
            record = self.record_other_content
            fields = {
                "status": "error" if is_error else "ok",
                "note": "无返回值，或已直接发送给用户" if text is None else None,
                "error": (text if record else ERROR_NOT_RECORDED) if is_error else None,
            }
        kb = self._kb_results.pop(row_id, None)
        is_kb = self._is_kb(tool, self._source(tool))
        if is_kb and kb is None:
            kb = {
                "mode": kbtrace.MODE_UNSCORED,
                "tokens": {"result": kbtrace.estimate_tokens(text)},
            }
        if kb is not None:
            # 工具结果会让模型再跑一轮，下一轮的输入约等于上一轮的总量
            current = self._rounds.get(event_key)
            if current is not None and current.tokens:
                kb["tokens"]["next_input"] = current.tokens
                # 同一轮的几条检索各自显示这一轮的用量，汇总时只算一次
                kb["tokens"]["round"] = (
                    current.kb_rows[0] if current.kb_rows else row_id
                )
            fields["kb"] = kb
        if kb is not None and kb["mode"] == kbtrace.MODE_TOOL:
            # 知识库按语义匹配，有分数和来源时不用字面重合判断相关度
            if fields["status"] == "ok" and not kb["count"]:
                fields["status"] = "empty"
            fields["top_hit"] = kbtrace.top_line(kb)
        else:
            if fields["status"] == "ok":
                quality, score = classify(query, text)
                fields["quality"] = (
                    None if is_kb and quality == "irrelevant" else quality
                )
                fields["relevance"] = score
            # 第一条结果的简述跟随“记录搜索词”开关，只记搜索工具和知识库工具，不记其他工具的返回内容
            if (
                fields["status"] in ("ok", "cache_hit")
                and self.activity.record_targets
                and (name in self.own_tools or self.is_search(name) or is_kb)
            ):
                fields["top_hit"] = extract_top_hit(text)
        await asyncio.to_thread(
            self.log.finish,
            row_id,
            duration_ms=duration,
            result=text,
            record_content=record,
            **fields,
        )

    def _kb_tool_call(self, query: str) -> int | None:
        """这次检索属于哪条 astr_kb_search 记录。不是工具发起的检索返回 None。"""
        current = _CURRENT_CALL.get()
        waiting = [
            (row_id, pending_query)
            for (_event, name), queue in self._pending.items()
            if name == KB_TOOL
            for row_id, _started, pending_query in queue
            if row_id not in self._kb_results
        ]
        if any(row_id == current for row_id, _query in waiting):
            return current
        # 当前任务不知道自己的记录（AstrBot 改了调用方式）时才按搜索词找，同样的词有多条就不猜
        same = [row_id for row_id, pending in waiting if pending == query]
        return same[0] if current is None and len(same) == 1 else None

    async def on_kb_retrieve(
        self,
        *,
        query: str,
        results: list[dict] | None,
        context_text: str | None,
        kb_count: int,
        embedding_calls: int,
        rerank_calls: int,
        duration_ms: int,
        error: str | None,
        degraded: dict[str, Any] | None = None,
        billing: dict[str, Any] | None = None,
    ) -> None:
        """一次知识库检索完成。工具发起的写进工具那条记录，其他的单独记一条。"""
        if not self.enabled:
            return
        error = self._error_text(error)
        info = kbtrace.summarize(
            results,
            kb_count=kb_count,
            embedding_calls=embedding_calls,
            rerank_calls=rerank_calls,
            record_sources=self.activity.record_targets,
            record_snippets=self.record_other_content,
            error=error,
            degraded=degraded,
            context_text=context_text,
            billing=billing,
        )
        row_id = self._kb_tool_call(query.strip())
        if row_id is not None:
            info["mode"] = kbtrace.MODE_TOOL
            self._kb_results[row_id] = info
            return
        status = "error" if error else "ok" if info["count"] else "empty"
        row_id = await asyncio.to_thread(
            self.log.add,
            tool=kbtrace.KB_DIRECT_TOOL,
            source="builtin",
            status=status,
            args={"query": query},
            record_content=self.record_other_content,
            duration_ms=duration_ms,
            result=context_text,
            error=error,
            top_hit=kbtrace.top_line(info),
            kb=info,
        )
        self._kb_direct.append((row_id, query, info, self._clock()))

    async def on_llm_request(self, session: str | None, prompt: str | None) -> None:
        """发给模型的内容就是刚才检索的搜索词：这次检索是 AstrBot 自动注入的，补上会话。"""
        if not prompt:
            return
        now = self._clock()
        for entry in self._kb_direct:
            row_id, query, info, ts = entry
            if query == prompt and now - ts <= KB_INJECT_WINDOW:
                self._kb_direct.remove(entry)
                info["mode"] = kbtrace.MODE_INJECT
                await asyncio.to_thread(
                    self.log.relabel_kb,
                    row_id,
                    tool=kbtrace.KB_INJECT_TOOL,
                    session=session,
                    kb=info,
                )
                return

    async def on_plugin_inject(
        self,
        *,
        plugin: str,
        session: str | None,
        query: str,
        text: str,
        duration_ms: int,
        error: str | None,
        usage: dict[str, Any],
        cancelled: bool = False,
    ) -> None:
        """社区知识库插件的钩子跑完了。什么都没放、也没做事的（比如没开启）不算一次检索。"""
        if not self.enabled:
            return
        busy = usage["embedding"] or usage["rerank"] or duration_ms >= PLUGIN_BUSY_MS
        if not text and not error and not busy and not cancelled:
            return
        status = (
            "interrupted"
            if cancelled
            else "error"
            if error
            else "ok"
            if text
            else "empty"
        )
        error = self._error_text(error)
        await asyncio.to_thread(
            self.log.add,
            tool=kbtrace.KB_PLUGIN_INJECT_TOOL,
            source=f"plugin:{plugin}",
            status=status,
            session=session,
            args={"query": query},
            record_content=self.record_other_content,
            duration_ms=duration_ms,
            result=text or None,
            error=error,
            kb={
                "mode": kbtrace.MODE_PLUGIN_INJECT,
                "embedding_calls": usage["embedding"],
                "rerank_calls": usage["rerank"],
                "tokens": {"result": kbtrace.estimate_tokens(text)},
                "billing": {
                    key: usage[key]
                    for key in (
                        "embedding_provider",
                        "embedding_tokens",
                        "embedding_by",
                        "rerank_provider",
                        "rerank_tokens",
                        "rerank_by",
                    )
                },
            },
        )

    def _error_text(self, error: str | None) -> str | None:
        """检索的错误写成“类型: 说明”，说明里可能有内容，没开记录时只留类型。"""
        if not error or self.record_other_content:
            return error
        return error.split(":", 1)[0]

    def _own_fields(self, text: str | None) -> dict[str, Any]:
        """DeepSeek 工具：用结果第一行的请求 ID 找到详细记录。"""
        match = REQUEST_ID_RE.search((text or "").split("\n", 1)[0])
        entry = self.activity.find(match.group(1)) if match else None
        if entry is None:
            return {"status": "ok"}
        return {
            "status": OUTCOME_TO_STATUS.get(entry["outcome"], "ok"),
            "request_id": entry["request_id"],
            "http_status": entry["status"] or None,
            "note": entry["note"] or None,
            "error": (entry["detail"].get("error") or {}).get("message"),
            "detail": entry["detail"],
        }

    async def on_agent_done(
        self, event_key: int, usage: dict[str, int] | None = None
    ) -> int:
        """Agent 结束时，把本事件里没收到“调用后”钩子的记录标为异常。"""
        keys = [k for k in self._pending if k[0] == event_key]
        closed = 0
        for key in keys:
            for row_id, started, _query in self._pending.pop(key, ()):
                kb = self._kb_results.pop(row_id, None)
                await asyncio.to_thread(
                    self.log.finish,
                    row_id,
                    status="error",
                    duration_ms=int((self._clock() - started) * 1000),
                    error=(kb or {}).get("error")
                    or "工具执行时抛出异常或被中断，AstrBot 没有返回结果，详见 AstrBot 日志",
                    kb=kb,
                )
                closed += 1
        # 最后一轮的用量只归给紧挨着它的检索；更早的轮次 AstrBot 没有给出明细
        last = self._rounds.pop(event_key, None)
        if usage and last is not None and last.tokens:
            for row_id in last.kb_rows:
                await asyncio.to_thread(
                    self.log.update_kb_tokens,
                    row_id,
                    final=usage,
                    shared_tools=last.tools,
                    round=last.kb_rows[0],
                )
        return closed
