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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .log import logger

SCHEMA_VERSION = 1
MAX_ARGS_CHARS = 4000
MAX_RESULT_CHARS = 8000
# 每写入这么多条清理一次过期记录
PRUNE_EVERY = 200

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
    content_recorded INTEGER NOT NULL DEFAULT 0
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
)


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
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE calls SET status=?, duration_ms=?, result=?, result_chars=?, error=?, "
                "note=COALESCE(?, note), request_id=COALESCE(?, request_id), "
                "http_status=COALESCE(?, http_status), detail=COALESCE(?, detail) WHERE id=?",
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
                    row_id,
                ),
            )
            self._conn.commit()

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
                    "DELETE FROM calls WHERE ts < ?", (cutoff,)
                ).rowcount
            if self.max_rows > 0:
                removed += self._conn.execute(
                    "DELETE FROM calls WHERE id <= (SELECT id FROM calls ORDER BY id DESC LIMIT 1 OFFSET ?)",
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
        q = (filters.get("q") or "").strip()
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            clauses.append(
                "(tool LIKE ? ESCAPE '\\' OR args LIKE ? ESCAPE '\\' OR result LIKE ? ESCAPE '\\' "
                "OR error LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR request_id LIKE ? ESCAPE '\\' "
                "OR detail LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 7)
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
                "http_status, result_chars, error, note, content_recorded, "
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
        for key in ("args", "detail"):
            if item.get(key):
                with contextlib.suppress(ValueError):
                    item[key] = json.loads(item[key])
        item["content_recorded"] = bool(item.get("content_recorded"))
        return item
