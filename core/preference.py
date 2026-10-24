"""搜索偏好：用户自己决定每类问题先用哪个搜索工具、不用哪个。

三类场景：一般问题（general）、技术问题（tech）、读取网页（read）。每类是一串工具名规则（支持 *），
排在前面的先用；前面加 ! 表示这一类不用它。
- 预设“推荐”不改变任何行为（沿用评测得出的默认顺序），老用户升级后提示词不变
- 其他预设和“自定义”会改变分工说明里的顺序和备用推荐顺序；一个工具在它参与的所有场景都“不用”时，
  每次请求里把它拿掉（不改 AstrBot 的工具开关）

配置：search_preference（rec / cheap / zh / custom），search_order（自定义时用，每行“场景 = 规则, !规则”）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

SCENES = ("general", "tech", "read")
SCENE_LABELS = {"general": "一般问题", "tech": "技术问题", "read": "读取网页"}
DEFAULT_PRESET = "rec"
PRESET_LABELS = {
    "rec": "推荐（按评测）",
    "cheap": "省钱优先",
    "zh": "中文资料优先",
    "custom": "自定义",
}

# 预设的顺序。没写到的工具排在后面，保持默认的相对顺序
PRESETS: dict[str, dict[str, list[str]]] = {
    # 免费的先用：自建的 SearXNG，再是有免费额度的 Tavily、Google、Brave、Exa，按次计费的最后
    "cheap": {
        "general": [
            "searxng_*general*",
            "deepdeep_tavily_search",
            "web_search_tavily",
            "*serpapi*",
            "web_search_brave",
            "web_search_exa",
            "searxng_*",
        ],
        "tech": [
            "deepdeep_tavily_search",
            "web_search_tavily",
            "searxng_*general*",
            "*serpapi*",
        ],
        "read": ["searxng_web_fetch_url", "*fetch_url*", "*extract*", "*get_contents*"],
    },
    # 中文网站覆盖好的先用
    "zh": {
        "general": [
            "deepdeep_web_search",
            "web_search_bocha",
            "web_search_baidu",
            "kimi_web_search",
            "web_search_deepseek",
            "*serpapi*",
        ],
        "tech": [
            "deepdeep_web_search",
            "deepdeep_tavily_search",
            "web_search_tavily",
            "*serpapi*",
        ],
        "read": ["deepdeep_web_fetch", "deepseek_fetch_web_page"],
    },
}


@dataclass(slots=True)
class Preference:
    order: dict[str, list[str]] = field(default_factory=dict)
    disabled: dict[str, list[str]] = field(default_factory=dict)

    def is_disabled(self, scene: str, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatchcase(lowered, p) for p in self.disabled.get(scene, []))

    def sort(self, scene: str, names: list[str]) -> list[str]:
        """按这一类的偏好排序（去掉“不用”的）；没写到的保持原来的相对顺序，排在后面。"""
        patterns = self.order.get(scene, [])

        def rank(name: str) -> int:
            lowered = name.lower()
            return next(
                (i for i, p in enumerate(patterns) if fnmatchcase(lowered, p)),
                len(patterns),
            )

        kept = [n for n in names if not self.is_disabled(scene, n)]
        return sorted(kept, key=rank)  # sorted 是稳定排序


def parse_order(lines: Any) -> Preference:
    """["general = a, b, !c", ...] → Preference。写错的行和未知的场景忽略。"""
    pref = Preference()
    if not isinstance(lines, list):
        return pref
    for line in lines:
        if not isinstance(line, str) or "=" not in line:
            continue
        scene, _, items = line.partition("=")
        scene = scene.strip().lower()
        if scene not in SCENES:
            continue
        for raw in items.split(","):
            item = raw.strip().lower()
            if item.startswith("!"):
                if item[1:].strip():
                    pref.disabled.setdefault(scene, []).append(item[1:].strip())
            elif item:
                pref.order.setdefault(scene, []).append(item)
    return pref


def format_order(
    order: dict[str, list[str]], disabled: dict[str, list[str]]
) -> list[str]:
    out = []
    for scene in SCENES:
        items = [*order.get(scene, []), *(f"!{p}" for p in disabled.get(scene, []))]
        if items:
            out.append(f"{scene} = {', '.join(items)}")
    return out


def resolve(preset: Any, lines: Any = None) -> Preference | None:
    """当前生效的偏好；“推荐”返回 None（沿用默认顺序）。"""
    preset = preset if preset in PRESET_LABELS else DEFAULT_PRESET
    if preset == "rec":
        return None
    if preset == "custom":
        return parse_order(lines)
    return Preference({k: list(v) for k, v in PRESETS[preset].items()})
