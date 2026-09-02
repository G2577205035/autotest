"""Deterministic rule scoring and token-estimation helpers."""

from __future__ import annotations

import json
import math
import re
from typing import Any


_CJK = re.compile(r"[\u3400-\u9fff]")
_NON_CJK_TOKEN = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]")


def estimate_token_count(text: str) -> int:
    """Return a conservative trend-only estimate, never an exact billing count."""

    value = str(text or "")
    cjk = len(_CJK.findall(value))
    non_cjk = len(_NON_CJK_TOKEN.findall(_CJK.sub(" ", value)))
    return max(0, cjk + math.ceil(non_cjk * 1.25))


def _json_type_matches(value: Any, expected: str) -> bool:
    normalized = str(expected or "").lower()
    if normalized == "string":
        return isinstance(value, str)
    if normalized == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized == "boolean":
        return isinstance(value, bool)
    if normalized == "array":
        return isinstance(value, list)
    if normalized == "object":
        return isinstance(value, dict)
    if normalized == "null":
        return value is None
    return False


def score_response(text: str, rules: dict[str, Any] | None) -> dict[str, Any]:
    value = str(text or "").strip()
    rules = dict(rules or {})
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, expected: Any = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "expected": expected})

    if "exact_text" in rules:
        expected = str(rules.get("exact_text") or "").strip()
        check("固定表达", value == expected, expected)
    for term in list(rules.get("required_terms") or rules.get("required_facts") or []):
        check(f"必须包含：{term}", str(term) in value, str(term))
    for term in list(rules.get("forbidden_terms") or []):
        check(f"禁止包含：{term}", str(term) not in value, str(term))
    for pattern in list(rules.get("required_patterns") or []):
        try:
            passed = re.search(str(pattern), value) is not None
        except re.error:
            passed = False
        check(f"正则匹配：{pattern}", passed, str(pattern))
    if rules.get("min_chars") is not None:
        minimum = max(0, int(rules["min_chars"]))
        check("最少字符", len(value) >= minimum, minimum)
    if rules.get("max_chars") is not None:
        maximum = max(0, int(rules["max_chars"]))
        check("最多字符", len(value) <= maximum, maximum)
    schema = rules.get("json_schema")
    if isinstance(schema, dict):
        try:
            document = json.loads(value)
            check("JSON 合法", True)
        except (TypeError, ValueError, json.JSONDecodeError):
            document = None
            check("JSON 合法", False)
        if isinstance(document, dict):
            for field in list(schema.get("required") or []):
                check(f"JSON 字段：{field}", str(field) in document, str(field))
            for field, expected_type in dict(schema.get("types") or {}).items():
                check(
                    f"JSON 类型：{field}",
                    field in document and _json_type_matches(document[field], expected_type),
                    expected_type,
                )
    if not checks:
        check("非空响应", bool(value), "有效正文")
    passed_count = sum(1 for item in checks if item["passed"])
    score = passed_count / len(checks)
    return {
        "score": round(score, 6),
        "passed": score >= float(rules.get("pass_threshold") or 0.8),
        "passed_checks": passed_count,
        "total_checks": len(checks),
        "checks": checks,
        "scoring_source": "deterministic_rules",
    }
