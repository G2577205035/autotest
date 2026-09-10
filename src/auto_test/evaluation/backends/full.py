"""Sequential all-dimension backend with durable partial evidence and safe stop."""

from __future__ import annotations

import csv
import json
import threading
import time
from copy import deepcopy

from auto_test.evaluation.contracts import BackendEvent, BackendResult, EvaluationRequest


def summarize_full(stages, cases):
    native = [item for item in cases if item.get("evaluation_backend") == "native"]
    passed = sum(item.get("status") == "passed" for item in native)
    failed = sum(item.get("status") == "failed" for item in native)
    errored = sum(item.get("status") == "error" for item in native)
    performance, execution = [], []
    for component in stages:
        summary = component.get("summary") or {}
        for stage in (summary.get("performance") or {}).get("stages", []):
            performance.append({**stage, "evaluation_stage": component["id"]})
        for window in (summary.get("performance_execution") or {}).get("stages", []):
            execution.append({**window, "evaluation_stage": component["id"]})
    return {
        "mode": "full", "stages": deepcopy(stages), "completed_stages": sum(s["status"] == "completed" for s in stages),
        "total_stages": len(stages), "total_cases": sum(s["expected_cases"] for s in stages if s["backend"] == "native"),
        "completed_cases": len(native), "passed_cases": passed, "failed_cases": failed, "error_cases": errored,
        "success_rate": round(100 * passed / len(native), 2) if native else None,
        "quality_score": None, "performance": {"stages": performance}, "performance_execution": {"stages": execution},
    }


class FullEvaluationBackend:
    name, version = "full", "1.0"

    def __init__(self, resolver):
        self.resolver = resolver
        self._lock = threading.Lock()
        self._active = {}
        self._stopped = set()

    def stop(self, run_id):
        with self._lock:
            self._stopped.add(run_id)
            backend = self._active.get(run_id)
        if backend:
            backend.stop(run_id)
        return True

    def run(self, request, *, on_event=None, should_stop=None):
        root = request.work_dir
        root.mkdir(parents=True, exist_ok=True)
        configs = request.task_config["stages"]
        stages = [{k: deepcopy(v) for k, v in item.items() if k not in {"task_config", "case_ids"}} | {"status": "pending"} for item in configs]
        cases, evidence = {}, []
        def stopped():
            return request.run_id in self._stopped or bool(should_stop and should_stop())
        def summary():
            return summarize_full(stages, list(cases.values()))
        def checkpoint():
            (root / "summary.json").write_text(json.dumps(summary(), ensure_ascii=False, indent=2), encoding="utf-8")
            (root / "responses.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in cases.values()), encoding="utf-8")
            (root / "full_evidence.json").write_text(json.dumps({"schema_version": "1.0", "task_config": request.task_config, "summary": summary(), "components": evidence}, ensure_ascii=False, indent=2), encoding="utf-8")
        perf_blocked = False
        for index, (config, stage) in enumerate(zip(configs, stages)):
            if stopped() or (perf_blocked and config["mode"] == "perf"):
                stage.update(status="skipped", error_type="user_stopped" if stopped() else "preceding_performance_failure")
                checkpoint()
                continue
            stage.update(status="running", started_at=time.time())
            def record(event):
                data = deepcopy(event.data)
                item = data.pop("case_result", None)
                if item:
                    item["evaluation_stage"] = config["id"]
                    item["evaluation_backend"] = config["backend"]
                    item.setdefault("metrics", {})["evaluation_stage"] = config["id"]
                    item["metrics"]["stream"] = config["stream"]
                    cases[item["case_id"]] = item
                    data["case_result"] = item
                partial = data.pop("partial_summary", None)
                if partial is not None:
                    stage["summary"] = partial
                data["partial_summary"] = summary()
                data["evaluation_stage"] = config["id"]
                if on_event:
                    on_event(BackendEvent(event.event_type, f"{index + 1}/{len(stages)} {config['label']}：{event.message}", "running", min(99, int((index + (event.progress or 0) / 100) / len(stages) * 100)), data))
            record(BackendEvent("stage", "开始执行", progress=0))
            try:
                backend = self.resolver(config)
                with self._lock:
                    self._active[request.run_id] = backend
                result = backend.run(EvaluationRequest(
                    run_id=request.run_id, project_id=request.project_id, mode=config["mode"],
                    task_config=deepcopy(config["task_config"]), work_dir=root / config["id"],
                    backend_version=config["backend_version"], secret_env=request.secret_env,
                ), on_event=record, should_stop=stopped)
                if config["backend"] == "evalscope" and result.status != "completed":
                    from auto_test.evaluation.result_mapper import EvalScopeResultMapper
                    partial = EvalScopeResultMapper().map_directory(root / config["id"], secret_values=request.secret_env.values())
                    result = BackendResult(result.status, {**partial.get("summary", {}), **result.summary}, raw=partial, error_type=result.error_type)
                stage.update(status=result.status, summary=result.summary, error_type=result.error_type)
                if result.status == "completed" and config["mode"] != "perf" and int(result.summary.get("completed_cases") or 0) < config["expected_cases"]:
                    stage.update(status="failed", error_type="incomplete_case_execution")
                evidence.append({"id": config["id"], "raw": result.raw})
                items = result.raw.get("cases") or [s for s in result.raw.get("samples", []) if "/predictions/" in "/" + str(s.get("path") or "").replace("\\", "/")]
                for n, original in enumerate(items):
                    item = deepcopy(original)
                    if config["backend"] != "native":
                        item["case_id"] = (config["case_ids"][n] if n < len(config["case_ids"]) else f"{config['id']}:result-{n + 1}")
                        output = item.get("model_output") or {}
                        observed = output.get("perf_metrics") or {}
                        usage = output.get("usage") or {}
                        item["metrics"] = {
                            "latency_ms": observed["latency"] * 1000 if isinstance(observed.get("latency"), (int, float)) else None,
                            "ttft_ms": observed["ttft"] * 1000 if isinstance(observed.get("ttft"), (int, float)) else None,
                            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "total_tokens": usage.get("total_tokens"),
                            "token_source": "api_usage" if usage else "unknown", "error_type": "backend_request_error" if output.get("error") else "",
                        }
                        item["status"] = "error" if output.get("error") else "completed"
                    item["evaluation_stage"] = config["id"]
                    item["evaluation_backend"] = config["backend"]
                    if not isinstance(item.get("metrics"), dict):
                        item["metrics"] = {}
                    item["metrics"]["evaluation_stage"] = config["id"]
                    item["metrics"]["stream"] = config["stream"]
                    cases[item["case_id"]] = item
                if result.status == "stopped":
                    self._stopped.add(request.run_id)
                if config["mode"] == "perf" and result.status != "completed":
                    perf_blocked = True
            except Exception as exc:
                # Never persist exception text containing endpoint credentials.
                stage.update(status="failed", error_type=type(exc).__name__)
                if config["mode"] == "perf":
                    perf_blocked = True
            finally:
                with self._lock:
                    self._active.pop(request.run_id, None)
                stage["finished_at"] = time.time()
                checkpoint()
                record(BackendEvent("stage_finished", "阶段结束：" + stage["status"], progress=100))
        outcome = "stopped" if stopped() else "completed" if all(s["status"] == "completed" for s in stages) else "failed"
        final = summary()
        fields = ["evaluation_stage", "phase", "concurrency", "target_rps", "total_requests", "successful_requests", "failed_requests", "success_rate", "request_throughput", "output_token_throughput", "total_token_throughput", "ttft_p50_ms", "ttft_p95_ms", "ttft_p99_ms", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "input_tokens_average", "output_tokens_average"]
        with (root / "performance_samples.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(final["performance"]["stages"])
        return BackendResult(status=outcome, summary=final, raw={"cases": list(cases.values())}, error_type="incomplete_full_evaluation" if outcome == "failed" else "")
