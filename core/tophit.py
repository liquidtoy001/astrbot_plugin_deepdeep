"""从搜索工具返回的内容里取第一条结果的简述，显示在调用日志的“说明”列。

各家返回的格式不一样：DeepDeep 自己的工具是“[1] 标题 / 摘要: …”，AstrBot 内置工具是 JSON，
插件各写各的。认不出来时返回 None，不猜。
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_CHARS = 160

_LIST_FIELDS = (
    "results",
    "organic_results",
    "organic",
    "news_results",
    "news",
    "items",
    "articles",
    "papers",
    "pages",
    "data",
    "web",
)
_TITLE_FIELDS = ("title", "name", "headline")
_SNIPPET_FIELDS = ("snippet", "summary", "description", "content", "text", "abstract")

_FIRST_ITEM_RE = re.compile(r"^\s*(?:\[1\]|1[.、)]|#\s*1\b|\*\*1[.、)]?\*?\*?)\s*(.+)$")
_TITLE_LINE_RE = re.compile(r"^\s*(?:标题|title)\s*[:：]\s*(.+)$", re.IGNORECASE)
_SNIPPET_LINE_RE = re.compile(
    r"^\s*(?:摘要|内容|正文节选|简介|snippet|summary|content|description)\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)
# 结果块里不算简述的行：链接、日期、相关度这些
_META_LINE_RE = re.compile(
    r"^\s*(?:https?://|(?:链接|网址|来源|日期|相关度|权威度|url|link|source|date|score)\s*[:：|])",
    re.IGNORECASE,
)
_STATUS_LINE_RE = re.compile(r"^\[[^\]]*#[0-9a-f]{6}\]")


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" *#_`")


def _join(title: str, snippet: str) -> str | None:
    # 有些摘要以标题开头
    if title and snippet.startswith(title):
        snippet = snippet[len(title) :].lstrip(" ：:-—")
    text = " — ".join(x for x in (title, snippet) if x)
    if not text:
        return None
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def _first_of(data: Any, depth: int = 0) -> dict | None:
    """JSON 里第一条结果：顶层的列表，或 results / organic_results 这类字段里的列表。"""
    if isinstance(data, list):
        return next((x for x in data if isinstance(x, dict)), None)
    if isinstance(data, dict) and depth < 3:
        for field in _LIST_FIELDS:
            found = _first_of(data[field], depth + 1) if field in data else None
            if found is not None:
                return found
    return None


def _pick(item: dict, fields: tuple[str, ...]) -> str:
    return next(
        (_clean(item[f]) for f in fields if isinstance(item.get(f), str) and item[f]),
        "",
    )


def _from_json(text: str) -> str | None:
    try:
        first = _first_of(json.loads(text))
    except ValueError:
        return None
    if first is None:
        return None
    return _join(_pick(first, _TITLE_FIELDS), _pick(first, _SNIPPET_FIELDS))


def _from_text(text: str) -> str | None:
    lines = text.splitlines()
    start = next((i for i, x in enumerate(lines) if _FIRST_ITEM_RE.match(x)), None)
    if start is None:
        # 抓取工具的“标题: …”
        for line in lines[:5]:
            title = _TITLE_LINE_RE.match(line)
            if title:
                return _join(_clean(title.group(1)), "")
        return None
    title = _clean(_FIRST_ITEM_RE.match(lines[start]).group(1))
    fallback = ""
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        snippet = _SNIPPET_LINE_RE.match(line)
        if snippet:
            return _join(title, _clean(snippet.group(1)))
        if not fallback and not _META_LINE_RE.match(line) and " | " not in line:
            fallback = _clean(line)
    return _join(title, fallback)


def extract(text: str | None) -> str | None:
    """第一条结果的“标题 — 摘要”，最多 MAX_CHARS 字；认不出来返回 None。"""
    if not text:
        return None
    body = text.strip()
    first_line, _, rest = body.partition("\n")
    if _STATUS_LINE_RE.match(first_line):
        body = rest.strip()
    if body[:1] in "[{":
        found = _from_json(body)
        if found:
            return found
    return _from_text(body)
