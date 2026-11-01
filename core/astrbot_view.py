"""DeepDeep 看到的 AstrBot：配置、已注册的工具、对话模型。core/ 里只有这里直接用 Context。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from . import model_search
from .providers import BUILTIN_PROVIDER_TOOLS, BUILTIN_TOOLS_SINCE


def _astrbot_version() -> tuple[int, ...]:
    """读不到版本时按最老的算：面板不列可能不存在的工具。"""
    try:
        from astrbot.core.config.default import VERSION

        return tuple(int(x) for x in str(VERSION).split("-")[0].split("."))
    except Exception:  # noqa: BLE001
        return (0,)


class AstrBotView:
    def __init__(self, context: Any) -> None:
        self._context = context

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
