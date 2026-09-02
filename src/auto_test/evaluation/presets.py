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
}


RUN_KINDS = {
    "mock": {
        "label": "Mock 流程验证",
        "description": "不调用真实模型，用于验证排队、监控、停止和结果链路。",
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
    if run_kind == "mock":
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
    if run_kind == "foundation":
        return (
            "native",
            "1.0",
            "eval",
            {
                **common,
                "temperature": float(profile.get("temperature") or 0.0),
                "cases": normalized_cases,
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
    stage = PLAN_CONFIGS[plan]
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
            "number": list(stage["number"]),
            "parallel": list(stage["parallel"]),
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
        },
    )
