"""其他搜索工具的每月调用上限（比如 AstrBot 内置的博查、Brave、Exa，或其他付费搜索插件）。

按自然月统计这些工具成功的调用次数（工具返回错误的不算），保存在插件数据目录。
达到上限后只在每次请求里把工具拿掉（不改 AstrBot 的工具开关），整个月保持不变，下个月自动恢复。
DeepDeep 自己的工具不在这里管：DeepSeek 有费用上限，Tavily 有自己的每月上限。
"""

from __future__ import annotations

import fnmatch
from typing import Any

from .stats import MonthlyCounter

MAX_LIMIT = 10_000_000


def parse_limit_rules(lines: Any) -> list[tuple[str, int]]:
    """每行“工具名规则 = 次数”（支持 * 通配）。写错的行忽略。"""
    if not isinstance(lines, list):
        return []
    out: list[tuple[str, int]] = []
    for line in lines:
        if not isinstance(line, str) or "=" not in line:
            continue
        pattern, _, num = line.rpartition("=")
        pattern = pattern.strip()
        try:
            limit = int(float(num.strip()))
        except ValueError:
            continue
        if pattern and 0 < limit <= MAX_LIMIT:
            out.append((pattern, limit))
    return out


def format_limit_rules(rules: list[tuple[str, int]]) -> list[str]:
    return [f"{p} = {n}" for p, n in rules]


class ToolCaps:
    def __init__(self, counter: MonthlyCounter) -> None:
        self.counter = counter
        self.rules: list[tuple[str, int]] = []

    def rule_for(self, name: str) -> tuple[str, int] | None:
        for pattern, limit in self.rules:
            if fnmatch.fnmatchcase(name, pattern):
                return pattern, limit
        return None

    def record(self, name: str) -> None:
        self.counter.add(name)

    def used(self, pattern: str) -> int:
        """本月匹配这条规则的工具一共成功调用了多少次。"""
        return sum(
            n
            for tool, n in self.counter.counts().items()
            if fnmatch.fnmatchcase(tool, pattern)
        )

    def blocked(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """这些工具里本月已达上限的：{工具名: {"pattern", "used", "limit"}}。"""
        if not self.rules:
            return {}
        out = {}
        for name in names:
            rule = self.rule_for(name)
            if rule is None:
                continue
            used = self.used(rule[0])
            if used >= rule[1]:
                out[name] = {"pattern": rule[0], "used": used, "limit": rule[1]}
        return out

    def snapshot(self) -> list[dict[str, Any]]:
        counts = self.counter.counts()
        return [
            {
                "pattern": pattern,
                "limit": limit,
                "used": self.used(pattern),
                "tools": {
                    t: n for t, n in counts.items() if fnmatch.fnmatchcase(t, pattern)
                },
            }
            for pattern, limit in self.rules
        ]
