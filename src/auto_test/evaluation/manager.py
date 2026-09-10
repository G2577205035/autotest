"""Persistent model-evaluation manager backed by the platform queue contract."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from auto_test.common.config_loader import model_evaluation_cfg
from auto_test.common.env import get_env
from auto_test.common.logging import log
from auto_test.core.task_queue import LocalTaskSignalQueue, TaskSignalQueue
from auto_test.evaluation.backends import (
    DeterministicMockBackend,
    EvalScopeBackend,
    EvalScopeRuntimeConfig,
    NativeEvaluationBackend,
)
from auto_test.evaluation.contracts import BackendEvent, EvaluationBackend, EvaluationRequest
from auto_test.platform.artifact_storage import ArtifactStorage
from auto_test.platform.contracts import PlatformRepository
from auto_test.platform.models import decrypt_secret


BackendResolver = Callable[[dict[str, Any]], EvaluationBackend]
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def create_model_evaluation_backend_resolver(
    project_root: str | Path,
    overrides: dict[str, Any] | None = None,
) -> BackendResolver:
    """Build a lazy resolver; the Worker can start before EvalScope is installed."""

    values = dict(model_evaluation_cfg())
    values.update(overrides or {})
    root = Path(project_root).resolve()
    version = str(values.get("evalscope_version") or "1.11.1").strip()
    configured_python = str(
        get_env("EVALSCOPE_PYTHON", values.get("evalscope_python") or "") or ""
    ).strip()
    configured_cache = str(
        get_env("EVALSCOPE_CACHE_ROOT", values.get("cache_root") or "") or ""
    ).strip()

    def resolve(run: dict[str, Any]) -> EvaluationBackend:
        backend_name = str(run.get("backend") or values.get("default_backend") or "mock").lower()
        if backend_name == "mock":
            return DeterministicMockBackend()
        if backend_name == "native":
            return NativeEvaluationBackend()
        if backend_name == "full":
            from auto_test.evaluation.backends.full import FullEvaluationBackend
            return FullEvaluationBackend(resolve)
        if backend_name != "evalscope":
            raise ValueError(f"不支持的模型评测后端：{backend_name}")
        requested_version = str(run.get("backend_version") or version).strip()
        if requested_version != version:
            raise RuntimeError(
                f"EvalScope 运行版本不匹配：请求 {requested_version}，当前锁定 {version}"
            )
        if not configured_python:
            raise RuntimeError("未配置 LIEMA_EVALSCOPE_PYTHON，无法运行 EvalScope 后端")
        python_path = Path(configured_python)
        if not python_path.is_absolute():
            python_path = root / python_path
        cache_root = Path(configured_cache) if configured_cache else None
        if cache_root is not None and not cache_root.is_absolute():
            cache_root = root / cache_root
        return EvalScopeBackend(
            EvalScopeRuntimeConfig(
                python_executable=python_path.resolve(),
                version=version,
                # Wheels install under site-packages; deployment work directories
                # do not necessarily contain the repository's src/ tree.
                source_root=Path(__file__).resolve().parents[2],
                stop_grace_seconds=float(values.get("stop_grace_seconds") or 5),
                cache_root=cache_root.resolve() if cache_root else None,
            )
        )

    return resolve


def _resolve_request_secrets(
    task_config: dict[str, Any],
    *,
    platform_store: PlatformRepository,
    model_profile_id: str,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    source_name = str(task_config.pop("api_key_env", "") or "").strip()
    if source_name:
        if not _ENV_NAME.fullmatch(source_name):
            raise ValueError("api_key_env 不是有效的环境变量名")
        value = os.environ.get(source_name, "")
        if not value:
            raise RuntimeError(f"模型评测凭据环境变量未设置：{source_name}")
        resolved["LIEMA_EVAL_MODEL_API_KEY"] = value
    elif model_profile_id:
        profile = platform_store.get_model_profile(model_profile_id)
        if not profile:
            raise RuntimeError("被测模型配置不存在")
        encrypted = str(profile.get("api_key_enc") or "")
        if encrypted:
            resolved["LIEMA_EVAL_MODEL_API_KEY"] = decrypt_secret(encrypted)
    judge = task_config.get("judge")
    judge_profile_id = str(judge.get("profile_id") or "") if isinstance(judge, dict) else ""
    if judge_profile_id:
        judge_profile = platform_store.get_model_profile(judge_profile_id)
        if not judge_profile:
            raise RuntimeError("裁判模型配置不存在")
        judge_encrypted = str(judge_profile.get("api_key_enc") or "")
        if judge_encrypted:
            resolved["LIEMA_EVAL_JUDGE_API_KEY"] = decrypt_secret(judge_encrypted)
    return resolved


class ModelEvaluationManager:
    queue_topic = "model-evaluations"

    def __init__(
        self,
        platform_store: PlatformRepository,
        artifact_storage: ArtifactStorage,
        backend_resolver: BackendResolver,
        *,
        signal_queue: TaskSignalQueue | None = None,
        poll_interval: float = 1.0,
    ):
        self.platform_store = platform_store
        self.artifact_storage = artifact_storage
        self.backend_resolver = backend_resolver
        self.signal_queue = signal_queue or LocalTaskSignalQueue()
        self.poll_interval = max(0.05, float(poll_interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._active_lock = threading.Lock()
        self._active_backend: dict[str, EvaluationBackend] = {}

    def _notify_worker(self) -> None:
        self._wake.set()
        self.signal_queue.notify(self.queue_topic)

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._loop, name="liema-model-evaluation-worker", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._notify_worker()
        with self._active_lock:
            active = list(self._active_backend.items())
        for run_id, backend in active:
            backend.stop(run_id)
        if self._worker:
            self._worker.join(timeout=max(0.0, float(timeout)))
        self._worker = None

    def submit(
        self,
        project_id: str,
        *,
        snapshot: dict[str, Any],
        model_profile_id: str = "",
        suite_version_id: str = "",
        backend: str = "mock",
        backend_version: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        run = self.platform_store.create_model_eval_run(
            project_id,
            model_profile_id=model_profile_id,
            suite_version_id=suite_version_id,
            backend=backend,
            backend_version=backend_version,
            snapshot=snapshot,
            created_by=created_by,
        )
        self._notify_worker()
        return run

    def stop_run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.platform_store.request_stop_model_eval_run(project_id, run_id)
        with self._active_lock:
            backend = self._active_backend.get(run_id)
        if backend:
            backend.stop(run_id)
        self._notify_worker()
        return run

    def _record_event(self, project_id: str, run_id: str, event: BackendEvent) -> None:
        event_data = dict(event.data)
        case_result = event_data.pop("case_result", None)
        partial_summary = event_data.pop("partial_summary", None)
        if isinstance(case_result, dict):
            self._persist_case_results(
                project_id,
                run_id,
                {"cases": [case_result]},
            )
        self.platform_store.add_model_eval_run_event(
            run_id,
            event.event_type,
            event.message,
            phase=event.phase,
            progress=event.progress,
            data=event_data,
        )
        self.platform_store.update_model_eval_run(
            run_id,
            status=(event.phase if event.phase in {"preparing", "running", "scoring", "reporting"} else None),
            phase=event.phase or None,
            progress=event.progress,
            message=event.message,
            summary=partial_summary if isinstance(partial_summary, dict) else None,
        )

    def run_once(self) -> dict[str, Any] | None:
        run = self.platform_store.claim_model_eval_run()
        if not run:
            return None
        run_id = str(run["id"])
        project_id = str(run["project_id"])
        backend: EvaluationBackend | None = None
        work_dir = None
        resource_sampler = None
        try:
            backend = self.backend_resolver(run)
            work_dir = self.artifact_storage.workspace(
                "model-evaluations", project_id, run_id
            )
            snapshot = dict(run.get("snapshot") or {})
            from auto_test.evaluation.resources import capture_execution_environment
            test_environment = capture_execution_environment()
            (work_dir / "test_environment.json").write_text(json.dumps(test_environment, ensure_ascii=False, indent=2), encoding="utf-8")
            if snapshot.get("resource_binding"):
                from auto_test.evaluation.resources import EvaluationResourceSampler
                resource_sampler = EvaluationResourceSampler(self.platform_store, snapshot["resource_binding"], work_dir)
                resource_sampler.start()
            task_config = dict(snapshot.get("task_config") or {})
            secret_env = _resolve_request_secrets(
                task_config,
                platform_store=self.platform_store,
                model_profile_id=str(run.get("model_profile_id") or ""),
            )
            request = EvaluationRequest(
                run_id=run_id,
                project_id=project_id,
                mode=str(snapshot.get("mode") or "mock"),
                task_config=task_config,
                work_dir=work_dir,
                backend_version=str(
                    run.get("backend_version") or getattr(backend, "version", "")
                ),
                secret_env=secret_env,
            )
            with self._active_lock:
                self._active_backend[run_id] = backend
            result = backend.run(
                request,
                on_event=lambda event: self._record_event(project_id, run_id, event),
                should_stop=lambda: self._stop.is_set()
                or self.platform_store.is_model_eval_run_stop_requested(run_id),
            )
            self._persist_case_results(project_id, run_id, result.raw)
            result.summary["test_environment"] = test_environment
            if resource_sampler:
                result.summary["resource_correlation"] = resource_sampler.finish(result.summary)
            (work_dir / "summary.json").write_text(json.dumps(result.summary, ensure_ascii=False, indent=2), encoding="utf-8")
            self.artifact_storage.publish_tree(work_dir)
            error = result.error_type if result.status == "failed" else ""
            return self.platform_store.finish_model_eval_run(
                project_id,
                run_id,
                status=result.status,
                summary=result.summary,
                artifact_ref=self.artifact_storage.reference(work_dir),
                error=error,
            )
        except Exception as exc:
            log.error("模型评测 Worker 执行失败：run=%s error=%s", run_id, type(exc).__name__)
            current = self.platform_store.get_model_eval_run(project_id, run_id) or {}
            return self.platform_store.finish_model_eval_run(
                project_id,
                run_id,
                status="failed",
                summary=dict(current.get("summary") or {}),
                artifact_ref=(
                    self.artifact_storage.reference(work_dir) if work_dir else ""
                ),
                error=f"Worker 运行时异常：{type(exc).__name__}",
            )
        finally:
            if resource_sampler:
                resource_sampler.close()
            with self._active_lock:
                self._active_backend.pop(run_id, None)

    def _persist_case_results(
        self, project_id: str, run_id: str, raw: dict[str, Any]
    ) -> None:
        cases = list(raw.get("cases") or [])
        if not cases:
            cases = [
                item
                for item in list(raw.get("samples") or [])
                if "/predictions/" in "/" + str(item.get("path") or "").replace("\\", "/")
            ]
        normalized = []
        for index, item in enumerate(cases, start=1):
            metrics = dict(item.get("metrics") or {})
            if not metrics:
                metrics = {key: value for key, value in item.items() if key not in {"score", "response"}}
            score = item.get("score")
            if not isinstance(score, dict):
                score = {"score": score} if score is not None else {}
            normalized.append(
                {
                    "case_id": str(
                        item.get("case_id")
                        or item.get("case_key")
                        or item.get("id")
                        or item.get("index")
                        or f"result-{index}"
                    ),
                    "attempt": int(item.get("attempt") or 1),
                    "status": str(item.get("status") or ("error" if item.get("error") else "completed")),
                    "metrics": metrics,
                    "score": score,
                    "artifact_ref": str(item.get("path") or ""),
                }
            )
        if normalized:
            self.platform_store.save_model_eval_case_results(
                project_id, run_id, normalized
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception as exc:
                log.error("模型评测调度异常：%s", type(exc).__name__)
            if self.signal_queue.backend == "redis":
                self.signal_queue.wait(self.queue_topic, self.poll_interval)
            else:
                self._wake.wait(self.poll_interval)
                self._wake.clear()
