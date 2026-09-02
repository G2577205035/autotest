"""Stable domain contracts for model-evaluation execution backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, urlsplit


SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
SAFE_REFERENCE_KEYS = {
    "api_key_env",
    "avg_decoded_tokens_per_iter",
    "avg_input_tokens",
    "avg_output_tokens",
    "cached_tokens",
    "completion_tokens",
    "decode_tokens_per_second",
    "input_tokens",
    "input_tokens_average",
    "input token throughput",
    "input tokens",
    "input_token_throughput",
    "judge_api_key_env",
    "max_tokens",
    "output_tokens",
    "output_tokens_average",
    "output token throughput",
    "output tokens",
    "output_token_throughput",
    "output_tokens_per_second",
    "prompt_tokens",
    "token_source",
    "token_source_policy",
    "token_usage",
    "input_tokens_per_second",
    "tokens_per_second",
    "perf.throughput.tokens_per_second",
    "total_input_tokens",
    "total_output_tokens",
    "total_token_throughput",
    "total_tokens",
    "total_tokens_count",
    "usage.input_tokens",
    "usage.output_tokens",
    "usage.total_tokens",
}


def assert_secret_free(value: Any, *, path: str = "snapshot") -> None:
    """Reject plaintext credential-shaped fields before persistence."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            child_path = f"{path}.{raw_key}"
            if key not in SAFE_REFERENCE_KEYS and any(
                fragment in key for fragment in SENSITIVE_KEY_FRAGMENTS
            ):
                if child is not None and child != "" and child is not False:
                    raise ValueError(f"评测快照禁止保存敏感字段：{child_path}")
            if key.endswith("url") and isinstance(child, str):
                parsed = urlsplit(child)
                sensitive_query = any(
                    any(fragment in query_key.lower() for fragment in SENSITIVE_KEY_FRAGMENTS)
                    and query_value
                    for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
                )
                if parsed.username or parsed.password or sensitive_query:
                    raise ValueError(f"评测快照禁止在 URL 中保存凭据：{child_path}")
            assert_secret_free(child, path=child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_secret_free(child, path=f"{path}[{index}]")


@dataclass(frozen=True)
class BackendEvent:
    event_type: str
    message: str
    phase: str = ""
    progress: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "message": self.message,
            "phase": self.phase,
            "progress": self.progress,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class BackendResult:
    status: str
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    error_type: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "stopped"}:
            raise ValueError(f"不支持的评测后端终态：{self.status}")
        assert_secret_free(self.summary, path="backend_result.summary")
        assert_secret_free(self.raw, path="backend_result.raw")


@dataclass(frozen=True)
class EvaluationRequest:
    run_id: str
    project_id: str
    mode: str
    task_config: dict[str, Any]
    work_dir: Path
    backend_version: str = ""
    secret_env: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not str(self.run_id).strip() or not str(self.project_id).strip():
            raise ValueError("评测运行必须包含 run_id 和 project_id")
        if self.mode not in {"eval", "perf", "mock"}:
            raise ValueError(f"不支持的评测模式：{self.mode}")
        assert_secret_free(self.task_config, path="evaluation_request.task_config")
        for name in self.secret_env:
            normalized = str(name or "").strip()
            if not normalized.startswith("LIEMA_EVAL_") or not normalized.replace("_", "").isalnum():
                raise ValueError("评测子进程凭据只能使用 LIEMA_EVAL_* 环境变量")


EventCallback = Callable[[BackendEvent], None]
StopPredicate = Callable[[], bool]


class EvaluationBackend(Protocol):
    name: str
    version: str

    def run(
        self,
        request: EvaluationRequest,
        *,
        on_event: EventCallback | None = None,
        should_stop: StopPredicate | None = None,
    ) -> BackendResult: ...

    def stop(self, run_id: str) -> bool: ...
