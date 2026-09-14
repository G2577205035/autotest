"""Persistent background manager for interface automation scenarios."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from auto_test.common.logging import log
from auto_test.core.task_queue import LocalTaskSignalQueue, TaskSignalQueue
from auto_test.platform.contracts import PlatformRepository


class InterfaceScenarioManager:
    def __init__(
        self,
        platform_store: PlatformRepository,
        execute_callback: Callable[..., dict[str, Any]],
        *,
        signal_queue: TaskSignalQueue | None = None,
        poll_interval: float = 1.0,
        concurrency: int = 5,
    ):
        self.platform_store = platform_store
        self.execute_callback = execute_callback
        self.signal_queue = signal_queue or LocalTaskSignalQueue()
        self.poll_interval = max(0.05, float(poll_interval))
        self.concurrency = max(1, min(int(concurrency), 5))
        self.queue_topic = "interface-scenarios"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._workers: list[threading.Thread] = []

    def _notify_workers(self, count: int = 1) -> None:
        self._wake.set()
        for _ in range(max(1, int(count))):
            self.signal_queue.notify(self.queue_topic)

    def start(self) -> None:
        if any(worker.is_alive() for worker in self._workers):
            return
        self._stop.clear()
        self._workers = []
        for index in range(self.concurrency):
            worker = threading.Thread(
                target=self._loop,
                kwargs={"schedule": index == 0},
                name=f"liema-interface-worker-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._notify_workers(len(self._workers) or 1)
        for worker in self._workers:
            worker.join(timeout=max(0.0, float(timeout)))
        self._workers = []

    def submit(
        self,
        project_id: str,
        scenario: dict[str, Any],
        parameters: dict[str, Any],
        *,
        batch_id: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        run = self.platform_store.create_interface_scenario_run(
            project_id,
            scenario,
            batch_id=batch_id,
            created_by=created_by,
            status="queued",
            parameters=parameters,
        )
        self._notify_workers()
        return run

    def submit_batch(
        self,
        project_id: str,
        scenarios: list[dict[str, Any]],
        parameters: dict[str, Any],
        *,
        batch_id: str,
        created_by: str = "",
        concurrency: int = 3,
    ) -> list[dict[str, Any]]:
        concurrency_limit = max(1, min(int(concurrency), self.concurrency, 5))
        runs = [
            self.platform_store.create_interface_scenario_run(
                project_id,
                scenario,
                batch_id=batch_id,
                created_by=created_by,
                status="queued",
                parameters=parameters,
                concurrency_limit=concurrency_limit,
            )
            for scenario in scenarios
        ]
        self._notify_workers(len(runs))
        return runs

    def stop_run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.platform_store.request_stop_interface_scenario_run(
            project_id, run_id
        )
        self._notify_workers()
        return run

    def run_once(self) -> dict[str, Any] | None:
        run = self.platform_store.claim_interface_scenario_run()
        if not run:
            return None
        try:
            return self.execute_callback(
                str(run["project_id"]),
                run.get("scenario_snapshot") or {},
                run.get("parameters") or {},
                batch_id=str(run.get("batch_id") or ""),
                created_by=str(run.get("created_by") or ""),
                existing_run=run,
            )
        except Exception as exc:
            log.error(
                "接口场景 Worker 执行失败：run=%s error=%s",
                run.get("id"),
                type(exc).__name__,
            )
            # The callback normally records its own terminal state. Keep an
            # unexpected Worker/runtime failure from leaving a claimed row in
            # ``running`` forever. Exception text is intentionally not persisted
            # because it may contain request secrets.
            current = self.platform_store.get_interface_scenario_run(
                str(run["project_id"]), str(run["id"])
            )
            if current and current.get("status") == "running":
                summary = {
                    "total_steps": int(
                        ((current.get("summary") or {}).get("total_steps") or 0)
                    ),
                    "passed_steps": 0,
                    "failed_steps": 1,
                    "skipped_steps": 0,
                    "elapsed_ms": 0.0,
                }
                return self.platform_store.finish_interface_scenario_run(
                    str(run["project_id"]),
                    str(run["id"]),
                    status="failed",
                    summary=summary,
                    result={
                        "schema_version": "1.0",
                        "scenario": {
                            "id": run.get("scenario_id") or "",
                            "name": run.get("scenario_name") or "接口场景",
                        },
                        "summary": summary,
                        "steps": [],
                        "error": f"Worker 运行时异常：{type(exc).__name__}",
                    },
                )
            return current

    def _loop(self, *, schedule: bool = False) -> None:
        next_schedule_check = 0.0
        while not self._stop.is_set():
            try:
                if schedule and time.monotonic() >= next_schedule_check:
                    next_schedule_check = time.monotonic() + 1.0
                    dispatched = self.platform_store.dispatch_interface_schedule()
                    if dispatched and dispatched.get("queued"):
                        self._notify_workers(dispatched["queued"])
                if self.run_once():
                    continue
            except Exception as exc:
                log.error("接口场景调度异常：%s: %s", type(exc).__name__, exc)
            if self.signal_queue.backend == "redis":
                self.signal_queue.wait(self.queue_topic, self.poll_interval)
            else:
                self._wake.wait(self.poll_interval)
                self._wake.clear()
