"""Map EvalScope public artifacts into a stable platform result envelope."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable

from auto_test.evaluation.contracts import SAFE_REFERENCE_KEYS, SENSITIVE_KEY_FRAGMENTS


def _redact(value: Any, secret_values: Iterable[str] = ()) -> Any:
    secrets = tuple(str(item) for item in secret_values if str(item))
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized not in SAFE_REFERENCE_KEYS and any(
                fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS
            ):
                result[key] = (
                    "***"
                    if child is not None and child != "" and child is not False
                    else child
                )
            else:
                result[key] = _redact(child, secrets)
        return result
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "***")
        return redacted
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _metric_value(metrics: list[dict[str, Any]], identity: dict[str, Any]) -> float | None:
    for item in metrics:
        if item.get("identity") == identity and item.get("score") is not None:
            try:
                return float(item["score"])
            except (TypeError, ValueError):
                return None
    return None


def _normalized_summary(documents: list[dict[str, Any]]) -> dict[str, Any]:
    performance_stages: list[dict[str, Any]] = []
    for document in documents:
        path = str(document.get("path") or "")
        payload = document.get("payload")
        if not path.endswith("benchmark_summary.json") or not isinstance(payload, dict):
            continue
        parent = path.rsplit("/", 1)[0]
        percentile_payload = next(
            (
                item.get("payload")
                for item in documents
                if str(item.get("path") or "") == parent + "/benchmark_percentile.json"
            ),
            [],
        )
        percentiles = {
            str(item.get("Percentiles") or ""): item
            for item in percentile_payload
            if isinstance(item, dict)
        } if isinstance(percentile_payload, list) else {}
        total = int(payload.get("Total Requests") or 0)
        succeeded = int(payload.get("Success Requests") or 0)
        stage = {
            "stage_index": next((int(part.split("-", 2)[1]) for part in path.split("/") if part.startswith("stage-") and len(part.split("-", 2)) == 3 and part.split("-", 2)[1].isdigit()), None),
            "phase": next((part.split("-", 2)[2] for part in path.split("/") if part.startswith("stage-") and len(part.split("-", 2)) == 3), "capacity"),
            "concurrency": int(payload.get("Concurrency") or 0),
            "target_rps": (
                float(payload.get("Request Rate (req/s)"))
                if payload.get("Request Rate (req/s)") is not None and float(payload.get("Request Rate (req/s)")) > 0
                else None
            ),
            "total_requests": total,
            "successful_requests": succeeded,
            "failed_requests": int(payload.get("Failed Requests") or 0),
            "success_rate": round(succeeded / total * 100, 2) if total else None,
            "request_throughput": payload.get("Req Throughput (req/s)"),
            "output_token_throughput": payload.get("Output Throughput (tok/s)"),
            "total_token_throughput": payload.get("Total Throughput (tok/s)"),
            "average_ttft_ms": payload.get("Avg TTFT (ms)"),
            "average_latency_ms": (
                float(payload.get("Avg Latency (s)")) * 1000
                if payload.get("Avg Latency (s)") is not None
                else None
            ),
            "ttft_p50_ms": (percentiles.get("50%") or {}).get("TTFT (ms)"),
            "ttft_p95_ms": (percentiles.get("95%") or {}).get("TTFT (ms)"),
            "ttft_p99_ms": (percentiles.get("99%") or {}).get("TTFT (ms)"),
            "latency_p50_ms": (
                float((percentiles.get("50%") or {}).get("Latency (s)")) * 1000
                if (percentiles.get("50%") or {}).get("Latency (s)") is not None
                else None
            ),
            "latency_p95_ms": (
                float((percentiles.get("95%") or {}).get("Latency (s)")) * 1000
                if (percentiles.get("95%") or {}).get("Latency (s)") is not None
                else None
            ),
            "latency_p99_ms": (
                float((percentiles.get("99%") or {}).get("Latency (s)")) * 1000
                if (percentiles.get("99%") or {}).get("Latency (s)") is not None
                else None
            ),
            "input_tokens_average": payload.get("Avg Input Tokens"),
            "output_tokens_average": payload.get("Avg Output Tokens"),
        }
        performance_stages.append(stage)
    if performance_stages:
        performance_stages.sort(
            key=lambda item: (item["stage_index"] if item.get("stage_index") is not None else float(item.get("target_rps") or item.get("concurrency") or 0))
        )
        total = sum(item["total_requests"] for item in performance_stages)
        succeeded = sum(item["successful_requests"] for item in performance_stages)
        stable = [
            item
            for item in performance_stages
            if item.get("success_rate") is not None and item["success_rate"] >= 95
        ]
        capacity_stages = [item for item in performance_stages if item["phase"] == "capacity"]
        last = (capacity_stages or performance_stages)[-1]
        return {
            "mode": "perf",
            "total_requests": total,
            "successful_requests": succeeded,
            "failed_requests": total - succeeded,
            "success_rate": round(succeeded / total * 100, 2) if total else None,
            "max_concurrency": max(
                (item["concurrency"] for item in performance_stages if item["concurrency"] > 0),
                default=None,
            ),
            "max_stable_concurrency": max(
                (item["concurrency"] for item in stable if item["concurrency"] > 0),
                default=None,
            ),
            "performance": {
                "request_throughput": last.get("request_throughput"),
                "output_tokens_per_second": last.get("output_token_throughput"),
                "ttft_p50_ms": last.get("ttft_p50_ms"),
                "ttft_p95_ms": last.get("ttft_p95_ms"),
                "ttft_p99_ms": last.get("ttft_p99_ms"),
                "latency_p50_ms": last.get("latency_p50_ms"),
                "latency_p95_ms": last.get("latency_p95_ms"),
                "latency_p99_ms": last.get("latency_p99_ms"),
                "stages": performance_stages,
            },
            "token_usage": {"source_counts": {"backend_reported": total}, "exact": False},
        }

    benchmarks: list[dict[str, Any]] = []
    for document in documents:
        path = str(document.get("path") or "")
        payload = document.get("payload")
        if "/reports/" not in "/" + path or not isinstance(payload, dict):
            continue
        if not payload.get("dataset_name") or not isinstance(payload.get("metrics"), list):
            continue
        execution = dict(payload.get("execution_summary") or {})
        requested = int(execution.get("requested") or payload.get("num") or 0)
        succeeded = int(execution.get("succeeded") or 0)
        primary = dict(payload.get("primary_metric_identity") or {})
        score = _metric_value(payload["metrics"], primary)
        perf_summary = dict(((payload.get("perf_metrics") or {}).get("summary") or {}))
        usage = dict(perf_summary.get("usage") or {})
        benchmarks.append(
            {
                "dataset": str(payload.get("dataset_name") or ""),
                "requested": requested,
                "succeeded": succeeded,
                "errored": int(execution.get("errored") or max(0, requested - succeeded)),
                "score": score,
                "primary_metric": primary,
                "latency": dict(perf_summary.get("latency") or {}),
                "throughput": dict(perf_summary.get("throughput") or {}),
                "input_tokens": int(usage.get("total_input_tokens") or 0),
                "output_tokens": int(usage.get("total_output_tokens") or 0),
            }
        )
    if benchmarks:
        requested = sum(item["requested"] for item in benchmarks)
        succeeded = sum(item["succeeded"] for item in benchmarks)
        scores = [float(item["score"]) for item in benchmarks if item.get("score") is not None]
        input_tokens = sum(item["input_tokens"] for item in benchmarks)
        output_tokens = sum(item["output_tokens"] for item in benchmarks)
        return {
            "mode": "standard_benchmark",
            "total_cases": requested,
            "completed_cases": succeeded,
            "passed_cases": succeeded,
            "failed_cases": max(0, requested - succeeded),
            "success_rate": round(succeeded / requested * 100, 2) if requested else None,
            "quality_score": round(sum(scores) / len(scores) * 100, 2) if scores else None,
            "benchmarks": benchmarks,
            "token_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "source_counts": {"api_usage": succeeded},
                "exact": True,
            },
        }
    return {}


class EvalScopeResultMapper:
    schema_version = "1.0"

    def map_directory(
        self, work_dir: str | Path, *, secret_values: Iterable[str] = ()
    ) -> dict[str, Any]:
        root = Path(work_dir).resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        json_documents: list[dict[str, Any]] = []
        jsonl_samples: list[dict[str, Any]] = []
        csv_rows = 0
        artifacts: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            artifacts.append(relative)
            suffix = path.suffix.lower()
            try:
                if suffix == ".json":
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, (dict, list)):
                        json_documents.append({"path": relative, "payload": payload})
                elif suffix == ".jsonl":
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        if isinstance(payload, dict):
                            jsonl_samples.append({"path": relative, **payload})
                elif suffix == ".csv":
                    with path.open("r", encoding="utf-8-sig", newline="") as handle:
                        csv_rows += sum(1 for _ in csv.DictReader(handle))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue
        primary = next(
            (
                item["payload"]
                for item in json_documents
                if isinstance(item["payload"], dict)
                and item["path"].endswith(("summary.json", "result.json", "output.json"))
            ),
            next(
                (item["payload"] for item in json_documents if isinstance(item["payload"], dict)),
                {},
            ),
        )
        summary = _normalized_summary(json_documents)
        if not summary:
            summary = primary.get("summary") if isinstance(primary.get("summary"), dict) else {}
        normalized = _redact(
            {
                "schema_version": self.schema_version,
                "backend": "evalscope",
                "summary": summary,
                "raw_primary": primary,
                "samples": jsonl_samples,
                "artifact_files": artifacts,
                "json_document_count": len(json_documents),
                "jsonl_sample_count": len(jsonl_samples),
                "csv_row_count": csv_rows,
            },
            secret_values,
        )
        summary_path = root / "summary.json"
        summary_path.write_text(
            json.dumps(normalized["summary"], ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        if "summary.json" not in normalized["artifact_files"]:
            normalized["artifact_files"].append("summary.json")
        performance_path = root / "performance_samples.csv"
        stages = list((normalized["summary"].get("performance") or {}).get("stages") or [])
        with performance_path.open("w", encoding="utf-8-sig", newline="") as handle:
            fieldnames = (
                "concurrency",
                "target_rps",
                "total_requests",
                "successful_requests",
                "failed_requests",
                "success_rate",
                "request_throughput",
                "output_token_throughput",
                "ttft_p50_ms",
                "ttft_p95_ms",
                "ttft_p99_ms",
                "latency_p50_ms",
                "latency_p95_ms",
                "latency_p99_ms",
            )
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for stage in stages:
                writer.writerow({key: stage.get(key) for key in fieldnames})
        if "performance_samples.csv" not in normalized["artifact_files"]:
            normalized["artifact_files"].append("performance_samples.csv")
        return normalized
