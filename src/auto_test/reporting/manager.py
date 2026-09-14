"""Persistent background worker for reliable enterprise report generation."""

from __future__ import annotations

import threading
import traceback

from auto_test.common.logging import log
from auto_test.monitoring.translation_speed import summarize_translation_speed
from auto_test.platform.artifact_storage import ArtifactStorage
from auto_test.platform.contracts import PlatformRepository, TaskRepository
from auto_test.reporting.enterprise import build_report_artifacts
from auto_test.core.task_queue import LocalTaskSignalQueue, TaskSignalQueue


class ReportManager:
    def __init__(
        self,
        platform_store: PlatformRepository,
        task_store: TaskRepository,
        artifact_storage: ArtifactStorage | None = None,
        poll_interval: float = 1.0,
        signal_queue: TaskSignalQueue | None = None,
    ):
        self.platform_store = platform_store
        self.task_store = task_store
        self.artifact_storage = artifact_storage
        self.poll_interval = poll_interval
        self.signal_queue = signal_queue or LocalTaskSignalQueue()
        self.queue_topic = "reports"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, name="liema-report-worker", daemon=True)
        self._worker.start()

    def _notify_worker(self) -> None:
        self._wake.set()
        self.signal_queue.notify(self.queue_topic)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._notify_worker()
        if self._worker:
            self._worker.join(timeout=timeout)

    def submit(self, run_id: str, options: dict) -> dict:
        run = self.task_store.get_run(run_id)
        if not run.get("run_dir"):
            raise ValueError("运行尚未产生报告目录")
        template = self.platform_store.get_report_template()
        job = self.platform_store.create_report_job(run_id, template["version"], options)
        self._notify_worker()
        return job

    def _project_models(self, job):
        from auto_test.platform.model_access import ProjectModelStore
        options = job.get("options") or {}
        return ProjectModelStore(self.platform_store, str(options.get("_project_id") or ""), str(options.get("_created_by_user_id") or ""))

    def _execute(self, job: dict) -> None:
        try:
            run = self.task_store.get_run(job["run_id"])
            template = self.platform_store.get_report_template(job["template_version"])
            metrics = self.task_store.get_metrics(job["run_id"], limit=100000)
            translation_speed = summarize_translation_speed(
                self.task_store.get_translation_progress_events(job["run_id"])
            )
            snapshot, artifacts = build_report_artifacts(
                run, metrics, template, job["options"], job["id"], self._project_models(job),
                self.artifact_storage, translation_speed=translation_speed,
            )
            if self.artifact_storage:
                self.artifact_storage.publish_tree(artifacts["artifact_dir"])
            self.platform_store.complete_report_job(job["id"], snapshot, artifacts)
            log.info(f"企业测试报告生成完成：{artifacts['artifact_dir']}")
        except Exception as exc:
            self.platform_store.fail_report_job(job["id"], f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            log.error(f"企业测试报告生成失败：{type(exc).__name__}: {exc}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.platform_store.claim_report_job()
                if job:
                    self._execute(job)
                    continue
            except Exception as exc:
                log.error(f"报告任务调度异常：{exc}")
            if self.signal_queue.backend == "redis":
                self.signal_queue.wait(self.queue_topic, self.poll_interval)
            else:
                self._wake.wait(self.poll_interval)
                self._wake.clear()
