"""Independent persistent task worker entry point."""

from __future__ import annotations

import signal
import threading

from auto_test.common.logging import log
from auto_test.common.paths import PROJECT_ROOT, prepare_runtime_layout
from auto_test.common.runtime_secrets import ensure_runtime_master_key
from auto_test.core.task_manager import TaskManager
from auto_test.core.task_queue import create_task_signal_queue
from auto_test.evaluation.manager import (
    ModelEvaluationManager,
    create_model_evaluation_backend_resolver,
)
from auto_test.platform.artifact_storage import create_artifact_storage
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.api import create_platform_api


def main() -> None:
    prepare_runtime_layout(PROJECT_ROOT)
    ensure_runtime_master_key()
    signal_queue = create_task_signal_queue()
    task_manager = TaskManager.default(PROJECT_ROOT, signal_queue=signal_queue)
    platform_store = create_platform_repository(PROJECT_ROOT)
    artifact_storage = create_artifact_storage(PROJECT_ROOT)
    _, report_manager, platform_store, _ = create_platform_api(
        task_manager.store,
        platform_store=platform_store,
        artifact_storage=artifact_storage,
        signal_queue=signal_queue,
    )
    interface_scenario_manager = report_manager.interface_scenario_manager
    model_evaluation_manager = ModelEvaluationManager(
        platform_store,
        artifact_storage,
        create_model_evaluation_backend_resolver(PROJECT_ROOT),
        signal_queue=signal_queue,
    )
    stopping = threading.Event()
    heartbeat_stopped = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stopped.is_set():
            signal_queue.heartbeat("primary", 20)
            heartbeat_stopped.wait(5)

    def request_stop(*_args) -> None:
        stopping.set()
        task_manager._notify_worker()
        report_manager._notify_worker()
        interface_scenario_manager._notify_workers()
        model_evaluation_manager._notify_worker()

    for signal_name in ("SIGINT", "SIGTERM"):
        available = getattr(signal, signal_name, None)
        if available is not None:
            signal.signal(available, request_stop)

    task_manager.start()
    report_manager.start()
    interface_scenario_manager.start()
    model_evaluation_manager.start()
    heartbeat = threading.Thread(
        target=heartbeat_loop,
        name="liema-worker-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    log.info(
        "独立 Worker 已启动：queue=%s，负责业务自动化、接口场景、模型评测与企业报告",
        signal_queue.backend,
    )
    try:
        stopping.wait()
    except KeyboardInterrupt:
        stopping.set()
    finally:
        heartbeat_stopped.set()
        heartbeat.join(timeout=2)
        signal_queue.clear_heartbeat("primary")
        model_evaluation_manager.stop()
        interface_scenario_manager.stop()
        report_manager.stop()
        task_manager.stop()
        close_platform = getattr(platform_store, "close", None)
        if callable(close_platform):
            close_platform()
        signal_queue.close()
        log.info("独立 Worker 已停止")


if __name__ == "__main__":
    main()
