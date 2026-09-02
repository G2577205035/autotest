"""Deterministic backend used for development and queue recovery tests."""

from __future__ import annotations

import json
import csv
import time

from auto_test.evaluation.backends.base import emit
from auto_test.evaluation.contracts import (
    BackendResult,
    EvaluationRequest,
    EventCallback,
    StopPredicate,
)


class DeterministicMockBackend:
    name = "mock"
    version = "1.0"

    def __init__(self, *, step_delay: float = 0.0):
        self.step_delay = max(0.0, float(step_delay))

    def stop(self, run_id: str) -> bool:
        return bool(run_id)

    def run(
        self,
        request: EvaluationRequest,
        *,
        on_event: EventCallback | None = None,
        should_stop: StopPredicate | None = None,
    ) -> BackendResult:
        request.work_dir.mkdir(parents=True, exist_ok=True)
        cases = list(request.task_config.get("cases") or [{"id": "mock-case-1"}])
        results = []
        emit(on_event, "phase", "Mock 评测准备完成", phase="preparing", progress=10)
        for index, case in enumerate(cases, start=1):
            if should_stop and should_stop():
                emit(on_event, "stopped", "Mock 评测已停止", phase="running")
                return BackendResult(status="stopped", summary={"completed_cases": len(results)})
            if self.step_delay:
                time.sleep(self.step_delay)
            results.append(
                {
                    "case_id": str(case.get("id") or f"mock-case-{index}"),
                    "status": "passed",
                    "score": {"score": 1.0, "passed": True, "scoring_source": "mock"},
                    "metrics": {
                        "latency_ms": float(index),
                        "ttft_ms": float(index),
                        "input_tokens": index * 4,
                        "output_tokens": 1,
                        "total_tokens": index * 4 + 1,
                        "token_source": "api_usage",
                        "output_tokens_per_second": 1000.0 / index,
                        "error_type": "",
                    },
                }
            )
            emit(
                on_event,
                "progress",
                f"Mock 用例 {index}/{len(cases)} 完成",
                phase="running",
                progress=10 + round(index / max(1, len(cases)) * 75),
                completed=index,
                total=len(cases),
            )
        summary = {
            "total_cases": len(results),
            "completed_cases": len(results),
            "passed_cases": len(results),
            "failed_cases": 0,
            "error_cases": 0,
            "success_rate": 100.0 if results else None,
            "quality_score": 100.0 if results else None,
            "token_usage": {
                "input_tokens": sum(item["metrics"]["input_tokens"] for item in results),
                "output_tokens": len(results),
                "total_tokens": sum(item["metrics"]["total_tokens"] for item in results),
                "source_counts": {"api_usage": len(results)},
                "exact": True,
            },
            "performance": {
                "ttft_p50_ms": float(max(1, len(results))) / 2,
                "ttft_p95_ms": float(max(1, len(results))),
                "latency_p95_ms": float(max(1, len(results))),
                "latency_p99_ms": float(max(1, len(results))),
                "output_tokens_per_second": 1000.0,
            },
            "error_types": {},
        }
        output_path = request.work_dir / "mock_result.json"
        output_path.write_text(
            json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary_path = request.work_dir / "summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        responses_path = request.work_dir / "responses.jsonl"
        responses_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
            encoding="utf-8",
        )
        performance_path = request.work_dir / "performance_samples.csv"
        with performance_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("case_id", "status", "token_source", "input_tokens", "output_tokens", "ttft_ms", "latency_ms"),
            )
            writer.writeheader()
            for item in results:
                writer.writerow(
                    {
                        "case_id": item["case_id"],
                        "status": item["status"],
                        **{key: item["metrics"].get(key) for key in writer.fieldnames[2:]},
                    }
                )
        emit(on_event, "completed", "Mock 评测完成", phase="completed", progress=100)
        return BackendResult(
            status="completed",
            summary=summary,
            artifacts={
                "raw_result": str(output_path),
                "summary_json": str(summary_path),
                "responses_jsonl": str(responses_path),
                "performance_csv": str(performance_path),
            },
            raw={"cases": results},
        )
