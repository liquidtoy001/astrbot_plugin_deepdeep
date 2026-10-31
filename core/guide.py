"""“搜索工具分工”提示的文字。用哪些工具由 ToolRouter 决定，这里只管怎么写。

这段文字会进系统提示，同样的输入必须逐字节不变（tests/test_golden_guide.py），
否则用户要多付一次提示词缓存未命中的钱。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .providers import FETCH_TOOL, SEARCH_TOOL

if TYPE_CHECKING:
    from .router import ToolRouter

# 搜索偏好里“技术问题”对应的分工
TECH_DESC = "编程、软件版本、安全漏洞、官方文档等技术问题"
# 有按类型分工时，通用搜索那一行改写成更具体的说明
GENERAL_DESC = "通用网页搜索（新闻、事实、中文资料等）"
GENERAL_DESC_WITH_LANES = "新闻时事、中文资料、海外新闻、比赛和最新动态"

# 分工提示里每类最多列出的工具数。像 arXiv MCP 这样一类有十来个工具时，
# 全部列出会让提示变长，模型看到前几个就知道该用哪一组了
GUIDE_MAX_PER_LINE = 3

_HEADER = "【搜索工具分工】先选最对口的工具，结果不理想时再换："
_WHEN_SEARCH = "没有结果或结果不理想时"
_WHEN_FETCH = "读取失败时"


def _short_list(names: list[str]) -> str:
    ranked = sorted(names, key=lambda n: ("search" not in n.lower(), n))
    shown = ranked[:GUIDE_MAX_PER_LINE]
    rest = len(ranked) - len(shown)
    text = "、".join(shown)
    return f"{text} 等 {len(ranked)} 个" if rest > 0 else text


def _ordered_line(items: list[str], when: str) -> str:
    """按偏好排好的一串工具：第一个是首选，其余是备用。"""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    rest = items[1:]
    shown = rest[:GUIDE_MAX_PER_LINE]
    more = f" 等 {len(rest)} 个" if len(rest) > len(shown) else ""
    return f"优先 {items[0]}；{when}再用 {'、'.join(shown)}{more}"


def _general_line(has_own: bool, own: str, alts: list[str], when: str) -> str:
    """通用搜索那一行：有 DeepDeep 时写清楚先用谁、什么情况再换。"""
    if has_own and alts:
        shown = alts[:GUIDE_MAX_PER_LINE]
        more = f" 等 {len(alts)} 个" if len(alts) > len(shown) else ""
        return f"优先 {own}；{when}再用 {'、'.join(shown)}{more}"
    if has_own:
        return own
    if not alts:
        return ""
    # 没有 DeepDeep 时按推荐顺序列出，第一个就是首选
    shown = alts[:GUIDE_MAX_PER_LINE]
    more = f" 等 {len(alts)} 个" if len(alts) > len(shown) else ""
    if len(shown) == 1:
        return shown[0]
    return f"优先 {shown[0]}；{when}再用 {'、'.join(shown[1:])}{more}"


def _render_with_preference(router: ToolRouter, present: set[str]) -> str | None:
    general = router.ordered("general", present)
    fetch = router.ordered("read", present)
    special = router.special_groups(present)
    if not special and len(general) <= 1 and len(fetch) <= 1:
        return None
    lanes = [
        (d, t)
        for d, t in router.lane_assignments(sorted(present))
        if not general or t != general[0]
    ]
    lines = [_HEADER]
    if general:
        desc = GENERAL_DESC_WITH_LANES if lanes else GENERAL_DESC
        lines.append(f"- {desc}：{_ordered_line(general, _WHEN_SEARCH)}")
    for desc, tool in lanes:
        backups = (
            [n for n in router.ordered("tech", present) if n != tool]
            if desc == TECH_DESC
            else [n for n in general[:1] if n != tool]
        )[: GUIDE_MAX_PER_LINE - 1]
        lines.append(
            f"- {desc}：优先 {tool}"
            + (f"；不理想时再用 {'、'.join(backups)}" if backups else "")
        )
    if fetch:
        lines.append(f"- 读取网页全文：{_ordered_line(fetch, _WHEN_FETCH)}")
    for desc, tools in special:
        lines.append(f"- {desc}：{_short_list(tools)}")
    return "\n".join(lines)


def render_guide(router: ToolRouter, names: list[str]) -> str | None:
    """根据本次请求里实际可用的工具，生成“搜索工具分工”提示。

    内容只取决于工具集和配置，工具集不变时提示词保持一致，不会破坏模型的提示词缓存。
    没有专用搜索工具时返回 None（只有通用搜索时不需要分工说明）。
    """
    present = set(names)
    if router.preference is not None:
        return _render_with_preference(router, present)
    alt_search = router.alternatives(sorted(present), "search")
    alt_fetch = router.alternatives(sorted(present), "fetch")
    special = router.special_groups(present)
    # 只有 DeepDeep 自己、没有备用和专用工具时不需要分工说明
    if not special and not alt_search and not alt_fetch:
        return None
    lines = [_HEADER]
    search_line = _general_line(
        SEARCH_TOOL in present, SEARCH_TOOL, alt_search, _WHEN_SEARCH
    )
    general_primary = (
        SEARCH_TOOL
        if SEARCH_TOOL in present
        else (alt_search[0] if alt_search else None)
    )
    # 通用搜索的首选本身就是这类问题的首选时（比如关闭了自带搜索、Tavily 排第一），不必再单独写一行
    assigned = [
        (d, t)
        for d, t in router.lane_assignments(sorted(present))
        if t != general_primary
    ]
    if search_line:
        desc = GENERAL_DESC_WITH_LANES if assigned else GENERAL_DESC
        lines.append(f"- {desc}：{search_line}")
    for desc, tool in assigned:
        if SEARCH_TOOL in present:
            backup = SEARCH_TOOL
        else:
            backup = next((n for n in alt_search if n != tool), "")
        lines.append(
            f"- {desc}：优先 {tool}" + (f"；不理想时再用 {backup}" if backup else "")
        )
    fetch_line = _general_line(
        FETCH_TOOL in present, FETCH_TOOL, alt_fetch, _WHEN_FETCH
    )
    if fetch_line:
        lines.append(f"- 读取网页全文：{fetch_line}")
    for desc, tools in special:
        lines.append(f"- {desc}：{_short_list(tools)}")
    return "\n".join(lines)
