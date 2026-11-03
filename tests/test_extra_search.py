"""表驱动的搜索客户端：请求和返回都按各家官方文档的格式模拟，没有访问真实接口。"""

import json
import sys
import types
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_plugin import (
    CURRENT_REQUEST,
    FakeConfig,
    FakeContext,
    FakeEvent,
    FakeToolSet,
    api,
    plugin_module,  # noqa: F401 - pytest 夹具
)

from core import providers, setup_check
from core.extra_search import ExtraClient, ExtraSearchError, parse_results

# 官方文档里的返回样例
RESPONSES = {
    "perplexity": {
        "id": "x",
        "results": [
            {
                "title": "标题",
                "url": "https://a.example/1",
                "snippet": "摘要",
                "date": "2026-10-01",
                "last_updated": None,
            }
        ],
    },
    "tinyfish": {
        "query": "q",
        "results": [
            {
                "position": 1,
                "site_name": "a.example",
                "title": "标题",
                "snippet": "摘要",
                "url": "https://a.example/1",
            }
        ],
        "total_results": 10,
        "page": 0,
    },
    "jina": {
        "code": 200,
        "status": 20000,
        "data": [
            {"title": "标题", "url": "https://a.example/1", "content": "摘要" * 3}
        ],
    },
    "serper": {
        "organic": [
            {
                "title": "标题",
                "link": "https://a.example/1",
                "snippet": "摘要",
                "position": 1,
                "date": "Oct 1, 2026",
            }
        ],
        "relatedSearches": [],
    },
}
AUTH = {
    "perplexity": ("Authorization", "Bearer k-123"),
    "tinyfish": ("X-API-Key", "k-123"),
    "jina": ("Authorization", "Bearer k-123"),
    "serper": ("X-API-KEY", "k-123"),
}
SERVICES = list(RESPONSES)


class FakeServer:
    def __init__(self):
        self.status = 200
        self.body: object = {}
        self.requests = []

    async def handle(self, request):
        self.requests.append(
            {
                "method": request.method,
                "headers": dict(request.headers),
                "query": dict(request.query),
                "json": await request.json() if request.can_read_body else None,
            }
        )
        if isinstance(self.body, str):
            return web.Response(status=self.status, text=self.body)
        return web.json_response(self.body, status=self.status)


@pytest.fixture
async def server():
    fake = FakeServer()
    app = web.Application()
    app.router.add_route("*", "/", fake.handle)
    srv = TestServer(app)
    await srv.start_server()
    fake.url = str(srv.make_url("/"))
    yield fake
    await srv.close()


def record(service):
    return providers.get(service).api


async def run(server, service, count=3, query="查询"):
    client = ExtraClient(timeout=5)
    a = replace(record(service), url=server.url)
    try:
        return await client.search(service, a, "k-123", query, count)
    finally:
        await client.close()


@pytest.mark.parametrize("service", SERVICES)
async def test_request_and_response_follow_the_record(server, service):
    server.body = RESPONSES[service]
    results = await run(server, service)
    assert [r.url for r in results] == ["https://a.example/1"]
    assert results[0].title == "标题" and results[0].snippet.startswith("摘要")
    req = server.requests[0]
    header, value = AUTH[service]
    assert req["headers"][header] == value
    assert req["method"] == record(service).method
    sent = req["json"] if req["method"] == "POST" else req["query"]
    assert sent[record(service).query_field] == "查询"
    # Key 只放在请求头里
    assert "k-123" not in json.dumps({"q": req["query"], "j": req["json"]})


async def test_count_is_sent_where_supported_and_cut_otherwise(server):
    server.body = RESPONSES["perplexity"]
    await run(server, "perplexity", count=4)
    assert server.requests[-1]["json"]["max_results"] == 4
    await run(server, "perplexity", count=500)
    assert server.requests[-1]["json"]["max_results"] == 20
    many = {"results": [dict(RESPONSES["tinyfish"]["results"][0]) for _ in range(9)]}
    server.body = many
    assert len(await run(server, "tinyfish", count=2)) == 2
    assert "count" not in server.requests[-1]["query"]


def test_malformed_responses_give_no_results():
    a = record("perplexity")
    for data in (
        None,
        [],
        "x",
        {},
        {"results": None},
        {"results": ["x", {}, {"url": 1}]},
    ):
        assert parse_results(a, data) == []


async def test_http_errors_become_search_errors(server):
    server.status = 401
    server.body = {"error": "secret detail"}
    with pytest.raises(ExtraSearchError) as err:
        await run(server, "serper")
    assert err.value.status == 401
    assert "secret detail" not in err.value.message
    assert len(server.requests) == 1  # Key 无效不重试
    server.requests.clear()
    server.status = 500
    with pytest.raises(ExtraSearchError):
        await run(server, "serper")
    assert len(server.requests) == 2  # 服务端错误再试一次


async def test_non_json_body_is_a_search_error(server):
    server.body = "<html>not json</html>"
    with pytest.raises(ExtraSearchError):
        await run(server, "jina")


# ---------- 工具注册 ----------


def names(plugin):
    return [t.name for t in plugin.context._tools.func_list]


def make(module, **config):
    return module.DeepSeekSearchPlugin(
        FakeContext(tool_names=["deepdeep_deepseek_search"]), FakeConfig(config)
    )


async def test_tool_registered_only_with_a_key(plugin_module):  # noqa: F811
    plugin = make(plugin_module)
    assert names(plugin) == ["deepdeep_deepseek_search"]
    save = api(plugin, "keys", "POST")
    CURRENT_REQUEST.body = {"service": "perplexity", "key": "pplx-secret-123456"}
    assert (await save())["changed"] is True
    assert "deepdeep_perplexity_search" in names(plugin)
    # 关开关、清 Key 都会拿掉
    CURRENT_REQUEST.body = {"perplexity_enabled": False}
    await api(plugin, "settings", "POST")()
    assert "deepdeep_perplexity_search" not in names(plugin)
    CURRENT_REQUEST.body = {"perplexity_enabled": True}
    await api(plugin, "settings", "POST")()
    assert "deepdeep_perplexity_search" in names(plugin)
    CURRENT_REQUEST.body = {"service": "perplexity", "clear": True}
    await save()
    assert "deepdeep_perplexity_search" not in names(plugin)
    await plugin.terminate()


async def test_tinyfish_is_opt_in(plugin_module):  # noqa: F811
    plugin = make(plugin_module, tinyfish_api_key="tf-key-12345678")
    assert "deepdeep_tinyfish_search" not in names(plugin)
    CURRENT_REQUEST.body = {"tinyfish_enabled": True}
    await api(plugin, "settings", "POST")()
    assert "deepdeep_tinyfish_search" in names(plugin)
    await plugin.terminate()
    # 快速开始里选了它就是要用
    plugin = make(plugin_module)
    CURRENT_REQUEST.body = {
        "service": "tinyfish",
        "key": "tf-key-12345678",
        "enable": True,
    }
    await api(plugin, "keys", "POST")()
    assert "deepdeep_tinyfish_search" in names(plugin)
    await plugin.terminate()


async def test_terminate_removes_tools_and_nothing_registers_twice(plugin_module):  # noqa: F811
    plugin = make(plugin_module, serper_api_key="sp-key-12345678")
    plugin.apply_config()
    plugin.apply_config()
    assert names(plugin).count("deepdeep_serper_search") == 1
    await plugin.terminate()
    assert "deepdeep_serper_search" not in names(plugin)


async def test_registration_failure_does_not_break_the_plugin(plugin_module):  # noqa: F811
    ctx = FakeContext()
    ctx.add_llm_tools = None  # 模拟 AstrBot 改了接口
    plugin = plugin_module.DeepSeekSearchPlugin(
        ctx, FakeConfig({"serper_api_key": "sp-key-12345678"})
    )
    assert "deepdeep_serper_search" not in names(plugin)
    await plugin.terminate()


async def test_tool_call_end_to_end(plugin_module, server, monkeypatch):  # noqa: F811
    plugin = make(plugin_module, jina_api_key="jina-key-12345678")
    plugin.extra_search.client.url_for = lambda _a: server.url
    tool = next(
        t for t in plugin.context._tools.func_list if t.name.endswith("jina_search")
    )
    server.body = RESPONSES["jina"]
    text = await tool.call(None, query="查询", count=3)
    assert "https://a.example/1" in text and "Jina" in text
    # 失败时返回 isError，调用日志和健康检查才知道这次失败了
    monkeypatch.setitem(
        sys.modules,
        "mcp",
        types.SimpleNamespace(
            types=types.SimpleNamespace(
                CallToolResult=types.SimpleNamespace,
                TextContent=types.SimpleNamespace,
            )
        ),
    )
    server.status = 401
    failed = await tool.call(None, query="另一个查询", count=3)
    assert failed.isError is True
    assert "请改用其他搜索工具" in failed.content[0].text
    assert "jina-key" not in failed.content[0].text
    assert plugin.extra_search.status()["jina"]["last_failure"]["status"] == 401
    await plugin.terminate()


async def test_recognised_as_fallback_never_first_choice(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        api_key="sk-x-12345678",
        perplexity_api_key="pplx-secret-123456",
        routing_mode="guide_only",
    )
    toolset = FakeToolSet(
        [
            "deepdeep_deepseek_search",
            "deepdeep_deepseek_fetch",
            "deepdeep_perplexity_search",
        ]
    )
    req = types.SimpleNamespace(func_tool=toolset, system_prompt="")
    await plugin.route_search_tools(FakeEvent(), req)
    assert (
        "优先 deepdeep_deepseek_search；没有结果或结果不理想时再用 deepdeep_perplexity_search"
        in req.system_prompt
    )
    assert plugin.router.search_category("deepdeep_perplexity_search") == "fallback"
    assert not plugin.router.is_same_backend("deepdeep_perplexity_search")
    await plugin.terminate()


async def test_no_secret_in_any_panel_response(plugin_module):  # noqa: F811
    secret = "pplx-secret-123456"
    plugin = make(plugin_module, perplexity_api_key=secret)
    for route in ("status", "overview", "search-tools", "diagnostics", "network"):
        data = await api(plugin, route)()
        assert secret not in json.dumps(data, ensure_ascii=False, default=str), route
    await plugin.terminate()


async def test_monthly_cap_and_panel_entries(plugin_module):  # noqa: F811
    plugin = make(
        plugin_module,
        serper_api_key="sp-key-12345678",
        tool_monthly_limits=["deepdeep_serper_search = 1"],
    )
    name = "deepdeep_serper_search"
    plugin.tool_caps.record(name)
    assert name in plugin.tool_caps.blocked([name])
    data = await api(plugin, "overview")()
    item = next(i for i in data["setup"]["items"] if i["key"] == "extra:serper")
    assert item["status"] == "ok" and "未实测" in item["detail"]
    assert "sp-" in item["detail"] and "12345678" not in item["detail"]
    assert "serper" in [s["service"] for s in data["key_services"]]
    rows = {e["key"]: e for e in (await api(plugin, "search-tools")())["catalog"]}
    assert rows["serper"]["untested"] is True
    assert rows["serper"]["key_input"]["own"] is True
    assert rows["perplexity"]["status"] == "no_key"
    assert plugin.web.fixable([name], {name: True}) == []
    await plugin.terminate()


def test_setup_check_reports_key_failures():
    items = setup_check.check_extra(
        {
            "a": {"service": "a", "name": "A", "tool": "t", "configured": False},
            "b": {
                "service": "b",
                "name": "B",
                "tool": "t",
                "configured": True,
                "enabled": True,
                "last_failure": {"status": 401, "label": "Key 无效", "at": 1},
            },
        }
    )
    assert [i["status"] for i in items] == ["warn"]
