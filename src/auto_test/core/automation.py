"""Service boundary around the automation pipeline."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable


ProgressCallback = Callable[["RunEvent"], None]
MetricCallback = Callable[[str, dict[str, Any]], None]
Pipeline = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class RunEvent:
    stage: str
    message: str
    progress: int
    timestamp: float


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    run_dir: str
    started_at: float
    finished_at: float
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutomationBusyError(RuntimeError):
    """Raised when another in-process automation run is still active."""


class AutomationService:
    """Run the existing pipeline behind a stable, serial service interface.

    The current pipeline still contains a few compatibility globals used by
    legacy modules, so executions are intentionally serialized in-process.
    Web jobs are additionally serialized by the persistent task store.
    """

    _run_lock = threading.Lock()

    def __init__(self, pipeline: Pipeline | None = None):
        self._pipeline = pipeline

    def _get_pipeline(self) -> Pipeline:
        if self._pipeline is None:
            from auto_test.pipeline.runner import run_pipeline

            self._pipeline = run_pipeline
        return self._pipeline

    def run(
        self,
        *,
        run_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
        metric_callback: MetricCallback | None = None,
        options: dict[str, Any] | None = None,
    ) -> RunResult:
        run_id = run_id or uuid.uuid4().hex
        if not self._run_lock.acquire(blocking=False):
            raise AutomationBusyError("another automation run is already active")

        started_at = time.time()

        def emit(stage: str, message: str, progress: int) -> None:
            if progress_callback:
                progress_callback(
                    RunEvent(
                        stage=stage,
                        message=message,
                        progress=max(0, min(100, int(progress))),
                        timestamp=time.time(),
                    )
                )

        try:
            emit("preparing", "正在准备自动化测试", 1)
            pipeline = self._get_pipeline()
            pipeline_kwargs = {
                "progress_callback": emit,
                "metric_callback": metric_callback,
                "run_id": run_id,
                "options": options or {},
            }
            # Keep custom/legacy pipeline callables compatible while the built-in
            # pipeline accepts the complete Web execution context.
            import inspect
            signature = inspect.signature(pipeline)
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                pipeline_kwargs = {
                    key: value for key, value in pipeline_kwargs.items()
                    if key in signature.parameters
                }
            details = pipeline(**pipeline_kwargs) or {}
            finished_at = time.time()
            emit("completed", "自动化测试执行完成", 100)
            return RunResult(
                run_id=run_id,
                status="succeeded",
                run_dir=str(details.get("run_dir", "")),
                started_at=started_at,
                finished_at=finished_at,
                details=details,
            )
        finally:
            self._run_lock.release()
