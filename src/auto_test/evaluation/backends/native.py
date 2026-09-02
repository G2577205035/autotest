"""Native low-concurrency backend for deterministic platform-owned evaluation cases."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from auto_test.evaluation.backends.base import emit
from auto_test.evaluation.contracts import (
    BackendResult,
    EvaluationRequest,
    EventCallback,
    StopPredicate,
)
from auto_test.evaluation.model_client import EvaluationModelClient
from auto_test.evaluation.robustness import summarize_robustness
from auto_test.evaluation.scoring import score_response


def _percentile(values: list[float], percentile: float) -> float | None:
    clean = sorted(float(item) for item in values if item is not None and math.isfinite(float(item)))
    if not clean:
        return None
    position = (len(clean) - 1) * max(0.0, min(percentile, 100.0)) / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(clean[lower], 4)
    value = clean[lower] * (upper - position) + clean[upper] * (position - lower)
    return round(value, 4)


class NativeEvaluationBackend:
    name = "native"
    version = "1.0"

    def __init__(self, *, client: EvaluationModelClient | None = None):
        self.client = client or EvaluationModelClient()

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
        config = dict(request.task_config)
        cases = list(config.get("cases") or [])
        if not cases:
            raise ValueError("原生模型评测没有可执行用例")
        api_key = str(request.secret_env.get("LIEMA_EVAL_MODEL_API_KEY") or "")
        results: list[dict[str, Any]] = []
        emit(on_event, "phase", "基础能力测试准备完成", phase="preparing", progress=5)
        for index, case in enumerate(cases, start=1):
            if should_stop and should_stop():
                self._write_artifacts(request.work_dir, results, self._summary(results))
                emit(on_event, "stopped", "基础评测已安全停止", phase="stopped")
                return BackendResult(
                    status="stopped", summary=self._summary(results), raw={"cases": results}
                )
            payload = dict(case.get("payload") or case)
            messages = [
                {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
                for item in list(payload.get("messages") or [])
                if isinstance(item, dict)
            ]
            if not messages and payload.get("prompt"):
                messages = [{"role": "user", "content": str(payload["prompt"])}]
            observed = self.client.call(
                base_url=str(config.get("api_url") or ""),
                model=str(config.get("model") or ""),
                messages=messages,
                api_key=api_key,
                temperature=float(config.get("temperature") or 0.0),
                max_tokens=int(config.get("max_tokens") or 512),
                timeout=float(config.get("timeout") or 60.0),
                stream=bool(config.get("stream")),
            )
            score = score_response(observed.text, payload.get("rules"))
            status = "passed" if observed.succeeded and score["passed"] else (
                "error" if not observed.succeeded else "failed"
            )
            result = {
                "case_id": str(case.get("id") or case.get("case_id") or case.get("case_key") or f"case-{index}"),
                "case_key": str(case.get("case_key") or ""),
                "name": str(payload.get("name") or case.get("case_key") or f"用例 {index}"),
                "category": str(case.get("category") or payload.get("category") or "general"),
                "status": status,
                "attempt": 1,
                "metrics": observed.metrics(),
                "score": score,
                "response": observed.text,
                "robustness_group": str(payload.get("robustness_group") or ""),
                "variant_type": str(payload.get("variant_type") or ""),
            }
            results.append(result)
            partial = self._summary(results)
            emit(
                on_event,
                "progress",
                f"基础用例 {index}/{len(cases)} 完成：{result['name']}",
                phase="running",
                progress=5 + round(index / len(cases) * 85),
                completed=index,
                total=len(cases),
                success_rate=partial.get("success_rate"),
                p95_ms=(partial.get("performance") or {}).get("latency_p95_ms"),
                token_throughput=(partial.get("performance") or {}).get("output_tokens_per_second"),
            )
        emit(on_event, "phase", "正在汇总规则评分与鲁棒性", phase="scoring", progress=94)
        summary = self._summary(results)
        artifacts = self._write_artifacts(request.work_dir, results, summary)
        emit(on_event, "completed", "基础能力评测完成", phase="completed", progress=100)
        return BackendResult(
            status="completed", summary=summary, artifacts=artifacts, raw={"cases": results}
        )

    @staticmethod
    def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = [dict(item.get("metrics") or {}) for item in results]
        latency = [float(item["latency_ms"]) for item in metrics if item.get("latency_ms") is not None]
        ttft = [float(item["ttft_ms"]) for item in metrics if item.get("ttft_ms") is not None]
        output_tps = [
            float(item["output_tokens_per_second"])
            for item in metrics
            if item.get("output_tokens_per_second") is not None
        ]
        input_tokens = sum(int(item.get("input_tokens") or 0) for item in metrics)
        output_tokens = sum(int(item.get("output_tokens") or 0) for item in metrics)
        source_counts: dict[str, int] = {}
        errors: dict[str, int] = {}
        for item in metrics:
            source = str(item.get("token_source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
            error = str(item.get("error_type") or "")
            if error:
                errors[error] = errors.get(error, 0) + 1
        passed = sum(1 for item in results if item.get("status") == "passed")
        failed = sum(1 for item in results if item.get("status") == "failed")
        errored = sum(1 for item in results if item.get("status") == "error")
        scores = [float((item.get("score") or {}).get("score") or 0.0) for item in results]
        return {
            "mode": "foundation",
            "total_cases": len(results),
            "completed_cases": len(results),
            "passed_cases": passed,
            "failed_cases": failed,
            "error_cases": errored,
            "success_rate": round(passed / len(results) * 100, 2) if results else None,
            "quality_score": round(mean(scores) * 100, 2) if scores else None,
            "token_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "source_counts": source_counts,
                "exact": bool(source_counts) and set(source_counts) == {"api_usage"},
            },
            "performance": {
                "ttft_p50_ms": _percentile(ttft, 50),
                "ttft_p95_ms": _percentile(ttft, 95),
                "latency_p50_ms": _percentile(latency, 50),
                "latency_p95_ms": _percentile(latency, 95),
                "latency_p99_ms": _percentile(latency, 99),
                "output_tokens_per_second": round(mean(output_tps), 4) if output_tps else None,
            },
            "error_types": errors,
            "robustness": summarize_robustness(results),
        }

    @staticmethod
    def _write_artifacts(
        work_dir: Path, results: list[dict[str, Any]], summary: dict[str, Any]
    ) -> dict[str, str]:
        summary_path = work_dir / "summary.json"
        responses_path = work_dir / "responses.jsonl"
        performance_path = work_dir / "performance_samples.csv"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        responses_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
            encoding="utf-8",
        )
        with performance_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "case_id",
                    "status",
                    "token_source",
                    "input_tokens",
                    "output_tokens",
                    "ttft_ms",
                    "latency_ms",
                    "output_tokens_per_second",
                    "error_type",
                ),
            )
            writer.writeheader()
            for item in results:
                metrics = dict(item.get("metrics") or {})
                writer.writerow({"case_id": item.get("case_id"), "status": item.get("status"), **{key: metrics.get(key) for key in writer.fieldnames[2:]}})
        return {
            "summary_json": str(summary_path),
            "responses_jsonl": str(responses_path),
            "performance_csv": str(performance_path),
        }
