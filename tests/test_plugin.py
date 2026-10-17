"""在不安装 AstrBot 的情况下加载 main.py：用桩模块替代 astrbot.api，
并按 AstrBot 4.29 的规则（astrbot/core/star/register/star_handler.py）校验工具注释能被解析。"""

import importlib
import json
import sys
import types
from pathlib import Path

import docstring_parser
import pytest
import yaml
from conftest import make_result

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 与 AstrBot 4.29 register_llm_tool 一致
SUPPORTED_TYPES = {"string", "number", "object", "array", "boolean"}
PY_TO_JSON_TYPE = {
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "str": "string",
    "dict": "object",
    "list": "array",
    "tuple": "array",
    "set": "array",
}

registered_tools: dict[str, object] = {}


class FakeQuery(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        if type is not None and value is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class FakeRequest:
    def __init__(self):
        self.query = FakeQuery()
        self.body = None

    async def json(self, default=None):
        return default if self.body is None else self.body


CURRENT_REQUEST = FakeRequest()


class ErrorResponse(dict):
    pass


def _install_astrbot_stubs():
    def llm_tool(name=None, **_):
        def deco(fn):
            registered_tools[name or fn.__name__] = fn
            return fn

        return deco

    def passthrough(*_a, **_k):
        return lambda fn: fn

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    filter_mod = types.SimpleNamespace(
        llm_tool=llm_tool,
        command=passthrough,
        permission_type=passthrough,
        on_llm_request=passthrough,
        on_using_llm_tool=passthrough,
        on_llm_tool_respond=passthrough,
        on_agent_done=passthrough,
        PermissionType=PermissionType,
    )

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        data_dir: Path | None = None

        @classmethod
        def get_data_dir(cls, name=None):
            return cls.data_dir

    noop = types.SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = noop
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = filter_mod
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = Star
    star.StarTools = StarTools
    star.register = passthrough
    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderRequest = object
    web = types.ModuleType("astrbot.api.web")
    web.request = CURRENT_REQUEST
    web.json_response = lambda data=None, **_: data
    web.file_response = lambda path, filename=None, content_type=None, **_: {
        "file": str(path),
        "filename": filename,
        "content_type": content_type,
    }
    web.error_response = lambda message, status_code=400, **_: ErrorResponse(
        error=message, status_code=status_code
    )
    sys.modules.update(
        {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.provider": provider,
            "astrbot.api.web": web,
        }
    )
    return StarTools


@pytest.fixture
def plugin_module(tmp_path):
    star_tools = _install_astrbot_stubs()
    star_tools.data_dir = tmp_path
    CURRENT_REQUEST.query.clear()
    CURRENT_REQUEST.body = None
    # main.py 使用相对导入，需要作为包加载
    pkg_name = "astrbot_plugin_deepdeep"
    parent = str(PLUGIN_ROOT.parent)
    sys.path.insert(0, parent)
    for mod in [m for m in sys.modules if m.startswith(pkg_name)]:
        del sys.modules[mod]
    try:
        yield importlib.import_module(f"{pkg_name}.main")
    finally:
        sys.path.remove(parent)


class FakeConfig(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saved = 0

    async def save_config_async(self):
        self.saved += 1


class FakeProvider:
    def __init__(self, api_base, keys, pid="p"):
        self.provider_config = {"api_base": api_base, "id": pid}
        self._keys = keys

    def get_keys(self):
        return self._keys


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.active = True


class FakeToolSet:
    def __init__(self, names):
        self.tools = [FakeTool(n) for n in names]

    def names(self):
        return [t.name for t in self.tools]

    def get_tool(self, name):
        return next((t for t in self.tools if t.name == name), None)

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]

    def add_tool(self, tool):
        self.remove_tool(tool.name)
        self.tools.append(tool)


class FakeContext:
    def __init__(self, providers=(), tool_names=(), web_search_provider=None):
        self._providers = list(providers)
        self.web_apis = {}
        self._tools = types.SimpleNamespace(func_list=[FakeTool(n) for n in tool_names])
        self._config = {
            "provider_settings": {
                "web_search": web_search_provider is not None,
                "websearch_provider": web_search_provider or "",
            }
        }

    def get_all_providers(self):
        return self._providers

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis[(route, methods[0])] = handler

    def get_llm_tool_manager(self):
        return self._tools

    def get_config(self, umo=None):
        return self._config


class FakeEvent:
    def __init__(self, text=""):
        self.message_str = text
        self.extras = {}

    def plain_result(self, text):
        return text

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        return self.extras.get(key, default)


SEARCH_TOOLS = [
    "deepdeep_web_search",
    "deepdeep_web_fetch",
    "web_search_tavily",
    "tavily_extract_web_page",
    "searxng_web_search_general",
    "searxng_web_search_images",
    "searxng_web_fetch_url",
    "set_user_nickname",
]


def make_plugin(module, fake_api=None, providers=(), **config):
    # 大部分路由测试针对“隐藏同类工具”的模式；只引导模式另有专门的测试
    config.setdefault("routing_mode", "deepseek_first")
    cfg = FakeConfig({"api_key": "sk-test-key-123456", **config})
    ctx = FakeContext(
        providers,
        tool_names=SEARCH_TOOLS[:2] + SEARCH_TOOLS[4:],
        web_search_provider="tavily",
    )
    plugin = module.DeepSeekSearchPlugin(ctx, cfg)
    if fake_api is not None:
        plugin.service.client._base_url = fake_api.base_url
        plugin.service.client._sleep = _instant
        plugin.service.client._backoff_base = 0
    return plugin


async def _instant(_):
    return None


def api(plugin, route, method="GET"):
    return plugin.context.web_apis[(f"/astrbot_plugin_deepdeep/{route}", method)]


# ---------- 注册与元数据 ----------


def test_tool_docstrings_parse_like_astrbot(plugin_module):
    assert set(registered_tools) == {"deepdeep_web_search", "deepdeep_web_fetch"}
    for name, fn in registered_tools.items():
        doc = docstring_parser.parse(fn.__doc__)
        assert doc.description and len(doc.description) > 20, name
        params = {
            p.arg_name: PY_TO_JSON_TYPE.get(p.type_name, p.type_name)
            for p in doc.params
        }
        assert params and all(t in SUPPORTED_TYPES for t in params.values()), (
            name,
            params,
        )
    search_params = [
        p.arg_name
        for p in docstring_parser.parse(
            registered_tools["deepdeep_web_search"].__doc__
        ).params
    ]
    assert search_params == ["query", "count"]


def test_metadata_schema_view_and_i18n_are_consistent(plugin_module):
    meta = yaml.safe_load((PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    for field in ("name", "author", "desc", "version", "repo"):
        assert meta.get(field), field
    assert meta["name"] == PLUGIN_ROOT.name == plugin_module.PLUGIN_NAME
    assert meta["version"] == plugin_module.PLUGIN_VERSION

    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    for key, spec in schema.items():
        assert spec["type"] in {"string", "int", "float", "bool", "list"}, key
        assert "description" in spec and "default" in spec, key
        if "options" in spec:
            assert spec["default"] in spec["options"], key
            assert len(spec.get("labels", spec["options"])) == len(spec["options"]), key

    assert (PLUGIN_ROOT / "pages" / "dashboard" / "index.html").exists()
    for locale in ("zh-CN", "en-US"):
        data = json.loads(
            (PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        assert data["views"]["dashboard"]["title"]
        assert data["pages"] == data["views"]  # 4.26–4.28 读 "pages"
        for key in data.get("config", {}):
            assert key in schema, (locale, key)

    changelog = (PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {meta['version']}" in changelog


def test_api_key_resolution(plugin_module):
    P = plugin_module.DeepSeekSearchPlugin
    ctx = FakeContext(
        [
            FakeProvider("https://opencode.ai/zen/v1", ["sk-other"]),
            FakeProvider(
                "https://api.deepseek.com/v1", ["", "sk-deepseek"], "deepseek"
            ),
        ]
    )
    plugin = P(ctx, FakeConfig({"api_key": ""}))
    assert plugin.resolve_key_source() == ("sk-deepseek", "复用提供商 deepseek")
    assert P(ctx, FakeConfig({"api_key": " sk-own "}))._resolve_api_key() == "sk-own"
    assert (
        P(
            FakeContext([FakeProvider("https://opencode.ai", ["x"])]), FakeConfig()
        )._resolve_api_key()
        is None
    )


# ---------- 工具调用 ----------


async def test_tools_end_to_end(plugin_module, fake_api, tmp_path):
    plugin = make_plugin(plugin_module, fake_api)
    fake_api.search_queue.append((200, {"results": [make_result(1)]}, {}))
    fake_api.fetch_queue.append((200, {"title": "T", "content": "body"}, {}))

    text = await plugin.deepdeep_web_search(
        FakeEvent("沙沙帮我查下 DeepSeek"), "DeepSeek", "3"
    )
    page = await plugin.deepdeep_web_fetch(FakeEvent(), "https://example.com/1")
    assert "标题1" in text and "body" in page
    assert fake_api.requests[0]["json"]["count"] == 3
    assert fake_api.requests[0]["json"]["ori_question"] == "沙沙帮我查下 DeepSeek"

    stats = [r async for r in plugin.deepdeep_command(FakeEvent(), "stats")]
    assert "计费 1 次" in stats[0]
    assert (tmp_path / "usage.json").exists()

    fake_api.search_queue.append((200, {"results": [make_result(9)]}, {}))
    test = [r async for r in plugin.deepdeep_command(FakeEvent(), "test")]
    assert test[0].startswith("✅") and "标题9" in test[0]
    await plugin.terminate()


async def test_dsearch_test_without_key(plugin_module):
    plugin = plugin_module.DeepSeekSearchPlugin(FakeContext(), FakeConfig())
    out = [r async for r in plugin.deepdeep_command(FakeEvent(), "test")]
    assert "未找到 DeepSeek API Key" in out[0]
    await plugin.terminate()


# ---------- 路由 ----------


async def test_route_hides_fallback_tools_when_deepseek_available(plugin_module):
    plugin = make_plugin(plugin_module)
    toolset = FakeToolSet(SEARCH_TOOLS)
    req = types.SimpleNamespace(func_tool=toolset)
    await plugin.route_search_tools(FakeEvent(), req)
    assert toolset.names() == [
        "deepdeep_web_search",
        "deepdeep_web_fetch",
        "searxng_web_search_images",  # 图片搜索保留
        "set_user_nickname",
    ]
    await plugin.terminate()


async def test_route_uses_fallback_when_budget_exceeded(plugin_module):
    plugin = make_plugin(plugin_module, daily_budget=0.006)
    plugin.service.stats.record("search", "billable")  # 已花 ¥0.005，再来一次就超
    toolset = FakeToolSet(SEARCH_TOOLS)
    await plugin.route_search_tools(
        FakeEvent(), types.SimpleNamespace(func_tool=toolset)
    )
    assert "deepdeep_web_search" not in toolset.names()
    assert "web_search_tavily" in toolset.names()
    await plugin.terminate()


async def test_failure_mid_conversation_restores_fallback(plugin_module, fake_api):
    plugin = make_plugin(plugin_module, fake_api, max_retries=0)
    fake_api.search_queue.append(
        (402, {"error": {"message": "Insufficient Balance"}}, {})
    )
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent("查一下")
    await plugin.route_search_tools(event, types.SimpleNamespace(func_tool=toolset))
    assert "searxng_web_search_general" not in toolset.names()

    text = await plugin.deepdeep_web_search(event, "x")
    assert "余额不足" in text and "已为本次对话启用备用搜索工具" in text
    names = toolset.names()
    assert "searxng_web_search_general" in names and "web_search_tavily" in names
    assert "deepdeep_web_search" not in names  # 服务不可用，撤掉 DeepSeek 工具
    # 402 直接熔断，下一轮请求开始就走备用
    assert not plugin.service.availability().ok
    await plugin.terminate()


async def test_page_specific_fetch_failure_keeps_deepseek_tools(
    plugin_module, fake_api
):
    plugin = make_plugin(plugin_module, fake_api)
    fake_api.fetch_queue.append((404, {"error": {"message": "no content"}}, {}))
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent()
    await plugin.route_search_tools(event, types.SimpleNamespace(func_tool=toolset))
    text = await plugin.deepdeep_web_fetch(event, "https://example.com/x")
    assert "searxng_web_fetch_url" in text and "再读取一次" in text
    assert "web_search_tavily" not in text  # 抓取失败不推荐搜索工具
    assert (
        "deepdeep_web_search" in toolset.names()
        and "searxng_web_fetch_url" in toolset.names()
    )
    assert plugin.service.availability().ok  # 404 不计入熔断
    await plugin.terminate()


async def test_deepseek_only_mode_never_falls_back(plugin_module, fake_api):
    plugin = make_plugin(
        plugin_module, fake_api, routing_mode="deepseek_only", max_retries=0
    )
    fake_api.search_queue.append((500, {"error": {"message": "x"}}, {}))
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent()
    await plugin.route_search_tools(event, types.SimpleNamespace(func_tool=toolset))
    text = await plugin.deepdeep_web_search(event, "x")
    assert "联网搜索暂时不可用" in text
    assert "web_search_tavily" not in toolset.names()
    await plugin.terminate()


# ---------- WebUI 接口 ----------


async def test_web_status_and_usage(plugin_module):
    plugin = make_plugin(plugin_module, monthly_budget=1)
    plugin.service.stats.record("search", "billable")
    status = await api(plugin, "status")()
    assert status["available"] is True
    assert status["key"] == {
        "configured": True,
        "masked": "sk-****3456",
        "source": "插件配置",
    }
    assert status["settings"]["routing_mode"] == "deepseek_first"
    assert status["budget"]["monthly_limit"] == 1
    assert status["budget"]["currency"] == "cny" and status["budget"]["symbol"] == "¥"
    assert "sk-test-key-123456" not in json.dumps(status)

    CURRENT_REQUEST.query.update(days="7")
    usage = await api(plugin, "usage")()
    assert len(usage["days"]) == 7 and usage["days"][-1]["search"]["billable"] == 1
    await plugin.terminate()


async def test_web_tools_preview_includes_builtin_and_unsaved_patterns(plugin_module):
    plugin = make_plugin(plugin_module)
    tools = {t["name"]: t for t in (await api(plugin, "tools")())["tools"]}
    assert tools["web_search_tavily"]["role"] == "fallback"  # 来自内置网页搜索配置
    assert tools["web_search_tavily"]["visible_when_ok"] is False
    assert tools["web_search_tavily"]["visible_when_down"] is True
    assert tools["searxng_web_search_images"]["role"] == "specialist"
    assert tools["searxng_web_search_images"]["specialty"] == "找图片"

    CURRENT_REQUEST.query.update(fallback="set_user_*", specialists="")
    preview = {t["name"]: t for t in (await api(plugin, "tools")())["tools"]}
    assert preview["set_user_nickname"]["role"] == "fallback"
    await plugin.terminate()


async def test_web_save_settings_validates_persists_and_applies(plugin_module):
    plugin = make_plugin(plugin_module)
    save = api(plugin, "settings", "POST")

    CURRENT_REQUEST.body = {"routing_mode": "nope"}
    assert "status_code" in await save()
    CURRENT_REQUEST.body = {"daily_budget": -1}
    assert "status_code" in await save()
    CURRENT_REQUEST.body = {"record_queries": "yes"}
    assert "status_code" in await save()
    assert plugin.config.saved == 0

    CURRENT_REQUEST.body = {
        "routing_mode": "fallback_only",
        "daily_budget": 0.5,
        "fallback_tool_patterns": [" web_search_* ", ""],
        "exchange_rate": 7.2,
        "billing_currency": "usd",
        "api_key": "should-be-ignored",
    }
    status = await save()
    assert plugin.config.saved == 1
    assert plugin.config["api_key"] == "sk-test-key-123456"
    assert status["settings"]["routing_mode"] == "fallback_only"
    assert status["settings"]["fallback_tool_patterns"] == ["web_search_*"]
    assert plugin.router.mode == "fallback_only"
    assert plugin.service.budget.daily_limit == 0.5
    assert status["settings"]["exchange_rate"] == 7.2 and plugin.usd_cny_rate == 7.2
    assert status["settings"]["billing_currency"] == "usd"
    assert abs(plugin.service.stats.price - 0.00075) < 1e-12
    CURRENT_REQUEST.body = {"billing_currency": "eur"}
    assert "status_code" in await save()
    await plugin.terminate()


async def test_web_test_and_reset_circuit(plugin_module, fake_api):
    plugin = make_plugin(plugin_module, fake_api, max_retries=0)
    fake_api.search_queue.append((401, {"error": {"message": "bad key"}}, {}))
    r = await api(plugin, "test", "POST")()
    assert r["ok"] is False

    plugin.service.breaker.record_failure(402, "余额不足")
    assert (await api(plugin, "status")())["circuit"]["open"] is True
    status = await api(plugin, "reset-circuit", "POST")()
    assert status["circuit"]["open"] is False and status["available"] is True

    activity = await api(plugin, "activity")()
    assert activity["items"][0]["target"] == "[连接测试]"
    await plugin.terminate()


# ---------- 调试信息与调用日志 ----------


async def test_fallback_is_annotated_on_activity(plugin_module, fake_api):
    plugin = make_plugin(plugin_module, fake_api, max_retries=0)
    fake_api.search_queue.append((500, {"error": {"message": "x"}}, {}))
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent()
    await plugin.route_search_tools(event, types.SimpleNamespace(func_tool=toolset))
    text = await plugin.deepdeep_web_search(event, "q")
    assert text.startswith("[DeepSeek 搜索 #")
    detail = plugin.service.activity.recent(1)[0]["detail"]
    assert detail["fallback"]["restored_tools"]
    assert detail["fallback"]["deepseek_tools_removed"] is True
    await plugin.terminate()


async def test_diagnostics_has_no_secret(plugin_module, fake_api):
    plugin = make_plugin(plugin_module, fake_api, max_retries=0)
    fake_api.search_queue.append((401, {"error": {"message": "bad key"}}, {}))
    # 按 AstrBot 的顺序触发：调用前钩子 -> 工具 -> 调用后钩子
    event, tool = FakeEvent(), FakeTool("deepdeep_web_search")
    await plugin.record_tool_start(event, tool, {"query": "q"})
    text = await plugin.deepdeep_web_search(event, "q")
    result = types.SimpleNamespace(
        content=[types.SimpleNamespace(text=text)], isError=False
    )
    await plugin.record_tool_end(event, tool, {"query": "q"}, result)
    diag = await api(plugin, "diagnostics")()
    dumped = json.dumps(diag, ensure_ascii=False)
    assert "sk-test-key-123456" not in dumped
    assert diag["plugin_version"] == plugin_module.PLUGIN_VERSION
    assert diag["recent_problems"][0]["http_status"] == 401
    assert diag["recent_problems"][0]["status"] == "error"
    assert diag["call_log"]["total"] == 1
    assert {t["name"] for t in diag["search_tools"]} >= {
        "deepdeep_web_search",
        "web_search_tavily",
    }
    await plugin.terminate()


async def test_call_log_web_endpoints(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, call_log_record_other_content=True)
    event = FakeEvent()
    for name, text in (("web_search_tavily", "ok"), ("searxng_web_fetch_url", "fail")):
        t = FakeTool(name)
        await plugin.record_tool_start(event, t, {"q": name})
        res = types.SimpleNamespace(
            content=[types.SimpleNamespace(text=text)], isError=text == "fail"
        )
        await plugin.record_tool_end(event, t, {"q": name}, res)

    CURRENT_REQUEST.query.update(status="problems", range="24h")
    data = await api(plugin, "calls")()
    assert data["total"] == 1 and data["items"][0]["tool"] == "searxng_web_fetch_url"
    detail = await api(plugin, "call/<call_id>")(str(data["items"][0]["id"]))
    assert (
        detail["args"] == {"q": "searxng_web_fetch_url"} and detail["result"] == "fail"
    )
    assert "status_code" in await api(plugin, "call/<call_id>")("abc")
    assert (await api(plugin, "call-facets")())["tools"]

    CURRENT_REQUEST.query.clear()
    exported = await api(plugin, "call-export")()
    lines = Path(exported["file"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["tool"] == "web_search_tavily"

    assert (await api(plugin, "call-clear", "POST")())["removed"] == 2
    assert (await api(plugin, "calls")())["total"] == 0
    await plugin.terminate()


async def test_call_log_settings_editable(plugin_module):
    plugin = make_plugin(plugin_module)
    assert plugin.recorder.record_other_content is False  # 默认只记元数据
    CURRENT_REQUEST.body = {
        "call_log_record_other_content": True,
        "call_log_retention_days": 30,
    }
    status = await api(plugin, "settings", "POST")()
    assert status["settings"]["call_log_record_other_content"] is True
    assert plugin.call_log.retention_days == 30
    await plugin.terminate()


async def test_search_stats_endpoint_only_counts_search_tools(plugin_module):
    plugin = make_plugin(plugin_module)
    event = FakeEvent()
    for name in (
        "web_search_tavily",
        "web_search_tavily",
        "searxng_web_search_images",
        "set_user_nickname",
    ):
        t = FakeTool(name)
        await plugin.record_tool_start(event, t, {})
        res = types.SimpleNamespace(
            content=[types.SimpleNamespace(text="r")], isError=False
        )
        await plugin.record_tool_end(event, t, {}, res)
    CURRENT_REQUEST.query.update(range="7d")
    data = await api(plugin, "search-stats")()
    rows = {r["tool"]: r for r in data["rows"]}
    assert set(rows) == {"web_search_tavily", "searxng_web_search_images"}
    assert (
        rows["web_search_tavily"]["total"] == 2
        and rows["web_search_tavily"]["category"] == "fallback"
    )
    assert rows["searxng_web_search_images"]["category"] == "special"
    assert data["total"] == 3
    report = ""
    async for r in plugin.deepdeep_command(FakeEvent(), "stats"):
        report = r
    assert "web_search_tavily 2 次" in report
    await plugin.terminate()


async def test_route_injects_search_guide(plugin_module):
    plugin = make_plugin(plugin_module)
    toolset = FakeToolSet(SEARCH_TOOLS)
    req = types.SimpleNamespace(func_tool=toolset, system_prompt="你叫沙沙。")
    await plugin.route_search_tools(FakeEvent(), req)
    assert req.system_prompt.startswith("你叫沙沙。")
    assert "【搜索工具分工】" in req.system_prompt
    assert (
        "通用网页搜索（新闻、事实、中文资料等）：deepdeep_web_search"
        in req.system_prompt
    )
    assert "- 找图片：searxng_web_search_images" in req.system_prompt
    assert "web_search_tavily" not in req.system_prompt  # 被隐藏的工具不出现

    # 同样的工具集，提示完全相同（不破坏提示词缓存）
    req2 = types.SimpleNamespace(
        func_tool=FakeToolSet(SEARCH_TOOLS), system_prompt="你叫沙沙。"
    )
    await plugin.route_search_tools(FakeEvent(), req2)
    assert req2.system_prompt == req.system_prompt

    # 关闭后不注入；请求里没有本插件工具时也不注入
    off = make_plugin(plugin_module, inject_search_guide=False)
    req3 = types.SimpleNamespace(func_tool=FakeToolSet(SEARCH_TOOLS), system_prompt="x")
    await off.route_search_tools(FakeEvent(), req3)
    assert req3.system_prompt == "x"
    req4 = types.SimpleNamespace(
        func_tool=FakeToolSet(["web_search_tavily", "searxng_web_search_images"]),
        system_prompt="x",
    )
    await plugin.route_search_tools(FakeEvent(), req4)
    assert req4.system_prompt == "x"
    for p_ in (plugin, off):
        await p_.terminate()


# ---------- 只引导模式（默认） ----------


async def test_guide_only_is_default_and_never_hides(plugin_module):
    plugin = plugin_module.DeepSeekSearchPlugin(
        FakeContext(), FakeConfig({"api_key": "sk-test-key-123456"})
    )
    assert plugin.router.mode == "guide_only"
    toolset = FakeToolSet(SEARCH_TOOLS)
    req = types.SimpleNamespace(func_tool=toolset, system_prompt="")
    await plugin.route_search_tools(FakeEvent(), req)
    assert toolset.names() == SEARCH_TOOLS  # 一个都不藏
    assert (
        "通用网页搜索（新闻、事实、中文资料等）：优先 deepdeep_web_search；"
        "没有结果或结果不理想时再用 searxng_web_search_general、web_search_tavily"
    ) in req.system_prompt
    assert (
        "读取网页全文：优先 deepdeep_web_fetch；读取失败时再用 searxng_web_fetch_url、tavily_extract_web_page"
        in req.system_prompt
    )

    # DeepSeek 不可用时工具列表和提示都不变（保持提示词缓存）
    plugin.service.breaker.record_failure(402, "余额不足")
    toolset2 = FakeToolSet(SEARCH_TOOLS)
    req2 = types.SimpleNamespace(func_tool=toolset2, system_prompt="")
    await plugin.route_search_tools(FakeEvent(), req2)
    assert toolset2.names() == SEARCH_TOOLS and req2.system_prompt == req.system_prompt
    await plugin.terminate()


async def test_guide_only_failure_suggests_alternatives(plugin_module, fake_api):
    plugin = make_plugin(
        plugin_module, fake_api, routing_mode="guide_only", max_retries=0
    )
    fake_api.search_queue.append((500, {"error": {"message": "x"}}, {}))
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent()
    await plugin.route_search_tools(
        event, types.SimpleNamespace(func_tool=toolset, system_prompt="")
    )
    text = await plugin.deepdeep_web_search(event, "q")
    assert "请改用 searxng_web_search_general、web_search_tavily 继续完成任务" in text
    assert toolset.names() == SEARCH_TOOLS  # 不改工具列表
    detail = plugin.service.activity.recent(1)[0]["detail"]
    assert detail["fallback"]["suggested_tools"] == [
        "searxng_web_search_general",
        "web_search_tavily",
    ]
    await plugin.terminate()


async def test_guide_only_budget_excludes_deepseek_billed(plugin_module):
    plugin = make_plugin(plugin_module, routing_mode="guide_only", daily_budget=0.004)
    names = [*SEARCH_TOOLS, "web_search_deepseek"]
    toolset = FakeToolSet(names)
    event = FakeEvent()
    await plugin.route_search_tools(
        event, types.SimpleNamespace(func_tool=toolset, system_prompt="")
    )
    text = await plugin.deepdeep_web_search(event, "q")  # 上限低于单价，直接拦截
    assert "已达到今日费用上限" in text
    assert "web_search_deepseek" not in text.split("请改用", 1)[1].split("继续", 1)[0]
    assert "不要改用其他同样走 DeepSeek 计费的工具" in text
    await plugin.terminate()


async def test_empty_and_low_quality_results_suggest_other_tools(
    plugin_module, fake_api
):
    plugin = make_plugin(
        plugin_module, fake_api, routing_mode="guide_only", min_rerank_score=1.0
    )
    fake_api.search_queue += [
        (200, {"results": []}, {}),
        (200, {"results": [make_result(1, score=0.3), make_result(2, score=0.2)]}, {}),
        (200, {"results": [make_result(3, score=2.0)]}, {}),
    ]
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent()
    await plugin.route_search_tools(
        event, types.SimpleNamespace(func_tool=toolset, system_prompt="")
    )
    empty = await plugin.deepdeep_web_search(event, "冷门问题")
    low = await plugin.deepdeep_web_search(event, "模糊问题")
    good = await plugin.deepdeep_web_search(event, "清楚的问题")
    hint = "如果结果不够好，可以改用 searxng_web_search_general、web_search_tavily 再搜一次"
    assert hint in empty and hint in low and hint not in good
    assert plugin.service.availability().ok  # 不算故障，不触发熔断
    await plugin.terminate()


async def test_hide_mode_soft_fallback_keeps_own_tools(plugin_module, fake_api):
    plugin = make_plugin(plugin_module, fake_api)  # deepseek_first
    fake_api.search_queue.append((200, {"results": []}, {}))
    toolset = FakeToolSet(SEARCH_TOOLS)
    event = FakeEvent()
    await plugin.route_search_tools(
        event, types.SimpleNamespace(func_tool=toolset, system_prompt="")
    )
    text = await plugin.deepdeep_web_search(event, "冷门问题")
    assert "已为本次对话启用" in text
    names = toolset.names()
    assert "deepdeep_web_search" in names and "web_search_tavily" in names
    await plugin.terminate()
