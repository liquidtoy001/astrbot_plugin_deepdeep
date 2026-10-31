"""面板上怎么展示路由：把专用工具归到几个大类，给每个服务一盏状态灯。

只影响面板。发给模型的分工说明还是按 specialist_rules 一条条写（core/guide.py）：说明一变，
所有用户都要多付一次提示词缓存未命中的钱，而且老用户配置里存的规则也不会跟着变。
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from . import catalog
from .guide import TECH_DESC
from .preference import SCENE_LABELS
from .providers import KB_TOOL


@dataclass(frozen=True, slots=True)
class Category:
    key: str
    title: str
    patterns: tuple[str, ...]
    # 用户没有这类工具时，要不要提醒“这类问题由一般问题的工具负责”
    suggest: bool = True


NEWS_PATTERN = "*news*"
NEWS_TITLE = "新闻时事"

# 按顺序匹配：医学放在学术前面，pubmed 算医学
CATEGORIES: tuple[Category, ...] = (
    Category(
        "media",
        "图片、视频和音乐",
        ("*image*", "*photo*", "*video*", "*youtube*", "*bilibili*", "*music*"),
    ),
    Category("medical", "医学健康", ("*medical*", "*medicine*", "*pubmed*", "*drug*")),
    Category("academic", "学术论文", ("*academic*", "*scholar*", "*paper*", "*arxiv*")),
    Category(
        "code",
        "代码和开发",
        (
            "*github*",
            "search_repositories",
            "search_code",
            "search_issues",
            "search_pull_requests",
            "*aur*",
            "*code-context*",
        ),
    ),
    Category("finance", "财经行情", ("*finance*", "*stock*", "*crypto*")),
    Category("law", "法律", ("*legal*", "*law_*", "*_law*", "*court*")),
    Category("game", "游戏", ("*game*", "*steam*")),
    Category("map", "地点和地图", ("*map*",), suggest=False),
    Category("shot", "网页截图", ("*webshot*",), suggest=False),
    Category("kb", "本地知识库", (KB_TOOL,), suggest=False),
)


def category_of(tool: str) -> Category | None:
    lowered = tool.lower()
    for category in CATEGORIES:
        if any(fnmatchcase(lowered, p) for p in category.patterns):
            return category
    return None


def _plain(lane: dict[str, Any]) -> str:
    if lane["key"] == "search":
        return SCENE_LABELS["general"]
    if lane["key"] == "fetch":
        return SCENE_LABELS["read"]
    return SCENE_LABELS["tech"] if lane["title"] == TECH_DESC else lane["title"]


def arrange(lanes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """router.lanes() 的结果整理成面板上的行。返回 (行, 没有专门工具的大类)。

    行里的 names 是要展示的工具，第一个是首选。一般问题的 backups 保持和路由一致。"""
    main: list[dict[str, Any]] = []
    buckets: dict[str, tuple[str, list[str]]] = {}
    news: dict[str, Any] | None = None
    for lane in lanes:
        names = [lane["primary"], *lane["backups"]]
        if lane["key"].startswith("special:"):
            for name in names:
                c = category_of(name)
                key, title = (
                    (f"cat:{c.key}", c.title) if c else (lane["key"], lane["title"])
                )
                buckets.setdefault(key, (title, []))[1].append(name)
            continue
        row = {
            **lane,
            "plain": _plain(lane),
            "primary_label": catalog.label_for(lane["primary"]),
            "group": "main",
            "names": names,
        }
        if lane["key"] == "search":
            found = [n for n in lane["backups"] if fnmatchcase(n.lower(), NEWS_PATTERN)]
            if found:
                # 新闻工具也是可以互相替代的通用搜索，先用谁和一般问题一样
                row["names"] = [n for n in names if n not in found]
                news = {
                    "key": "cat:news",
                    "title": NEWS_TITLE,
                    "desc": "",
                    "primary": lane["primary"],
                    "backups": found,
                    "plain": NEWS_TITLE,
                    "primary_label": row["primary_label"],
                    "group": "main",
                    "names": [lane["primary"], *found],
                }
        main.append(row)
    if news is not None:
        main.insert(1, news)
    order = {f"cat:{c.key}": i for i, c in enumerate(CATEGORIES)}
    special = [
        {
            "key": key,
            "title": title,
            "desc": "",
            "primary": names[0],
            "backups": names[1:],
            "plain": title,
            "primary_label": catalog.label_for(names[0]),
            "group": "special",
            "names": names,
        }
        for key, (title, names) in sorted(
            buckets.items(), key=lambda kv: order.get(kv[0], len(order))
        )
    ]
    missing = [
        c.title for c in CATEGORIES if c.suggest and f"cat:{c.key}" not in buckets
    ]
    if news is None:
        missing.insert(0, NEWS_TITLE)
    return main + special, missing


class ToolStatus:
    """每个服务的状态灯。出现在这里的工具都已经提供给模型了，所以没问题就是绿的；
    近 24 小时没人调用不算问题，调用次数另外显示。"""

    def __init__(
        self, health: dict[str, dict[str, Any]], setup_status: dict[str, str]
    ) -> None:
        self._health = health
        self._setup = setup_status

    def lamp(self, tools: list[str]) -> str:
        setup = {self._setup.get(t) for t in tools}
        verdicts = {
            self._health[t]["verdict"]
            for t in tools
            if t in self._health and not self._health[t].get("ignored")
        }
        if "missing" in setup or "broken" in verdicts:
            return "bad"
        if "warn" in setup or "degraded" in verdicts:
            return "warn"
        return "ok"

    def calls(self, tools: list[str]) -> int:
        return sum(self._health.get(t, {}).get("total", 0) for t in tools)

    def items(self, tools: list[str]) -> list[dict[str, Any]]:
        """按服务合并：同一家的几个工具只显示一次。"""
        merged: dict[str, list[str]] = {}
        for group in catalog.grouped(tools, False):
            merged.setdefault(group["label"], []).extend(group["tools"])
        return [
            {
                "label": label,
                "tools": names,
                "lamp": self.lamp(names),
                "calls": self.calls(names),
            }
            for label, names in merged.items()
        ]
