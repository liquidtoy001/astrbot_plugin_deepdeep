"""知识库用到的嵌入和重排模型的花费：价格从哪来，一段用量值多少钱。

价格的优先级：用户手填 > 本地模型（免费） > 服务商官方价 > 其他渠道的参考价 > 未定价。
官方价和参考价来自 models.dev（AstrBot 也用它取模型信息），每天更新一次，存在插件数据目录。
没有按上下文长度分档，中转站和套餐折扣也看不到，所以都是估算；不准的时候可以手填。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .log import logger

CATALOG_URL = "https://models.dev/api.json"
CATALOG_TTL = 86400.0
RETRY_AFTER = 1800.0
FETCH_TIMEOUT = 60
WAIT_FOR_FETCH = 8.0
MAX_PRICE = 100000.0

# 接口地址 -> models.dev 里的服务商
HOST_PROVIDERS = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.deepseek.com": "deepseek",
    "generativelanguage.googleapis.com": "google",
    "dashscope.aliyuncs.com": "alibaba-cn",
    "dashscope-intl.aliyuncs.com": "alibaba",
    "maas.aliyuncs.com": "alibaba-cn",
    "api.moonshot.cn": "moonshotai-cn",
    "api.moonshot.ai": "moonshotai",
    "open.bigmodel.cn": "zhipuai",
    "api.siliconflow.cn": "siliconflow-cn",
    "api.siliconflow.com": "siliconflow",
    "openrouter.ai": "openrouter",
    "opencode.ai": "opencode",
    "api.groq.com": "groq",
    "api.x.ai": "xai",
    "api.mistral.ai": "mistral",
    "api.minimax.chat": "minimax-cn",
    "api.xiaomimimo.com": "xiaomi",
    "integrate.api.nvidia.com": "nvidia",
    "ai.api.nvidia.com": "nvidia",
}
# AstrBot 的适配器 -> models.dev 里的服务商，地址留空用默认值时靠它判断
TYPE_PROVIDERS = {
    "openai_embedding": "openai",
    "gemini_embedding": "google",
    "nvidia_embedding": "nvidia",
    "nvidia_rerank": "nvidia",
    "dashscope_embedding": "alibaba-cn",
    "bailian_rerank": "alibaba-cn",
}
# 这些适配器连的都是用户自己部署的模型
LOCAL_TYPES = (
    "ollama",
    "lm_studio",
    "lmstudio",
    "llama_cpp",
    "vllm",
    "localai",
    "xinference",
    "tei_rerank",
)
# 阿里云百炼的文本输入价（元/每百万 token，2026-11 百炼模型广场）。models.dev 上没有这些模型
ALIYUN_PRICES = {
    "qwen3.7-text-embedding": 0.5,
    "qwen3.7-text-rerank": 0.5,
    "qwen3-vl-embedding": 0.7,
    "tongyi-embedding-vision-flash-2026-03-06": 0.15,
}


@dataclass
class Price:
    """每百万 token 的价格。"""

    input: float
    cached: float
    output: float
    currency: str
    source: str


FREE = Price(0, 0, 0, "cny", "local")


def parse_model_prices(lines: Any) -> dict[str, Price]:
    """用户手填的价格。每行“模型 = 价格 币种”，写错的行忽略。"""
    if not isinstance(lines, list):
        return {}
    out: dict[str, Price] = {}
    for line in lines:
        if not isinstance(line, str) or "=" not in line:
            continue
        key, _, value = line.rpartition("=")
        parts = value.split()
        if not key.strip() or len(parts) < 2 or parts[-1] not in ("cny", "usd"):
            continue
        try:
            numbers = [float(x) for x in parts[:-1]]
        except ValueError:
            continue
        # 嵌入和重排只有一个价格；三个数是输入、缓存、输出
        if len(numbers) == 1:
            numbers = [numbers[0], numbers[0], 0.0]
        if len(numbers) == 3 and all(0 <= n <= MAX_PRICE for n in numbers):
            out[key.strip().lower()] = Price(*numbers, parts[-1], "manual")
    return out


def is_local(api_base: str | None, provider_type: str | None = None) -> bool:
    """本地部署的模型：地址是本机或内网，或者是 Ollama、LM Studio 这类适配器。"""
    lowered = (provider_type or "").lower()
    if any(name in lowered for name in LOCAL_TYPES):
        return True
    if not api_base:
        return False
    host = (
        urlparse(api_base if "//" in api_base else f"//{api_base}").hostname or ""
    ).lower()
    if not host:
        return False
    # 没有点的主机名是 Docker 服务名或局域网机器
    if "." not in host or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def slim_catalog(raw: Any) -> dict[str, dict[str, list[float]]]:
    """只留价格：{服务商: {模型: [输入, 输出, 缓存]}}。完整文件有几兆。"""
    out: dict[str, dict[str, list[float]]] = {}
    if not isinstance(raw, dict):
        return out
    for provider, info in raw.items():
        models = info.get("models") if isinstance(info, dict) else None
        if not isinstance(models, dict):
            continue
        for model, data in models.items():
            cost = data.get("cost") if isinstance(data, dict) else None
            if not isinstance(cost, dict) or "input" not in cost:
                continue
            try:
                entry = [
                    float(cost.get("input") or 0),
                    float(cost.get("output") or 0),
                    float(cost.get("cache_read", cost.get("input")) or 0),
                ]
            except (TypeError, ValueError):
                continue
            out.setdefault(provider, {})[str(model).lower()] = entry
    return out


def _bare(model: str) -> str:
    return model.lower().rsplit("/", 1)[-1]


class PriceBook:
    def __init__(
        self,
        path: Path | None,
        *,
        usd_rate: Callable[[], float] = lambda: 7.1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.manual: dict[str, Price] = {}
        self._usd_rate = usd_rate
        self._clock = clock
        self._catalog: dict[str, dict[str, list[float]]] = {}
        self._reference: dict[str, list[float]] = {}
        self._fetched_at = 0.0
        self._tried_at = 0.0
        self._task: asyncio.Task | None = None
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._use(data["catalog"], float(data["fetched_at"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug(f"[deepdeep] 读取模型价格缓存失败: {exc}")

    def _use(
        self, catalog: dict[str, dict[str, list[float]]], fetched_at: float
    ) -> None:
        self._catalog = catalog
        self._fetched_at = fetched_at
        # 同一个模型在各渠道的价格取中位数，给没有官方价的情况参考
        groups: dict[str, list[list[float]]] = {}
        for models in catalog.values():
            for model, entry in models.items():
                groups.setdefault(_bare(model), []).append(entry)
        self._reference = {
            model: [statistics.median(column) for column in zip(*entries, strict=True)]
            for model, entries in groups.items()
        }

    @property
    def fetched_at(self) -> float:
        return self._fetched_at

    async def _fetch(self) -> None:
        self._tried_at = self._clock()
        try:
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
            async with (
                aiohttp.ClientSession(trust_env=True, timeout=timeout) as session,
                session.get(CATALOG_URL) as response,
            ):
                response.raise_for_status()
                raw = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - 取不到价格时显示未定价，不影响其他功能
            logger.warning(f"[deepdeep] 获取模型价格失败: {exc}")
            return
        catalog = slim_catalog(raw)
        if not catalog:
            return
        self._use(catalog, self._clock())
        if self.path is not None:
            payload = {"fetched_at": self._fetched_at, "catalog": catalog}
            text = json.dumps(payload, ensure_ascii=False)
            await asyncio.to_thread(self.path.write_text, text, encoding="utf-8")

    async def ensure_fresh(self) -> None:
        """价格过期就更新。最多等几秒，没等到的话下次打开页面就有了。"""
        now = self._clock()
        stale = now - self._fetched_at > CATALOG_TTL
        if not stale or now - self._tried_at < RETRY_AFTER:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._fetch())
        await asyncio.wait({self._task}, timeout=WAIT_FOR_FETCH)

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def price_for(
        self,
        model: str | None,
        api_base: str | None = None,
        provider_type: str | None = None,
    ) -> Price | None:
        # TEI 这类本地服务可以不填模型名
        if not model:
            return FREE if is_local(api_base, provider_type) else None
        manual = self.manual.get(model.lower()) or self.manual.get(_bare(model))
        if manual is not None:
            return manual
        if is_local(api_base, provider_type):
            return FREE
        if self._is_aliyun(api_base, provider_type):
            listed = ALIYUN_PRICES.get(_bare(model))
            if listed is not None:
                return Price(listed, listed, 0, "cny", "official")
        official = self._official(model, api_base, provider_type)
        if official is not None:
            return Price(official[0], official[2], official[1], "usd", "official")
        reference = self._reference.get(_bare(model))
        if reference is not None:
            return Price(reference[0], reference[2], reference[1], "usd", "reference")
        return None

    @staticmethod
    def _host_provider(api_base: str | None, provider_type: str | None) -> str | None:
        host = (urlparse(api_base or "").hostname or "").lower()
        for suffix, provider in HOST_PROVIDERS.items():
            if host == suffix or host.endswith(f".{suffix}"):
                return provider
        # 填了别的地址就是中转站，不算官方价
        return None if host else TYPE_PROVIDERS.get(provider_type or "")

    def _is_aliyun(self, api_base: str | None, provider_type: str | None) -> bool:
        return self._host_provider(api_base, provider_type) == "alibaba-cn"

    def _official(
        self, model: str, api_base: str | None, provider_type: str | None = None
    ) -> list[float] | None:
        provider = self._host_provider(api_base, provider_type)
        models = self._catalog.get(provider or "") or {}
        return models.get(model.lower()) or models.get(_bare(model))

    def cost(
        self,
        price: Price | None,
        other: float = 0,
        cached: float = 0,
        output: float = 0,
    ) -> float | None:
        """这段用量值多少人民币；没有价格，或者美元价没有汇率可换算时是 None。"""
        if price is None:
            return None
        rate = self._usd_rate() if price.currency == "usd" else 1.0
        if rate <= 0:
            return None
        amount = (
            other * price.input + cached * price.cached + output * price.output
        ) / 1_000_000
        return amount * rate

    def kb_cost(
        self, tokens: dict[str, dict[str, int]], providers: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """知识库用到的嵌入和重排模型：每个模型的估算用量、价格和花费。"""
        rows = []
        totals = {"embedding": 0.0, "rerank": 0.0}
        for provider_id, info in providers.items():
            kind = info.get("kind")
            if kind not in totals:
                continue
            used = int((tokens.get(kind) or {}).get(provider_id, 0))
            price = self.price_for(
                info.get("model"), info.get("api_base"), info.get("type")
            )
            cost = self.cost(price, used)
            totals[kind] += cost or 0
            rows.append(
                {
                    "provider_id": provider_id,
                    "model": info.get("model") or provider_id,
                    "kind": kind,
                    "tokens": used,
                    "price": asdict(price) if price else None,
                    "cost_cny": round(cost, 4) if cost is not None else None,
                }
            )
        rows.sort(key=lambda r: (r["kind"], -r["tokens"]))
        return {
            "embedding": round(totals["embedding"], 4),
            "rerank": round(totals["rerank"], 4),
            "total": round(sum(totals.values()), 4),
            "rows": rows,
            "unpriced": [r["model"] for r in rows if r["cost_cny"] is None],
        }
