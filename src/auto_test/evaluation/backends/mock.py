"""Deterministic backend used for development and queue recovery tests."""

from __future__ import annotations

import json
import csv
import time
from statistics import mean

from auto_test.evaluation.backends.base import emit
from auto_test.evaluation.contracts import (
    BackendResult,
    EvaluationRequest,
    EventCallback,
    StopPredicate,
)
from auto_test.evaluation.advanced import merge_rule_and_judge_score, select_manual_review_indices
from auto_test.evaluation.scoring import score_response


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
        review_percent = max(0, min(int(request.task_config.get("manual_review_percent") or 0), 100))
        review_indices = select_manual_review_indices(len(cases), review_percent)
        results = []
        emit(on_event, "phase", "Mock 评测准备完成", phase="preparing", progress=10)
        for index, case in enumerate(cases, start=1):
            if should_stop and should_stop():
                emit(on_event, "stopped", "Mock 评测已停止", phase="running")
                return BackendResult(
                    status="stopped",
                    summary=self._summary(results),
                    raw={"cases": results},
                )
            if self.step_delay:
                time.sleep(self.step_delay)
            payload = dict(case.get("payload") or {})
            rules = dict(payload.get("rules") or {})
            response_value: Any = (
                payload.get("mock_response")
                or ((payload.get("references") or [""])[0] if isinstance(payload.get("references"), list) else "")
                or rules.get("exact_text")
            )
            schema = rules.get("json_schema")
            if not response_value and isinstance(schema, dict):
                response_value = {
                    field: {
                        "string": "ok",
                        "integer": 1,
                        "number": 1.0,
                        "boolean": True,
                        "array": [],
                        "object": {},
                        "null": None,
                    }.get(str((schema.get("types") or {}).get(field) or "string"), "ok")
                    for field in list(schema.get("required") or [])
                }
                response_value = json.dumps(response_value, ensure_ascii=False)
            if not response_value:
                response_value = " ".join(
                    str(item)
                    for key in ("required_terms", "required_facts", "required_entities", "required_numbers")
                    for item in list(rules.get(key) or [])
                ) or "READY"
            response_text = str(response_value)
            if payload.get("references") and not rules.get("references"):
                rules["references"] = list(payload.get("references") or [])
            score = merge_rule_and_judge_score(score_response(response_text, rules), None)
            review_required = index - 1 in review_indices
            score["manual_review"] = {
                "status": "pending" if review_required else "not_required",
                "required": review_required,
            }
            if review_required:
                score["scoring_source"] = "deterministic_rules_pending_review"
            result = {
                "case_id": str(case.get("id") or f"mock-case-{index}"),
                "case_key": str(case.get("case_key") or ""),
                "name": str(payload.get("name") or case.get("case_key") or f"Mock 用例 {index}"),
                "category": str(case.get("category") or "mock"),
                "status": "passed" if score.get("passed") else "failed",
                "score": score,
                "response": response_text,
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
            results.append(result)
            partial_summary = self._summary(results)
            emit(
                on_event,
                "progress",
                f"Mock 用例 {index}/{len(cases)} 完成",
                phase="running",
                progress=10 + round(index / max(1, len(cases)) * 75),
                completed=index,
                total=len(cases),
                case_result=result,
                partial_summary=partial_summary,
            )
        summary = self._summary(results)
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

    @staticmethod
    def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total_cases": len(results),
            "completed_cases": len(results),
            "passed_cases": sum(1 for item in results if item["status"] == "passed"),
            "failed_cases": sum(1 for item in results if item["status"] == "failed"),
            "error_cases": 0,
            "success_rate": round(sum(1 for item in results if item["status"] == "passed") / len(results) * 100, 2) if results else None,
            "quality_score": round(mean(float(item["score"]["score"]) for item in results) * 100, 2) if results else None,
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
            "dimensions": {
                category: {
                    "case_count": len(values),
                    "quality_score": round(mean(float(item["score"]["score"]) for item in values) * 100, 2),
                }
                for category, values in {
                    name: [item for item in results if item.get("category") == name]
                    for name in sorted({str(item.get("category") or "mock") for item in results})
                }.items()
            },
            "scoring": {
                "rule_scored_cases": len(results),
                "judge_status_counts": {"not_configured": len(results)},
                "manual_reviewed_cases": 0,
                "pending_manual_review_cases": sum(
                    1
                    for item in results
                    if ((item.get("score") or {}).get("manual_review") or {}).get("status")
                    == "pending"
                ),
                "confidence": 75.0,
            },
        }
