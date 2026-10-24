"""搜索工具路由与分工。

- 通用搜索：DeepSeek 可用时由本插件负责，同类的通用搜索工具作为备用（隐藏）；不可用时换回备用
- 专用搜索：按“分工规则”识别（图片、视频、学术、GitHub 等），始终保留，并在提示里写明各自擅长什么

只在单次 LLM 请求的工具集上增删，不改 AstrBot 全局的工具启用状态，
所以不会影响用户在 WebUI 里手动设置的工具开关。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Protocol

from .preference import Preference

SEARCH_TOOL = "deepdeep_web_search"
FETCH_TOOL = "deepdeep_web_fetch"
OWN_TOOLS = (SEARCH_TOOL, FETCH_TOOL)
# DeepDeep 自带的 Tavily 搜索：不受“启用自带搜索”开关影响，作为技术类问题的首选和第一备用
TAVILY_TOOL = "deepdeep_tavily_search"

# guide_only：不隐藏任何工具，只靠分工提示安排优先级。工具列表和提示始终不变，
# 不会因为 DeepSeek 可用性变化而让模型的提示词缓存失效（长上下文时最省钱）
ROUTING_MODES = (
    "guide_only",
    "deepseek_first",
    "deepseek_only",
    "fallback_only",
    "coexist",
)
DEFAULT_MODE = "guide_only"

# 已知会和 DeepSeek 重复的搜索工具（2026-10 对照插件市场整理，见 README「兼容性」）：
#   AstrBot 内置网页搜索  web_search_tavily / tavily_extract_web_page / web_search_bocha /
#                         web_search_brave / web_search_firecrawl / firecrawl_extract_web_page /
#                         web_search_baidu / web_search_exa / exa_get_contents / web_search_anysearch
#   web_searcher_pro      searxng_web_search_* / searxng_web_fetch_url
#   kimi_web_search       kimi_web_search / kimi_web_fetch
#   grok_web_search       grok_web_search / grok_web_fetch（grok_suite 同名）
#   mmx_cli_tool          mmx_web_search
#   anysearch             anysearch_search / anysearch_batch_search / anysearch_extract
#   gemini_search         gemini_search / web_fetch
#   exa_web_search        exa-search / web_fetch_exa
#   同类的 DeepSeek 搜索插件  deepseek_web_search（SkyL3gend）/
#                         web_search_deepseek、deepseek_fetch_web_page（Fantasality）
DEFAULT_FALLBACK_PATTERNS = [
    "*web_search*",
    "*web_fetch*",
    "*fetch_url*",
    "*fetch_web_page*",
    "*_extract_web_page",
    "exa_get_contents",
    "exa-search",
    "anysearch_*",
    "gemini_search",
]
# 分工规则：“工具名规则 = 擅长什么”。匹配的工具视为专用搜索，始终保留，
# 并写进每次请求的“搜索工具分工”提示里。按顺序匹配，第一条命中的生效
DEFAULT_SPECIALIST_RULES = [
    "*image* = 找图片",
    "*video* = 找视频（B 站、YouTube 等视频平台）",
    "*music* = 找音乐",
    "*map* = 地点和地图",
    "*finance* = 股票、汇率等财经行情",
    "*academic* = 学术论文和期刊",
    "*scholar* = 学术论文和期刊",
    "*paper* = 学术论文（arXiv 等）",
    "*github* = GitHub 上的仓库、代码、Issue 和 PR",
    "search_repositories = GitHub 上的仓库、代码、Issue 和 PR",
    "search_code = GitHub 上的仓库、代码、Issue 和 PR",
    "search_issues = GitHub 上的仓库、代码、Issue 和 PR",
    "search_pull_requests = GitHub 上的仓库、代码、Issue 和 PR",
    "*aur* = Arch Linux AUR 软件包",
    "*code-context* = 编程文档和代码示例",
    "*webshot* = 网页截图",
    "astr_kb_search = 本地知识库里的资料",
]

# 按问题类型分工：“问题类型 = 工具名规则, 工具名规则, …”。本次请求里第一个存在的工具
# 作为这类问题的首选，其余问题仍先用 DeepDeep。2026-10 评测：技术类问题 Tavily 明显更好
DEFAULT_LANE_RULES = [
    "编程、软件版本、安全漏洞、官方文档等技术问题 = deepdeep_tavily_search, web_search_tavily",
]
# 搜索偏好里“技术问题”对应的分工
TECH_DESC = "编程、软件版本、安全漏洞、官方文档等技术问题"
# 有按类型分工时，通用搜索那一行改写成更具体的说明
GENERAL_DESC = "通用网页搜索（新闻、事实、中文资料等）"
GENERAL_DESC_WITH_LANES = "新闻时事、中文资料、海外新闻、比赛和最新动态"

# 和本插件用同一个 DeepSeek 账户计费的工具（AstrBot 4.29.1 起内置的 DeepSeek 搜索、
# 其他 DeepSeek 搜索插件）。因为超出费用上限而降级时，这些工具也保持隐藏，否则会绕过上限
SAME_BACKEND_PATTERNS = ["*deepseek*"]
BUDGET_REASONS = frozenset({"budget_daily", "budget_monthly"})

# 抓取类工具：只有本次请求里有 deepdeep_web_fetch 时才把它们藏起来
FETCH_LIKE_PATTERNS = ["*fetch*", "*extract*", "*get_contents*", "*read_page*"]

# 内置网页搜索工具名，用于 WebUI 预览（它们在每次请求时才加入工具集）
BUILTIN_WEB_SEARCH_TOOLS = [
    "web_search_tavily",
    "tavily_extract_web_page",
    "web_search_bocha",
    "web_search_brave",
    "web_search_firecrawl",
    "firecrawl_extract_web_page",
    "web_search_baidu",
    "web_search_exa",
    "exa_get_contents",
    "web_search_anysearch",
    "web_search_serpapi",
    "image_search_serpapi",
    "finance_search_serpapi",
    "scholar_search_serpapi",
    "web_search_deepseek",
    "deepseek_fetch_web_page",
]


# 分工提示里每类最多列出的工具数。像 arXiv MCP 这样一类有十来个工具时，
# 全部列出会让提示变长，模型看到前几个就知道该用哪一组了
GUIDE_MAX_PER_LINE = 3


def _short_list(names: list[str]) -> str:
    ranked = sorted(names, key=lambda n: ("search" not in n.lower(), n))
    shown = ranked[:GUIDE_MAX_PER_LINE]
    rest = len(ranked) - len(shown)
    text = "、".join(shown)
    return f"{text} 等 {len(ranked)} 个" if rest > 0 else text


def _alt_rank(name: str, same_backend: bool) -> int:
    """备用工具的推荐顺序：Tavily 在前，其次是 AstrBot 内置的网页搜索、其他综合搜索，
    分类搜索（书籍、文件等）在后，和 DeepDeep 同样走 DeepSeek 计费的最后。"""
    lowered = name.lower()
    if same_backend:
        return 5
    # 2026-10 评测：Tavily 平均 6.3 分，Google（SerpApi）4.7，SearXNG 综合搜索 0.5
    if "tavily" in lowered and not ToolRouter.is_fetch_like(name):
        return 0
    if lowered.startswith("web_search_") or "fetch_url" in lowered:
        return 1
    if "general" in lowered:
        return 2
    if any(
        k in lowered
        for k in ("news", "technical", "kimi", "grok", "exa", "anysearch", "gemini")
    ):
        return 3
    return 4


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


class ToolSetLike(Protocol):
    def names(self) -> list[str]: ...
    def get_tool(self, name: str) -> Any: ...
    def remove_tool(self, name: str) -> None: ...
    def add_tool(self, tool: Any) -> None: ...


def _clean_patterns(patterns: Any, default: list[str]) -> list[str]:
    if not isinstance(patterns, list):
        return list(default)
    return [p.strip().lower() for p in patterns if isinstance(p, str) and p.strip()]


def parse_lane_rules(rules: Any) -> list[tuple[str, list[str]]]:
    """把 ["技术问题 = a, b*", ...] 解析成 [(问题类型, [模式, ...]), ...]；非法配置回退到默认。"""
    if not isinstance(rules, list):
        rules = DEFAULT_LANE_RULES
    parsed = []
    for rule in rules:
        if not isinstance(rule, str) or "=" not in rule:
            continue
        desc, _, pats = rule.partition("=")
        patterns = [p.strip().lower() for p in pats.split(",") if p.strip()]
        if desc.strip() and patterns:
            parsed.append((desc.strip(), patterns))
    return parsed


def parse_specialist_rules(rules: Any) -> list[tuple[str, str]]:
    """把 ["*image* = 找图片", ...] 解析成 [(模式, 说明), ...]；非法配置回退到默认。"""
    if not isinstance(rules, list):
        rules = DEFAULT_SPECIALIST_RULES
    parsed = []
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            continue
        pattern, _, desc = rule.partition("=")
        pattern = pattern.strip().lower()
        if pattern:
            parsed.append((pattern, desc.strip()))
    return parsed


@dataclass(slots=True)
class RoutingState:
    """一次 LLM 请求里被藏起来的工具，供调用失败时恢复。"""

    toolset: ToolSetLike
    # AstrBot 的 ProviderRequest。skills_like 模式下 AstrBot 会在本钩子之后把
    # req.func_tool 换成精简版工具集，恢复备用工具时需要同时改它
    req: Any = None
    hidden_fallback: list[Any] = field(default_factory=list)
    hidden_own: list[str] = field(default_factory=list)
    restored: bool = False


class ToolRouter:
    def __init__(
        self,
        mode: str = DEFAULT_MODE,
        fallback_patterns: Any = None,
        specialist_rules: Any = None,
        own_enabled: bool = True,
        lane_rules: Any = None,
        preference: Preference | None = None,
    ) -> None:
        self.mode = mode if mode in ROUTING_MODES else DEFAULT_MODE
        # 用户的搜索偏好；None 表示沿用默认顺序（“推荐”预设）
        self.preference = preference
        # 关闭后，每次请求都把 DeepDeep 自己的两个工具拿掉，交给其他搜索工具
        self.own_enabled = own_enabled
        self.fallback_patterns = _clean_patterns(
            fallback_patterns, DEFAULT_FALLBACK_PATTERNS
        )
        self.specialist_rules = parse_specialist_rules(specialist_rules)
        self.lane_rules = parse_lane_rules(lane_rules)

    # ---------- 搜索偏好 ----------

    def with_preference(self, preference: Preference | None) -> ToolRouter:
        """同样的配置、换一个搜索偏好（设置页预览各个预设时用）。"""
        clone = copy.copy(self)
        clone.preference = preference
        return clone

    def _general_candidates(self, present: set[str]) -> list[str]:
        """一般问题的候选工具（默认顺序）：DeepDeep 搜索在前，其余按推荐顺序。"""
        own = [SEARCH_TOOL] if SEARCH_TOOL in present else []
        return own + self._default_alternatives(sorted(present), "search")

    def _fetch_candidates(self, present: set[str]) -> list[str]:
        own = [FETCH_TOOL] if FETCH_TOOL in present else []
        return own + self._default_alternatives(sorted(present), "fetch")

    def ordered(self, scene: str, names: list[str] | set[str]) -> list[str]:
        """某类场景按偏好排好的工具（去掉“不用”的），第一个是首选。"""
        present = set(names)
        base = (
            self._fetch_candidates(present)
            if scene == "read"
            else self._general_candidates(present)
        )
        if self.preference is None:
            return base
        out = self.preference.sort(scene, base)
        # “DeepSeek 优先 / 只用 DeepSeek”模式下 DeepSeek 固定排第一（其他工具反正会被藏起来）
        own = FETCH_TOOL if scene == "read" else SEARCH_TOOL
        if self.mode in ("deepseek_first", "deepseek_only") and own in out:
            out.remove(own)
            out.insert(0, own)
        return out

    def disabled_tools(self, names: list[str]) -> set[str]:
        """在它参与的所有场景都设为“不用”的工具：每次请求里拿掉。"""
        if self.preference is None:
            return set()
        present = set(names)
        pref = self.preference
        out = {
            n
            for n in self._general_candidates(present)
            if pref.is_disabled("general", n) and pref.is_disabled("tech", n)
        }
        out |= {
            n for n in self._fetch_candidates(present) if pref.is_disabled("read", n)
        }
        return out

    def lane_assignments(self, names: list[str]) -> list[tuple[str, str]]:
        """本次请求里生效的按类型分工：[(问题类型, 首选工具), ...]。"""
        present = sorted(set(names))
        out = []
        if self.preference is not None:
            tech = self.ordered("tech", present)
            if tech:
                out.append((TECH_DESC, tech[0]))
        for desc, patterns in self.lane_rules:
            # 有搜索偏好时，技术问题按偏好来
            if self.preference is not None and desc == TECH_DESC:
                continue
            for pattern in patterns:
                hit = next(
                    (n for n in present if fnmatchcase(n.lower(), pattern)), None
                )
                if hit:
                    out.append((desc, hit))
                    break
        return out

    def specialty(self, name: str) -> str | None:
        """专用搜索工具擅长什么；不是专用工具返回 None。"""
        if name in OWN_TOOLS:
            return None
        lowered = name.lower()
        for pattern, desc in self.specialist_rules:
            if fnmatchcase(lowered, pattern):
                return desc or "专用搜索"
        return None

    def is_fallback(self, name: str) -> bool:
        if name == TAVILY_TOOL:
            return True
        if name in OWN_TOOLS or self.specialty(name) is not None:
            return False
        lowered = name.lower()
        return any(fnmatchcase(lowered, p) for p in self.fallback_patterns)

    @staticmethod
    def is_fetch_like(name: str) -> bool:
        lowered = name.lower()
        return any(fnmatchcase(lowered, p) for p in FETCH_LIKE_PATTERNS)

    def search_category(self, name: str) -> str | None:
        """给“各搜索工具用量”分类：deepdeep / fallback（可替代的搜索）/ special（专用搜索）。

        与搜索无关的工具返回 None，不参与统计。
        """
        if name in OWN_TOOLS or name == TAVILY_TOOL:
            return "deepdeep"
        if self.is_fallback(name):
            return "fallback"
        if self.specialty(name) is not None:
            return "special"
        return None

    @staticmethod
    def is_same_backend(name: str) -> bool:
        lowered = name.lower()
        return name not in OWN_TOOLS and any(
            fnmatchcase(lowered, p) for p in SAME_BACKEND_PATTERNS
        )

    def plan(
        self, names: list[str], deepseek_available: bool, reason: str = ""
    ) -> set[str]:
        """返回本次请求要藏起来的工具名。``reason`` 为不可用的原因代码。"""
        own = {n for n in names if n in OWN_TOOLS}
        if not own:
            # 本次请求里根本没有 DeepSeek 工具（人格只启用了部分工具、插件在本会话
            # 被禁用、其他插件自己发起的请求等），什么都不动，免得把唯一的搜索藏掉
            return set()
        if not self.own_enabled:
            return own
        # 只藏 DeepSeek 能替代的那一类：有搜索才藏搜索，有抓取才藏抓取
        # 按类型分工的首选工具（比如技术问题用 Tavily）不藏；“只用 DeepSeek”模式除外
        lane_primaries = (
            set()
            if self.mode == "deepseek_only"
            else {tool for _, tool in self.lane_assignments(names)}
        )
        fallback = {
            n
            for n in names
            if self.is_fallback(n)
            and n not in lane_primaries
            and (FETCH_TOOL if self.is_fetch_like(n) else SEARCH_TOOL) in own
        }
        if self.mode in ("coexist", "guide_only"):
            return set()
        if self.mode == "fallback_only":
            return own
        if self.mode == "deepseek_only":
            return fallback
        if deepseek_available:
            return fallback
        # 超出费用上限：同一 DeepSeek 账户计费的备用工具也不能用
        blocked = (
            {n for n in fallback if self.is_same_backend(n)}
            if reason in BUDGET_REASONS
            else set()
        )
        usable = fallback - blocked
        # deepseek_first：没有可用的备用工具时也不藏自己，让模型拿到明确的错误说明
        if not usable:
            return blocked
        return own | blocked

    def apply(
        self,
        toolset: ToolSetLike,
        deepseek_available: bool,
        req: Any = None,
        reason: str = "",
    ) -> RoutingState:
        state = RoutingState(toolset, req)
        for name in self.plan(toolset.names(), deepseek_available, reason):
            tool = toolset.get_tool(name)
            if tool is None:
                continue
            toolset.remove_tool(name)
            if name in OWN_TOOLS:
                state.hidden_own.append(name)
            else:
                state.hidden_fallback.append(tool)
        return state

    def restore_fallback(
        self,
        state: RoutingState | None,
        *,
        drop_own: bool = False,
        reason: str = "",
    ) -> list[str]:
        """DeepSeek 在对话中途失败时调用：把藏起来的备用工具放回本次请求。

        ``drop_own`` 为 True 时（服务不可用，而不是某个网页抓不到），
        同时把 DeepSeek 工具移出本次请求，避免模型继续重试。
        ``reason`` 为超出费用上限时，不放出同一 DeepSeek 账户计费的工具。
        """
        if (
            state is None
            or self.mode == "deepseek_only"
            or not self.own_enabled
            or not state.hidden_fallback
        ):
            return []
        tools = [
            t
            for t in state.hidden_fallback
            if not (reason in BUDGET_REASONS and self.is_same_backend(t.name))
        ]
        if not tools:
            return []
        targets = [state.toolset]
        current = getattr(state.req, "func_tool", None)
        if current is not None and current is not state.toolset:
            targets.append(current)
        for target in targets:
            for tool in tools:
                if target.get_tool(tool.name) is None:
                    target.add_tool(tool)
            if drop_own:
                for name in OWN_TOOLS:
                    if target.get_tool(name) is not None:
                        target.remove_tool(name)
                        if name not in state.hidden_own:
                            state.hidden_own.append(name)
        state.restored = True
        return [t.name for t in tools]

    def preview(self, names: list[str]) -> list[dict]:
        """WebUI 用：列出每个工具在 DeepSeek 可用 / 故障 / 超出预算时是否可见。"""
        hide_ok = self.plan(names, True)
        hide_down = self.plan(names, False, "circuit_open")
        hide_budget = self.plan(names, False, "budget_daily")
        lane_of = {tool: desc for desc, tool in self.lane_assignments(names)}
        return [
            {
                "name": n,
                "role": "deepseek"
                if n in OWN_TOOLS
                else (
                    "fallback"
                    if self.is_fallback(n)
                    else ("specialist" if self.specialty(n) else "other")
                ),
                "visible_when_ok": n not in hide_ok,
                "visible_when_down": n not in hide_down,
                "visible_when_budget": n not in hide_budget,
                "same_backend": self.is_same_backend(n),
                "specialty": self.specialty(n)
                or (f"{lane_of[n]}首选" if n in lane_of else None),
            }
            for n in sorted(set(names), key=lambda x: (x not in OWN_TOOLS, x))
        ]

    def alternatives(self, names: list[str], kind: str, reason: str = "") -> list[str]:
        """本次请求里可以替代 DeepDeep 的通用工具（search / fetch 两类分开），按偏好排序。"""
        alts = self._default_alternatives(names, kind, reason)
        if self.preference is None:
            return alts
        return self.preference.sort("read" if kind == "fetch" else "general", alts)

    def _default_alternatives(
        self, names: list[str], kind: str, reason: str = ""
    ) -> list[str]:
        """默认顺序的替代工具。超出费用上限时去掉同样走 DeepSeek 计费的工具；
        其余按“非 DeepSeek 计费优先”排序。"""
        want_fetch = kind == "fetch"
        alts = [
            n
            for n in names
            if self.is_fallback(n) and self.is_fetch_like(n) == want_fetch
        ]
        if reason in BUDGET_REASONS:
            alts = [n for n in alts if not self.is_same_backend(n)]
        # 关闭自带搜索时，AstrBot 内置的 DeepSeek 搜索就是正常的首选之一，不再往后排
        return sorted(
            alts,
            key=lambda n: (
                _alt_rank(n, self.own_enabled and self.is_same_backend(n)),
                n,
            ),
        )

    def _special_groups(self, present: set[str]) -> list[tuple[str, list[str]]]:
        groups: dict[str, list[str]] = {}
        for name in sorted(present):
            desc = self.specialty(name)
            if desc:
                groups.setdefault(desc, []).append(name)
        order = {d or "专用搜索": i for i, (_, d) in enumerate(self.specialist_rules)}
        return [
            (desc, groups[desc])
            for desc in sorted(groups, key=lambda d: order.get(d, len(order)))
        ]

    def _pref_lanes(self, present: set[str]) -> list[dict[str, Any]]:
        general = self.ordered("general", present)
        fetch = self.ordered("read", present)
        lanes = [
            (d, t)
            for d, t in self.lane_assignments(sorted(present))
            if not general or t != general[0]
        ]
        out: list[dict[str, Any]] = []
        if general:
            out.append(
                {
                    "key": "search",
                    "title": "通用网页搜索",
                    "desc": GENERAL_DESC_WITH_LANES
                    if lanes
                    else "新闻、事实、中文资料等",
                    "primary": general[0],
                    "backups": general[1:],
                }
            )
        for desc, tool in lanes:
            backups = (
                [n for n in self.ordered("tech", present) if n != tool]
                if desc == TECH_DESC
                else [n for n in general[:1] if n != tool]
            )
            out.append(
                {
                    "key": f"lane:{desc}",
                    "title": desc,
                    "desc": "按问题类型分工",
                    "primary": tool,
                    "backups": backups,
                }
            )
        if fetch:
            out.append(
                {
                    "key": "fetch",
                    "title": "读取网页全文",
                    "desc": "用户发来链接、摘要不够详细时",
                    "primary": fetch[0],
                    "backups": fetch[1:],
                }
            )
        for desc, tools in self._special_groups(present):
            ranked = sorted(tools, key=lambda n: ("search" not in n.lower(), n))
            out.append(
                {
                    "key": f"special:{desc}",
                    "title": desc,
                    "desc": "",
                    "primary": ranked[0],
                    "backups": ranked[1:],
                }
            )
        return out

    def _pref_guide(self, present: set[str]) -> str | None:
        general = self.ordered("general", present)
        fetch = self.ordered("read", present)
        special = self._special_groups(present)
        if not special and len(general) <= 1 and len(fetch) <= 1:
            return None
        lanes = [
            (d, t)
            for d, t in self.lane_assignments(sorted(present))
            if not general or t != general[0]
        ]
        lines = ["【搜索工具分工】先选最对口的工具，结果不理想时再换："]
        if general:
            desc = GENERAL_DESC_WITH_LANES if lanes else GENERAL_DESC
            lines.append(
                f"- {desc}：{_ordered_line(general, '没有结果或结果不理想时')}"
            )
        for desc, tool in lanes:
            backups = (
                [n for n in self.ordered("tech", present) if n != tool]
                if desc == TECH_DESC
                else [n for n in general[:1] if n != tool]
            )[: GUIDE_MAX_PER_LINE - 1]
            lines.append(
                f"- {desc}：优先 {tool}"
                + (f"；不理想时再用 {'、'.join(backups)}" if backups else "")
            )
        if fetch:
            lines.append(f"- 读取网页全文：{_ordered_line(fetch, '读取失败时')}")
        for desc, tools in special:
            lines.append(f"- {desc}：{_short_list(tools)}")
        return "\n".join(lines)

    def lanes(self, names: list[str]) -> list[dict[str, Any]]:
        """WebUI 顶部的“主力搜索工具”：每类需求的首选工具和备用工具，与分工说明一致。"""
        present = set(names)
        if not self.own_enabled:
            present -= set(OWN_TOOLS)
        if self.preference is not None:
            return self._pref_lanes(present)
        out: list[dict[str, Any]] = []
        assigned = self.lane_assignments(sorted(present))
        for kind, own, title, desc in (
            (
                "search",
                SEARCH_TOOL,
                "通用网页搜索",
                GENERAL_DESC_WITH_LANES if assigned else "新闻、事实、中文资料等",
            ),
            ("fetch", FETCH_TOOL, "读取网页全文", "用户发来链接、摘要不够详细时"),
        ):
            alts = self.alternatives(sorted(present), kind)
            if own in present:
                out.append(
                    {
                        "key": kind,
                        "title": title,
                        "desc": desc,
                        "primary": own,
                        "backups": alts,
                    }
                )
            elif alts:
                out.append(
                    {
                        "key": kind,
                        "title": title,
                        "desc": desc,
                        "primary": alts[0],
                        "backups": alts[1:],
                    }
                )
        general_alts = self.alternatives(sorted(present), "search")
        general_primary = (
            SEARCH_TOOL
            if SEARCH_TOOL in present
            else (general_alts[0] if general_alts else None)
        )
        assigned = [(d, t) for d, t in assigned if t != general_primary]
        if not assigned and out and out[0]["key"] == "search":
            out[0]["desc"] = "新闻、事实、中文资料等"
        for pos, (desc, tool) in enumerate(assigned, start=1):
            backup = SEARCH_TOOL if SEARCH_TOOL in present else None
            backups = [backup] if backup else [n for n in general_alts if n != tool][:1]
            out.insert(
                pos,
                {
                    "key": f"lane:{desc}",
                    "title": desc,
                    "desc": "按问题类型分工",
                    "primary": tool,
                    "backups": backups,
                },
            )
        groups: dict[str, list[str]] = {}
        for name in sorted(present):
            desc = self.specialty(name)
            if desc:
                groups.setdefault(desc, []).append(name)
        order = {d or "专用搜索": i for i, (_, d) in enumerate(self.specialist_rules)}
        for desc in sorted(groups, key=lambda d: order.get(d, len(order))):
            ranked = sorted(groups[desc], key=lambda n: ("search" not in n.lower(), n))
            out.append(
                {
                    "key": f"special:{desc}",
                    "title": desc,
                    "desc": "",
                    "primary": ranked[0],
                    "backups": ranked[1:],
                }
            )
        return out

    def guide(self, names: list[str]) -> str | None:
        """根据本次请求里实际可用的工具，生成“搜索工具分工”提示。

        内容只取决于工具集和配置，工具集不变时提示词保持一致，不会破坏模型的提示词缓存。
        没有专用搜索工具时返回 None（只有通用搜索时不需要分工说明）。
        """
        present = set(names)
        if self.preference is not None:
            return self._pref_guide(present)
        alt_search = self.alternatives(sorted(present), "search")
        alt_fetch = self.alternatives(sorted(present), "fetch")
        groups: dict[str, list[str]] = {}
        for name in sorted(present):
            desc = self.specialty(name)
            if desc:
                groups.setdefault(desc, []).append(name)
        # 只有 DeepDeep 自己、没有备用和专用工具时不需要分工说明
        if not groups and not alt_search and not alt_fetch:
            return None
        order = {
            desc or "专用搜索": i for i, (_, desc) in enumerate(self.specialist_rules)
        }
        lines = ["【搜索工具分工】先选最对口的工具，结果不理想时再换："]
        search_line = _general_line(
            SEARCH_TOOL in present, SEARCH_TOOL, alt_search, "没有结果或结果不理想时"
        )
        general_primary = (
            SEARCH_TOOL
            if SEARCH_TOOL in present
            else (alt_search[0] if alt_search else None)
        )
        # 通用搜索的首选本身就是这类问题的首选时（比如关闭了自带搜索、Tavily 排第一），不必再单独写一行
        assigned = [
            (d, t)
            for d, t in self.lane_assignments(sorted(present))
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
                f"- {desc}：优先 {tool}"
                + (f"；不理想时再用 {backup}" if backup else "")
            )
        fetch_line = _general_line(
            FETCH_TOOL in present, FETCH_TOOL, alt_fetch, "读取失败时"
        )
        if fetch_line:
            lines.append(f"- 读取网页全文：{fetch_line}")
        for desc in sorted(groups, key=lambda d: order.get(d, len(order))):
            lines.append(f"- {desc}：{_short_list(groups[desc])}")
        return "\n".join(lines)
