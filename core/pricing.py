"""计费币种与金额格式化。

DeepSeek 中国站按人民币计费（每 1000 次成功请求 5 元），国际站按美元计费（0.75 美元）。
费用上限按所选币种计算；另一种币种只按汇率换算后用于显示。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Currency:
    code: str
    symbol: str
    label: str
    # 每次成功请求的价格（搜索和抓取相同）
    price: float


CURRENCIES = {
    "cny": Currency("cny", "¥", "人民币", 5 / 1000),
    "usd": Currency("usd", "$", "美元", 0.75 / 1000),
}
DEFAULT_CURRENCY = "cny"


def get_currency(code: str | None) -> Currency:
    return CURRENCIES.get((code or "").lower(), CURRENCIES[DEFAULT_CURRENCY])


def format_amount(amount: float, currency: Currency) -> str:
    """¥0.021 / $0.0030 / ¥35.50：小额多保留几位，否则看不出变化。"""
    if amount >= 1:
        return f"{currency.symbol}{amount:.2f}"
    digits = 3 if currency.code == "cny" else 4
    return f"{currency.symbol}{amount:.{digits}f}"


def convert(
    amount: float, currency: Currency, usd_cny_rate: float
) -> tuple[float, Currency] | None:
    """换算成另一种币种，汇率无效时返回 None。"""
    if usd_cny_rate <= 0:
        return None
    if currency.code == "cny":
        return amount / usd_cny_rate, CURRENCIES["usd"]
    return amount * usd_cny_rate, CURRENCIES["cny"]


def format_money(amount: float, currency: Currency, usd_cny_rate: float = 0.0) -> str:
    """主币种金额，后面括号里附上换算后的另一种币种，例如 ¥0.021（$0.0030）。"""
    text = format_amount(amount, currency)
    other = convert(amount, currency, usd_cny_rate)
    if other is not None:
        text += f"（{format_amount(*other)}）"
    return text
