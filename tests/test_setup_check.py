"""配置检查：各搜索工具的 Key 有没有配好。"""

from core import setup_check as sc

BUILTIN_TOOLS = {"serpapi": ["web_search_serpapi"], "tavily": ["web_search_tavily"]}


def test_scan_fields_only_reports_filled_state():
    cfg = {
        "api_key": "",
        "connection_settings": {"api_key": "", "base_url": ""},
        "reverse_image_search": {"serpapi_api_key": "", "image_search_timeout": 30},
        "refresh_token_interval_minutes": 60,
        "searxng_api_url": "http://searxng:8080",
        "github_token": "ghp_x",
    }
    f = sc.scan_fields(cfg)
    assert f["primary"] == [("api_key", False), ("connection_settings.api_key", False)]
    assert ("reverse_image_search.serpapi_api_key", False) in f["secondary"]
    assert ("github_token", True) in f["secondary"]
    assert f["url"] == [("searxng_api_url", True)]
    assert all(isinstance(ok, bool) for group in f.values() for _, ok in group)


def test_plugin_without_key_is_missing():
    # 真实情况：Grok 插件装了但没填 Key
    item = sc.check_plugin(
        "astrbot_plugin_grok_web_search",
        ["grok_web_search"],
        {"api_key": "", "connection_settings": {"api_key": ""}},
    )
    assert item["status"] == "missing" and "API Key" in item["detail"]
    assert "grok_web_search" in item["fix"]


def test_plugin_optional_token_is_only_info():
    item = sc.check_plugin(
        "astrbot_plugin_web_search_pro",
        ["searxng_web_search_general"],
        {"searxng_api_url": "http://s:8080", "github_token": ""},
    )
    assert item["status"] == "info" and "github_token" in item["detail"]


def test_plugin_missing_service_url():
    item = sc.check_plugin(
        "astrbot_plugin_web_search_pro",
        ["searxng_web_search_general"],
        {"searxng_api_url": ""},
    )
    assert item["status"] == "missing" and "searxng_api_url" in item["detail"]


def test_plugin_ok_and_unknown_config():
    assert sc.check_plugin("p", ["t"], {"api_key": "k"})["status"] == "ok"
    assert sc.check_plugin("p", ["t"], {"no_creds": 1})["status"] == "ok"
    assert sc.check_plugin("p", ["t"], None)["status"] == "info"


def test_builtin_checks_the_selected_providers_key():
    ps = {
        "web_search": True,
        "websearch_provider": "serpapi",
        "websearch_serpapi_key": [],
        "websearch_tavily_key": ["t"],
    }
    item = sc.check_builtin(ps, BUILTIN_TOOLS)
    assert (
        item["status"] == "missing"
        and "SerpApi" in item["detail"]
        and item["tools"] == ["web_search_serpapi"]
    )
    ps["websearch_serpapi_key"] = ["s"]
    assert sc.check_builtin(ps, BUILTIN_TOOLS)["status"] == "ok"
    assert sc.check_builtin({"web_search": False}, BUILTIN_TOOLS)["status"] == "info"
    # 内置的 DeepSeek 搜索读自己的 Key，不复用 DeepSeek 提供商
    item = sc.check_builtin(
        {"web_search": True, "websearch_provider": "deepseek"}, BUILTIN_TOOLS
    )
    assert item["status"] == "missing"


def test_deepseek_and_tavily_items():
    assert sc.check_deepseek((None, ""), True)["status"] == "missing"
    assert sc.check_deepseek(("k", "插件配置"), True)["status"] == "ok"
    assert sc.check_deepseek((None, ""), False)["status"] == "info"
    assert sc.check_tavily({"enabled": True, "configured": False})["status"] == "warn"
    assert sc.check_tavily({"enabled": False})["status"] == "info"
    assert (
        sc.check_tavily(
            {
                "enabled": True,
                "configured": True,
                "usable": False,
                "reason": "内置已是 Tavily",
            }
        )["status"]
        == "info"
    )
    assert (
        sc.check_tavily(
            {"enabled": True, "configured": True, "usable": True, "source": "插件配置"}
        )["status"]
        == "ok"
    )


def test_build_sorts_problems_first_and_maps_tools():
    result = sc.build(
        deepseek=sc.check_deepseek(("k", "s"), True),
        tavily=sc.check_tavily({"enabled": True, "configured": False}),
        builtin=sc.check_builtin({"web_search": False}, BUILTIN_TOOLS),
        plugins=[
            sc.check_plugin(
                "astrbot_plugin_grok_web_search", ["grok_web_search"], {"api_key": ""}
            )
        ],
    )
    assert [it["status"] for it in result["items"]] == ["missing", "warn", "info", "ok"]
    assert result["tool_status"] == {
        "grok_web_search": "missing",
        "deepdeep_tavily_search": "warn",
    }
    assert (result["missing"], result["warn"]) == (1, 1)
