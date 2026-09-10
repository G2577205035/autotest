"""Conservative, opt-in equivalence checks for bilingual factual values.

Dates, quantities and sequence numbers are separate types: a date or a protected
identifier must never satisfy a batch number, and currency scales remain exact.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation


_MONTHS = "january february march april may june july august september october november december".split()
_MONTH = "(?:" + "|".join(_MONTHS) + ")"
_DATE_PATTERNS = (
    (re.compile(r"(?<!\d)(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})(?:日)?(?!\d)"), (1, 2, 3)),
    (re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b", re.I), (3, 1, 2)),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})[,]?\s+(\d{{4}})\b", re.I), (3, 2, 1)),
)
_CURRENCY = r"(?:RMB|CNY|yuan|人民币|元|USD|dollars?|美元|EUR|euros?|欧元|[$€¥￥])"
_SCALE = r"(?:thousand|million|billion|万|亿|千)"
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_QUANTITY = re.compile(rf"(?<![A-Za-z0-9_.,])(?P<prefix>{_CURRENCY})?\s*(?P<number>{_NUMBER})\s*(?P<scale>{_SCALE})?\s*(?P<suffix>{_CURRENCY}|%|percent)?(?![A-Za-z0-9_]|[.,]\d)", re.I)
_SCALES = {"thousand": 1000, "千": 1000, "million": 1000000, "billion": 1000000000, "万": 10000, "亿": 100000000}
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_ORDINALS = "zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth".split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_TENTHS = "twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth".split()


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


def dates_in(text: str) -> set[str]:
    found = set()
    for pattern, order in _DATE_PATTERNS:
        for match in pattern.finditer(normalized(text)):
            year, month, day = (match.group(index) for index in order)
            try:
                month_number = _MONTHS.index(month) + 1 if month in _MONTHS else int(month)
                found.add(date(int(year), month_number, int(day)).isoformat())
            except (ValueError, OverflowError):
                continue
    return found


def _currency(value: str) -> str:
    if value in {"rmb", "cny", "yuan", "人民币", "元", "¥", "￥"}:
        return "CNY"
    if value in {"usd", "dollar", "dollars", "美元", "$"}:
        return "USD"
    if value in {"eur", "euro", "euros", "欧元", "€"}:
        return "EUR"
    return value


def has_quantity(text: str, expected: dict) -> bool:
    try:
        target = Decimal(str(expected["value"]))
    except (KeyError, InvalidOperation):
        return False
    value = re.sub(r"百分之\s*(\d+(?:\.\d+)?)", r"\1%", normalized(text))
    for match in _QUANTITY.finditer(value):
        amount = Decimal(match["number"].replace(",", "")) * _SCALES.get(match["scale"], 1)
        prefix, suffix = _currency(match["prefix"] or ""), _currency(match["suffix"] or "")
        if prefix and suffix and prefix != suffix:
            continue
        unit = prefix or suffix
        if expected.get("currency") and unit != expected["currency"]:
            continue
        if expected.get("unit") == "%" and unit not in {"%", "percent"}:
            continue
        if amount == target:
            return True
    return False


def _sequence_forms(number: int) -> set[str]:
    forms = {str(number), f"{number}(?:st|nd|rd|th)"}
    if 0 <= number < 20:
        forms.update((_ONES[number], _ORDINALS[number]))
    elif 20 <= number < 100:
        tens, ones = divmod(number, 10)
        forms.add(_TENS[tens - 2] if not ones else _TENS[tens - 2] + r"[ -]" + _ONES[ones])
        forms.add(_TENTHS[tens - 2] if not ones else _TENS[tens - 2] + r"[ -]" + _ORDINALS[ones])
    elif number == 100:
        forms.update(("one hundred", "hundredth", "one hundredth"))
    digits = "零一二三四五六七八九"
    if 0 <= number < 10:
        forms.add(digits[number])
    elif 10 <= number < 100:
        tens, ones = divmod(number, 10)
        forms.add((digits[tens] if tens > 1 else "") + "十" + (digits[ones] if ones else ""))
    elif number == 100:
        forms.add("一百")
    return forms


def has_sequence(text: str, number: int, protected_spans: list[str]) -> bool:
    value = normalized(text)
    for span in protected_spans:
        value = value.replace(normalized(span), " ")
    for pattern, _ in _DATE_PATTERNS:
        value = pattern.sub(" ", value)
    form = "(?:" + "|".join(sorted(_sequence_forms(number), key=len, reverse=True)) + ")"
    patterns = (
        rf"\b(?:batch|round)\s+(?:(?:number|no\.?)\s*)?{form}\b",
        rf"\b{form}\s+(?:test\s+|acceptance\s+)?(?:batch|round)\b",
        rf"第\s*{form}\s*(?:批|轮)",
        rf"(?:验收)?(?:轮次|批次)\s*{form}(?![A-Za-z0-9一二三四五六七八九十百])",
    )
    return any(re.search(pattern, value) for pattern in patterns)
