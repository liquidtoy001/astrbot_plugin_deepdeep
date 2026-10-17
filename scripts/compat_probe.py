"""在 AstrBot 官方镜像里运行，逐项检查 DeepDeep 依赖的 AstrBot 接口是否还在。

用法（容器内）：python compat_probe.py
输出每一项的检查结果；有缺失时退出码为 1。
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import sys

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        ok = bool(fn())
        results.append((name, ok, ""))
    except Exception as exc:
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def has_attrs(module: str, names: list[str]) -> bool:
    mod = importlib.import_module(module)
    missing = [n for n in names if not hasattr(mod, n)]
    if missing:
        raise AttributeError(f"缺少 {missing}")
    return True


def has_methods(obj, names: list[str]) -> bool:
    missing = [n for n in names if not callable(getattr(obj, n, None))]
    if missing:
        raise AttributeError(f"缺少 {missing}")
    return True


# 插件导入的公开接口
check(
    "astrbot.api: AstrBotConfig, logger",
    lambda: has_attrs("astrbot.api", ["AstrBotConfig", "logger"]),
)
# filter 是子包，hasattr 在它被导入前看不到，所以单独导入
check(
    "astrbot.api.event: AstrMessageEvent, filter",
    lambda: (
        has_attrs("astrbot.api.event", ["AstrMessageEvent"])
        and importlib.import_module("astrbot.api.event.filter") is not None
    ),
)
check(
    "astrbot.api.star: Context, Star, StarTools, register",
    lambda: has_attrs("astrbot.api.star", ["Context", "Star", "StarTools", "register"]),
)
check(
    "astrbot.api.provider: ProviderRequest",
    lambda: has_attrs("astrbot.api.provider", ["ProviderRequest"]),
)
check(
    "astrbot.api.web: json/error/file_response, request",
    lambda: has_attrs(
        "astrbot.api.web",
        ["json_response", "error_response", "file_response", "request"],
    ),
)


def _filter_hooks():
    from astrbot.api.event import filter as f

    return has_methods(
        f,
        [
            "llm_tool",
            "command",
            "permission_type",
            "on_llm_request",
            "on_using_llm_tool",
            "on_llm_tool_respond",
            "on_agent_done",
        ],
    ) and hasattr(f, "PermissionType")


check("filter: 工具注册、命令和各个钩子", _filter_hooks)


def _web_query():
    from astrbot.api import web

    # 插件用到 request.query.get(key, default, type=int) 和 request.query.getlist()
    multidict = getattr(web, "PluginMultiDict", None)
    if multidict is None:
        raise LookupError("找不到 PluginMultiDict")
    return "type" in inspect.signature(multidict.get).parameters and callable(
        getattr(multidict, "getlist", None)
    )


check("request.query.get(type=) / getlist", _web_query)


def _context():
    from astrbot.core.star.context import Context

    return has_methods(
        Context,
        ["register_web_api", "get_llm_tool_manager", "get_all_providers", "get_config"],
    )


check("Context: register_web_api 等", _context)


def _register_web_api_signature():
    from astrbot.core.star.context import Context

    params = list(inspect.signature(Context.register_web_api).parameters)
    return params[1:5] == ["route", "view_handler", "methods", "desc"]


check(
    "register_web_api(route, view_handler, methods, desc)", _register_web_api_signature
)


def _star_tools():
    from astrbot.api.star import StarTools

    return callable(getattr(StarTools, "get_data_dir", None))


check("StarTools.get_data_dir", _star_tools)


def _toolset():
    from astrbot.core.agent.tool import ToolSet

    return has_methods(ToolSet, ["add_tool", "remove_tool", "get_tool", "names"])


check("ToolSet: add/remove/get_tool, names", _toolset)


def _event_extras():
    from astrbot.core.platform.astr_message_event import AstrMessageEvent

    return has_methods(AstrMessageEvent, ["set_extra", "get_extra"])


check("AstrMessageEvent.set_extra / get_extra", _event_extras)


def _provider_request():
    from astrbot.api.provider import ProviderRequest

    names = {f.name for f in dataclasses.fields(ProviderRequest)}
    return {"func_tool", "system_prompt"} <= names


check("ProviderRequest.func_tool / system_prompt", _provider_request)


def _provider_keys():
    from astrbot.core.provider.provider import Provider

    return callable(getattr(Provider, "get_keys", None))


check("Provider.get_keys（复用提供商 Key）", _provider_keys)


def _hook_order():
    mod = importlib.import_module(
        "astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal"
    )
    src = inspect.getsource(mod)
    i_hook = src.find("EventType.OnLLMRequestEvent")
    i_reset = src.find("await reset_coro")
    if i_hook == -1:
        raise LookupError("找不到 on_llm_request 的调用")
    return i_reset == -1 or i_hook < i_reset


check("on_llm_request 在 Agent 重置之前触发（路由依赖它）", _hook_order)


def _tool_hooks_fired():
    src = inspect.getsource(importlib.import_module("astrbot.core.astr_agent_hooks"))
    return "OnUsingLLMToolEvent" in src and "OnLLMToolRespondEvent" in src


check("工具调用前后钩子会被触发（调用日志依赖它）", _tool_hooks_fired)


def _pages_dir():
    s = importlib.import_module("astrbot.dashboard.services.plugin_page_service")
    names = getattr(s, "PLUGIN_PAGE_ROOT_DIR_NAMES", None) or (
        getattr(s, "PLUGIN_PAGE_ROOT_DIR_NAME", None),
    )
    return "pages" in names


check("插件页面扫描 pages/ 目录", _pages_dir)


def _llm_tool_docstring():
    # 按 AstrBot 自己的规则解析一个和插件同格式的工具注释
    from astrbot.core.star.register.star_handler import register_llm_tool

    async def deepdeep_probe_tool(event, query: str, count: int = 0):
        """探针工具。

        Args:
            query(string): 搜索关键词
            count(number): 结果数
        """

    register_llm_tool(name="deepdeep_compat_probe")(deepdeep_probe_tool)
    return True


check("llm_tool 注释格式仍可解析", _llm_tool_docstring)

# 输出
width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, err in results:
    failed += not ok
    print(f"{'✓' if ok else '✗'} {name.ljust(width)} {err}")
try:
    from astrbot.core.config.default import VERSION
except Exception:
    VERSION = "unknown"
print(f"\nAstrBot {VERSION}: {len(results) - failed}/{len(results)} 项通过")
sys.exit(1 if failed else 0)
