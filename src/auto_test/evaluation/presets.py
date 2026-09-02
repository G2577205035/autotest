"""Translate user-facing evaluation plans into safe backend task configurations."""

from __future__ import annotations

from typing import Any

from auto_test.evaluation.model_client import chat_completions_url


PLAN_CONFIGS = {
    "quick": {
        "label": "快速体检",
        "description": "小样本基础能力与 1→2 并发阶梯。",
        "parallel": [1, 2],
        "number": [4, 8],
    },
    "standard": {
        "label": "标准评测",
        "description": "完整 MVP 基础集与 1→2→4 并发阶梯。",
        "parallel": [1, 2, 4],
        "number": [8, 12, 16],
    },
    "deep": {
        "label": "深度评测",
        "description": "全量专项能力与 1→2→4→8→16 容量阶梯，包含突发、持续与恢复观察。",
        "parallel": [1, 2, 4, 8, 16],
        "number": [16, 24, 40, 80, 160],
    },
}


RUN_KINDS = {
    "mock": {
        "label": "Mock 流程验证",
        "description": "不调用真实模型，用于验证排队、监控、停止和结果链路。",
    },
    "mock_full": {
        "label": "完整进阶流程验证",
        "description": "不调用真实模型，验收翻译、报告写作、情报、复核和报告链路。",
    },
    "foundation": {
        "label": "基础能力与鲁棒性",
        "description": "平台规则用例，覆盖 Token 来源、上下文、结构化输出和鲁棒变体。",
    },
    "standard_benchmark": {
        "label": "标准 Benchmark",
        "description": "通过隔离 EvalScope 1.11.1 执行本地 general_qa。",
    },
    "concurrency": {
        "label": "并发阶梯",
        "description": "通过 EvalScope perf 执行固定并发阶梯并采集吞吐、TTFT 与错误率。",
    },
    "deep_performance": {
        "label": "深度性能与容量",
        "description": "固定并发/RPS、突发和持续负载，识别容量拐点并保留恢复观察配置。",
    },
    "translation": {
        "label": "中英双向翻译",
        "description": "平台专项用例，检查术语、实体、数字、格式、占位符与多参考译文。",
    },
    "wmt_translation": {
        "label": "WMT2024++ 标准翻译",
        "description": "通过隔离 EvalScope 1.11.1 执行本地 en-zh_cn 标准翻译样例。",
    },
    "report_writing": {
        "label": "报告写作能力",
        "description": "检查事实覆盖、禁止虚构、章节结构、风险区分和建议可执行性。",
    },
    "intelligence": {
        "label": "情报生产能力",
        "description": "检查时间线、来源映射、矛盾识别、不确定性与线索建议。",
    },
    "custom": {
        "label": "项目自定义测试集",
        "description": "执行当前项目已经校验并发布的不可变测试集版本。",
    },
}


_CASE_LIMITS = {
    "translation": {"quick": 20, "standard": 100, "deep": 200},
    "report_writing": {"quick": 2, "standard": 8, "deep": 8},
    "intelligence": {"quick": 2, "standard": 8, "deep": 8},
    "custom": {"quick": 20, "standard": 200, "deep": 1000},
}


def public_preset_catalog() -> dict[str, Any]:
    return {
        "plans": [{"id": key, **value} for key, value in PLAN_CONFIGS.items()],
        "run_kinds": [{"id": key, **value} for key, value in RUN_KINDS.items()],
    }


def build_task_configuration(
    *,
    run_kind: str,
    plan: str,
    profile: dict[str, Any] | None,
    cases: list[dict[str, Any]],
    stream: bool,
    max_tokens: int,
    timeout: float,
    judge_profile: dict[str, Any] | None = None,
) -> tuple[str, str, str, dict[str, Any]]:
    """Return backend, version, mode and a secret-free task configuration."""

    if run_kind not in RUN_KINDS:
        raise ValueError("不支持的模型评测类型")
    if plan not in PLAN_CONFIGS:
        raise ValueError("不支持的模型评测方案")
    normalized_cases = [
        {
            "id": str(item.get("id") or ""),
            "case_key": str(item.get("case_key") or ""),
            "category": str(item.get("category") or "general"),
            "payload": dict(item.get("payload") or {}),
        }
        for item in cases
    ]
    case_limit = (_CASE_LIMITS.get(run_kind) or {}).get(plan)
    if case_limit:
        normalized_cases = normalized_cases[:case_limit]
    if run_kind in {"mock", "mock_full"}:
        return "mock", "1.0", "mock", {"cases": normalized_cases}
    if not profile:
        raise ValueError("请选择已配置的被测模型")
    model = str(profile.get("model_name") or "").strip()
    api_url = chat_completions_url(str(profile.get("base_url") or ""))
    common = {
        "model": model,
        "api_url": api_url,
        "stream": bool(stream),
        "max_tokens": max(1, int(max_tokens)),
        "timeout": max(1.0, float(timeout)),
    }
    if run_kind in {"foundation", "translation", "report_writing", "intelligence", "custom"}:
        judge = {}
        if judge_profile:
            judge = {
                "profile_id": str(judge_profile.get("id") or ""),
                "model": str(judge_profile.get("model_name") or ""),
                "api_url": chat_completions_url(str(judge_profile.get("base_url") or "")),
                "temperature": 0.0,
                "max_tokens": min(1024, max(256, int(max_tokens))),
            }
        return (
            "native",
            "1.0",
            "eval",
            {
                **common,
                "temperature": float(profile.get("temperature") or 0.0),
                "cases": normalized_cases,
                "evaluation_dimension": run_kind,
                "judge": judge,
            },
        )
    if run_kind == "standard_benchmark":
        inline = [
            {
                "question": str((item.get("payload") or {}).get("question") or ""),
                "answer": str((item.get("payload") or {}).get("answer") or ""),
            }
            for item in normalized_cases
        ]
        return (
            "evalscope",
            "1.11.1",
            "eval",
            {
                "model": model,
                "model_id": model,
                "datasets": ["general_qa"],
                "dataset_args": {
                    "general_qa": {
                        "dataset_id": "__LIEMA_INLINE_DATASET__",
                        "subset_list": ["default"],
                        "default_subset": "default",
                        "eval_split": "test",
                    }
                },
                "eval_type": "openai_api",
                "api_url": api_url,
                "generation_config": {"max_tokens": max(1, int(max_tokens)), "stream": bool(stream)},
                "eval_batch_size": 1,
                "limit": len(inline) if plan == "standard" else min(2, len(inline)),
                "no_timestamp": True,
                "collect_perf": True,
                "_inline_dataset": inline,
                "_inline_dataset_kind": "general_qa",
            },
        )
    if run_kind == "wmt_translation":
        inline = [
            {
                "source": str((item.get("payload") or {}).get("source") or ""),
                "target": str((item.get("payload") or {}).get("target") or ""),
                "language_pair": str((item.get("payload") or {}).get("language_pair") or "en-zh_cn"),
            }
            for item in normalized_cases
        ]
        return (
            "evalscope",
            "1.11.1",
            "eval",
            {
                "model": model,
                "model_id": model,
                "datasets": ["wmt24pp"],
                "dataset_args": {
                    "wmt24pp": {
                        "local_path": "__LIEMA_INLINE_DATASET__",
                        "subset_list": ["en-zh_cn"],
                        "default_subset": "default",
                        "eval_split": "test",
                        "metric_list": [{"bleu": {}}],
                        "primary_metric": {"name": "bleu", "dimensions": {"ngram": 1}},
                    }
                },
                "eval_type": "openai_api",
                "api_url": api_url,
                "generation_config": {"max_tokens": max(1, int(max_tokens)), "stream": bool(stream)},
                "eval_batch_size": 1,
                "limit": len(inline),
                "no_timestamp": True,
                "collect_perf": True,
                "_inline_dataset": inline,
                "_inline_dataset_kind": "wmt24pp",
            },
        )
    stage = PLAN_CONFIGS[plan]
    deep_rate_sweep = run_kind == "deep_performance"
    fixed_rates = [1.0, 2.0, 4.0, 8.0]
    request_counts = [60, 120, 240, 480]
    prompt_rows = [
        {"question": str((item.get("payload") or {}).get("question") or "")}
        for item in normalized_cases
    ]
    return (
        "evalscope",
        "1.11.1",
        "perf",
        {
            "model": model,
            "api": "openai",
            "url": api_url,
            "number": request_counts if deep_rate_sweep else list(stage["number"]),
            "parallel": [1] if deep_rate_sweep else list(stage["parallel"]),
            "rate": fixed_rates if deep_rate_sweep else -1,
            "open_loop": deep_rate_sweep,
            "warmup_num": 0.1 if deep_rate_sweep else 0,
            "duration": 60 if deep_rate_sweep else None,
            "dataset": "openqa",
            "dataset_path": "__LIEMA_INLINE_DATASET__",
            "data_source": "local",
            "max_tokens": max(1, int(max_tokens)),
            "stream": bool(stream),
            "sleep_interval": 0,
            "num_workers": max(stage["parallel"]),
            "_inline_dataset": prompt_rows,
            "_inline_dataset_kind": "openqa",
            "_safety": {
                "max_concurrency": max(stage["parallel"]),
                "stop_on_error_rate": 0.2,
                "request_timeout_seconds": max(1.0, float(timeout)),
            },
            "_load_profile": {
                "kind": "deep" if run_kind == "deep_performance" else "concurrency",
                "fixed_rps": fixed_rates if deep_rate_sweep else [],
                "open_loop": deep_rate_sweep,
                "stage_duration_seconds": 60 if deep_rate_sweep else 0,
                "burst": {"target_rps": max(fixed_rates), "requests": max(request_counts)} if deep_rate_sweep else {},
                "sustained": {"target_rps": fixed_rates[-2], "duration_seconds": 60} if deep_rate_sweep else {},
                "recovery_observation_seconds": 60 if run_kind == "deep_performance" else 0,
            },
        },
    )
