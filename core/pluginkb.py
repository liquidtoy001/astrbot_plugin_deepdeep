"""社区知识库插件的检索：它们大多在发消息前把内容放进请求，没有工具调用可以记录。

给这些插件的 on_llm_request 钩子包一层，比较执行前后请求多了什么，并记下期间用了多少
AstrBot 的嵌入和重排服务。只旁听，不改请求，所以不影响提示词缓存；AstrBot 内部接口变了
就什么都不做。分数和来源在插件内部，这里拿不到。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from . import kbtrace
from .log import logger

SYNC_INTERVAL = 30.0
_ORIGINAL_ATTR = "__deepdeep_plugin_kb_original__"


def registered_handlers() -> Iterable[tuple[Any, str]]:
    """(钩子信息, 插件名)。放在函数里导入：内部接口变了只影响这里。"""
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.OnLLMRequestEvent
    ):
        plugin = star_map.get(handler.handler_module_path)
        if plugin is not None and getattr(plugin, "name", None):
            yield handler, plugin.name


def _part_text(part: Any) -> Any:
    return part.get("text") if isinstance(part, dict) else getattr(part, "text", None)


def _content_mark(message: Any) -> Any:
    """记下消息现在的内容，只存引用不复制：对话很长时也不费内存。列表可能被原地改，所以拆开存。"""
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    return (
        tuple(_part_text(p) for p in content) if isinstance(content, list) else content
    )


def snapshot(req: Any) -> dict[str, Any]:
    return {
        "system": getattr(req, "system_prompt", None) or "",
        "prompt": getattr(req, "prompt", None) or "",
        "contexts": {
            id(item): _content_mark(item)
            for item in getattr(req, "contexts", None) or ()
        },
        "extra": {
            id(item) for item in getattr(req, "extra_user_content_parts", None) or ()
        },
    }


def _added_text(before: str, after: str) -> str:
    if after == before or len(after) <= len(before):
        return ""
    return after.replace(before, "", 1) if before and before in after else after


def _mark_text(mark: Any) -> str:
    if isinstance(mark, str):
        return mark
    if isinstance(mark, tuple):
        return "\n".join(p for p in mark if isinstance(p, str))
    return ""


def _context_added(before: dict[int, Any], message: Any) -> str:
    """新消息的全部文字；已有的消息被原地改过时，多出来的文字。"""
    mark = _content_mark(message)
    if id(message) not in before:
        return _mark_text(mark)
    old = before[id(message)]
    # 大多数消息没变，引用相同，这里不用比内容
    if mark is old or mark == old:
        return ""
    if isinstance(mark, tuple) and isinstance(old, tuple):
        return "\n".join(p for p in mark if isinstance(p, str) and p not in old)
    return _added_text(_mark_text(old), _mark_text(mark))


def added_text(before: dict[str, Any], req: Any) -> str:
    """请求比快照多出来的文字。"""
    parts = [
        _added_text(before["system"], getattr(req, "system_prompt", None) or ""),
        _added_text(before["prompt"], getattr(req, "prompt", None) or ""),
    ]
    parts += [
        _context_added(before["contexts"], item)
        for item in getattr(req, "contexts", None) or ()
    ]
    parts += [
        str(getattr(item, "text", "") or "")
        for item in getattr(req, "extra_user_content_parts", None) or ()
        if id(item) not in before["extra"]
    ]
    return "\n".join(p for p in parts if p)


def _provider_id(provider: Any) -> str | None:
    config = getattr(provider, "provider_config", None)
    return config.get("id") if isinstance(config, dict) else None


def _input_tokens(method: str, args: tuple, kwargs: dict) -> int:
    if method == "rerank":
        query = kwargs.get("query", args[0] if args else "")
        documents = kwargs.get("documents", args[1] if len(args) > 1 else ())
        texts = [query, *(documents or ())]
    else:
        value = kwargs.get("text", args[0] if args else "")
        texts = value if isinstance(value, (list, tuple)) else [value]
    return sum(kbtrace.estimate_tokens(str(text or "")) for text in texts)


class PluginKbWatch:
    def __init__(
        self,
        *,
        kb_plugins: Callable[[], list[dict[str, Any]]],
        providers: Callable[[], Iterable[tuple[str, Any]]],
        on_inject: Callable[..., Awaitable[None]],
        handlers: Callable[[], Iterable[tuple[Any, str]]] = registered_handlers,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kb_plugins = kb_plugins
        self._providers = providers
        self._on_inject = on_inject
        self._handlers = handlers
        self._clock = clock
        self._next_sync = 0.0
        self._warned = False
        # (包了谁, 还原函数)。这里引用着对象，所以不再使用的要及时去掉
        self._restores: list[tuple[Any, Callable[[], None]]] = []

    def sync(self) -> None:
        """包上新出现的钩子和服务商。插件和服务商可能晚于本插件加载，也可能被重载，所以定期看一次。"""
        now = self._clock()
        if now < self._next_sync:
            return
        self._next_sync = now + SYNC_INTERVAL
        try:
            names = {p["name"] for p in self._kb_plugins()}
            if not names:
                self._keep_only(set())
                return
            wanted: set[int] = set()
            for handler, plugin in self._handlers():
                if plugin in names:
                    wanted.add(id(handler))
                    self._wrap_handler(handler, plugin)
            for kind, provider in self._providers():
                wanted.add(id(provider))
                self._wrap_provider(kind, provider)
            self._keep_only(wanted)
        except Exception as exc:  # noqa: BLE001 - 接口变化时不影响插件其他功能
            if not self._warned:
                self._warned = True
                logger.warning(f"[deepdeep] 无法记录社区知识库插件的注入: {exc}")

    def restore(self) -> None:
        self._keep_only(set())

    def _keep_only(self, wanted: set[int]) -> None:
        """还原并忘掉不再需要的：服务商重建、插件重载或卸载之后，旧对象才能被回收。"""
        kept = []
        for owner, restore in self._restores:
            if id(owner) in wanted:
                kept.append((owner, restore))
            else:
                restore()
        self._restores = kept

    def _wrap_handler(self, meta: Any, plugin: str) -> None:
        current = meta.handler
        if hasattr(current, _ORIGINAL_ATTR):
            return
        on_inject = self._on_inject
        clock = self._clock

        async def wrapped(event: Any, *args: Any, **kwargs: Any) -> Any:
            req = next((a for a in args if hasattr(a, "system_prompt")), None)
            if req is None:
                return await current(event, *args, **kwargs)
            before = snapshot(req)
            usage = kbtrace.new_usage()
            token = kbtrace.USAGE.set(usage)
            started = clock()
            error = None
            cancelled = False
            try:
                return await current(event, *args, **kwargs)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                kbtrace.USAGE.reset(token)
                try:
                    await on_inject(
                        plugin=plugin,
                        session=getattr(event, "unified_msg_origin", None),
                        query=before["prompt"],
                        text=added_text(before, req),
                        duration_ms=int((clock() - started) * 1000),
                        error=error,
                        usage=usage,
                        cancelled=cancelled,
                    )
                except Exception as exc:  # noqa: BLE001 - 记录失败绝不能影响请求
                    logger.warning(f"[deepdeep] 记录知识库插件注入失败: {exc}")

        setattr(wrapped, _ORIGINAL_ATTR, current)
        meta.handler = wrapped

        def restore() -> None:
            if meta.handler is wrapped:
                meta.handler = current

        self._restores.append((meta, restore))

    def _wrap_provider(self, kind: str, provider: Any) -> None:
        methods = (
            ("rerank",) if kind == "rerank" else ("get_embedding", "get_embeddings")
        )
        provider_id = _provider_id(provider)
        for method in methods:
            current = getattr(provider, method, None)
            if current is None or hasattr(current, _ORIGINAL_ATTR):
                continue
            self._wrap_method(kind, provider, provider_id, method, current)

    def _wrap_method(
        self,
        kind: str,
        provider: Any,
        provider_id: str | None,
        method: str,
        current: Any,
    ) -> None:
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            usage = kbtrace.USAGE.get()
            if usage is None:
                return await current(*args, **kwargs)
            tokens = _input_tokens(method, args, kwargs)
            usage[kind] += 1
            usage[f"{kind}_tokens"] += tokens
            usage[f"{kind}_provider"] = usage[f"{kind}_provider"] or provider_id
            if provider_id:
                by = usage[f"{kind}_by"]
                by[provider_id] = by.get(provider_id, 0) + tokens
            # 服务商内部可能一个方法调另一个，只算最外层
            token = kbtrace.USAGE.set(None)
            try:
                return await current(*args, **kwargs)
            finally:
                kbtrace.USAGE.reset(token)

        setattr(wrapped, _ORIGINAL_ATTR, current)
        # 实例上原来可能就有这个方法（比如别的插件包过），还原时要放回去
        had_own = method in provider.__dict__
        own = provider.__dict__.get(method)
        setattr(provider, method, wrapped)

        def restore() -> None:
            if provider.__dict__.get(method) is not wrapped:
                return
            if had_own:
                provider.__dict__[method] = own
            else:
                del provider.__dict__[method]

        self._restores.append((provider, restore))
