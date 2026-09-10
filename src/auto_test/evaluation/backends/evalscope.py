"""Isolated EvalScope subprocess adapter with cooperative stop support."""

from __future__ import annotations

import json
import csv
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from auto_test.evaluation.backends.base import emit
from auto_test.evaluation.advanced import analyze_capacity, correlate_resources
from auto_test.evaluation.contracts import (
    BackendEvent,
    BackendResult,
    EvaluationRequest,
    EventCallback,
    StopPredicate,
)
from auto_test.evaluation.result_mapper import EvalScopeResultMapper


@dataclass(frozen=True)
class EvalScopeRuntimeConfig:
    python_executable: Path
    version: str
    source_root: Path
    stop_grace_seconds: float = 5.0
    cache_root: Path | None = None

    def validate(self) -> None:
        if not self.python_executable.is_file():
            raise FileNotFoundError(self.python_executable)
        if not (self.source_root / "auto_test" / "evaluation" / "evalscope_runner.py").is_file():
            raise FileNotFoundError(self.source_root / "auto_test" / "evaluation" / "evalscope_runner.py")


class EvalScopeBackend:
    name = "evalscope"

    def __init__(
        self,
        runtime: EvalScopeRuntimeConfig,
        *,
        mapper: EvalScopeResultMapper | None = None,
    ):
        runtime.validate()
        self.runtime = runtime
        self.version = runtime.version
        self.mapper = mapper or EvalScopeResultMapper()
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    @staticmethod
    def _terminate(process: subprocess.Popen[str], grace_seconds: float) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=max(0.1, float(grace_seconds)))
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def stop(self, run_id: str) -> bool:
        with self._lock:
            process = self._processes.get(str(run_id))
        if not process:
            return False
        self._terminate(process, self.runtime.stop_grace_seconds)
        return True

    @staticmethod
    def _event_from_line(line: str) -> BackendEvent | None:
        prefix = "LIEMA_EVENT "
        if not line.startswith(prefix):
            return None
        try:
            payload = json.loads(line[len(prefix):])
        except (TypeError, ValueError):
            return None
        return BackendEvent(
            event_type=str(payload.get("event_type") or "progress"),
            message=str(payload.get("message") or "EvalScope 运行中"),
            phase=str(payload.get("phase") or "running"),
            progress=(
                max(0, min(int(payload["progress"]), 100))
                if payload.get("progress") is not None
                else None
            ),
            data=dict(payload.get("data") or {}),
        )

    def run(
        self,
        request: EvaluationRequest,
        *,
        on_event: EventCallback | None = None,
        should_stop: StopPredicate | None = None,
    ) -> BackendResult:
        request.work_dir.mkdir(parents=True, exist_ok=True)
        request_path = request.work_dir / "evalscope_request.json"
        output_path = request.work_dir / "evalscope_runner_output.json"
        task_config = dict(request.task_config)
        inline_dataset = task_config.pop("_inline_dataset", None)
        inline_kind = str(task_config.pop("_inline_dataset_kind", "") or "")
        load_profile = dict(task_config.pop("_load_profile", {}) or {})
        safety = dict(task_config.pop("_safety", {}) or {})
        if inline_dataset is not None:
            if inline_kind == "wmt24pp":
                dataset_root = request.work_dir / "wmt24pp"
                dataset_root.mkdir(parents=True, exist_ok=True)
                dataset_path = dataset_root / "test.jsonl"
            else:
                dataset_root = None
                dataset_path = request.work_dir / f"{inline_kind or 'dataset'}.jsonl"
            dataset_path.write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False) + "\n"
                    for item in list(inline_dataset)
                ),
                encoding="utf-8",
            )
            if request.mode == "perf":
                task_config["dataset_path"] = str(dataset_path)
            else:
                for options in dict(task_config.get("dataset_args") or {}).values():
                    if options.get("dataset_id") == "__LIEMA_INLINE_DATASET__":
                        options["dataset_id"] = str(dataset_path)
                    if options.get("local_path") == "__LIEMA_INLINE_DATASET__":
                        options["local_path"] = str(dataset_root or dataset_path.parent)
        request_payload = {
            "schema_version": "1.0",
            "run_id": request.run_id,
            "mode": request.mode,
            "backend_version": request.backend_version or self.version,
            "work_dir": str(request.work_dir),
            "task_config": task_config,
            "safety": safety,
            "load_profile": load_profile,
            "secret_env_names": sorted(request.secret_env),
        }
        request_path.write_text(
            json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        environment = os.environ.copy()
        environment.update({name: str(value) for name, value in request.secret_env.items()})
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(self.runtime.source_root), existing_pythonpath) if item
        )
        # Windows child processes otherwise inherit the active console code page
        # (commonly GBK), while this adapter intentionally reads the pipe as UTF-8.
        # Pin the child stream encoding so EvalScope diagnostics remain readable.
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        environment.setdefault("HF_HUB_OFFLINE", "1")
        environment.setdefault("TRANSFORMERS_OFFLINE", "1")
        cache_root = self.runtime.cache_root
        if cache_root is None:
            cache_root = Path(tempfile.gettempdir()) / "liema-evalscope" / request.run_id
        environment.setdefault("HF_HOME", str(cache_root / "huggingface"))
        environment.setdefault("MODELSCOPE_CACHE", str(cache_root / "modelscope"))
        environment.setdefault("NLTK_DATA", str(cache_root / "nltk_data"))
        command = [
            str(self.runtime.python_executable),
            str(self.runtime.source_root / "auto_test" / "evaluation" / "evalscope_runner.py"),
            "--request",
            str(request_path),
            "--output",
            str(output_path),
        ]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(request.work_dir),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        with self._lock:
            self._processes[request.run_id] = process
        lines: queue.SimpleQueue[str] = queue.SimpleQueue()

        def read_output() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))

        reader = threading.Thread(target=read_output, name=f"evalscope-output-{request.run_id}", daemon=True)
        reader.start()
        stopped = False
        diagnostic_tail: list[str] = []
        try:
            while process.poll() is None:
                while not lines.empty():
                    line = lines.get()
                    event = self._event_from_line(line)
                    if event and on_event:
                        on_event(event)
                    elif line:
                        diagnostic_tail.append(line[:500])
                        diagnostic_tail = diagnostic_tail[-20:]
                if should_stop and should_stop():
                    stopped = True
                    self._terminate(process, self.runtime.stop_grace_seconds)
                    break
                time.sleep(0.05)
            reader.join(timeout=1.0)
            if process.stdout is not None:
                process.stdout.close()
            while not lines.empty():
                line = lines.get()
                event = self._event_from_line(line)
                if event and on_event:
                    on_event(event)
                elif line:
                    diagnostic_tail.append(line[:500])
                    diagnostic_tail = diagnostic_tail[-20:]
        finally:
            with self._lock:
                self._processes.pop(request.run_id, None)
        if stopped:
            emit(on_event, "stopped", "EvalScope 子进程已停止", phase="stopped")
            return BackendResult(status="stopped", summary={"diagnostic_tail": diagnostic_tail})
        if process.returncode != 0:
            emit(on_event, "failed", "EvalScope 子进程执行失败", phase="failed")
            return BackendResult(
                status="failed",
                summary={"exit_code": process.returncode, "diagnostic_tail": diagnostic_tail},
                error_type="evalscope_process_error",
            )
        mapped = self.mapper.map_directory(
            request.work_dir, secret_values=request.secret_env.values()
        )
        summary = dict(mapped.get("summary") or {})
        execution = json.loads(output_path.read_text(encoding="utf-8")) if output_path.is_file() else {}
        if request.mode == "perf":
            performance = dict(summary.get("performance") or {})
            summary["load_profile"] = load_profile
            summary["performance_execution"] = execution.get("performance_execution") or {}
            summary["capacity"] = analyze_capacity(performance.get("stages") or [])
            resource_path = request.work_dir / "resource_samples.csv"
            resource_rows: list[dict[str, str]] = []
            if resource_path.is_file():
                with resource_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    resource_rows = list(csv.DictReader(handle))
            summary["resource_correlation"] = correlate_resources(resource_rows)
            mapped["summary"] = summary
            (request.work_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        safety_stopped = bool((execution.get("performance_execution") or {}).get("safety_stopped"))
        emit(on_event, "failed" if safety_stopped else "completed", "错误率超过保护阈值，已停止增加负载" if safety_stopped else "EvalScope 子进程执行完成", phase="failed" if safety_stopped else "completed", progress=100)
        return BackendResult(
            status="failed" if safety_stopped else "completed",
            error_type="safety_error_rate" if safety_stopped else "",
            summary=summary,
            artifacts={"work_dir": str(request.work_dir), "runner_output": str(output_path)},
            raw=mapped,
        )
