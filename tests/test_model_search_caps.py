"""对话模型内置的联网搜索、其他搜索工具的每月调用上限、Tavily 按 Key 记录失败。"""

from datetime import date

from core import model_search as ms
from core import setup_check as sc
from core.caps import ToolCaps, format_limit_rules, parse_limit_rules
from core.stats import MonthlyCounter
from core.tavily import QUOTA_PAUSE_SECONDS, TavilyClient, TavilySearch


def test_detect_gemini_and_xai_switches():
    g = ms.detect(
        {"id": "gem", "type": "googlegenai_chat_completion", "gm_native_search": True},
        "gemini-2.5-flash",
        default_id="gem",
    )
    assert g.method == "gemini" and g.tools_broken and g.is_default
    assert g.model == "gemini-2.5-flash" and g.config_keys == ["gm_native_search"]
    x = ms.detect({"id": "grok", "xai_native_search": True})
    assert x.method == "xai" and x.extra_cost and not x.tools_broken
    url = ms.detect({"id": "g2", "gm_url_context": True})
    assert url.method == "gemini_url" and url.url_context
    assert ms.detect({"id": "plain", "gm_native_search": False}, "gpt-4o") is None


def test_detect_extra_body_and_model_names():
    cases = {
        "enable_search": {"enable_search": True},
        "search_parameters": {"search_parameters": {"mode": "auto"}},
        "web_search_options": {"web_search_options": {}},
        "plugins: web": {"plugins": [{"id": "web"}]},
        "tools: web_search": {"tools": [{"type": "web_search"}]},
        "$web_search": {
            "tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}]
        },
    }
    for marker, body in cases.items():
        m = ms.detect({"id": "p", "custom_extra_body": body}, "m")
        assert m is not None and m.method == "extra_body" and marker in m.label, marker
    assert ms.detect({"id": "p", "custom_extra_body": {"enable_search": False}}) is None
    off = {"search_parameters": {"mode": "off"}}
    assert ms.detect({"id": "p", "custom_extra_body": off}) is None
    for model in ("gpt-4o-search-preview", "sonar-pro", "perplexity/sonar", "x:online"):
        assert ms.detect({"id": "p"}, model).method == "model", model
    assert ms.detect({"id": "p"}, "deepseek-chat") is None


def test_scan_puts_default_first_and_reads_model():
    class P:
        def __init__(self, pc, model):
            self.provider_config = pc
            self._model = model

        def get_model(self):
            return self._model

    found = ms.scan(
        [
            P({"id": "a", "xai_native_search": True}, "grok-4"),
            P({"id": "b", "gm_native_search": True}, "gemini-3"),
            P({"id": "c"}, "gpt-4o"),
        ],
        default_id="b",
    )
    assert [m.provider_id for m in found] == ["b", "a"]
    assert found[0].model == "gemini-3"


def test_setup_items_for_model_search():
    items = sc.check_model_search(
        [
            ms.detect({"id": "gem", "gm_native_search": True}).to_dict(),
            ms.detect({"id": "grok", "xai_native_search": True}).to_dict(),
            ms.detect({"id": "url", "gm_url_context": True}).to_dict(),
            ms.detect(
                {"id": "off", "gm_native_search": True, "enable": False}
            ).to_dict(),
        ]
    )
    by_key = {it["key"]: it for it in items}
    assert set(by_key) == {"model:gem", "model:grok", "model:url"}  # 未启用的不提醒
    assert by_key["model:gem"]["status"] == "warn"
    assert "函数工具会失效" in by_key["model:gem"]["detail"]
    assert "gem" in by_key["model:gem"]["fix"]
    assert by_key["model:grok"]["status"] == "info"
    assert "额外计费" in by_key["model:grok"]["detail"]
    assert "读取网页" in by_key["model:url"]["detail"]
    result = sc.build(
        deepseek=sc.check_deepseek(("k", "s"), True),
        tavily=sc.check_tavily({"enabled": False}),
        builtin=sc.check_builtin({"web_search": False}, {}),
        plugins=[],
        models=items,
    )
    assert result["items"][0]["key"] == "model:gem" and result["warn"] == 1


def test_parse_limit_rules():
    rules = parse_limit_rules(
        ["web_search_bocha = 500", "exa* = 1e3", "bad", "x = 0", "y = abc", 3]
    )
    assert rules == [("web_search_bocha", 500), ("exa*", 1000)]
    assert format_limit_rules(rules) == ["web_search_bocha = 500", "exa* = 1000"]
    assert parse_limit_rules(None) == []


def test_tool_caps_block_until_next_month(tmp_path):
    today = {"d": date(2026, 10, 30)}
    caps = ToolCaps(MonthlyCounter(tmp_path / "t.json", today=lambda: today["d"]))
    caps.rules = [("exa*", 2), ("web_search_bocha", 1)]
    names = ["exa-search", "exa_get_contents", "web_search_bocha", "web_search_brave"]
    assert caps.blocked(names) == {}
    caps.record("exa-search")
    caps.record("exa_get_contents")  # 同一条规则合计
    caps.record("web_search_brave")  # 没有规则：只计数
    blocked = caps.blocked(names)
    assert set(blocked) == {"exa-search", "exa_get_contents"}
    assert blocked["exa-search"] == {"pattern": "exa*", "used": 2, "limit": 2}
    snap = {s["pattern"]: s for s in caps.snapshot()}
    assert snap["exa*"]["tools"] == {"exa-search": 1, "exa_get_contents": 1}
    assert snap["web_search_bocha"]["used"] == 0
    today["d"] = date(2026, 11, 1)
    assert caps.blocked(names) == {}


async def test_tavily_failures_are_per_key_and_pause(fake_api):
    now = {"t": 1000.0}
    keys = ["tvly-aaaaaaaa1", "tvly-bbbbbbbb2"]
    client = TavilyClient(
        lambda: list(keys),
        api_url=f"{fake_api.base_url}/search",
        wall_clock=lambda: now["t"],
    )
    s = TavilySearch(client)
    # 第一个 Key 无效，第二个可用：搜索成功，但记下第一个 Key 的问题
    fake_api.tavily_queue += [
        (401, {}, {}),
        (200, {"results": [{"url": "https://a", "title": "A"}]}, {}),
    ]
    assert "成功" in (await s.search("q", 1)).text
    f = s.last_failure()
    assert f["position"] == 1 and f["total"] == 2 and not f["all_failed"]
    assert f["masked"] == "tvly****aaa1" and s.paused() == ""
    item = sc.check_tavily(
        {"enabled": True, "configured": True, "usable": True, "last_failure": f}
    )
    assert "第 1 个 Key（tvly****aaa1）Key 无效" in item["detail"]
    assert "还有其他 Key 可用" in item["detail"]
    # 第二个 Key 额度也用完：全部有问题，暂停
    fake_api.tavily_queue += [(401, {}, {}), (432, {}, {})]
    out = await s.search("q2", 1)
    assert out.should_fallback and s.last_failure()["all_failed"]
    assert s.paused() == "Tavily 额度用完，约 24 小时后自动重试"
    now["t"] += QUOTA_PAUSE_SECONDS / 2
    assert "约 12 小时" in s.paused()
    # 到期后放行试探（无效的那个 Key 仍记着）
    now["t"] += QUOTA_PAUSE_SECONDS
    assert s.paused() == ""
    # 只换掉一个 Key：新 Key 没有失败记录，不暂停
    keys[1] = "tvly-cccccccc3"
    assert len(s.failures()) == 1 and s.paused() == ""
    # 全部是无效 Key：一直暂停到换 Key
    keys[:] = ["tvly-aaaaaaaa1"]
    assert s.paused() == "Tavily Key 无效，换成有效的 Key 后自动恢复"
    await client.close()


async def test_tavily_search_uses_explicit_keys(fake_api):
    client = TavilyClient(
        lambda: ["tvly-default"], api_url=f"{fake_api.base_url}/search"
    )
    s = TavilySearch(client)
    fake_api.tavily_queue.append((200, {"results": []}, {}))
    await s.search("q", 1, ["tvly-session"])
    assert fake_api.requests[-1]["auth"] == "Bearer tvly-session"
    await client.close()


def test_monthly_counter_keys(tmp_path):
    c = MonthlyCounter(tmp_path / "c.json", today=lambda: date(2026, 10, 1))
    c.add()
    c.add("web_search_bocha", 3)
    assert c.count() == 1 and c.count("web_search_bocha") == 3
    assert c.counts() == {"total": 1, "web_search_bocha": 3}
    assert (
        MonthlyCounter(tmp_path / "c.json", today=lambda: date(2026, 10, 9)).counts()[
            "web_search_bocha"
        ]
        == 3
    )
