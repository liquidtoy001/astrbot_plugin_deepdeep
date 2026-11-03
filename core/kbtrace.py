"""记录知识库检索的痕迹：查了哪些库、命中什么、多少分、用了多少次向量和重排。

AstrBot 的知识库检索都经过 ``kb_manager.retrieve``：模型调用 astr_kb_search、发消息前自动注入、
插件直接检索、WebUI 的检索测试都一样。所以在这里包一层，只旁听，不改参数和结果，记录出错也不影响检索。
"""

from __future__ import annotations

import contextvars
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .log import logger

# 不是真实工具名，只用来在调用日志里区分检索是怎么发起的
KB_INJECT_TOOL = "astr_kb_inject"
KB_DIRECT_TOOL = "astr_kb_direct"
KB_PLUGIN_INJECT_TOOL = "kb_plugin_inject"

MODE_TOOL = "tool"
MODE_INJECT = "inject"
MODE_DIRECT = "direct"
# 插件的知识库工具：只有工具返回的文本，没有分数和来源
MODE_UNSCORED = "unscored"
# 插件在发消息前放进请求的内容：只有多少内容和耗时，没有分数和来源
MODE_PLUGIN_INJECT = "plugin_inject"

SNIPPET_CHARS = 120

_PLUGIN_KB_RE = re.compile(
    r"knowledge|知识库|(?:^|[_-])(?:kb|rag)(?:[_-]|$)", re.IGNORECASE
)
_KB_TEXT_RE = re.compile(r"知识库|knowledge[ -]?base", re.IGNORECASE)
_ORIGINAL_ATTR = "__deepdeep_original__"
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
# 这次检索里实际发生了什么，由 retrieve 的包装设置，内部方法的包装写入
_TRACE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "deepdeep_kb_trace", default=None
)
# 社区知识库插件的钩子正在用多少嵌入和重排，由 pluginkb 设置
USAGE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "deepdeep_kb_plugin_usage", default=None
)


def new_usage() -> dict[str, Any]:
    return {
        "embedding": 0,
        "embedding_tokens": 0,
        "embedding_provider": None,
        "embedding_by": {},
        "rerank": 0,
        "rerank_tokens": 0,
        "rerank_provider": None,
        "rerank_by": {},
    }


def is_plugin_kb_tool(name: str) -> bool:
    return bool(_PLUGIN_KB_RE.search(name or ""))


def is_plugin_kb_text(name: str, description: str = "") -> bool:
    """工具名或说明里写明了是知识库。说明只认明确的词，免得把提到“知识”的工具都算进来。"""
    return is_plugin_kb_tool(name) or bool(_KB_TEXT_RE.search(description or ""))


def trim_result(
    result: Any, min_score: float, chunk_chars: int, *, scores_reliable: bool = True
) -> tuple[Any, int]:
    """按用户的设置精简检索结果，返回 (结果, 省下的 token)。两项都是 0 时原样返回。

    格式和 AstrBot 的 _format_context 一样，模型看到的只是内容变少了。至少留一条。
    重排失败时分数不是重排分，这时不按分数过滤。
    """
    if not isinstance(result, dict) or not result.get("results"):
        return result, 0
    if min_score <= 0 and chunk_chars <= 0:
        return result, 0
    before = estimate_tokens(result.get("context_text"))

    def clipped(item: dict[str, Any]) -> dict[str, Any]:
        content = str(item.get("content") or "")
        if chunk_chars > 0 and len(content) > chunk_chars:
            return {**item, "content": content[:chunk_chars] + "…"}
        return item

    kept, seen = [], set()
    for item in result["results"]:
        content = str(item.get("content") or "")
        if content in seen:
            continue
        seen.add(content)
        if (
            scores_reliable
            and min_score > 0
            and float(item.get("score") or 0) < min_score
        ):
            continue
        kept.append(clipped(item))
    kept = kept or [clipped(result["results"][0])]
    lines = ["以下是相关的知识库内容,请参考这些信息回答用户的问题:\n"]
    for i, item in enumerate(kept, 1):
        lines.append(f"【知识 {i}】")
        lines.append(f"来源: {item.get('kb_name')} / {item.get('doc_name')}")
        lines.append(f"内容: {item.get('content')}")
        lines.append(f"相关度: {float(item.get('score') or 0):.2f}")
        lines.append("")
    trimmed = {**result, "results": kept, "context_text": "\n".join(lines)}
    return trimmed, max(0, before - estimate_tokens(trimmed["context_text"]))


def summarize(
    results: list[dict] | None,
    *,
    kb_count: int,
    embedding_calls: int,
    rerank_calls: int,
    record_sources: bool,
    record_snippets: bool,
    error: str | None = None,
    degraded: dict[str, Any] | None = None,
    context_text: str | None = None,
    billing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 retrieve 的结果整理成调用日志的 kb 字段。分数和次数总是记，来源和片段跟随记录开关。"""
    hits = []
    for item in results or []:
        hit: dict[str, Any] = {"score": round(float(item.get("score") or 0), 4)}
        if record_sources:
            hit.update(
                kb=item.get("kb_name"),
                doc=item.get("doc_name"),
                chunk=item.get("chunk_index"),
            )
        if record_snippets:
            hit["snippet"] = str(item.get("content") or "")[:SNIPPET_CHARS]
        hits.append(hit)
    info: dict[str, Any] = {
        "mode": MODE_DIRECT,
        "kb_count": kb_count,
        "embedding_calls": embedding_calls,
        "rerank_calls": rerank_calls,
        "count": len(hits),
        "top_score": max((h["score"] for h in hits), default=None),
        "hits": hits,
        "tokens": {"result": estimate_tokens(context_text)},
    }
    if error:
        info["error"] = error
    if degraded:
        info["degraded"] = degraded
    if billing:
        info["tokens"]["saved"] = billing.get("saved") or 0
        info["billing"] = {k: v for k, v in billing.items() if k != "saved"}
    return info


def top_line(info: dict[str, Any]) -> str | None:
    """说明列显示的第一条来源；没记录来源时返回 None。"""
    hits = info.get("hits") or []
    if not hits or not hits[0].get("kb"):
        return None
    return f"{hits[0]['kb']} / {hits[0].get('doc') or '?'}"


async def _usage(manager: Any, kb_names: list[str]) -> dict[str, Any]:
    """这次检索会用多少次接口：每个可用的库查一次向量，所有库合起来重排一次（有重排模型时）。"""
    out: dict[str, Any] = {
        "embedding": 0,
        "rerank": 0,
        "embedding_provider": None,
        "rerank_provider": None,
        # 每个嵌入模型查了几个库：不同的库可以用不同的模型
        "embedding_by": {},
    }
    for name in kb_names:
        helper = await manager.get_kb_by_name(name)
        if helper is None or getattr(helper, "init_error", None):
            continue
        out["embedding"] += 1
        kb = getattr(helper, "kb", None)
        provider = getattr(kb, "embedding_provider_id", None)
        out["embedding_provider"] = out["embedding_provider"] or provider
        if provider:
            out["embedding_by"][provider] = out["embedding_by"].get(provider, 0) + 1
        if getattr(getattr(helper, "vec_db", None), "rerank_provider", None):
            out["rerank"] = 1
            out["rerank_provider"] = out["rerank_provider"] or getattr(
                kb, "rerank_provider_id", None
            )
    return out


def estimate_tokens(text: str | None) -> int:
    """粗略估算：中日韩字符约 0.6 token，其他字符约 0.3 token。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return int(cjk * 0.6 + (len(text) - cjk) * 0.3)


def usage_fields(response: Any) -> dict[str, int] | None:
    """模型这一轮的实际用量，服务商没有返回时是 None。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {
        "input_other": int(getattr(usage, "input_other", 0) or 0),
        "input_cached": int(getattr(usage, "input_cached", 0) or 0),
        "output": int(getattr(usage, "output", 0) or 0),
    }


def assess_status(config: dict[str, Any], recent: dict[str, Any]) -> dict[str, Any]:
    """知识库能不能用。只看配置和检索是否出错，分数不参与：还没有证据说明多少分算差。"""
    reasons = list(config.get("problems") or [])
    bad = any(r["code"] in ("missing", "init_error") for r in reasons)
    total, errors = recent["total"], recent["error"]
    if errors:
        reasons.append({"code": "errors", "n": errors})
        # 偶尔失败只提醒；一半以上都失败才算有问题。分母不算社区插件，它们成功也说明不了 AstrBot 知识库好不好
        bad = bad or errors * 2 >= recent.get("builtin_total", total)
    if recent["degraded"]:
        reasons.append({"code": "degraded", "n": recent["degraded"]})
    # 社区插件和 MCP 出错只提醒：它不代表 AstrBot 知识库有问题
    if recent.get("plugin_error"):
        reasons.append({"code": "plugin_errors", "n": recent["plugin_error"]})
    if bad:
        verdict = "bad"
    elif reasons:
        verdict = "warn"
    elif not config.get("names") and not total:
        verdict = "off"
    elif not total:
        verdict = "idle"
    else:
        verdict = "ok"
    return {"verdict": verdict, "reasons": reasons}


class _VecProbe:
    """替 AstrBot 的向量库记账：它自己吞掉了向量检索的异常，从外面看不出来。"""

    def __init__(self, vec_db: Any, kb_id: str, trace: dict[str, Any]) -> None:
        self._vec_db = vec_db
        self._kb_id = kb_id
        self._trace = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._vec_db, name)

    async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
        self._trace["embedding"] += 1
        try:
            return await self._vec_db.retrieve(*args, **kwargs)
        except Exception:
            self._trace["dense_failed"].append(self._kb_id)
            raise


def _watch_internals(manager: Any) -> Callable[[], None]:
    """旁听私有方法，发现向量或重排失败。这些方法没有了也没关系，只是看不出降级。"""
    inner = getattr(manager, "retrieval_manager", None)
    current_dense = getattr(inner, "_dense_retrieve", None)
    current_rerank = getattr(inner, "_rerank", None)
    if current_dense is None or current_rerank is None:
        return lambda: None
    dense = getattr(current_dense, _ORIGINAL_ATTR, current_dense)
    rerank = getattr(current_rerank, _ORIGINAL_ATTR, current_rerank)

    async def dense_retrieve(*args: Any, **kwargs: Any) -> Any:
        trace = _TRACE.get()
        options = kwargs.get("kb_options")
        if trace is not None and isinstance(options, dict):
            trace["watched"] = True
            kwargs["kb_options"] = {
                kb_id: {**opt, "vec_db": _VecProbe(opt["vec_db"], kb_id, trace)}
                for kb_id, opt in options.items()
            }
        return await dense(*args, **kwargs)

    async def rerank_results(*args: Any, **kwargs: Any) -> Any:
        trace = _TRACE.get()
        if trace is not None:
            trace["rerank"] += 1
            trace["rerank_tokens"] += sum(
                estimate_tokens(getattr(item, "content", ""))
                for item in kwargs.get("results") or ()
            )
        try:
            return await rerank(*args, **kwargs)
        except Exception:
            if trace is not None:
                trace["rerank_failed"] = True
            raise

    setattr(dense_retrieve, _ORIGINAL_ATTR, dense)
    setattr(rerank_results, _ORIGINAL_ATTR, rerank)
    inner._dense_retrieve = dense_retrieve
    inner._rerank = rerank_results

    def restore() -> None:
        if inner._dense_retrieve is dense_retrieve:
            inner._dense_retrieve = dense
        if inner._rerank is rerank_results:
            inner._rerank = rerank

    return restore


async def _degraded(manager: Any, trace: dict[str, Any]) -> dict[str, Any] | None:
    if not trace["dense_failed"] and not trace["rerank_failed"]:
        return None
    names = []
    for kb_id in trace["dense_failed"]:
        helper = await manager.get_kb(kb_id)
        names.append(helper.kb.kb_name if helper else kb_id)
    return {"dense_failed": names, "rerank_failed": trace["rerank_failed"]}


def install(
    manager: Any,
    on_retrieve: Callable[..., Awaitable[None]],
    trim: Callable[[], tuple[float, int]] = lambda: (0.0, 0),
) -> Callable[[], None] | None:
    """包住 ``manager.retrieve``，返回还原函数；这个版本的 AstrBot 没有该接口时返回 None。

    默认只旁听。``trim`` 返回用户设置的（最低分，每条最多字数），设了才会精简结果。
    """
    current = getattr(manager, "retrieve", None)
    if current is None:
        return None
    # 插件热重载时可能还留着上一次的包装
    original = getattr(current, _ORIGINAL_ATTR, current)
    # AstrBot 启动时先加载插件、后初始化知识库，加载插件时检索管理器还没有创建。
    # 所以每次检索前看一眼：还没挂上、或者换了一个新的检索管理器，就挂上
    watched: dict[str, Any] = {"inner": None, "restore": lambda: None}

    def watch_internals() -> None:
        inner = getattr(manager, "retrieval_manager", None)
        if inner is None or inner is watched["inner"]:
            return
        watched["restore"]()
        watched["restore"] = _watch_internals(manager)
        watched["inner"] = inner

    watch_internals()

    async def retrieve(*args: Any, **kwargs: Any) -> Any:
        query = kwargs.get("query", args[0] if args else "")
        kb_names = kwargs.get("kb_names", args[1] if len(args) > 1 else [])
        started = time.monotonic()
        result = error = None
        trace = {
            "watched": False,
            "embedding": 0,
            "rerank": 0,
            "rerank_tokens": 0,
            "dense_failed": [],
            "rerank_failed": False,
        }
        try:
            watch_internals()
        except Exception as exc:  # noqa: BLE001 - 挂不上只是看不出降级和重排用量
            logger.debug(f"[deepdeep] 旁听知识库内部步骤失败: {exc}")
        token = _TRACE.set(trace)
        # 插件调用 AstrBot 知识库时，这次检索单独有记录，不再算到插件头上
        usage_token = USAGE.set(None)
        saved = 0
        try:
            result = await original(*args, **kwargs)
            try:
                result, saved = trim_result(
                    result, *trim(), scores_reliable=not trace["rerank_failed"]
                )
            except Exception as exc:  # noqa: BLE001 - 精简失败就用原结果
                logger.warning(f"[deepdeep] 精简知识库结果失败: {exc}")
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _TRACE.reset(token)
            USAGE.reset(usage_token)
            try:
                results = (result or {}).get("results")
                usage = await _usage(manager, list(kb_names or []))
                each_query = estimate_tokens(str(query or ""))
                # 能旁听到实际调用就用实际次数，否则按代码逻辑推算
                if trace["watched"]:
                    embedding, rerank = trace["embedding"], trace["rerank"]
                else:
                    embedding = usage["embedding"]
                    rerank = usage["rerank"] if results else 0
                await on_retrieve(
                    query=str(query or ""),
                    results=results,
                    context_text=(result or {}).get("context_text"),
                    kb_count=len(kb_names or []),
                    embedding_calls=embedding,
                    rerank_calls=rerank,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=error,
                    degraded=await _degraded(manager, trace),
                    billing={
                        "embedding_provider": usage["embedding_provider"],
                        "embedding_tokens": each_query * embedding,
                        # 实际次数和推算对不上（比如中途出错）时，不知道失败的是哪一家，都算第一家
                        "embedding_by": {
                            provider: each_query * n
                            for provider, n in usage["embedding_by"].items()
                        }
                        if sum(usage["embedding_by"].values()) == embedding
                        else None,
                        "rerank_provider": usage["rerank_provider"],
                        "rerank_tokens": trace["rerank_tokens"],
                        "saved": saved,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 记录失败绝不能影响检索
                logger.warning(f"[deepdeep] 记录知识库检索失败: {exc}")

    setattr(retrieve, _ORIGINAL_ATTR, original)
    manager.retrieve = retrieve

    def restore() -> None:
        watched["restore"]()
        if getattr(manager, "retrieve", None) is retrieve:
            manager.retrieve = original

    return restore
