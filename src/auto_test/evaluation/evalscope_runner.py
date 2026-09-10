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
            # Keep the event transport ASCII-only. The parent decodes the JSON
            # escapes back to Unicode, independent of the host console code page.
            ensure_ascii=True,
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

        return _run_perf_stages(payload, task, run_perf_benchmark)
    raise RuntimeError(f"不支持的 EvalScope 运行模式：{mode}")


def _run_perf_stages(payload: dict[str, Any], task: dict[str, Any], benchmark) -> dict[str, Any]:
    """Bound each load stage and stop escalation after an unhealthy stage."""
    safety = dict(payload.get("safety") or {})
    load = dict(payload.get("load_profile") or {})
    def items(value):
        return value if isinstance(value, list) else [value]
    numbers, parallels, rates = items(task.get("number", 1)), items(task.get("parallel", 1)), items(task.get("rate", -1))
    maximum = max(1, int(safety.get("max_concurrency") or max(parallels)))
    timeout = max(1, int(safety.get("request_timeout_seconds") or 60))
    threshold = float(safety.get("stop_on_error_rate", 0.2))
    stages = []
    for index, number in enumerate(numbers):
        stages.append({"phase": "capacity", "number": number, "parallel": min(maximum, int(parallels[min(index, len(parallels)-1)])), "rate": rates[min(index, len(rates)-1)], "duration": task.get("duration")})
    if load.get("kind") == "deep":
        burst, sustained = dict(load.get("burst") or {}), dict(load.get("sustained") or {})
        if burst:
            stages.append({"phase": "burst", "number": min(int(burst.get("requests") or 1), maximum * 2), "parallel": maximum, "rate": -1, "duration": 30})
        if sustained:
            seconds = max(1, int(sustained.get("duration_seconds") or 60))
            rate = float(sustained.get("target_rps") or 1)
            stages.append({"phase": "sustained", "number": int(seconds * rate), "parallel": maximum, "rate": rate, "duration": seconds})
        recovery = max(0, int(load.get("recovery_observation_seconds") or 0))
        if recovery:
            stages.append({"phase": "recovery", "number": recovery, "parallel": 1, "rate": 1, "duration": recovery})
    execution = {"dispatch_policy": "bounded_rate" if any(float(rate) > 0 for rate in rates) else "fixed_concurrency", "max_concurrency": maximum, "request_timeout_seconds": timeout, "stop_on_error_rate": threshold, "safety_stopped": False, "stages": []}
    results = {}
    for index, stage in enumerate(stages):
        if execution["safety_stopped"] and stage["phase"] != "recovery":
            continue
        root = Path(payload["work_dir"]) / f"stage-{index:02d}-{stage['phase']}"
        options = {**task, **{key: value for key, value in stage.items() if key != "phase"}, "open_loop": False, "outputs_dir": str(root), "no_timestamp": True, "connect_timeout": min(timeout, 15), "read_timeout": timeout, "total_timeout": timeout}
        if stage["phase"] == "recovery":
            options["warmup_num"] = 0
        started = time.time()
        _emit("phase", f"性能阶段 {index+1}/{len(stages)}：{stage['phase']}", phase="running", progress=5 + int(index / len(stages) * 85), stage_index=index, load_phase=stage["phase"], started_at=started)
        result = benchmark(options)
        results[str(index)] = _jsonable(result)
        summaries = [json.loads(path.read_text(encoding="utf-8")) for path in root.rglob("benchmark_summary.json")]
        total = sum(int(item.get("Total Requests") or 0) for item in summaries)
        failed = sum(int(item.get("Failed Requests") or 0) for item in summaries)
        unhealthy = not total or failed / total > threshold
        execution["stages"].append({**stage, "path": root.name, "started_at": started, "finished_at": time.time(), "total_requests": total, "failed_requests": failed, "safety_stopped": unhealthy})
        if unhealthy:
            execution["safety_stopped"] = True
        (Path(payload["work_dir"]) / "performance_execution.json").write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
        _emit("progress", f"性能阶段 {stage['phase']} 完成，请求 {total}，异常 {failed}", phase="running", progress=5 + int((index+1) / len(stages) * 85), load_phase=stage["phase"], total_requests=total, failed_requests=failed)
    return {"summary": {"run_groups": len(results)}, "evalscope_return": results, "performance_execution": execution}


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
