"""搜索来源表（core/providers.py）自身的规矩，以及其他模块确实都从这张表读。"""

import ast
import json
import re
import sys
import types
from pathlib import Path

import pytest
from test_plugin import (
    FakeConfig,
    FakeContext,
    plugin_module,  # noqa: F401 - pytest 夹具
)

from core import astrbot_view, catalog, connectivity, preference, providers
from core.router import (
    DEFAULT_FALLBACK_PATTERNS,
    DEFAULT_LANE_RULES,
    DEFAULT_SPECIALIST_RULES,
    ToolRouter,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CORE = PLUGIN_ROOT / "core"


def exact_tools() -> set[str]:
    """表里写死的工具名（不含通配规则）。"""
    names = {t for b in providers.BUILTINS for t in b.tools}
    for entry in providers.CATALOG:
        names |= {p for p in entry.patterns if "*" not in p}
    for net in providers.BACKENDS:
        names |= {p for p in net.patterns if "*" not in p}
    return names


def test_covers_every_astrbot_builtin_provider():
    assert {b.id for b in providers.BUILTINS} >= {
        "tavily",
        "bocha",
        "brave",
        "firecrawl",
        "baidu_ai_search",
        "exa",
        "anysearch",
        "serpapi",
        "deepseek",
    }
    for b in providers.BUILTINS:
        assert b.label and b.tools and re.fullmatch(r"websearch_\w+_key", b.key_field)


def test_records_are_consistent():
    for rows in (
        [p.key for p in providers.PROVIDERS],
        [e.key for e in providers.CATALOG],
        [n.key for n in providers.BACKENDS],
    ):
        assert len(rows) == len(set(rows))
    for entry in providers.CATALOG:
        assert entry.patterns, entry.key
        assert (entry.source == "builtin") == (entry.builtin is not None), entry.key
        assert entry.source in catalog.SOURCE_LABELS, entry.key
        # 用户没装的来源要告诉他们怎么装；自带的和内置的不用装
        if entry.show_when_missing:
            assert bool(entry.install) == (entry.source in ("plugin", "mcp", "kb")), (
                entry
            )
    for net in providers.BACKENDS:
        assert net.kind in connectivity.KIND_LABELS, net.key
        assert net.region in connectivity.REGION_LABELS, net.key


def test_every_known_tool_is_recognised_by_each_module():
    router = ToolRouter()
    for tool in exact_tools():
        assert catalog.entry_for(tool) is not None, tool
        assert router.search_category(tool) is not None, tool


def test_builtin_tools_resolve_to_their_own_row_and_network():
    for entry in providers.CATALOG:
        if entry.builtin is None:
            continue
        for tool in entry.builtin.tools:
            assert catalog.entry_for(tool) is entry, tool
            assert connectivity.match_backend(tool) is not None, tool


def test_new_services_are_recognised_with_default_and_saved_config():
    # 老用户的配置里存的是旧的默认规则，新工具也要认得
    saved = ToolRouter(fallback_patterns=DEFAULT_FALLBACK_PATTERNS[:9])
    for router in (ToolRouter(), saved, ToolRouter(fallback_patterns=["my_tool"])):
        for tool in (
            "pplx_search",
            "perplexity_search",
            "tinyfish_search",
            "tinyfish_fetch",
            "jina_search",
            "keenable_web_search",
            "keenable_fetch_page",
            "keenable_fetch",
            "linkup_search",
            "you_search",
            "youcom_search",
            "nimble_search",
            "serper_search",
            "search_serper",
            "news_search_serpapi",
        ):
            assert router.search_category(tool) == "fallback", tool
    assert ToolRouter().is_fetch_like("keenable_fetch_page")
    assert catalog.entry_for("keenable_fetch_page").key == "keenable"
    assert connectivity.match_backend("keenable_fetch_page").key == "keenable"


def test_unrelated_search_tools_stay_unrecognised():
    router = ToolRouter()
    for tool in (
        "memory_search",
        "history_search",
        "msg_search",
        "message_search",
        "game_search",
        "shop_search",
        "product_search",
        "steam_search",
        "your_memory_search",
        "search_your_notes",
        "you_search_history_of_mine",
        "fetch_group_members",
    ):
        assert router.search_category(tool) is None, tool


def test_user_rules_still_work_and_specialist_rules_win():
    router = ToolRouter(
        fallback_patterns=["memory_search"],
        specialist_rules=["jina_search = 我自己的专用搜索"],
    )
    assert router.search_category("memory_search") == "fallback"
    assert router.search_category("jina_search") == "special"
    assert router.specialty("jina_search") == "我自己的专用搜索"
    assert not router.is_fallback("jina_search")
    # 默认规则下，带专用类别的工具也按专用算
    assert ToolRouter().search_category("tinyfish_image_search") == "special"


def test_serpapi_tools_stay_in_their_roles():
    router = ToolRouter()
    assert router.is_fallback("web_search_serpapi")
    # 新闻和通用搜索互相替代，和 searxng_web_search_news 一样；写成专用会把新闻问题从 DeepSeek 引走
    assert router.is_fallback("news_search_serpapi")
    assert router.specialty("news_search_serpapi") is None
    for tool in (
        "image_search_serpapi",
        "finance_search_serpapi",
        "scholar_search_serpapi",
    ):
        assert router.specialty(tool) is not None, tool
    # 4.29.4 才有的工具：面板要按版本判断，老版本不列
    assert "news_search_serpapi" in providers.BUILTIN_PROVIDER_TOOLS["serpapi"]
    assert providers.BUILTIN_TOOLS_SINCE["news_search_serpapi"] == (4, 29, 4)


def test_schema_defaults_match_the_table():
    """AstrBot 读 _conf_schema.json 的默认值，读不到 Python 里的表，两边要手动保持一致。"""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["fallback_tool_patterns"]["default"] == DEFAULT_FALLBACK_PATTERNS
    assert schema["specialist_rules"]["default"] == DEFAULT_SPECIALIST_RULES
    assert schema["search_lane_rules"]["default"] == DEFAULT_LANE_RULES


def test_preset_tools_exist_in_the_table():
    known = exact_tools()
    for scenes in preference.PRESETS.values():
        for patterns in scenes.values():
            for pattern in patterns:
                if "*" not in pattern:
                    # 只有精确的工具名才查得出写错；通配规则可能匹配表里没列的工具
                    assert pattern in known, pattern


def test_provider_facts_live_only_in_the_table():
    """工具名和 Key 配置项不能再散落到别的模块。preference.py 的预设是跨多家的排序策略，例外。"""
    facts = {t for b in providers.BUILTINS for t in b.tools}
    facts |= {b.key_field for b in providers.BUILTINS}
    facts |= {n.endpoint for n in providers.BACKENDS if n.endpoint}
    for path in sorted(CORE.glob("*.py")):
        if path.name in ("providers.py", "preference.py"):
            continue
        # 只比完整的字符串字面量：说明文字和注释里举例提到某家不算
        literals = {
            node.value
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not literals & facts, path.name


def test_web_api_only_uses_the_declared_interface(plugin_module):  # noqa: F811
    source = (CORE / "web_api.py").read_text(encoding="utf-8")
    used = set(re.findall(r"\bself\.host\.(\w+)", source))
    used |= set(re.findall(r"\bhost\.(\w+)", source))
    declared = re.search(r"class WebHost\(Protocol\):(.*?)\nclass WebApi", source, re.S)
    assert declared
    members = set(re.findall(r"^    (\w+):", declared[1], re.M))
    members |= set(re.findall(r"^    (?:async )?def (\w+)", declared[1], re.M))
    assert used <= members, used - members
    # 不直接碰 AstrBot 的 Context
    assert "context" not in members

    plugin = plugin_module.DeepSeekSearchPlugin(FakeContext(), FakeConfig({}))
    for name in members:
        assert hasattr(plugin, name), name


@pytest.mark.parametrize(
    ("version", "listed"),
    [("4.29.3", False), ("4.29.4", True), ("4.30.0-beta.1", True)],
)
def test_serpapi_news_listed_only_on_versions_that_have_it(
    monkeypatch, version, listed
):
    default = types.ModuleType("astrbot.core.config.default")
    default.VERSION = version
    monkeypatch.setitem(sys.modules, "astrbot.core.config.default", default)
    mgr = types.SimpleNamespace(func_list=[])
    config = {
        "provider_settings": {"web_search": True, "websearch_provider": "serpapi"}
    }
    context = types.SimpleNamespace(
        get_llm_tool_manager=lambda: mgr, get_config=lambda *a, **k: config
    )
    names = astrbot_view.AstrBotView(context).current_tool_names()
    assert "web_search_serpapi" in names
    assert ("news_search_serpapi" in names) is listed
