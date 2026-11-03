"""持久化的工具调用日志（SQLite），记录所有工具的调用，不只是 DeepSeek。

- DeepSeek 工具：记录请求参数和完整详情（遵循“记录搜索词”开关）
- 其他工具：默认只记录工具名、来源、耗时和状态；开启“记录其他工具的完整内容”后才保存参数和结果
- 按保留天数和最大条数自动清理

所有方法都是同步的，内部加锁；在异步代码里请用 asyncio.to_thread 调用。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from .log import logger
from .providers import KB_TOOL

SCHEMA_VERSION = 4
MAX_ARGS_CHARS = 4000
MAX_RESULT_CHARS = 8000
# 每写入这么多条清理一次过期记录
PRUNE_EVERY = 200
# 同一会话里，下一次搜索在这么多秒内开始，就算“连续搜索”
CONSECUTIVE_WINDOW = 180.0
# 上一次结束后这么多秒内就开始：模型在同一条回复里一起发出的，没有多花一轮 LLM。
# 实测同一条回复里的间隔是几十毫秒，隔了一轮 LLM 的至少 1.6 秒
SAME_ROUND_GAP = 0.5

CATEGORIES = ("search", "kb", "mcp", "plugin", "builtin")
# 分类互斥，前面的优先：知识库 > MCP > 搜索 > 插件 > 内置
_IS_KB = "(kb IS NOT NULL OR tool = ?)"
_IS_MCP = "source LIKE 'mcp:%'"
_IS_PLUGIN = "source LIKE 'plugin:%'"
# 不是 AstrBot 知识库自己的检索
_IS_COMMUNITY = "(source LIKE 'plugin:%' OR source LIKE 'mcp:%')"

STATUSES = ("running", "ok", "error", "blocked", "empty", "cache_hit", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    duration_ms INTEGER,
    tool TEXT NOT NULL,
    source TEXT NOT NULL,
    session TEXT,
    status TEXT NOT NULL,
    request_id TEXT,
    http_status INTEGER,
    args TEXT,
    result TEXT,
    result_chars INTEGER,
    error TEXT,
    note TEXT,
    detail TEXT,
    content_recorded INTEGER NOT NULL DEFAULT 0,
    quality TEXT,
    relevance REAL,
    top_hit TEXT,
    kb TEXT,
    kb_top_score REAL,
    kb_count INTEGER,
    kb_degraded INTEGER,
    feedback TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_tool ON calls(tool);
CREATE INDEX IF NOT EXISTS idx_calls_request_id ON calls(request_id);
"""

_COLUMNS = (
    "id",
    "ts",
    "duration_ms",
    "tool",
    "source",
    "session",
    "status",
    "request_id",
    "http_status",
    "args",
    "result",
    "result_chars",
    "error",
    "note",
    "detail",
    "content_recorded",
    "quality",
    "relevance",
    "top_hit",
    "kb",
    "kb_top_score",
    "kb_count",
    "kb_degraded",
    "feedback",
)

# 版本 1 的数据库没有这些列，打开时补上
_MIGRATIONS = (
    ("quality", "ALTER TABLE calls ADD COLUMN quality TEXT"),
    ("relevance", "ALTER TABLE calls ADD COLUMN relevance REAL"),
    ("top_hit", "ALTER TABLE calls ADD COLUMN top_hit TEXT"),
    ("kb", "ALTER TABLE calls ADD COLUMN kb TEXT"),
    ("kb_top_score", "ALTER TABLE calls ADD COLUMN kb_top_score REAL"),
    ("kb_count", "ALTER TABLE calls ADD COLUMN kb_count INTEGER"),
    ("kb_degraded", "ALTER TABLE calls ADD COLUMN kb_degraded INTEGER"),
    ("feedback", "ALTER TABLE calls ADD COLUMN feedback TEXT"),
)

# 一次调用在健康统计里算哪一类（running / blocked / cache_hit 不算）
_HEALTH_SQL = """
SELECT tool, source,
  COUNT(*) AS total,
  SUM(status IN ('error', 'interrupted') OR COALESCE(quality, '') = 'error') AS error,
  SUM(status NOT IN ('error', 'interrupted') AND COALESCE(quality, '') != 'error'
      AND (status = 'empty' OR COALESCE(quality, '') = 'empty')) AS empty,
  SUM(COALESCE(quality, '') = 'irrelevant'
      AND status NOT IN ('error', 'interrupted', 'empty')) AS irrelevant,
  AVG(relevance) AS avg_relevance,
  MAX(CASE WHEN status = 'ok' AND quality IS NULL THEN ts END) AS last_ok_ts,
  MAX(ts) AS last_ts
FROM calls
WHERE status NOT IN ('running', 'blocked', 'cache_hit') {extra}
GROUP BY tool, source
"""


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return (
        text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字，已截断）"
    )


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _has_usage(kb: dict) -> bool:
    """这条检索有没有记下嵌入或重排的 token。升级前的旧记录只有调用次数。"""
    extra = kb.get("billing") or {}
    return any(
        extra.get(f"{kind}_by") or extra.get(f"{kind}_tokens")
        for kind in ("embedding", "rerank")
    )


def _add_billing(billing: dict[str, Any], kb: dict) -> None:
    """一条检索记录用了多少嵌入和重排 token。"""
    extra = kb.get("billing") or {}
    for kind in ("embedding", "rerank"):
        by_provider = extra.get(f"{kind}_by")
        if by_provider:
            for provider, tokens in by_provider.items():
                billing[kind][provider] = billing[kind].get(provider, 0) + tokens
            continue
        provider = extra.get(f"{kind}_provider")
        if provider and extra.get(f"{kind}_tokens"):
            billing[kind][provider] = (
                billing[kind].get(provider, 0) + extra[f"{kind}_tokens"]
            )


def _add_plugin(
    by_plugin: dict[str, dict[str, Any]],
    source: str | None,
    status: str,
    used: dict[str, Any],
    duration: int | None,
) -> None:
    name = (source or "").removeprefix("plugin:") or "?"
    row = by_plugin.setdefault(
        name, {"total": 0, "empty": 0, "error": 0, "tokens": 0, "ms": 0}
    )
    row["total"] += 1
    row["empty"] += status == "empty"
    row["error"] += status in ("error", "interrupted")
    row["tokens"] += used.get("result") or 0
    row["ms"] += duration or 0


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


class CallLog:
    def __init__(
        self,
        path: Path | str | None,
        *,
        retention_days: float = 14,
        max_rows: int = 50000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``path`` 为 None 时使用内存数据库（测试或数据目录不可用时）。"""
        self.retention_days = retention_days
        self.max_rows = max_rows
        self._clock = clock
        self._lock = threading.Lock()
        self._writes = 0
        target = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(calls)")}
            for column, sql in _MIGRATIONS:
                if column not in columns:
                    self._conn.execute(sql)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            # 上次运行时没收尾的记录（进程退出、重启）标记为中断
            self._conn.execute(
                "UPDATE calls SET status='interrupted', note=COALESCE(note, 'AstrBot 重启前未收到结果') "
                "WHERE status='running'"
            )
            self._conn.commit()
        self.prune()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 写入 ----------

    def start(
        self,
        *,
        tool: str,
        source: str,
        session: str | None = None,
        args: Any = None,
        record_content: bool = False,
        ts: float | None = None,
    ) -> int:
        """记录一次调用开始，返回记录 ID。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO calls (ts, tool, source, session, status, args, content_recorded) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (
                    ts if ts is not None else self._clock(),
                    tool,
                    source,
                    session,
                    _clip(_dumps(args), MAX_ARGS_CHARS) if record_content else None,
                    int(record_content),
                ),
            )
            self._conn.commit()
            row_id = int(cur.lastrowid)
        self._after_write()
        return row_id

    def finish(
        self,
        row_id: int,
        *,
        status: str,
        duration_ms: int | None = None,
        result: str | None = None,
        record_content: bool = False,
        error: str | None = None,
        note: str | None = None,
        request_id: str | None = None,
        http_status: int | None = None,
        detail: dict | None = None,
        quality: str | None = None,
        relevance: float | None = None,
        top_hit: str | None = None,
        kb: dict | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE calls SET status=?, duration_ms=?, result=?, result_chars=?, error=?, "
                "note=COALESCE(?, note), request_id=COALESCE(?, request_id), "
                "http_status=COALESCE(?, http_status), detail=COALESCE(?, detail), "
                "quality=?, relevance=?, top_hit=?, kb=?, kb_top_score=?, kb_count=?, kb_degraded=? WHERE id=?",
                (
                    status,
                    duration_ms,
                    _clip(result, MAX_RESULT_CHARS)
                    if record_content and result
                    else None,
                    len(result) if result is not None else None,
                    _clip(error, 2000),
                    note,
                    request_id,
                    http_status,
                    _dumps(detail),
                    quality,
                    relevance,
                    top_hit,
                    _dumps(kb),
                    (kb or {}).get("top_score"),
                    (kb or {}).get("count"),
                    int(bool((kb or {}).get("degraded"))),
                    row_id,
                ),
            )
            self._conn.commit()

    def relabel_kb(
        self, row_id: int, *, tool: str, session: str | None, kb: dict
    ) -> None:
        """确认了一次知识库检索的发起方式后，改工具名、会话和方式。"""
        with self._lock:
            self._conn.execute(
                "UPDATE calls SET tool=?, session=?, kb=? WHERE id=?",
                (tool, session, _dumps(kb), row_id),
            )
            self._conn.commit()

    def update_kb_tokens(self, row_id: int, **tokens: Any) -> None:
        """检索结束后才知道的模型用量，补进这条记录。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT kb FROM calls WHERE id=?", (row_id,)
            ).fetchone()
            if row is None or not row[0]:
                return
            kb = json.loads(row[0])
            kb.setdefault("tokens", {}).update(tokens)
            self._conn.execute("UPDATE calls SET kb=? WHERE id=?", (_dumps(kb), row_id))
            self._conn.commit()

    def set_feedback(self, row_id: int, feedback: str | None) -> bool:
        """人工标记这次知识库检索有没有用，None 表示取消。只能标记知识库的记录。"""
        with self._lock:
            changed = self._conn.execute(
                "UPDATE calls SET feedback=? WHERE id=? AND kb IS NOT NULL",
                (feedback, row_id),
            ).rowcount
            self._conn.commit()
        return bool(changed)

    def add(self, *, tool: str, source: str, status: str, **fields: Any) -> int:
        """一次性写入一条已完成的记录（例如连接测试）。"""
        row_id = self.start(
            tool=tool,
            source=source,
            session=fields.pop("session", None),
            args=fields.pop("args", None),
            record_content=fields.get("record_content", False),
        )
        self.finish(row_id, status=status, **fields)
        return row_id

    def prune(self) -> int:
        """删除过期和超出条数上限的记录，返回删除条数。"""
        removed = 0
        with self._lock:
            if self.retention_days > 0:
                cutoff = self._clock() - self.retention_days * 86400
                removed += self._conn.execute(
                    # 人工标记过的记录留着，用来对比分数，只有清空日志时才删
                    "DELETE FROM calls WHERE ts < ? AND feedback IS NULL",
                    (cutoff,),
                ).rowcount
            if self.max_rows > 0:
                removed += self._conn.execute(
                    "DELETE FROM calls WHERE feedback IS NULL AND "
                    "id <= (SELECT id FROM calls ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (self.max_rows,),
                ).rowcount
            self._conn.commit()
        if removed:
            logger.debug(f"[deepdeep] 调用日志清理了 {removed} 条过期记录")
        return removed

    def clear(self) -> int:
        with self._lock:
            n = self._conn.execute("DELETE FROM calls").rowcount
            self._conn.commit()
            self._conn.execute("VACUUM")
        return n

    def _after_write(self) -> None:
        self._writes += 1
        if self._writes % PRUNE_EVERY == 0:
            self.prune()

    # ---------- 查询 ----------

    @staticmethod
    def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key in ("tool", "source", "status", "session", "request_id"):
            value = filters.get(key)
            if value:
                clauses.append(f"{key} = ?")
                params.append(value)
        if filters.get("since"):
            clauses.append("ts >= ?")
            params.append(float(filters["since"]))
        if filters.get("until"):
            clauses.append("ts <= ?")
            params.append(float(filters["until"]))
        if filters.get("problems_only"):
            clauses.append("status IN ('error', 'blocked', 'interrupted')")
        category = filters.get("category")
        if category in CATEGORIES:
            names = list(filters.get("search_tools") or [])
            is_search = f"tool IN ({', '.join('?' * len(names))})" if names else "0"
            if category == "kb":
                clauses.append(_IS_KB)
                params.append(KB_TOOL)
            else:
                clauses.append(f"NOT {_IS_KB}")
                params.append(KB_TOOL)
                if category == "mcp":
                    clauses.append(_IS_MCP)
                else:
                    clauses.append(f"NOT {_IS_MCP}")
                    if category == "search":
                        clauses.append(is_search)
                        params.extend(names)
                    else:
                        clauses.append(f"NOT {is_search}")
                        params.extend(names)
                        clauses.append(
                            _IS_PLUGIN if category == "plugin" else f"NOT {_IS_PLUGIN}"
                        )
        q = (filters.get("q") or "").strip()
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            clauses.append(
                "(tool LIKE ? ESCAPE '\\' OR args LIKE ? ESCAPE '\\' OR result LIKE ? ESCAPE '\\' "
                "OR error LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR request_id LIKE ? ESCAPE '\\' "
                "OR detail LIKE ? ESCAPE '\\' OR top_hit LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 8)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def query(
        self, filters: dict[str, Any] | None = None, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict], int]:
        """返回 (记录列表, 总数)。列表里不含大字段（args/result/detail），点开详情再取。"""
        where, params = self._where(filters or {})
        limit = max(1, min(500, limit))
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM calls{where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                "SELECT id, ts, duration_ms, tool, source, session, status, request_id, "
                "http_status, result_chars, error, note, top_hit, content_recorded, "
                "kb_top_score, kb_count, kb_degraded, feedback, "
                "args IS NOT NULL AS has_args, detail IS NOT NULL AS has_detail "
                f"FROM calls{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, max(0, offset)],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def get(self, row_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM calls WHERE id = ?", (row_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def iter_all(self, filters: dict[str, Any] | None = None) -> Iterator[dict]:
        """导出用，按时间升序。"""
        where, params = self._where(filters or {})
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM calls{where} ORDER BY id ASC",
                params,
            ).fetchall()
        for row in rows:
            yield self._decode(row)

    def category_counts(
        self, since: float | None, search_tools: Iterable[str]
    ) -> dict[str, int]:
        """调用日志顶部的分类和每类的次数。"""
        names = list(search_tools)
        base = {"since": since} if since else {}
        wheres = {
            category or "all": self._where(
                {**base, "category": category, "search_tools": names}
            )
            for category in (None, *CATEGORIES)
        }
        # 一次查完，中间不让写入，各类加起来才等于“全部”
        with self._lock:
            return {
                key: self._conn.execute(
                    f"SELECT COUNT(*) FROM calls{where}", params
                ).fetchone()[0]
                for key, (where, params) in wheres.items()
            }

    def rename_tools(self, names: dict[str, str]) -> int:
        """工具改名后，旧记录也换成新名字。{旧名: 新名}。"""
        changed = 0
        with self._lock:
            for old, new in names.items():
                changed += self._conn.execute(
                    "UPDATE calls SET tool=? WHERE tool=?", (new, old)
                ).rowcount
            self._conn.commit()
        return changed

    def fix_mcp_sources(self, tools: dict[str, str]) -> int:
        """旧版本把 MCP 工具记成了内置工具，按工具名改回来。{工具名: 服务名}。"""
        changed = 0
        with self._lock:
            for tool, server in tools.items():
                changed += self._conn.execute(
                    "UPDATE calls SET source=? WHERE tool=? AND source='builtin'",
                    (f"mcp:{server}", tool),
                ).rowcount
            self._conn.commit()
        return changed

    def mcp_stats(self, since: float | None = None) -> list[dict]:
        """按服务和工具汇总 MCP 调用。"""
        where, params = ("AND ts >= ?", [since]) if since else ("", [])
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, tool, COUNT(*) AS total, "
                "SUM(status IN ('error', 'interrupted')) AS errors, "
                "AVG(duration_ms) AS avg_ms, SUM(result_chars) AS result_chars, "
                "MAX(ts) AS last_ts "
                f"FROM calls WHERE source LIKE 'mcp:%' AND status != 'running' {where} "
                "GROUP BY source, tool",
                params,
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["errors"] = int(item["errors"] or 0)
            item["avg_ms"] = int(item["avg_ms"]) if item["avg_ms"] is not None else None
            out.append(item)
        return out

    def facets(self) -> dict[str, list[str]]:
        """筛选下拉框用：出现过的工具名和来源。"""
        with self._lock:
            tools = [
                r[0]
                for r in self._conn.execute(
                    "SELECT tool FROM calls GROUP BY tool ORDER BY COUNT(*) DESC LIMIT 200"
                )
            ]
            sources = [
                r[0]
                for r in self._conn.execute(
                    "SELECT source FROM calls GROUP BY source ORDER BY COUNT(*) DESC LIMIT 100"
                )
            ]
        return {"tools": tools, "sources": sources}

    def tool_stats(self, since: float | None = None) -> list[dict]:
        """按工具汇总调用次数、结果分布和平均耗时，给“各搜索工具用量”使用。"""
        where, params = ("WHERE ts >= ?", [since]) if since else ("", [])
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool, source, COUNT(*) AS total, "
                "SUM(status = 'ok') AS ok, SUM(status = 'empty') AS empty, "
                "SUM(status = 'cache_hit') AS cache_hit, "
                "SUM(status IN ('error', 'interrupted')) AS error, "
                "SUM(status = 'blocked') AS blocked, "
                "AVG(CASE WHEN status IN ('ok', 'empty', 'error') THEN duration_ms END) AS avg_ms, "
                "MAX(ts) AS last_ts "
                f"FROM calls {where} GROUP BY tool, source ORDER BY total DESC",
                params,
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("ok", "empty", "cache_hit", "error", "blocked"):
                item[key] = int(item[key] or 0)
            item["avg_ms"] = int(item["avg_ms"]) if item["avg_ms"] is not None else None
            out.append(item)
        return out

    def daily_tool_counts(self, since: float) -> list[dict]:
        """按本地日期、按工具统计调用次数（不算被拦下的和还在进行的），给用量页的图表用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, tool, COUNT(*) AS n "
                "FROM calls WHERE ts >= ? "
                "AND status IN ('ok', 'empty', 'cache_hit', 'error', 'interrupted') "
                "GROUP BY day, tool",
                [since],
            ).fetchall()
        return [dict(r) for r in rows]

    def tool_health(
        self, since: float | None = None, streak_window: int = 5
    ) -> list[dict]:
        """按工具汇总失败、空结果、不相关的次数，以及最近连续出问题的次数。"""
        extra, params = ("AND ts >= ?", [since]) if since else ("", [])
        with self._lock:
            rows = self._conn.execute(
                _HEALTH_SQL.format(extra=extra), params
            ).fetchall()
            recent = self._conn.execute(
                "SELECT tool, status, quality FROM calls "
                f"WHERE status NOT IN ('running', 'blocked', 'cache_hit') {extra} "
                "ORDER BY id DESC",
                params,
            ).fetchall()
        streaks: dict[str, int] = {}
        done: set[str] = set()
        seen: dict[str, int] = {}
        for tool, status, quality in recent:
            if tool in done:
                continue
            seen[tool] = seen.get(tool, 0) + 1
            bad = status in ("error", "interrupted", "empty") or quality is not None
            if bad:
                streaks[tool] = streaks.get(tool, 0) + 1
            if not bad or seen[tool] >= streak_window:
                done.add(tool)
        out = []
        for r in rows:
            item = dict(r)
            for key in ("total", "error", "empty", "irrelevant"):
                item[key] = int(item[key] or 0)
            if item["avg_relevance"] is not None:
                item["avg_relevance"] = round(float(item["avg_relevance"]), 2)
            item["bad_streak"] = streaks.get(item["tool"], 0)
            out.append(item)
        return out

    def consecutive_searches(
        self,
        tools: Iterable[str],
        since: float | None = None,
        window: float = CONSECUTIVE_WINDOW,
    ) -> dict[str, Any]:
        """连续搜索率：搜索之后，同一会话在 ``window`` 秒内又发起搜索的比例。

        ``tools`` 是算作搜索的工具名。``same_round`` 是其中由模型在同一条回复里
        一起发出的次数，这些不多花 LLM 调用。没有会话的记录（如连接测试）不算。
        """
        names = sorted(set(tools))
        rows = []
        if names:
            marks = ", ".join("?" * len(names))
            extra, params = ("AND ts >= ?", [since]) if since else ("", [])
            with self._lock:
                rows = self._conn.execute(
                    "SELECT session, ts, duration_ms FROM calls "
                    f"WHERE session IS NOT NULL AND session != '' AND tool IN ({marks}) "
                    f"{extra} ORDER BY ts, id",
                    [*names, *params],
                ).fetchall()
        followed = same_round = 0
        last: dict[str, tuple[float, int | None]] = {}
        for session, ts, duration_ms in rows:
            prev = last.get(session)
            if prev and ts - prev[0] <= window:
                followed += 1
                if (
                    prev[1] is not None
                    and ts - prev[0] - prev[1] / 1000 < SAME_ROUND_GAP
                ):
                    same_round += 1
            last[session] = (ts, duration_ms)
        return {
            "searches": len(rows),
            "followed": followed,
            "same_round": same_round,
            "rate": round(followed / len(rows), 3) if rows else None,
        }

    def kb_stats(self, since: float | None = None) -> dict[str, Any]:
        """知识库检索的汇总：次数、用量、分数、人工标记，以及各库和各文档的命中情况。"""
        where, params = ("AND ts >= ?", [since]) if since else ("", [])
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, result_chars, kb, kb_top_score, feedback, duration_ms, "
                "source FROM calls "
                f"WHERE kb IS NOT NULL AND status != 'running' {where}",
                params,
            ).fetchall()
        out: dict[str, Any] = {
            "total": len(rows),
            "unscored": 0,
            "empty": 0,
            "error": 0,
            "multi_kb": 0,
            "embedding_calls": 0,
            "rerank_calls": 0,
            # 调用过嵌入或重排、却没有 token 记录的检索：花费算不出来，不能当成 0
            "unbilled": 0,
            "degraded": 0,
            "result_chars": 0,
            "avg_top_score": None,
        }
        tokens = {
            "result": 0,
            "next_input": 0,
            "next_rounds": 0,
            "final_other": 0,
            "final_cached": 0,
            "final_output": 0,
            "final_rounds": 0,
            "saved": 0,
        }
        modes: dict[str, int] = defaultdict(int)
        tops: list[float] = []
        marked: dict[str, list[float]] = {"good": [], "bad": []}
        counts: dict[str, int] = {"good": 0, "bad": 0}
        by_kb: dict[str, dict[str, Any]] = {}
        by_doc: dict[tuple[str, str], int] = defaultdict(int)
        billing: dict[str, Any] = {"embedding": {}, "rerank": {}}
        durations: list[int] = []
        by_plugin: dict[str, dict[str, Any]] = {}
        seen_next: set[int] = set()
        seen_final: set[int] = set()

        def first_of_round(round_id: Any, seen: set[int]) -> bool:
            """同一轮模型回复的用量写在这一轮每条检索上，只算一次。旧记录没有轮次，各算各的。"""
            if round_id is None:
                return True
            if round_id in seen:
                return False
            seen.add(round_id)
            return True

        for status, chars, raw, top_score, feedback, duration, source in rows:
            try:
                kb = json.loads(raw)
            except ValueError:
                continue
            modes[kb.get("mode") or "unscored"] += 1
            out["result_chars"] += chars or 0
            scored = kb.get("mode") not in ("unscored", "plugin_inject")
            # 插件的记录只进下面的 by_plugin，不算进“查得到吗”
            out["empty"] += scored and status == "empty"
            out["error"] += scored and status in ("error", "interrupted")
            if feedback in counts:
                counts[feedback] += 1
            used = kb.get("tokens") or {}
            # 平均耗时和内容量只看带分数的检索：精简设置和“库越多越慢”的建议都管不到插件
            if scored:
                tokens["result"] += used.get("result") or 0
                if duration is not None:
                    durations.append(duration)
            tokens["saved"] += used.get("saved") or 0
            _add_billing(billing, kb)
            if used.get("next_input") and first_of_round(used.get("round"), seen_next):
                tokens["next_input"] += used["next_input"]
                tokens["next_rounds"] += 1
            final = used.get("final")
            if final and first_of_round(used.get("round"), seen_final):
                tokens["final_other"] += final.get("input_other") or 0
                tokens["final_cached"] += final.get("input_cached") or 0
                tokens["final_output"] += final.get("output") or 0
                tokens["final_rounds"] += 1
            out["embedding_calls"] += kb.get("embedding_calls") or 0
            out["rerank_calls"] += kb.get("rerank_calls") or 0
            if (kb.get("embedding_calls") or kb.get("rerank_calls")) and not _has_usage(
                kb
            ):
                out["unbilled"] += 1
            if not scored:
                out["unscored"] += 1
                _add_plugin(by_plugin, source, status, used, duration)
                continue
            out["degraded"] += bool(kb.get("degraded"))
            if top_score is not None:
                tops.append(top_score)
                if feedback in marked:
                    marked[feedback].append(top_score)
            hits = kb.get("hits") or []
            if len({h.get("kb") for h in hits if h.get("kb")}) > 1:
                out["multi_kb"] += 1
            for i, hit in enumerate(hits):
                name = hit.get("kb")
                if not name:
                    continue
                row = by_kb.setdefault(
                    name, {"kb": name, "hits": 0, "first": 0, "score_sum": 0.0}
                )
                row["hits"] += 1
                row["first"] += i == 0
                row["score_sum"] += hit.get("score") or 0
                by_doc[(name, hit.get("doc") or "?")] += 1
        out["avg_top_score"] = _average(tops)
        out["by_plugin"] = [
            {
                "plugin": name,
                "total": row["total"],
                "empty": row["empty"],
                "error": row["error"],
                # 只平均放了内容的几次，没放内容的已经单独列出
                "avg_tokens": round(
                    row["tokens"] / max(row["total"] - row["empty"] - row["error"], 1)
                ),
                "avg_ms": round(row["ms"] / row["total"]),
            }
            for name, row in sorted(
                by_plugin.items(), key=lambda item: -item[1]["total"]
            )
        ]
        out["modes"] = dict(modes)
        out["tokens"] = tokens
        out["billing"] = billing
        out["avg_ms"] = int(sum(durations) / len(durations)) if durations else None
        out["feedback"] = {
            key: {"count": counts[key], "avg_top_score": _average(marked[key])}
            for key in counts
        }
        out["by_kb"] = sorted(
            (
                {
                    "kb": r["kb"],
                    "hits": r["hits"],
                    "first": r["first"],
                    "avg_score": round(r["score_sum"] / r["hits"], 3),
                }
                for r in by_kb.values()
            ),
            key=lambda r: -r["hits"],
        )
        out["by_doc"] = [
            {"kb": kb, "doc": doc, "hits": n}
            for (kb, doc), n in sorted(by_doc.items(), key=lambda x: -x[1])
        ]
        return out

    def kb_health(self, since: float) -> dict[str, Any]:
        """概览用：这段时间知识库检索的次数、失败和降级。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*), SUM(status = 'error' AND NOT {_IS_COMMUNITY}), "
                f"SUM(kb_degraded), MAX(ts), SUM(status = 'error' AND {_IS_COMMUNITY}), "
                f"SUM(NOT {_IS_COMMUNITY}) "
                "FROM calls WHERE kb IS NOT NULL AND status != 'running' AND ts >= ?",
                (since,),
            ).fetchone()
        return {
            "total": int(row[0] or 0),
            "error": int(row[1] or 0),
            "degraded": int(row[2] or 0),
            "last_ts": row[3],
            "plugin_error": int(row[4] or 0),
            "builtin_total": int(row[5] or 0),
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), MIN(ts), SUM(status IN ('error','blocked','interrupted')) FROM calls"
            ).fetchone()
        return {
            "total": int(row[0] or 0),
            "oldest_ts": row[1],
            "problems": int(row[2] or 0),
            "retention_days": self.retention_days,
            "max_rows": self.max_rows,
        }

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("args", "detail", "kb"):
            if item.get(key):
                with contextlib.suppress(ValueError):
                    item[key] = json.loads(item[key])
        item["content_recorded"] = bool(item.get("content_recorded"))
        return item
