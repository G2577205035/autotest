"""Immutable, exhaustive composition of the platform's real evaluation suites."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from auto_test.evaluation.catalog import latest_suite_for_run_kind
from auto_test.evaluation.presets import RUN_KINDS, build_task_configuration


FULL_STAGES = (
    ("foundation_sync", "foundation", False),
    ("foundation_stream", "foundation", True),
    ("translation", "translation", False),
    ("standard_benchmark", "standard_benchmark", False),
    ("wmt_translation", "wmt_translation", False),
    ("report_writing", "report_writing", False),
    ("intelligence", "intelligence", False),
    ("concurrency", "concurrency", True),
    ("deep_performance", "deep_performance", True),
)


def build_full_configuration(store, project_id, *, profile, judge_profile=None,
                             suite_version_id="", max_tokens=4096, timeout=120,
                             manual_review_percent=0):
    """Freeze every component, including its complete input and provenance.

    The bundle is stored in the run snapshot; it is not a fictitious published
    dataset. Component versions remain normal immutable repository versions.
    """
    definitions = list(FULL_STAGES)
    custom = None
    if suite_version_id:
        version = store.get_model_eval_suite_version(project_id, suite_version_id)
        if not version or version.get("project_id") != project_id:
            raise ValueError("全量测评只能附加当前项目已发布的测试集")
        suite = store.get_model_eval_suite(project_id, version["suite_id"], include_global=False)
        if not suite:
            raise ValueError("项目测试集不存在")
        cases = store.list_model_eval_suite_cases(project_id, version["id"], 5000)
        custom = (suite, version, cases)
        definitions.insert(7, ("custom", "custom", False))
    stages, catalog = [], []
    for stage_id, kind, stream in definitions:
        suite, version, cases = custom if kind == "custom" else latest_suite_for_run_kind(store, project_id, kind)
        label = RUN_KINDS[kind]["label"]
        if kind == "foundation":
            label += "（流式）" if stream else "（非流式）"
        # Quality outputs need room; performance uses a fixed, bounded budget.
        budget = min(max_tokens, 512) if kind in {"concurrency", "deep_performance"} else max_tokens
        backend, backend_version, mode, config = build_task_configuration(
            run_kind=kind, plan="deep", profile=profile, judge_profile=judge_profile,
            cases=cases, stream=stream, max_tokens=budget, timeout=timeout,
        )
        if backend == "evalscope" and mode == "eval":
            config["generation_config"].update(timeout=float(timeout), temperature=float(profile.get("temperature") or 0.0))
        source_cases = deepcopy(cases)
        namespaced = []
        for item in source_cases:
            item["id"] = f"{stage_id}:{item['id']}"
            item["case_key"] = f"{stage_id}:{item.get('case_key') or item['id']}"
            item["payload"]["name"] = label + " / " + str(item["payload"].get("name") or item["case_key"])
            namespaced.append(item)
        if backend == "native":
            # A full run must not silently inherit the standalone custom limit.
            config["cases"] = namespaced
            config["manual_review_percent"] = manual_review_percent
        catalog.extend(namespaced)
        stages.append({
            "id": stage_id, "label": label, "run_kind": kind, "backend": backend,
            "backend_version": backend_version, "mode": mode, "stream": stream,
            "expected_cases": len(cases) if mode != "perf" else 0,
            "suite": {"id": suite["id"], "name": suite["name"], "version_id": version["id"],
                      "version": version["version"], "content_sha256": version["content_sha256"],
                      "manifest": version.get("manifest") or {}, "upstream": version.get("upstream") or {}},
            "case_ids": [item["id"] for item in namespaced], "task_config": config,
        })
    digest = hashlib.sha256(json.dumps(stages, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    content_digest = hashlib.sha256(json.dumps([{k: s[k] for k in ("id", "suite")} for s in stages], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    suite = {"id": "full-bundle", "name": "全量测评组合（全部内置维度）"}
    version = {"id": "", "version": "full-1.0", "content_sha256": content_digest,
               "manifest": {"kind": "immutable_run_bundle", "component_count": len(stages)}, "upstream": {}}
    config = {"stages": stages, "cases": catalog, "judge": stages[0]["task_config"].get("judge") or {},
              "manual_review_percent": manual_review_percent, "bundle_sha256": digest}
    return suite, version, config
