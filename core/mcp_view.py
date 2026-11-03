"""MCP 页的数据：每个服务连没连上、有哪些工具、用得怎么样、工具说明占多少 token。"""

from __future__ import annotations

import json
import re
from typing import Any

from .kbtrace import estimate_tokens, is_plugin_kb_text

_WRITE_RE = re.compile(
    r"(?:^|[_-])(create|update|delete|remove|write|edit|push|merge|add|fork|close|send|post|put|set|run|exec)(?:[_-]|$)",
    re.IGNORECASE,
)
_SEARCH_RE = re.compile(r"search|query|find|lookup", re.IGNORECASE)
# 模型每次请求都要带上工具说明；这是粗略估算
RESULT_TOKENS_PER_CHAR = 0.5
# 调用太少时偶然失败不算数
MIN_CALLS_TO_JUDGE = 2


def tool_kind(name: str, description: str = "") -> str:
    if is_plugin_kb_text(name, description):
        return "kb"
    if _WRITE_RE.search(name):
        return "write"
    if _SEARCH_RE.search(name):
        return "search"
    return "read"


def schema_tokens(tool: dict[str, Any]) -> int:
    text = json.dumps(
        [tool.get("name"), tool.get("description"), tool.get("parameters")],
        ensure_ascii=False,
        default=str,
    )
    return estimate_tokens(text)


def build(
    servers: list[dict[str, Any]], stats: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把 AstrBot 里的服务和调用日志的统计合起来，用得多的排前面。"""
    by_server: dict[str, dict[str, dict[str, Any]]] = {}
    for row in stats:
        name = row["source"].removeprefix("mcp:")
        by_server.setdefault(name, {})[row["tool"]] = row
    known = {s["name"] for s in servers}
    # 服务已经删了，但日志里还有记录
    extra = [
        {"name": name, "state": "removed", "tools": [], "errors": []}
        for name in by_server
        if name not in known
    ]
    out = []
    for server in [*servers, *extra]:
        used = by_server.get(server["name"], {})
        tools = []
        for tool in server["tools"]:
            row = used.get(tool["name"]) or {}
            tools.append(
                {
                    "name": tool["name"],
                    "description": (tool.get("description") or "")[:200],
                    "active": tool.get("active", True),
                    "kind": tool_kind(tool["name"], tool.get("description") or ""),
                    "schema_tokens": schema_tokens(tool),
                    "calls": row.get("total", 0),
                    "errors": row.get("errors", 0),
                    "avg_ms": row.get("avg_ms"),
                }
            )
        names = {t["name"] for t in tools}
        tools.extend(
            {
                "name": name,
                "description": "",
                "active": False,
                "kind": tool_kind(name),
                "schema_tokens": 0,
                "calls": row["total"],
                "errors": row["errors"],
                "avg_ms": row.get("avg_ms"),
            }
            for name, row in used.items()
            if name not in names
        )
        tools.sort(key=lambda t: (-t["calls"], t["name"]))
        calls = sum(t["calls"] for t in tools)
        chars = sum(r.get("result_chars") or 0 for r in used.values())
        timed = [(t["avg_ms"], t["calls"]) for t in tools if t["avg_ms"] is not None]
        out.append(
            {
                "name": server["name"],
                "state": server["state"],
                "transport": server.get("transport"),
                "errors_log": server.get("errors") or [],
                "tools": tools,
                "calls": calls,
                "errors": sum(t["errors"] for t in tools),
                "avg_ms": (
                    int(sum(ms * n for ms, n in timed) / sum(n for _, n in timed))
                    if timed and sum(n for _, n in timed)
                    else None
                ),
                "result_tokens": int(chars * RESULT_TOKENS_PER_CHAR),
                "schema_tokens": sum(t["schema_tokens"] for t in tools if t["active"]),
                "unused": sum(1 for t in tools if t["active"] and not t["calls"]),
            }
        )
    out.sort(key=lambda s: (-s["calls"], s["name"]))
    return out


def assess(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """MCP 能不能用：服务连没连上，调用是不是大多失败。"""
    active = [s for s in servers if s["state"] not in ("disabled", "removed")]
    down = [s["name"] for s in active if s["state"] != "connected"]
    failing = [
        {"code": "failing", "server": s["name"], "n": s["errors"]}
        for s in active
        if s["calls"] >= MIN_CALLS_TO_JUDGE and s["errors"] * 2 >= s["calls"]
    ]
    reasons = [{"code": "down", "servers": down}] if down else []
    reasons += failing
    if not active:
        verdict = "off"
    elif len(down) == len(active):
        verdict = "bad"
    elif reasons:
        verdict = "warn"
    else:
        verdict = "ok"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "connected": len(active) - len(down),
        "total": len(active),
        "calls": sum(s["calls"] for s in active),
    }
