"""Aggregate baseline/variant robustness retention without inflating weak baselines."""

from __future__ import annotations

from statistics import mean
from typing import Any


def summarize_robustness(
    case_results: list[dict[str, Any]], *, baseline_threshold: float = 0.6
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for item in case_results:
        group_name = str(item.get("robustness_group") or "")
        if not group_name:
            continue
        group = groups.setdefault(group_name, {"baseline": None, "variants": []})
        score = float((item.get("score") or {}).get("score") or 0.0)
        if str(item.get("variant_type") or "") == "baseline":
            group["baseline"] = score
        else:
            group["variants"].append(
                {"case_id": item.get("case_id"), "score": score, "variant_type": item.get("variant_type")}
            )

    valid_groups: list[dict[str, Any]] = []
    unavailable = 0
    all_retention: list[float] = []
    for name, group in groups.items():
        baseline = group["baseline"]
        if baseline is None or baseline < baseline_threshold or not group["variants"]:
            unavailable += 1
            valid_groups.append(
                {"group": name, "available": False, "baseline_score": baseline, "reason": "基准得分不足或没有变体"}
            )
            continue
        variants = []
        for variant in group["variants"]:
            retention = min(float(variant["score"]) / baseline, 1.0) * 100
            variants.append({**variant, "retention": round(retention, 2)})
            all_retention.append(retention)
        valid_groups.append(
            {
                "group": name,
                "available": True,
                "baseline_score": baseline,
                "average_retention": round(mean(item["retention"] for item in variants), 2),
                "worst_retention": round(min(item["retention"] for item in variants), 2),
                "variants": variants,
            }
        )
    return {
        "available_groups": sum(1 for item in valid_groups if item.get("available")),
        "unavailable_groups": unavailable,
        "average_retention": round(mean(all_retention), 2) if all_retention else None,
        "worst_retention": round(min(all_retention), 2) if all_retention else None,
        "groups": valid_groups,
    }
