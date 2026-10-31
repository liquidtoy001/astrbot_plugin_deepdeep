"""DeepDeep 自己的两个客户端（DeepSeek、Tavily）的 Key 从哪来，以及这次要不要提供 Tavily。

其他搜索来源走它们各自的 AstrBot 工具，Key 不归 DeepDeep 管。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .providers import DEEPSEEK_HOST, TAVILY_BUILTIN_SEARCH, TAVILY_KEY_FIELD

if TYPE_CHECKING:
    from .astrbot_view import AstrBotView
    from .tavily import TavilySearch


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    return f"{key[:3]}****{key[-4:]}" if len(key) > 10 else "****"


class Credentials:
    def __init__(self, astrbot: AstrBotView, config: Any) -> None:
        self._astrbot = astrbot
        self._config = config

    def deepseek_key_source(self) -> tuple[str | None, str]:
        """返回 (key, 来源说明)。"""
        key = str(self._config.get("api_key", "") or "").strip()
        if key:
            return key, "插件配置"
        for provider in self._astrbot.chat_providers():
            pc = getattr(provider, "provider_config", None) or {}
            if DEEPSEEK_HOST not in str(pc.get("api_base", "")):
                continue
            for k in provider.get_keys():
                if k and k.strip():
                    return k.strip(), f"复用提供商 {pc.get('id', '')}".strip()
        return None, ""

    def deepseek_key(self) -> str | None:
        return self.deepseek_key_source()[0]

    def tavily_key_source(self, umo: str | None = None) -> tuple[list[str], str]:
        """返回 (Key 列表, 来源说明)。插件配置优先，否则复用 AstrBot 网页搜索里的 Tavily Key。"""
        own = str(self._config.get("tavily_api_key", "") or "").strip()
        if own:
            return [own], "插件配置"
        keys = self._astrbot.web_search_settings(umo).get(TAVILY_KEY_FIELD) or []
        if isinstance(keys, str):
            keys = [keys]
        keys = [k.strip() for k in keys if isinstance(k, str) and k.strip()]
        return keys, ("复用 AstrBot 网页搜索的 Tavily Key" if keys else "")

    def tavily_keys(self, umo: str | None = None) -> list[str]:
        return self.tavily_key_source(umo)[0]


class TavilyGate:
    """本次请求要不要提供 deepdeep_tavily_search，以及不提供的原因。"""

    def __init__(
        self, credentials: Credentials, astrbot: AstrBotView, tavily: TavilySearch
    ) -> None:
        self._credentials = credentials
        self._astrbot = astrbot
        self._tavily = tavily
        self.enabled = True

    def usable(
        self, names: list[str] | None = None, umo: str | None = None
    ) -> tuple[bool, str]:
        if not self.enabled:
            return False, "已在插件设置中关闭"
        keys = self._credentials.tavily_keys(umo)
        if not keys:
            return False, "没有找到 Tavily API Key"
        ps = self._astrbot.web_search_settings(umo)
        builtin = ps.get("web_search") and ps.get("websearch_provider") == "tavily"
        if builtin or (names and TAVILY_BUILTIN_SEARCH in names):
            return False, "AstrBot 内置网页搜索已经是 Tavily，避免重复"
        # 到达每月上限后整个月都不提供（工具列表在这个月内保持不变），下个月自动恢复
        if self._tavily.limit_reached():
            return False, f"本月 Tavily 调用已达上限（{self._tavily.usage_text()}）"
        # 所有 Key 都无效或额度用完：暂停提供，换 Key、到期或重启后恢复
        paused = self._tavily.paused(keys)
        if paused:
            return False, paused
        return True, ""

    def status(self) -> dict[str, Any]:
        keys, source = self._credentials.tavily_key_source()
        usable, why = self.usable()
        return {
            "enabled": self.enabled,
            "configured": bool(keys),
            "masked": mask_key(keys[0]) if keys else "",
            "source": source,
            "usable": usable,
            "reason": why,
            "month_used": self._tavily.month_used(),
            "monthly_limit": self._tavily.monthly_limit,
            "limit_reached": self._tavily.limit_reached(),
            "usage_text": self._tavily.usage_text(),
            "last_failure": self._tavily.last_failure(),
        }
