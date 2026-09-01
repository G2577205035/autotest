"""Minimal child-process entry point for an isolated EvalScope runtime."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def _emit(event_type: str, message: str, *, phase: str, progress: int | None = None, **data) -> None:
    print(
        "LIEMA_EVENT "
        + json.dumps(
            {
                "event_type": event_type,
                "message": message,
                "phase": phase,
                "progress": progress,
                "data": data,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(child) for child in value]
    for method_name in ("model_dump", "to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _jsonable(method())
            except Exception:
                pass
    return str(value)


def _run_mock(payload: dict[str, Any]) -> dict[str, Any]:
    task = dict(payload.get("task_config") or {})
    delay = max(0.0, float(task.get("sleep_seconds") or 0.0))
    _emit("phase", "隔离 Mock 运行已启动", phase="running", progress=20)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return {
        "summary": {"total_cases": 1, "completed_cases": 1, "passed_cases": 1},
        "samples": [{"case_id": "isolated-mock", "status": "passed"}],
    }


def _run_evalscope(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "eval")
    task = dict(payload.get("task_config") or {})
    secret_names = list(payload.get("secret_env_names") or [])
    if secret_names:
        task["api_key"] = os.environ.get(secret_names[0], "")
    if mode == "eval":
        from evalscope import run_task
        from evalscope.api.metric.semantics import MetricSelector

        # EvalScope 1.11.1 converts string primary metrics to a selector but drops
        # variant dimensions. Restore the public structured selector from JSON so
        # metrics such as BLEU-1 remain unambiguous during report generation.
        for dataset_options in dict(task.get("dataset_args") or {}).values():
            selector = dataset_options.get("primary_metric")
            if isinstance(selector, dict):
                dataset_options["primary_metric"] = MetricSelector(**selector)

        task.setdefault("work_dir", str(payload["work_dir"]))
        _emit("phase", "EvalScope 标准评测已启动", phase="running", progress=20)
        result = run_task(task_cfg=task)
        return {
            "summary": {"run_groups": len(result) if isinstance(result, dict) else 1},
            "evalscope_return": _jsonable(result),
        }
    if mode == "perf":
        from evalscope.perf.main import run_perf_benchmark

        task.setdefault("outputs_dir", str(payload["work_dir"]))
        task.setdefault("no_timestamp", True)
        _emit("phase", "EvalScope 性能压测已启动", phase="running", progress=20)
        result = run_perf_benchmark(task)
        return {
            "summary": {"run_groups": len(result) if isinstance(result, dict) else 1},
            "evalscope_return": _jsonable(result),
        }
    raise RuntimeError(f"不支持的 EvalScope 运行模式：{mode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
    result = _run_mock(payload) if payload.get("mode") == "mock" else _run_evalscope(payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit("completed", "隔离评测执行完成", phase="completed", progress=100)


if __name__ == "__main__":
    main()
