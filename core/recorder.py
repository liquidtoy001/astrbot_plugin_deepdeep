"""把 AstrBot 的工具调用钩子转成调用日志记录。

AstrBot 在每次工具调用前后分别触发 on_using_llm_tool / on_llm_tool_respond。注意：
- 工具抛出异常时不会触发“调用后”钩子，所以在 Agent 结束时（on_agent_done）把没收尾的记录标记为异常
- 前后两个钩子拿到的参数可能不同（前者已过滤掉工具不接受的参数），所以按“事件 + 工具名”配对，而不是按参数
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from .activity import ActivityLog
from .calllog import CallLog

REQUEST_ID_RE = re.compile(r"#([0-9a-f]{6})\]")

# DeepSeek 工具在 ActivityLog 里的结果 -> 调用日志状态
OUTCOME_TO_STATUS = {
    "billable": "ok",
    "empty": "empty",
    "cache_hit": "cache_hit",
    "failed": "error",
    "blocked": "blocked",
}


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


class ToolCallRecorder:
    def __init__(
        self,
        log: CallLog,
        activity: ActivityLog,
        *,
        own_tools: tuple[str, ...],
        plugin_resolver: Callable[[str], str | None] = lambda _m: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log = log
        self.activity = activity
        self.own_tools = own_tools
        self.plugin_resolver = plugin_resolver
        self.enabled = True
        # 其他工具的参数和结果默认不记录，只记工具名、耗时和状态
        self.record_other_content = False
        self._clock = clock
        self._pending: dict[tuple[int, str], deque[tuple[int, float]]] = defaultdict(
            deque
        )

    def _record_content(self, source: str) -> bool:
        # DeepSeek 工具的内容遵循“记录搜索词”开关
        if source == "deepseek":
            return self.activity.record_targets
        return self.record_other_content

    async def on_start(
        self, event_key: int, session: str | None, tool: Any, args: Any
    ) -> None:
        if not self.enabled:
            return
        name = getattr(tool, "name", "unknown")
        source = detect_source(tool, self.own_tools, self.plugin_resolver)
        row_id = await asyncio.to_thread(
            self.log.start,
            tool=name,
            source=source,
            session=session,
            args=args,
            record_content=self._record_content(source),
        )
        self._pending[(event_key, name)].append((row_id, self._clock()))

    async def on_end(self, event_key: int, tool: Any, tool_result: Any) -> None:
        name = getattr(tool, "name", "unknown")
        queue = self._pending.get((event_key, name))
        if not queue:
            return
        row_id, started = queue.popleft()
        if not queue:
            self._pending.pop((event_key, name), None)
        duration = int((self._clock() - started) * 1000)
        text = result_to_text(tool_result)

        if name in self.own_tools:
            fields = self._own_fields(text)
            record = self.activity.record_targets
        else:
            is_error = bool(getattr(tool_result, "isError", False))
            fields = {
                "status": "error" if is_error else "ok",
                "note": "无返回值，或已直接发送给用户" if text is None else None,
                "error": text if is_error else None,
            }
            record = self.record_other_content
        await asyncio.to_thread(
            self.log.finish,
            row_id,
            duration_ms=duration,
            result=text,
            record_content=record,
            **fields,
        )

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

    async def on_agent_done(self, event_key: int) -> int:
        """Agent 结束时，把本事件里没收到“调用后”钩子的记录标为异常。"""
        keys = [k for k in self._pending if k[0] == event_key]
        closed = 0
        for key in keys:
            for row_id, started in self._pending.pop(key, ()):
                await asyncio.to_thread(
                    self.log.finish,
                    row_id,
                    status="error",
                    duration_ms=int((self._clock() - started) * 1000),
                    error="工具执行时抛出异常或被中断，AstrBot 没有返回结果，详见 AstrBot 日志",
                )
                closed += 1
        return closed
