"""金样测试：分工提示、请求里的工具列表、面板接口的结构，同样的输入必须逐字节不变。

分工提示和工具列表一变，用户就要多付一次提示词缓存未命中的钱，所以这里走真实的
route_search_tools 钩子，比对保存下来的结果。工具名都直接写在这里，不读 core/providers.py，
这样改动 providers.py 时这个测试才算数。

确实要改提示词时才重新生成：DEEPDEEP_UPDATE_GOLDEN=1 python -m pytest tests/test_golden_guide.py
"""

import json
import os
import types
from pathlib import Path

import pytest
from test_plugin import (
    FakeConfig,
    FakeContext,
    FakeEvent,
    FakeToolSet,
    api,
    plugin_module,  # noqa: F401 - pytest 夹具
)

GOLDEN_DIR = Path(__file__).parent / "golden"
GUIDE_FILE = GOLDEN_DIR / "guide_tools.json"
NEW_SERVICES_FILE = GOLDEN_DIR / "guide_tools_new_services.json"
SHAPE_FILE = GOLDEN_DIR / "api_shape.json"
UPDATE = bool(os.environ.get("DEEPDEEP_UPDATE_GOLDEN"))

KEY = {"api_key": "sk-golden-key-123456"}
OWN = ["deepdeep_web_search", "deepdeep_web_fetch", "deepdeep_tavily_search"]
NOT_SEARCH = ["set_user_nickname"]
TAVILY_KEY = {"websearch_tavily_key": ["tvly-golden"]}


def builtin(provider: str, key_field: str) -> dict:
    """AstrBot 内置网页搜索选了这一家，并填了它的 Key。"""
    return {
        "web_search": True,
        "websearch_provider": provider,
        key_field: ["golden-key"],
    }


# 名字 -> (插件配置, AstrBot 的 provider_settings, 已注册的工具, 内置网页搜索加入请求的工具)
SETUPS: dict[str, tuple[dict, dict, list[str], list[str]]] = {
    # ---------- 常见的几种用户 ----------
    "deepseek_tavily": (KEY, TAVILY_KEY, OWN + NOT_SEARCH, []),
    "deepseek_only": (KEY, {}, OWN + NOT_SEARCH, []),
    "no_key_bocha": (
        {},
        builtin("bocha", "websearch_bocha_key"),
        OWN + NOT_SEARCH,
        ["web_search_bocha"],
    ),
    "no_key_searxng": (
        {},
        {},
        [
            *OWN,
            *NOT_SEARCH,
            "searxng_web_search_general",
            "searxng_web_search_images",
            "searxng_web_search_news",
            "searxng_web_search_technical",
            "searxng_web_fetch_url",
        ],
        [],
    ),
    "no_key_brave_exa_plugin": (
        {},
        builtin("brave", "websearch_brave_key"),
        [*OWN, *NOT_SEARCH, "exa-search", "web_fetch_exa"],
        ["web_search_brave"],
    ),
    "no_key_nothing_else": ({}, {}, OWN + NOT_SEARCH, []),
    "own_search_off": (
        {**KEY, "own_tools_enabled": False},
        builtin("serpapi", "websearch_serpapi_key"),
        OWN + NOT_SEARCH,
        [
            "web_search_serpapi",
            "image_search_serpapi",
            "finance_search_serpapi",
            "scholar_search_serpapi",
        ],
    ),
    # ---------- AstrBot 内置的其余几家 ----------
    "no_key_tavily_builtin": (
        {},
        builtin("tavily", "websearch_tavily_key"),
        OWN + NOT_SEARCH,
        ["web_search_tavily", "tavily_extract_web_page"],
    ),
    "no_key_firecrawl": (
        {},
        builtin("firecrawl", "websearch_firecrawl_key"),
        OWN + NOT_SEARCH,
        ["web_search_firecrawl", "firecrawl_extract_web_page"],
    ),
    "no_key_baidu": (
        {},
        builtin("baidu_ai_search", "websearch_baidu_app_builder_key"),
        OWN + NOT_SEARCH,
        ["web_search_baidu"],
    ),
    "no_key_exa_builtin": (
        {},
        builtin("exa", "websearch_exa_key"),
        OWN + NOT_SEARCH,
        ["web_search_exa", "exa_get_contents"],
    ),
    "no_key_anysearch_builtin": (
        {},
        builtin("anysearch", "websearch_anysearch_key"),
        OWN + NOT_SEARCH,
        ["web_search_anysearch"],
    ),
    "own_off_deepseek_builtin": (
        {"own_tools_enabled": False},
        builtin("deepseek", "websearch_deepseek_key"),
        OWN + NOT_SEARCH,
        ["web_search_deepseek", "deepseek_fetch_web_page"],
    ),
    # ---------- 社区插件和 MCP ----------
    "no_key_plugins": (
        {},
        {},
        [
            *OWN,
            *NOT_SEARCH,
            "kimi_web_search",
            "kimi_web_fetch",
            "grok_web_search",
            "grok_web_fetch",
            "gemini_search",
            "mmx_web_search",
            "anysearch_search",
            "anysearch_batch_search",
            "anysearch_extract",
        ],
        [],
    ),
    "deepseek_everything": (
        KEY,
        {
            **TAVILY_KEY,
            **builtin("serpapi", "websearch_serpapi_key"),
        },
        [
            *OWN,
            *NOT_SEARCH,
            "searxng_web_search_general",
            "searxng_web_search_images",
            "searxng_web_fetch_url",
            "kimi_web_search",
            "grok_web_search",
            "gemini_search",
            "exa-search",
            "search_repositories",
            "search_code",
            "search_issues",
            "search_papers",
            "download_paper",
            "astr_kb_search",
            "web_search_deepseek",
            "deepseek_fetch_web_page",
        ],
        [
            "web_search_serpapi",
            "image_search_serpapi",
            "finance_search_serpapi",
            "scholar_search_serpapi",
        ],
    ),
}

# 新认识的工具（升级后只有装了它们的用户提示词会变）。和上面分开保存，上面的金样不能动
NEW_SERVICE_SETUPS: dict[str, tuple[dict, dict, list[str], list[str]]] = {
    "deepseek_keenable": (
        KEY,
        {},
        [*OWN, *NOT_SEARCH, "keenable_web_search", "keenable_fetch_page"],
        [],
    ),
    "no_key_keenable": (
        {},
        {},
        [*OWN, *NOT_SEARCH, "keenable_web_search", "keenable_fetch_page"],
        [],
    ),
    "no_key_unnamed_services": (
        {},
        {},
        [
            *OWN,
            *NOT_SEARCH,
            "pplx_search",
            "tinyfish_search",
            "jina_search",
            "linkup_search",
            "you_search",
            "nimble_search",
            "serper_search",
        ],
        [],
    ),
    "serpapi_with_news": (
        KEY,
        builtin("serpapi", "websearch_serpapi_key"),
        OWN + NOT_SEARCH,
        [
            "web_search_serpapi",
            "news_search_serpapi",
            "image_search_serpapi",
            "finance_search_serpapi",
            "scholar_search_serpapi",
        ],
    ),
}

# (路由模式, 搜索偏好)
VARIANTS = [
    ("guide_only", "rec"),
    ("deepseek_first", "rec"),
    ("deepseek_only", "rec"),
    ("guide_only", "cheap"),
    ("guide_only", "zh"),
]

ROUTES = [
    "status",
    "tools",
    "overview",
    "search-tools",
    "network",
    "search-stats",
    "diagnostics",
]


def make(module, setup: tuple, mode: str = "guide_only", preset: str = "rec"):
    config, settings, registered, _ = setup
    ctx = FakeContext(tool_names=registered)
    ctx._config["provider_settings"].update(settings)
    cfg = FakeConfig({**config, "routing_mode": mode, "search_preference": preset})
    return module.DeepSeekSearchPlugin(ctx, cfg)


async def route_once(module, setup, mode, preset) -> dict:
    plugin = make(module, setup, mode, preset)
    _, _, registered, added = setup
    toolset = FakeToolSet(registered + added)
    req = types.SimpleNamespace(func_tool=toolset, system_prompt="")
    await plugin.route_search_tools(FakeEvent(), req)
    await plugin.terminate()
    return {"tools": toolset.names(), "system_prompt": req.system_prompt}


def paths(value, prefix: str = "") -> set[str]:
    """接口返回值的结构：每个字段的路径和类型（列表里的元素合在一起）。"""
    if isinstance(value, dict):
        if not value:
            return {f"{prefix}:empty-dict"}
        return set().union(*(paths(v, f"{prefix}.{k}") for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        if not value:
            return {f"{prefix}:empty-list"}
        return set().union(*(paths(v, f"{prefix}[]") for v in value))
    return {f"{prefix}:{type(value).__name__}"}


def check_or_update(path: Path, actual: dict) -> None:
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert set(actual) == set(expected)
    for key in expected:
        assert actual[key] == expected[key], key


async def test_guide_and_visible_tools_are_byte_identical(plugin_module):  # noqa: F811
    actual = {}
    for name, setup in SETUPS.items():
        for mode, preset in VARIANTS:
            actual[f"{name} / {mode} / {preset}"] = await route_once(
                plugin_module, setup, mode, preset
            )
    check_or_update(GUIDE_FILE, actual)


async def test_guide_for_newly_recognised_tools(plugin_module):  # noqa: F811
    actual = {}
    for name, setup in NEW_SERVICE_SETUPS.items():
        for mode, preset in VARIANTS:
            actual[f"{name} / {mode} / {preset}"] = await route_once(
                plugin_module, setup, mode, preset
            )
    check_or_update(NEW_SERVICES_FILE, actual)


@pytest.mark.parametrize(
    "name",
    [
        "deepseek_tavily",
        "deepseek_only",
        "no_key_bocha",
        "no_key_searxng",
        "no_key_nothing_else",
        "own_search_off",
    ],
)
async def test_dashboard_api_shape(plugin_module, name):  # noqa: F811
    plugin = make(plugin_module, SETUPS[name])
    actual = {route: sorted(paths(await api(plugin, route)())) for route in ROUTES}
    await plugin.terminate()
    path = SHAPE_FILE.with_name(f"api_shape_{name}.json")
    check_or_update(path, actual)
