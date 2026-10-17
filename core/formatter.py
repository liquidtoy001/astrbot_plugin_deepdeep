"""把 API 结果整理成给模型读的紧凑文本。"""

from __future__ import annotations

import re

from .client import FetchResult, SearchResult

# 全部结果都低于阈值时，仍保留最靠前的几条，免得模型以为“什么都没搜到”
FALLBACK_KEEP = 2

# 查询里已经用 after:/before: 限定了时间。DeepSeek 的相关度分数偏向讨论多的
# 老内容，刚发布的内容分数往往很低（实测 0.4–0.8），这时不再按分数过滤
DATE_FILTER_RE = re.compile(r"(?:^|\s)(?:after|before):\S+", re.IGNORECASE)
# 问“最新/最近”但没限定时间：结果按相关度而不是时间排序，需要提醒模型
RECENCY_RE = re.compile(
    r"最新|最近|近期|新视频|新动态|今天|今日|昨天|本周|这周|刚刚|latest|recent|newest|today",
    re.IGNORECASE,
)


def has_date_filter(query: str) -> bool:
    return bool(DATE_FILTER_RE.search(query))


def wants_recent(query: str) -> bool:
    return bool(RECENCY_RE.search(query)) and not has_date_filter(query)


def filter_results(
    results: list[SearchResult], min_score: float
) -> tuple[list[SearchResult], bool]:
    """按相关度过滤。返回 (保留的结果, 是否只剩低相关结果)。"""
    kept = [r for r in results if r.rerank_score >= min_score]
    if kept or not results:
        return kept, False
    return results[:FALLBACK_KEEP], True


def _format_one(
    index: int, r: SearchResult, include_content: bool, content_chars: int
) -> str:
    meta = []
    if r.site_name:
        meta.append(f"来源: {r.site_name}")
    if r.date:
        meta.append(f"日期: {r.date[:10]}")
    meta.append(f"相关度: {r.rerank_score:.1f}")
    if r.authority >= 0:
        meta.append(f"权威度: {r.authority}/4")

    lines = [f"[{index}] {r.title or '(无标题)'}", " | ".join(meta), f"链接: {r.url}"]
    summary = r.summary or r.snippet
    if summary:
        lines.append(f"摘要: {summary.strip()}")
    if include_content and r.content and content_chars > 0:
        content = r.content.strip()
        if len(content) > content_chars:
            content = content[:content_chars] + "…"
        lines.append(f"正文节选: {content}")
    return "\n".join(lines)


def format_search_results(
    query: str,
    results: list[SearchResult],
    *,
    min_score: float = 0.5,
    max_chars: int = 6000,
    include_content: bool = False,
    content_chars: int = 800,
) -> str:
    if not results:
        return f"没有找到与「{query}」相关的网页。可以换个说法或去掉限定条件再搜一次。"

    recent = wants_recent(query)
    # 限定了时间或问的是“最新”时不按分数过滤：新内容分数偏低，过滤会把正确答案滤掉
    if has_date_filter(query) or recent:
        kept, low_quality = list(results), False
    else:
        kept, low_quality = filter_results(results, min_score)
    header = f"「{query}」的搜索结果（共 {len(kept)} 条，按相关度排序）："
    if recent:
        newest = max((r.date for r in kept if r.date), default=None)
        header += (
            "\n注意：结果按相关度而不是发布时间排序，讨论多的旧内容可能排在前面"
            + (f"（本次最新一条发布于 {newest[:10]}）" if newest else "")
            + "。要找最新内容，请用 after:YYYY-MM-DD 限定近几天后再搜一次，并以日期为准。"
        )
    if low_quality:
        header += "\n注意：以下结果相关度都较低，可能不能直接回答问题，请谨慎引用或换个关键词再搜。"

    blocks: list[str] = []
    used = len(header)
    for i, r in enumerate(kept, start=1):
        block = _format_one(i, r, include_content, content_chars)
        # 第一条无论多长都要保留（截断），之后超出总长度就停
        if blocks and used + len(block) + 2 > max_chars:
            blocks.append(f"（其余 {len(kept) - len(blocks)} 条结果因长度限制已省略）")
            break
        if not blocks and used + len(block) + 2 > max_chars:
            block = block[: max(0, max_chars - used - 3)] + "…"
        blocks.append(block)
        used += len(block) + 2

    footer = (
        "回答时请注明信息来源链接；如需某个网页的完整内容，可调用 deepdeep_web_fetch。"
    )
    return "\n\n".join([header, *blocks, footer])


def format_fetch_result(result: FetchResult, *, max_chars: int = 8000) -> str:
    content = result.content.strip()
    if not content:
        return f"网页 {result.url} 没有可提取的正文内容。"
    title = result.title.strip() or "(无标题)"
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    parts = [f"标题: {title}", f"链接: {result.url}", "", content]
    if truncated:
        parts.append(f"\n[内容过长已截断，仅显示前 {max_chars} 字]")
    return "\n".join(parts)
