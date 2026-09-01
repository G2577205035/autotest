"""Background worker that executes persistent automation tasks."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

from auto_test.common.logging import log
from auto_test.common.paths import prepare_runtime_layout
from auto_test.core.automation import AutomationService, RunEvent
from auto_test.platform.contracts import TaskRepository
from auto_test.platform.artifact_storage import ArtifactStorage, create_artifact_storage
from auto_test.platform.persistence import create_task_repository
from auto_test.platform.secrets import decrypt_secret
from auto_test.core.task_queue import LocalTaskSignalQueue, TaskSignalQueue


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Console-only logger for storage health messages: it never routes through
# the database handler, so failures here cannot feed an error storm.
_storage_logger = logging.getLogger("liema.storage")
_storage_logger.setLevel(logging.WARNING)
_storage_logger.propagate = False
if not _storage_logger.handlers:
    _storage_logger.addHandler(logging.StreamHandler())


def _short_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."


class _RunLogHandler(logging.Handler):
    """Persist run logs to the task store with graceful degradation.

    While the database is unreachable (network flap, machine sleep, server
    maintenance) the handler never raises: records are buffered in memory,
    database writes back off exponentially, and the handler only reports
    health issues through the console-only logger instead of feeding
    ``Logging error`` storms back into logging.
    """

    def __init__(
        self,
        manager: "TaskManager",
        *,
        buffer_limit: int = 2000,
        flush_batch: int = 100,
        backoff_max: float = 60.0,
    ):
        super().__init__()
        self.manager = manager
        self._buffer: deque[tuple[str, str, str]] = deque(maxlen=buffer_limit)
        self._flush_batch = max(1, int(flush_batch))
        self._backoff_max = float(backoff_max)
        self._lock = threading.Lock()
        self._backoff_until = 0.0
        self._failures = 0

    def emit(self, record: logging.LogRecord) -> None:
        run_id = self.manager.active_run_id
        if not run_id:
            return
        try:
            level = _ANSI_RE.sub("", record.levelname)
            message = _ANSI_RE.sub("", record.getMessage())
        except Exception:
            return  # formatting errors must never surface through logging
        event = (run_id, level, message)
        now = time.monotonic()
        with self._lock:
            self._buffer.append(event)
            if now < self._backoff_until:
                return
            pending = list(self._buffer)
            batch, rest = pending[: self._flush_batch], pending[self._flush_batch :]
            try:
                for event_run_id, event_level, event_message in batch:
                    self.manager.store.add_event(event_run_id, event_level, event_message)
            except Exception as exc:
                self._failures += 1
                delay = min(self._backoff_max, 2.0 ** min(self._failures, 6))
                self._backoff_until = now + delay
                _storage_logger.warning(
                    "任务日志写库失败，进入降级模式（缓冲 %d 条，%.0f 秒后重试）：%s",
                    len(self._buffer),
                    delay,
                    _short_error(exc),
                )
                return
            self._buffer = deque(rest, maxlen=self._buffer.maxlen)
            self._failures = 0


class TaskManager:
    """Submit and execute jobs persisted in :class:`TaskStore`.

    Multiple processes may poll the same database. ``claim_next`` guarantees
    that only one run is active at a time, which is required while legacy
    pipeline modules still use per-run compatibility globals.
    """

    def __init__(
        self,
        store: TaskRepository,
        service: AutomationService | None = None,
        *,
        poll_interval: float = 1.0,
        heartbeat_interval: float = 10.0,
        artifact_storage: ArtifactStorage | None = None,
        signal_queue: TaskSignalQueue | None = None,
    ):
        self.store = store
        self.service = service or AutomationService()
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.artifact_storage = artifact_storage
        self.signal_queue = signal_queue or LocalTaskSignalQueue()
        self.queue_topic = "automation"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._active_run_id = ""
        self._active_lock = threading.Lock()
        self._log_handler = _RunLogHandler(self)

    @classmethod
    def default(
        cls,
        base_dir: str | Path,
        *,
        signal_queue: TaskSignalQueue | None = None,
    ) -> "TaskManager":
        base_dir = Path(base_dir)
        prepare_runtime_layout(base_dir)
        return cls(
            create_task_repository(base_dir),
            artifact_storage=create_artifact_storage(base_dir),
            signal_queue=signal_queue,
        )

    def _notify_worker(self) -> None:
        self._wake.set()
        self.signal_queue.notify(self.queue_topic)

    @property
    def active_run_id(self) -> str:
        with self._active_lock:
            return self._active_run_id

    def _set_active(self, run_id: str) -> None:
        with self._active_lock:
            self._active_run_id = run_id

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        if self._log_handler not in log.handlers:
            log.addHandler(self._log_handler)
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="liema-task-worker",
            daemon=True,
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._notify_worker()
        if self._worker:
            self._worker.join(timeout=timeout)
        if self._log_handler in log.handlers:
            log.removeHandler(self._log_handler)
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    def submit(
        self,
        metadata: dict[str, Any] | None = None,
        *,
        secret_enc: str = "",
    ) -> dict[str, Any]:
        task = self.store.create_run(metadata, secret_enc=secret_enc)
        self._notify_worker()
        return task

    def stop_run(self, run_id: str) -> dict[str, Any]:
        task = self.store.request_stop(run_id)
        self._notify_worker()
        return task

    def restart_run(self, run_id: str) -> dict[str, Any]:
        original = self.store.get_run(run_id)
        metadata = dict(original.get("metadata") or {})
        metadata.pop("stop_requested", None)
        metadata["restarted_from"] = run_id
        task = self.store.create_run(metadata)
        self._notify_worker()
        return task

    def execute_run(self, run_id: str) -> dict[str, Any]:
        current = self.store.get_run(run_id)
        if current["status"] == "queued":
            task = self.store.wake_queued(run_id)
            self._notify_worker()
            return task
        if current["status"] in {"succeeded", "failed", "interrupted"}:
            return self.restart_run(run_id)
        return current

    def _heartbeat_loop(self, run_id: str, done: threading.Event) -> None:
        while not done.wait(self.heartbeat_interval):
            try:
                self.store.heartbeat(run_id)
            except Exception as exc:
                log.warning(f"任务 {run_id} 心跳更新失败：{exc}")

    def _on_progress(self, run_id: str, event: RunEvent) -> None:
        if self.store.is_stop_requested(run_id):
            raise InterruptedError("用户请求停止任务")
        self.store.update_progress(run_id, event.stage, event.message, event.progress)
        if self.store.is_stop_requested(run_id):
            raise InterruptedError("用户请求停止任务")

    def _on_metric(self, run_id: str, source: str, sample: dict[str, Any]) -> None:
        self.store.add_metric(run_id, source, sample)

    def _execute(self, task: dict[str, Any]) -> None:
        run_id = task["id"]
        self._set_active(run_id)
        heartbeat_done = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run_id, heartbeat_done),
            name=f"heartbeat-{run_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        secret_options: dict[str, Any] = {}
        try:
            secret_enc = str(task.pop("_secret_enc", "") or "")
            if secret_enc:
                decoded = json.loads(decrypt_secret(secret_enc))
                if not isinstance(decoded, dict):
                    raise ValueError("任务凭据格式无效")
                secret_options = decoded
            run_options = dict((task.get("metadata") or {}).get("options") or {})
            run_options.update(secret_options)
            run_kwargs = {
                "run_id": run_id,
                "progress_callback": lambda event: self._on_progress(run_id, event),
                "metric_callback": lambda source, sample: self._on_metric(run_id, source, sample),
                "options": run_options,
            }
            import inspect
            signature = inspect.signature(self.service.run)
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                run_kwargs = {key: value for key, value in run_kwargs.items() if key in signature.parameters}
            result = self.service.run(**run_kwargs)
            run_dir = result.run_dir
            if self.artifact_storage:
                try:
                    self.artifact_storage.publish_tree(result.run_dir)
                    run_dir = self.artifact_storage.reference(result.run_dir)
                except ValueError:
                    log.warning(f"运行产物目录不在配置的存储根目录内，跳过对象存储同步：{result.run_dir}")
            self.store.complete(run_id, run_dir)
        except InterruptedError as exc:
            message = str(exc) or "用户请求停止任务"
            self.store.add_event(run_id, "WARNING", message, "interrupted")
            self.store.interrupt(run_id, message)
            log.warning(f"任务 {run_id} 已停止：{message}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.store.add_event(run_id, "ERROR", traceback.format_exc(), "failed")
            self.store.fail(run_id, error)
            log.error(f"任务 {run_id} 执行失败：{error}")
        finally:
            secret_options.clear()
            heartbeat_done.set()
            heartbeat.join(timeout=2)
            self._set_active("")

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self.store.claim_next()
                if task:
                    self._execute(task)
                    continue
            except Exception as exc:
                log.error(f"任务调度异常：{exc}")
            if self.signal_queue.backend == "redis":
                self.signal_queue.wait(self.queue_topic, self.poll_interval)
            else:
                self._wake.wait(self.poll_interval)
                self._wake.clear()
