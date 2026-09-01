"""Deterministic backend used for development and queue recovery tests."""

from __future__ import annotations

import json
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
                    "score": 1.0,
                    "metrics": {"latency_ms": float(index)},
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
            "score": 1.0 if results else None,
        }
        output_path = request.work_dir / "mock_result.json"
        output_path.write_text(
            json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        emit(on_event, "completed", "Mock 评测完成", phase="completed", progress=100)
        return BackendResult(
            status="completed",
            summary=summary,
            artifacts={"raw_result": str(output_path)},
            raw={"cases": results},
        )
