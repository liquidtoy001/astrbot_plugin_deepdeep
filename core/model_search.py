"""对话模型内置的联网搜索：哪些对话模型自己会搜索、怎么开启的、和搜索工具有什么冲突。

模型内置的搜索发生在模型调用内部，不经过工具，DeepDeep 的路由、调用日志和费用统计都看不到它。
这里只读提供商配置，不发请求、不改配置。对照 AstrBot 4.29（2026-10）的提供商配置：
- Gemini：gm_native_search（Google 搜索）、gm_url_context（读取链接）。AstrBot 的说明是开启原生搜索后函数工具会失效
- xAI：xai_native_search（Live Search，按信息源计费）
- 其余厂商靠「自定义请求体」（custom_extra_body）开启，比如通义千问的 enable_search
- 有些模型本身就带搜索（OpenAI 的 *-search-preview、Perplexity 的 sonar、OpenRouter 的 :online 后缀）
"""

from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from typing import Any

# 模型名规则 -> 说明
MODEL_PATTERNS: list[tuple[str, str]] = [
    ("*search-preview*", "OpenAI 搜索模型（按次额外计费）"),
    ("*search-api*", "OpenAI 搜索模型（按次额外计费）"),
    ("sonar*", "Perplexity Sonar（每次回答都会联网搜索）"),
    ("*/sonar*", "Perplexity Sonar（每次回答都会联网搜索）"),
    ("*:online", "OpenRouter 联网版（按次额外计费）"),
]


@dataclass(slots=True)
class ModelSearch:
    provider_id: str
    model: str
    provider_type: str
    method: str  # gemini / xai / extra_body / model
    label: str
    # tools_broken：开启后函数工具可能失效；extra_cost：额外计费
    tools_broken: bool = False
    extra_cost: bool = False
    url_context: bool = False
    enabled: bool = True
    is_default: bool = False
    # 开启它的配置项，方便告诉用户去哪里关
    config_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def _extra_body_search(body: Any) -> tuple[str, bool] | None:
    """从自定义请求体里认出联网搜索参数，返回 (说明, 是否额外计费)。"""
    if not isinstance(body, dict):
        return None
    if _truthy(body.get("enable_search")):
        return "enable_search（通义千问等）", True
    if body.get("search_parameters"):
        mode = (
            (body["search_parameters"] or {}).get("mode")
            if isinstance(body["search_parameters"], dict)
            else ""
        )
        if mode != "off":
            return "search_parameters（xAI Live Search）", True
    if body.get("web_search_options") is not None:
        return "web_search_options（OpenAI 搜索模型）", True
    web = body.get("web_search")
    if (isinstance(web, dict) and _truthy(web.get("enable", True))) or web is True:
        return "web_search", True
    plugins = body.get("plugins")
    if isinstance(plugins, list) and any(
        isinstance(p, dict) and p.get("id") == "web" for p in plugins
    ):
        return "plugins: web（OpenRouter）", True
    tools = body.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if not isinstance(t, dict):
                continue
            kind = str(t.get("type") or "")
            name = str((t.get("function") or {}).get("name") or "")
            if kind.startswith("web_search") or kind == "google_search":
                return f"tools: {kind}", True
            if name == "$web_search":
                return "tools: $web_search（Kimi）", True
    return None


def detect(
    provider_config: dict[str, Any], model: str = "", default_id: str = ""
) -> ModelSearch | None:
    """一个对话模型提供商有没有开启内置联网搜索。"""
    pc = provider_config or {}
    pid = str(pc.get("id") or "")
    ptype = str(pc.get("type") or "")
    model = model or str(pc.get("model") or "")
    base = {
        "provider_id": pid,
        "model": model,
        "provider_type": ptype,
        "enabled": _truthy(pc.get("enable", True)),
        "is_default": bool(pid) and pid == default_id,
    }
    if _truthy(pc.get("gm_native_search")):
        return ModelSearch(
            **base,
            method="gemini",
            label="Gemini 原生搜索（Google 搜索）",
            tools_broken=True,
            url_context=_truthy(pc.get("gm_url_context")),
            config_keys=["gm_native_search"],
        )
    if _truthy(pc.get("xai_native_search")):
        return ModelSearch(
            **base,
            method="xai",
            label="xAI Live Search（按信息源计费）",
            extra_cost=True,
            config_keys=["xai_native_search"],
        )
    found = _extra_body_search(pc.get("custom_extra_body"))
    if found:
        return ModelSearch(
            **base,
            method="extra_body",
            label=f"自定义请求体：{found[0]}",
            extra_cost=found[1],
            config_keys=["custom_extra_body"],
        )
    lowered = model.lower()
    for pattern, desc in MODEL_PATTERNS:
        if fnmatch.fnmatchcase(lowered, pattern):
            return ModelSearch(**base, method="model", label=desc, extra_cost=True)
    if _truthy(pc.get("gm_url_context")):
        # 只开了读取链接：不算联网搜索，但能读网页，和抓取工具重复
        return ModelSearch(
            **base,
            method="gemini_url",
            label="Gemini 读取链接（URL Context）",
            url_context=True,
            config_keys=["gm_url_context"],
        )
    return None


def scan(providers: list[Any], default_id: str = "") -> list[ModelSearch]:
    """所有对话模型里开启了内置联网搜索的，默认对话模型排在最前。"""
    out = []
    for provider in providers:
        pc = getattr(provider, "provider_config", None) or {}
        try:
            model = provider.get_model() or ""
        except Exception:  # noqa: BLE001 - 读不到模型名时用配置里的
            model = ""
        found = detect(pc, str(model), default_id)
        if found is not None:
            out.append(found)
    out.sort(key=lambda m: (not m.is_default, not m.enabled, m.provider_id))
    return out
