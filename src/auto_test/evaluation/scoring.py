"""Deterministic rule scoring and token-estimation helpers."""

from __future__ import annotations

import json
import math
import re
from difflib import SequenceMatcher
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
    for term in list(rules.get("required_terms") or []):
        check(f"术语保持：{term}", str(term).casefold() in value.casefold(), str(term))
    for fact in list(rules.get("required_facts") or []):
        check(f"事实覆盖：{fact}", str(fact).casefold() in value.casefold(), str(fact))
    for entity in list(rules.get("required_entities") or []):
        check(f"实体保持：{entity}", str(entity).casefold() in value.casefold(), str(entity))
    for number in list(rules.get("required_numbers") or []):
        check(f"数字保持：{number}", str(number) in value, str(number))
    for unit in list(rules.get("required_units") or []):
        check(f"单位保持：{unit}", str(unit).casefold() in value.casefold(), str(unit))
    for span in list(rules.get("protected_spans") or []):
        check(f"保护片段：{span}", str(span) in value, str(span))
    for marker in list(rules.get("required_format") or rules.get("format_markers") or []):
        check(f"格式保持：{marker}", str(marker) in value, str(marker))
    for section in list(rules.get("required_sections") or []):
        pattern = rf"(?:^|\n)[ \t]*(?:#+[ \t]*)?{re.escape(str(section))}[ \t]*(?::|：)?[ \t]*(?=\n|$)"
        check(f"章节结构：{section}", re.search(pattern, value, re.I) is not None, str(section))
    for term in list(rules.get("forbidden_terms") or []):
        check(f"禁止包含：{term}", str(term) not in value, str(term))
    for pattern in list(rules.get("required_patterns") or []):
        try:
            passed = re.search(str(pattern), value) is not None
        except re.error:
            passed = False
        check(f"正则匹配：{pattern}", passed, str(pattern))
    allowed_numbers = rules.get("allowed_numbers")
    if isinstance(allowed_numbers, list):
        allowed = {str(item) for item in allowed_numbers}
        observed_numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)*(?:%|％)?", value))
        unsupported = sorted(observed_numbers - allowed)
        check("无依据数字", not unsupported, sorted(allowed))
    references = rules.get("references")
    if isinstance(references, list) and references:
        similarities = [
            SequenceMatcher(None, value.casefold(), str(reference).strip().casefold()).ratio()
            for reference in references
            if str(reference).strip()
        ]
        if similarities:
            minimum = max(0.0, min(float(rules.get("min_reference_similarity") or 0.2), 1.0))
            check("多参考译文相似度", max(similarities) >= minimum, minimum)
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
        "failed_checks": [item["name"] for item in checks if not item["passed"]],
        "scoring_source": "deterministic_rules",
    }
