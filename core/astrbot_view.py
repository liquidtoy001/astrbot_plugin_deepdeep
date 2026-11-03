"""DeepDeep 看到的 AstrBot：配置、已注册的工具、对话模型。core/ 里只有这里直接用 Context。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from astrbot.api import logger

from . import connectivity, model_search
from .providers import BUILTIN_PROVIDER_TOOLS, BUILTIN_TOOLS_SINCE


def _astrbot_version() -> tuple[int, ...]:
    """读不到版本时按最老的算：面板不列可能不存在的工具。"""
    try:
        from astrbot.core.config.default import VERSION

        return tuple(int(x) for x in str(VERSION).split("-")[0].split("."))
    except Exception:  # noqa: BLE001
        return (0,)


IDLE_DOCS_PER_KB = 500
_KB_PLUGIN_RE = re.compile(
    r"知识库|knowledge[ _-]?base|ragflow|(?<![a-z])rag(?![a-z])|记忆", re.IGNORECASE
)
# “kb”只看插件名：说明里的 KB 多半是文件大小
_KB_NAME_RE = re.compile(r"(?<![a-z0-9])kb(?![a-z0-9])", re.IGNORECASE)
_MEMORY_RE = re.compile(r"memory", re.IGNORECASE)
# 英文的 memory 也可能是内存：这些词说明插件看的是内存占用
_RAM_RE = re.compile(
    r"(?<![a-z])(?:ram|cpu|usage|monitor|leak)(?![a-z])|内存|占用|监控|系统资源",
    re.IGNORECASE,
)


def looks_like_kb_plugin(name: str, text: str) -> bool:
    """``text`` 是插件名和说明放在一起的文字。"""
    if _KB_PLUGIN_RE.search(text) or _KB_NAME_RE.search(name):
        return True
    return bool(_MEMORY_RE.search(text)) and not _RAM_RE.search(text)


def _field_ending(config: dict, suffix: str) -> Any:
    """适配器的字段名不统一，比如 NVIDIA 重排用 nvidia_rerank_api_base。"""
    return config.get(suffix) or next(
        (v for k, v in config.items() if k.endswith(f"_{suffix}") and v), None
    )


class AstrBotView:
    def __init__(self, context: Any) -> None:
        self._context = context

    async def idle_kb(
        self, hit_kbs: set[str], hit_docs: set[tuple[str, str]]
    ) -> dict[str, list]:
        """没被命中过的知识库和文档。取不到时返回已找到的部分，这只是附加信息。"""
        idle_kbs: list[str] = []
        idle_docs: list[dict[str, str]] = []
        try:
            manager = self._context.kb_manager
            for kb in await manager.list_kbs():
                if kb.kb_name not in hit_kbs:
                    idle_kbs.append(kb.kb_name)
                    continue
                helper = await manager.get_kb(kb.kb_id)
                if helper is None:
                    continue
                for doc in await helper.list_documents(limit=IDLE_DOCS_PER_KB):
                    if (kb.kb_name, doc.doc_name) not in hit_docs:
                        idle_docs.append({"kb": kb.kb_name, "doc": doc.doc_name})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[deepdeep] 读取知识库文档列表失败: {exc}")
        return {"idle_kbs": idle_kbs, "idle_docs": idle_docs}

    # ---------- 模型 ----------

    def providers_info(self) -> dict[str, dict[str, Any]]:
        """所有服务商的模型和地址，用来查价格。只读这几个字段，不碰 Key。"""
        out: dict[str, dict[str, Any]] = {}
        manager = getattr(self._context, "provider_manager", None)
        groups = [
            (
                self._safe_list("get_all_providers"),
                "model",
                "api_base",
                "chat",
            ),
            (
                self._safe_list("get_all_embedding_providers"),
                "embedding_model",
                "embedding_api_base",
                "embedding",
            ),
            (
                getattr(manager, "rerank_provider_insts", None) or [],
                "rerank_model",
                "rerank_api_base",
                "rerank",
            ),
        ]
        for providers, model_key, base_key, kind in groups:
            for provider in providers:
                config = getattr(provider, "provider_config", None) or {}
                provider_id = config.get("id")
                if not provider_id:
                    continue
                try:
                    model = provider.get_model() if kind == "chat" else None
                except Exception:  # noqa: BLE001
                    model = None
                out[provider_id] = {
                    "model": model
                    or config.get(model_key)
                    or _field_ending(config, "model"),
                    "api_base": config.get(base_key)
                    or _field_ending(config, "api_base"),
                    "type": config.get("type"),
                    "kind": kind,
                }
        return out

    def _safe_list(self, method: str) -> list[Any]:
        try:
            return list(getattr(self._context, method)())
        except Exception:  # noqa: BLE001 - 较早的 AstrBot 可能没有这些接口
            return []

    # ---------- MCP ----------

    def _mcp_tools(self) -> list[Any]:
        return [t for t in self.llm_tools() if getattr(t, "mcp_server_name", None)]

    def mcp_tool_servers(self) -> dict[str, str]:
        """{工具名: MCP 服务名}。"""
        return {t.name: t.mcp_server_name for t in self._mcp_tools()}

    def mcp_server_of(self, tool_name: str) -> str | None:
        for tool in self._mcp_tools():
            if tool.name == tool_name:
                return tool.mcp_server_name
        return None

    def mcp_servers(self) -> list[dict[str, Any]]:
        """已配置的 MCP 服务和状态。只给名字和连接方式，配置里可能有 Key，不往外传。"""
        out: dict[str, dict[str, Any]] = {}
        try:
            manager = self._context.get_llm_tool_manager()
        except Exception:  # noqa: BLE001
            return []
        try:
            configured = (manager.load_mcp_config() or {}).get("mcpServers") or {}
        except Exception:  # noqa: BLE001 - 读不到配置时只显示正在运行的
            configured = {}
        for name, config in configured.items():
            config = config if isinstance(config, dict) else {}
            out[name] = {
                "name": name,
                "state": "connecting" if config.get("active", True) else "disabled",
                "transport": "stdio" if config.get("command") else "http",
                "endpoint": connectivity.origin_of(config.get("url")),
                "tools": [],
                "errors": [],
            }
        try:
            clients = dict(manager.mcp_client_dict)
        except Exception:  # noqa: BLE001 - 较早的版本可能没有
            clients = {}
        for name, client in clients.items():
            item = out.setdefault(
                name, {"name": name, "transport": None, "tools": [], "errors": []}
            )
            item["state"] = "connected" if getattr(client, "session", None) else "error"
            item["errors"] = [
                str(line)[:200]
                for line in (getattr(client, "server_errlogs", None) or [])[-3:]
            ]
        for tool in self._mcp_tools():
            item = out.setdefault(
                tool.mcp_server_name,
                {
                    "name": tool.mcp_server_name,
                    "state": "connected",
                    "transport": None,
                    "tools": [],
                    "errors": [],
                },
            )
            item["tools"].append(
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "parameters": getattr(tool, "parameters", None),
                    "active": bool(getattr(tool, "active", True)),
                }
            )
        return list(out.values())

    # ---------- 知识库 ----------

    def kb_plugins(self) -> list[dict[str, Any]]:
        """看起来是知识库的社区插件，以及它们提供的工具。"""
        out = []
        tools = self.llm_tools()
        for star in self._safe_list("get_all_stars"):
            name = getattr(star, "name", None) or ""
            if name == "astrbot_plugin_deepdeep" or not getattr(
                star, "activated", True
            ):
                continue
            text = " ".join(
                str(getattr(star, key, None) or "")
                for key in ("name", "display_name", "desc", "short_desc")
            )
            if not looks_like_kb_plugin(name, text):
                continue
            package = (getattr(star, "module_path", None) or "").rpartition(".")[0]
            own = [
                t.name
                for t in tools
                if package
                and (getattr(t, "handler_module_path", None) or "").startswith(package)
            ]
            out.append(
                {
                    "name": name,
                    "display_name": getattr(star, "display_name", None) or name,
                    "tools": own,
                }
            )
        return out

    def is_kb_plugin(self, name: str) -> bool:
        return any(p["name"] == name for p in self.kb_plugins())

    async def set_kb_enabled(self, name: str, enabled: bool) -> None:
        """改默认配置文件的 kb_names，和 AstrBot 配置页选知识库是同一项。只在用户点开关时调用。"""
        if enabled and await self._context.kb_manager.get_kb_by_name(name) is None:
            raise ValueError("知识库不存在")
        config = self.config()
        names = [n for n in (config.get("kb_names") or []) if n != name]
        config["kb_names"] = [*names, name] if enabled else names
        await asyncio.to_thread(config.save_config)

    async def set_mcp_active(self, name: str, active: bool) -> None:
        """启停 MCP 服务，做法和 AstrBot 的工具页一样：先改配置，再启停。只在用户点开关时调用。"""
        manager = self._context.get_llm_tool_manager()
        config = await asyncio.to_thread(manager.load_mcp_config)
        server = (config.get("mcpServers") or {}).get(name)
        if not isinstance(server, dict):
            raise ValueError("找不到这个 MCP 服务")
        previous = server.get("active", True)
        server["active"] = active
        if not await asyncio.to_thread(manager.save_mcp_config, config):
            raise ValueError("保存 MCP 配置失败")
        try:
            if active:
                if name not in manager.mcp_client_dict:
                    await manager.enable_mcp_server(name, server, timeout=30)
            elif name in manager.mcp_client_dict:
                await manager.disable_mcp_server(name, timeout=10)
        except Exception:
            # 启停失败时配置和实际状态保持一致
            server["active"] = previous
            await asyncio.to_thread(manager.save_mcp_config, config)
            raise

    async def kb_config(self) -> dict[str, Any]:
        """默认配置文件启用了哪些知识库，有没有配置问题。不发请求。"""
        out: dict[str, Any] = {"names": [], "agentic": False, "problems": [], "all": []}
        try:
            config = self.config()
            out["names"] = list(config.get("kb_names") or [])
            out["agentic"] = bool(config.get("kb_agentic_mode"))
            manager = self._context.kb_manager
            for name in out["names"]:
                helper = await manager.get_kb_by_name(name)
                if helper is None:
                    out["problems"].append({"code": "missing", "kb": name})
                elif getattr(helper, "init_error", None):
                    out["problems"].append(
                        {
                            "code": "init_error",
                            "kb": name,
                            "detail": str(helper.init_error)[:200],
                        }
                    )
                elif not helper.kb.doc_count and not helper.kb.chunk_count:
                    out["problems"].append({"code": "empty", "kb": name})
            out["all"] = [
                {
                    "name": kb.kb_name,
                    "enabled": kb.kb_name in out["names"],
                    "docs": kb.doc_count or 0,
                }
                for kb in await manager.list_kbs()
            ]
        except Exception as exc:  # noqa: BLE001 - 较早的 AstrBot 没有知识库接口
            logger.debug(f"[deepdeep] 读取知识库配置失败: {exc}")
        return out

    # ---------- 配置 ----------

    def config(self, umo: str | None = None) -> Any:
        """AstrBot 配置。传入会话（unified_msg_origin）时取这个会话所用的配置文件，
        这样给不同会话绑定了不同配置文件时，网页搜索等设置也按会话读取。"""
        if umo:
            try:
                return self._context.get_config(umo=umo)
            except TypeError:  # 旧版本不支持按会话取配置
                pass
        return self._context.get_config()

    def web_search_settings(self, umo: str | None = None) -> dict:
        try:
            return self.config(umo).get("provider_settings", {}) or {}
        except Exception:  # noqa: BLE001 - 读不到配置时当作没有
            return {}

    # ---------- 对话模型 ----------

    def chat_providers(self) -> list[Any]:
        try:
            return self._context.get_all_providers()
        except Exception:  # noqa: BLE001 - 框架内部异常不应影响搜索
            return []

    def model_searches(self) -> list[dict]:
        """开启了内置联网搜索的对话模型（只读提供商配置）。"""
        providers = self.chat_providers()
        if not providers:
            return []
        default_id = ""
        try:
            using = self._context.get_using_provider()
            default_id = str(
                (getattr(using, "provider_config", None) or {}).get("id", "")
            )
        except Exception:  # noqa: BLE001
            pass
        return [m.to_dict() for m in model_search.scan(providers, default_id)]

    # ---------- 工具 ----------

    def llm_tools(self) -> list[Any]:
        try:
            return self._context.get_llm_tool_manager().func_list
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[deepdeep] 读取工具列表失败: {exc}")
            return []

    def current_tool_names(self) -> list[str]:
        names: list[str] = []
        try:
            mgr = self._context.get_llm_tool_manager()
            names += [t.name for t in mgr.func_list if getattr(t, "active", True)]
        except Exception as exc:  # noqa: BLE001 - 预览失败不影响其他功能
            logger.debug(f"[deepdeep] 读取工具列表失败: {exc}")
        try:
            ps = self.config().get("provider_settings", {})
            if ps.get("web_search"):
                version = _astrbot_version()
                names += [
                    t
                    for t in BUILTIN_PROVIDER_TOOLS.get(
                        ps.get("websearch_provider", ""), []
                    )
                    if version >= BUILTIN_TOOLS_SINCE.get(t, (0,))
                ]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[deepdeep] 读取网页搜索配置失败: {exc}")
        return names

    def registered_tools(self) -> dict[str, bool]:
        """已注册的工具（插件、MCP）：{工具名: 是否启用}。内置网页搜索的工具每次请求才加入，不在这里。"""
        try:
            return {
                t.name: bool(getattr(t, "active", True))
                for t in self._context.get_llm_tool_manager().func_list
            }
        except Exception:  # noqa: BLE001
            return {}

    def add_tool(self, tool: Any) -> None:
        self._context.add_llm_tools(tool)

    def remove_tool(self, name: str) -> None:
        self._context.unregister_llm_tool(name)

    async def set_tool_active(self, name: str, active: bool) -> None:
        """只在用户从面板上点了停用、启用时调用。"""
        ctx = self._context
        method = "activate_llm_tool" if active else "deactivate_llm_tool"
        fn = getattr(ctx, f"{method}_async", None)
        if fn is not None:
            ok = await fn(name)
        else:
            ok = getattr(ctx, method)(name)
        if ok is False:
            raise ValueError("找不到这个工具")
