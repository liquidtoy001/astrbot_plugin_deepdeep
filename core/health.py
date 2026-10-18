"""搜索工具健康检测：从每次调用的结果判断工具是不是“基本失效”。

只做提示，不改路由和分工说明。判断依据：
- 失败：工具报错，或返回的是一小段错误信息（被拦截、验证码、限流、超时等）
- 空结果：没有返回内容，或明确说“没有找到”
- 不相关：结果里几乎找不到搜索词（比如 SearXNG 里只剩 Bing 能用、返回一堆无关网页）

相关度只在内存里根据结果文本计算，数据库里只保存分数，不保存内容。
"""

from __future__ import annotations

import re
from typing import Any

# 工具参数里可能代表“搜索词”的字段名，按顺序取第一个
QUERY_KEYS = (
    "query",
    "q",
    "keyword",
    "keywords",
    "search_query",
    "search",
    "text",
    "prompt",
)

# 搜索语法，不参与相关度计算
OPERATOR_RE = re.compile(
    r"(?:^|\s)-?(?:site|after|before|lang|filetype|inurl|intitle):\S+", re.I
)
WORD_RE = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")
CJK_RUN_RE = re.compile(r"[㐀-鿿豈-﫿]+")

EMPTY_RE = re.compile(
    r"没有找到|未找到|无结果|未搜索到|搜索结果为空|no results|not found|0 results|nothing found|\bno\b.{0,40}\bfound\b"
    r"|\"results\"\s*:\s*\[\s*\]",
    re.I,
)
ERROR_RE = re.compile(
    r"error|exception|traceback|failed|失败|错误|异常|超时|timed? ?out|captcha|验证码"
    r"|too many requests|rate.?limit|限流|forbidden|unauthorized|denied|blocked|拦截"
    r"|\b(?:401|403|429|500|502|503|504)\b",
    re.I,
)
# 错误信息通常很短；长文本里出现 error 之类的词多半只是正文内容
ERROR_MAX_CHARS = 600
EMPTY_MAX_CHARS = 300
# 低于这个相关度视为“结果和搜索词无关”
IRRELEVANT_BELOW = 0.3

# 统计窗口里至少要有这么多次调用才下结论
MIN_SAMPLES = 3
BROKEN_RATIO = 0.7
DEGRADED_RATIO = 0.35

QUALITY_LABELS = {
    "error": "失败",
    "empty": "空结果",
    "irrelevant": "结果不相关",
}


def extract_query(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in QUERY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return " ".join(value)
    return None


def query_terms(query: str) -> set[str]:
    """搜索词拆成词：英文和数字按单词，中文按相邻两个字。"""
    text = OPERATOR_RE.sub(" ", query).lower()
    text = text.replace('"', " ")
    terms = {w for w in WORD_RE.findall(text) if len(w) >= 2 or w.isdigit()}
    # 版本号、CVE 编号这类带连字符的，再拆一层，结果里写法不同也能对上
    for w in list(terms):
        if "-" in w or "." in w:
            terms.update(p for p in re.split(r"[.\-]", w) if len(p) >= 2)
    for run in CJK_RUN_RE.findall(text):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms


ECHO_HEAD_CHARS = 120


def _strip_echo(body: str, query: str) -> str:
    """很多工具会在结果开头或 JSON 的 query 字段里回显搜索词，去掉，免得把无关结果算成相关。

    只去掉这两处：正文里出现的搜索词才说明结果相关。
    """
    body = re.sub(
        r'"(?:query|q|keyword|search_query)"\s*:\s*"' + re.escape(query) + '"',
        " ",
        body,
    )
    i = body.find(query)
    if 0 <= i < ECHO_HEAD_CHARS:
        body = body[:i] + " " + body[i + len(query) :]
    return body


def relevance(query: str, text: str) -> float | None:
    """结果文本里出现了多少比例的搜索词（0–1）。搜索词拆不出词时返回 None。"""
    terms = query_terms(query)
    if not terms:
        return None
    body = _strip_echo(text.lower(), query.lower())
    hit = sum(1 for t in terms if t in body)
    return round(hit / len(terms), 2)


def classify(
    query: str | None, text: str | None, is_error: bool = False
) -> tuple[str | None, float | None]:
    """返回 (质量, 相关度)。质量为 None 表示正常或无法判断。"""
    if is_error:
        return "error", None
    stripped = (text or "").strip()
    if not stripped:
        return "empty", None
    if len(stripped) <= ERROR_MAX_CHARS and ERROR_RE.search(stripped):
        return "error", None
    if len(stripped) <= EMPTY_MAX_CHARS and EMPTY_RE.search(stripped):
        return "empty", None
    if not query:
        return None, None
    score = relevance(query, stripped)
    if score is not None and score < IRRELEVANT_BELOW:
        return "irrelevant", score
    return None, score


def assess(row: dict[str, Any]) -> dict[str, Any]:
    """根据一个工具在统计窗口里的汇总，给出健康结论。

    ``row`` 需要 total、error、empty、irrelevant、avg_relevance、last_ok_ts 等字段。
    """
    total = int(row.get("total") or 0)
    bad = {
        "error": int(row.get("error") or 0),
        "empty": int(row.get("empty") or 0),
        "irrelevant": int(row.get("irrelevant") or 0),
    }
    bad_total = sum(bad.values())
    ratio = bad_total / total if total else 0.0
    if total < MIN_SAMPLES:
        verdict = "unknown"
    elif ratio >= BROKEN_RATIO:
        verdict = "broken"
    elif ratio >= DEGRADED_RATIO:
        verdict = "degraded"
    else:
        verdict = "ok"
    parts = [f"{QUALITY_LABELS[k]} {v}" for k, v in bad.items() if v]
    reason = f"{total} 次调用" + (f"：{'、'.join(parts)}" if parts else "，均正常")
    avg = row.get("avg_relevance")
    if avg is not None:
        reason += f"；平均相关度 {avg:.2f}"
    return {
        **bad,
        "total": total,
        "bad_ratio": round(ratio, 2),
        "verdict": verdict,
        "reason": reason,
    }


VERDICT_LABELS = {
    "broken": "基本失效",
    "degraded": "不稳定",
    "ok": "正常",
    "unknown": "数据不足",
}
