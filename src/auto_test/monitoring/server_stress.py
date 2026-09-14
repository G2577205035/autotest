"""Independent server stress jobs for the Web console."""

from __future__ import annotations

import json
import shlex
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from auto_test.common.config_loader import monitor_cfg, stress_cfg
from auto_test.common.logging import log
from auto_test.common.paths import PROJECT_ROOT
from auto_test.integrations.ssh import SSHClient
from auto_test.monitoring.cpu_stress import CpuStressRunner
from auto_test.monitoring.docker_monitor import DockerMonitor
from auto_test.monitoring.server_capabilities import ServerCapabilityProbe
from auto_test.monitoring.server_sessions import ServerSessionManager
from auto_test.platform.artifact_storage import ArtifactStorage, create_artifact_storage
from auto_test.platform.contracts import PlatformRepository
from auto_test.platform.secrets import SecretEncryptionError, decrypt_secret, encrypt_secret
from auto_test.reporting.server_performance import build_server_performance_artifacts


PYTHON_CPU_STRESS = r"""#!/usr/bin/env python3
import argparse
import math
import multiprocessing as mp
import os
import time


def worker(until, load):
    load = max(1, min(100, int(load)))
    period = 0.1
    busy = period * load / 100.0
    value = 0.0
    while time.time() < until:
        start = time.time()
        while time.time() - start < busy:
            value = math.sin(value + 1.234567) * math.cos(value + 0.765432)
        rest = period - busy
        if rest > 0:
            time.sleep(rest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--load", type=int, default=80)
    args = parser.parse_args()
    workers = args.workers or (os.cpu_count() or 1)
    until = time.time() + max(1, args.duration)
    procs = [mp.Process(target=worker, args=(until, args.load)) for _ in range(workers)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    print(f"python_cpu_stress completed workers={workers} load={args.load} duration={args.duration}s")


if __name__ == "__main__":
    main()
"""


class ServerStressManager:
    """Run CPU/GPU/monitoring stress jobs outside the automation pipeline."""

    def __init__(
        self,
        store: PlatformRepository | None,
        artifact_storage: ArtifactStorage | None = None,
        *,
        max_concurrent_jobs: int | None = None,
        poll_interval: float = 0.5,
        session_manager: ServerSessionManager | None = None,
    ):
        self.store = store
        self.artifact_storage = artifact_storage or create_artifact_storage(PROJECT_ROOT)
        self.capability_probe = ServerCapabilityProbe()
        self.session_manager = session_manager
        self._threads: dict[str, threading.Thread] = {}
        self._secrets: dict[str, str] = {}
        self._latest_samples: dict[str, dict[str, dict[str, Any]]] = {}
        self._safety_events: dict[str, str] = {}
        self._lock = threading.Lock()
        self._queue_lock = threading.Lock()
        configured_limit = max_concurrent_jobs
        if configured_limit is None:
            configured_limit = int(stress_cfg().get("max_concurrent_jobs") or 1)
        self.max_concurrent_jobs = max(1, min(int(configured_limit), 8))
        self.poll_interval = max(0.1, float(poll_interval))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._dispatcher: threading.Thread | None = None

    def start(self) -> None:
        if self.session_manager:
            self.session_manager.start()
        if self.store is None or (self._dispatcher and self._dispatcher.is_alive()):
            return
        self._stop.clear()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="liema-stress-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._dispatcher:
            self._dispatcher.join(timeout=timeout)
        with self._lock:
            active = list(self._threads.items())
        for job_id, _thread in active:
            try:
                if self.store:
                    self.store.request_stop_stress_job(job_id)
            except KeyError:
                pass
        deadline = time.time() + max(0.0, timeout)
        for _job_id, thread in active:
            thread.join(timeout=max(0.0, deadline - time.time()))
        if self.session_manager:
            self.session_manager.shutdown(timeout=max(0.0, timeout))

    def _active_hosts(self) -> set[str]:
        if self.store is None:
            return set()
        with self._lock:
            job_ids = list(self._threads)
        result = set()
        for job_id in job_ids:
            job = self.store.get_stress_job(job_id)
            if job and job.get("target_host"):
                result.add(str(job["target_host"]))
        return result

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                completed = [job_id for job_id, thread in self._threads.items() if not thread.is_alive()]
                for job_id in completed:
                    self._threads.pop(job_id, None)
                    self._secrets.pop(job_id, None)
                capacity = self.max_concurrent_jobs - len(self._threads)
            dispatched = False
            while capacity > 0 and not self._stop.is_set():
                with self._queue_lock:
                    job = self.store.claim_stress_job(self._active_hosts()) if self.store else None
                    if not job:
                        break
                    job_id = str(job["id"])
                    secret_enc = str(job.pop("_secret_enc", "") or "")
                if bool(job.get("secret_required")) and not secret_enc:
                    self.store.update_stress_job(
                        job_id,
                        status="interrupted",
                        message="服务器凭据不可用，请重新授权后提交",
                        finished=True,
                    )
                    continue
                if secret_enc:
                    try:
                        password = decrypt_secret(secret_enc)
                    except SecretEncryptionError as exc:
                        self.store.update_stress_job(
                            job_id,
                            status="failed",
                            message="服务器凭据无法解密，请重新授权后提交",
                            error=str(exc),
                            finished=True,
                        )
                        continue
                    with self._lock:
                        self._secrets[job_id] = password
                thread = threading.Thread(
                    target=self._run_job,
                    args=(job_id,),
                    name=f"server-stress-{job_id[:8]}",
                    daemon=True,
                )
                with self._lock:
                    self._threads[job_id] = thread
                thread.start()
                dispatched = True
                capacity -= 1
            if dispatched:
                continue
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def submit(self, options: dict[str, Any]) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError("未配置压测任务仓储")
        if options.get('cpu_benchmark') and ('cpu' not in options.get('modes', []) or 'gpu' in options.get('modes', []) or not 1 <= int(options.get('workers') or 0) <= 64 or not 10 <= int(options.get('duration') or 0) <= 300 or int(options.get('cpu_load') or 0) != 100):
            raise RuntimeError('CPU 基准参数无效：仅 CPU、100% 负载、1～64 个进程、10～300 秒')
        persisted_options = dict(options)
        password = str(persisted_options.pop("password", "") or "")
        session_id = str(persisted_options.get("session_id") or "").strip()
        if session_id:
            if not self.session_manager:
                raise RuntimeError("服务器会话服务不可用")
            session = self.session_manager.get(session_id, touch=True)
            persisted_options.update({
                "session_id": session_id,
                "server_name": session.get("name") or persisted_options.get("server_name") or "",
                "host": session.get("host") or "",
                "port": session.get("port") or 22,
                "user": session.get("user") or "",
            })
            password = ""
        secret_enc = encrypt_secret(password) if password else ""
        with self._queue_lock:
            job = self.store.create_stress_job(
                persisted_options,
                secret_required=bool(password),
                secret_enc=secret_enc,
            )
        self.start()
        self._wake.set()
        return job

    def request_stop(self, job_id: str) -> dict[str, Any]:
        if self.store is None:
            raise KeyError(job_id)
        job = self.store.request_stop_stress_job(job_id)
        self._wake.set()
        return job

    def _raise_if_stopped(self, job_id: str) -> None:
        if self.store and self.store.is_stress_stop_requested(job_id):
            raise InterruptedError("用户请求停止压测任务")

    def _record_sample(self, job_id: str, label: str, sample: dict[str, Any]) -> None:
        if self.store:
            self.store.add_stress_sample(job_id, label, sample)
        with self._lock:
            self._latest_samples.setdefault(job_id, {})[label] = dict(sample)

    @staticmethod
    def _numeric(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _raise_if_unsafe(self, job_id: str, options: dict[str, Any]) -> None:
        self._raise_if_stopped(job_id)
        if not bool(options.get("safety_enabled", True)):
            return
        with self._lock:
            prior_event = self._safety_events.get(job_id, "")
            samples = list(self._latest_samples.get(job_id, {}).values())
        if prior_event:
            raise InterruptedError(prior_event)
        limits = {
            "cpu_temp": float(options.get("cpu_temp_limit") or 90),
            "gpu_temp": float(options.get("gpu_temp_limit") or 85),
            "memory": float(options.get("memory_usage_limit") or 95),
            "disk": float(options.get("disk_usage_limit") or 95),
        }
        breaches = []
        for sample in samples:
            cpu_temp = self._numeric(sample.get("cpu_temp_c"))
            gpu_temps = [
                self._numeric(value)
                for key, value in sample.items()
                if key == "gpu_temp_c" or (key.startswith("gpu") and key.endswith("_temp_c"))
            ]
            gpu_temps = [value for value in gpu_temps if value is not None]
            memory_used = self._numeric(sample.get("mem_used_gb"))
            memory_total = self._numeric(sample.get("mem_total_gb"))
            memory_pct = memory_used / memory_total * 100 if memory_used is not None and memory_total else None
            disk_pct = self._numeric(sample.get("disk_util_pct"))
            if cpu_temp is not None and cpu_temp >= limits["cpu_temp"]:
                breaches.append(f"CPU 温度 {cpu_temp:.1f}°C ≥ {limits['cpu_temp']:.1f}°C")
            if gpu_temps and max(gpu_temps) >= limits["gpu_temp"]:
                breaches.append(f"GPU 温度 {max(gpu_temps):.1f}°C ≥ {limits['gpu_temp']:.1f}°C")
            if memory_pct is not None and memory_pct >= limits["memory"]:
                breaches.append(f"内存占用 {memory_pct:.1f}% ≥ {limits['memory']:.1f}%")
            if disk_pct is not None and disk_pct >= limits["disk"]:
                breaches.append(f"磁盘繁忙度 {disk_pct:.1f}% ≥ {limits['disk']:.1f}%")
        if breaches:
            event = "安全保护触发，已停止负载：" + "；".join(dict.fromkeys(breaches))
            with self._lock:
                self._safety_events[job_id] = event
            raise InterruptedError(event)

    def _resolve_target(self, options: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": str(options.get("host") or "").strip(),
            "port": int(options.get("port") or 22),
            "user": str(options.get("user") or "").strip(),
            "password": str(options.get("password") or ""),
        }

    def probe(self, options: dict[str, Any]) -> dict[str, Any]:
        """Connect to a target and run the read-only compatibility probe."""
        server = self._resolve_target(options)
        if not server["host"]:
            raise RuntimeError("请填写目标服务器地址")
        if not server["user"]:
            raise RuntimeError("请填写 SSH 用户")
        ssh = SSHClient(server["host"], server["user"], server["password"], server["port"])
        if not ssh.connect():
            raise RuntimeError(ssh.last_error or "SSH 连接失败")
        try:
            return self.capability_probe.collect(
                ssh,
                options.get("modes") or ["monitor"],
                gpu_burn_source=str(options.get("gpu_burn_source") or ""),
                gpu_burn_image=str(options.get("gpu_burn_image") or ""),
                gpu_burn_blackwell_image=str(options.get("gpu_burn_blackwell_image") or ""),
                gpu_devices=str(options.get("gpu_devices") or ""),
            )
        finally:
            ssh.disconnect()

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_stress_job(job_id)
        if not job:
            return
        options = dict(job.get("options") or {})
        with self._lock:
            submitted_password = self._secrets.get(job_id, "")
        if submitted_password:
            options["password"] = submitted_password
        modes = set(job.get("modes") or ["monitor"])
        duration = max(10, min(int(options.get("duration") or 60), 24 * 3600))
        artifact_dir = self.artifact_storage.workspace("server_stress", job_id)
        self.store.update_stress_job(
            job_id,
            status="running",
            message="正在连接服务器",
            artifact_dir=self.artifact_storage.reference(artifact_dir),
        )

        ssh = None
        session_id = str(options.get("session_id") or "").strip()
        session_owned = False
        monitor = None
        cpu_runner = None
        cpu_started = False
        cpu_fallback: dict[str, Any] | None = None
        cpu_summary: dict[str, Any] = {"status": "not_requested"}
        gpu_result: dict[str, Any] = {}
        capability_report: dict[str, Any] = {}
        try:
            self._raise_if_stopped(job_id)
            server = self._resolve_target(options)
            if session_id:
                if not self.session_manager:
                    raise RuntimeError("服务器会话服务不可用")
                try:
                    ssh = self.session_manager.acquire(session_id)
                    session_owned = True
                except KeyError as exc:
                    raise RuntimeError("服务器会话已关闭，请重新连接后再启动压测") from exc
            else:
                if not server["host"]:
                    raise RuntimeError("请填写目标服务器地址")
                if not server["user"]:
                    raise RuntimeError("请填写 SSH 用户")
                ssh = SSHClient(server["host"], server["user"], server["password"], server["port"])
                if not ssh.connect():
                    raise RuntimeError(ssh.last_error or "SSH 连接失败")

            self._raise_if_stopped(job_id)
            self.store.update_stress_job(job_id, message="正在执行只读兼容性预检")
            probe_modes = modes | {"monitor"}
            capability_report = self.capability_probe.collect(
                ssh,
                probe_modes,
                gpu_burn_source=str(options.get("gpu_burn_source") or ""),
                gpu_burn_image=str(options.get("gpu_burn_image") or ""),
                gpu_burn_blackwell_image=str(options.get("gpu_burn_blackwell_image") or ""),
                gpu_devices=str(options.get("gpu_devices") or ""),
            )
            (artifact_dir / "server_capabilities.json").write_text(
                json.dumps(capability_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            blocked = {
                mode: capability_report["capabilities"][mode]
                for mode in probe_modes
                if mode in capability_report["capabilities"]
                and capability_report["capabilities"][mode]["status"] == "blocked"
            }
            if blocked:
                missing = []
                for mode, decision in blocked.items():
                    requirements = "；".join(decision.get("prerequisites") or [])
                    missing.append(f"{mode}: {decision['message']}" + (f"（{requirements}）" if requirements else ""))
                raise RuntimeError("兼容性预检未通过：" + "；".join(missing))

            self._raise_if_stopped(job_id)
            modules = job.get("modules") or monitor_cfg().get("containers", [])
            self.store.update_stress_job(job_id, message="正在采集性能指标")
            monitor = DockerMonitor(
                ssh,
                ssh,
                ",".join(modules),
                enable_log=False,
                enable_perf=True,
                run_dir=str(artifact_dir),
                perf_prefix="server_",
                sample_callback=lambda label, sample: self._record_sample(job_id, label, sample),
            )
            monitor.start()

            if options.get('cpu_benchmark'):
                from auto_test.monitoring.cpu_benchmark import run_cpu_benchmark
                self.store.update_stress_job(job_id, message='正在执行 SHA-256 CPU 基准')
                benchmark = run_cpu_benchmark(ssh, duration=duration, workers=int(options['workers']), guard_callback=lambda: self._raise_if_unsafe(job_id, options))
                cpu_summary = {'status': 'succeeded', 'backend': 'liema-sha256-v1', 'benchmark': benchmark}
            elif "cpu" in modes:
                self._raise_if_stopped(job_id)
                self.store.update_stress_job(job_id, message="正在执行 CPU 压测")
                cpu_runner = CpuStressRunner(ssh)
                cpu_engine = capability_report.get("cpu", {}).get("stress_engine")
                if cpu_engine == "stress-ng" and cpu_runner.install(allow_install=False):
                    cpu_started = cpu_runner.start_background(
                        workers=int(options.get("workers") or 0),
                        cpu_load=int(options.get("cpu_load") or 80),
                        duration=duration,
                        run_dir=str(artifact_dir),
                    )
                    if not cpu_started:
                        cpu_summary = {
                            "status": "failed",
                            "message": "stress-ng 启动失败，请检查 stress-ng 参数和服务器权限",
                        }
                elif cpu_engine == "python":
                    install_diagnosis = cpu_runner.install_diagnosis()
                    cpu_summary = {"status": "degraded", "message": "使用 Python CPU 降级引擎", "stress_ng_diagnosis": install_diagnosis}
                    log.warning("stress-ng 不可用，按预检结论使用 Python CPU 降级方案")
                if not cpu_started:
                    self.store.update_stress_job(job_id, message="stress-ng 不可用，正在使用 Python CPU 压测降级方案")
                    cpu_fallback = self._start_python_cpu_stress(
                        ssh,
                        workers=int(options.get("workers") or 0),
                        cpu_load=int(options.get("cpu_load") or 80),
                        duration=duration,
                    )

            if "gpu" in modes:
                self._raise_if_stopped(job_id)
                self.store.update_stress_job(job_id, message="正在执行 GPU 压测")
                gpu_result = self._run_gpu_burn(
                    ssh,
                    duration,
                    str(options.get("gpu_burn_source") or ""),
                    job_id=job_id,
                    engine=str(capability_report.get("gpu", {}).get("stress_engine") or ""),
                    container_image=str(capability_report.get("gpu", {}).get("container_image") or ""),
                    gpu_devices=str(options.get("gpu_devices") or ""),
                    allow_busy_gpu=bool(options.get("allow_busy_gpu", False)),
                    gpu_memory_percent=int(options.get("gpu_memory_percent") or 90),
                    guard_callback=lambda: self._raise_if_unsafe(job_id, options),
                )

            if cpu_started and cpu_runner:
                cpu_runner.wait_and_collect(stop_callback=lambda: self._raise_if_unsafe(job_id, options))
                elapsed = cpu_runner.elapsed()
                cpu_summary = {"status": "succeeded", **cpu_runner.summary()}
                output = str(getattr(cpu_runner, "_output", "") or "")
                if elapsed < duration * 0.8:
                    cpu_summary.update({
                        "status": "failed",
                        "message": f"stress-ng 提前退出，实际运行 {elapsed:.1f}s，低于预期 {duration}s",
                        "output_tail": output[-2000:],
                    })
            elif cpu_fallback:
                cpu_summary = self._wait_python_cpu_stress(
                    ssh, cpu_fallback, duration, guard_callback=lambda: self._raise_if_unsafe(job_id, options)
                )
                if cpu_runner and cpu_runner.install_error():
                    cpu_summary["stress_ng_diagnosis"] = cpu_runner.install_diagnosis()
            elif "gpu" not in modes and not options.get('cpu_benchmark'):
                self._cooperative_wait(job_id, duration, guard_callback=lambda: self._raise_if_unsafe(job_id, options))

            if monitor:
                monitor.stop_perf()
                monitor.stop_logs()

            perf_summary = monitor.get_perf_summary() if monitor else {}
            if cpu_started and cpu_runner:
                perf_csv = artifact_dir / "report" / "server_perf_app.csv"
                cpu_runner.check_health(perf_csv=str(perf_csv) if perf_csv.exists() else None)
                cpu_runner.save_results()

            report_path = self._write_report(
                job_id,
                options,
                modes,
                duration,
                perf_summary,
                cpu_summary,
                gpu_result,
                capability_report,
                artifact_dir,
            )
            self.artifact_storage.publish_tree(artifact_dir)
            failed_modes = []
            if cpu_summary.get("status") == "failed":
                failed_modes.append("CPU")
            if gpu_result.get("status") == "failed":
                failed_modes.append("GPU")
            self.store.update_stress_job(
                job_id,
                status="failed" if failed_modes else "succeeded",
                message=("压测未完整执行：" + "、".join(failed_modes)) if failed_modes else "服务器压测完成",
                report_path=self.artifact_storage.reference(report_path),
                finished=True,
            )
        except InterruptedError as exc:
            report_path = None
            try:
                if monitor:
                    monitor.stop_perf()
                    monitor.stop_logs()
                perf_summary = monitor.get_perf_summary() if monitor else {}
                interruption_reason = str(exc) or "压测任务已停止"
                if cpu_summary.get("status") in {"not_requested", "degraded", ""} and "cpu" in modes:
                    cpu_summary = {"status": "interrupted", "message": interruption_reason}
                report_path = self._write_report(
                    job_id,
                    options,
                    modes,
                    duration,
                    perf_summary,
                    cpu_summary,
                    gpu_result,
                    capability_report,
                    artifact_dir,
                    safety_event=interruption_reason,
                )
                self.artifact_storage.publish_tree(artifact_dir)
            except Exception as report_exc:
                log.warning(f"服务器压测中断报告生成失败：{report_exc}")
            self.store.update_stress_job(
                job_id,
                status="interrupted",
                message=str(exc) or "压测任务已停止",
                report_path=self.artifact_storage.reference(report_path) if report_path else None,
                finished=True,
            )
            log.warning(f"服务器压测任务已停止：{job_id}")
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            report_path = None
            try:
                if monitor:
                    monitor.stop_perf()
                    monitor.stop_logs()
                perf_summary = monitor.get_perf_summary() if monitor else {}
                task_error_summary = {
                    "status": "failed",
                    "message": "服务器压测任务异常，已生成失败报告用于排查",
                    "error": error_text,
                    "traceback_tail": traceback.format_exc()[-4000:],
                }
                if cpu_summary.get("status") in {"not_requested", ""}:
                    cpu_summary = task_error_summary
                else:
                    cpu_summary["task_error"] = task_error_summary
                report_path = self._write_report(
                    job_id,
                    options,
                    modes,
                    duration,
                    perf_summary,
                    cpu_summary,
                    gpu_result,
                    capability_report,
                    artifact_dir,
                )
                self.artifact_storage.publish_tree(artifact_dir)
            except Exception as report_exc:
                log.warning(f"服务器压测失败报告生成失败：{report_exc}")
            self.store.update_stress_job(
                job_id,
                status="failed",
                message="服务器压测失败",
                error=error_text,
                report_path=self.artifact_storage.reference(report_path) if report_path else None,
                finished=True,
            )
            log.error(f"服务器压测任务失败：{traceback.format_exc()}")
        finally:
            if monitor:
                try:
                    monitor.stop_perf()
                    monitor.stop_logs()
                except Exception:
                    pass
            if ssh:
                if cpu_started and cpu_runner:
                    try:
                        cpu_runner.stop()
                    except Exception:
                        pass
                if cpu_fallback and cpu_fallback.get("pid"):
                    try:
                        ssh.exec(f"kill -TERM {cpu_fallback['pid']} 2>/dev/null || true")
                    except Exception:
                        pass
                if session_owned and self.session_manager:
                    self.session_manager.release(session_id)
                else:
                    ssh.disconnect()
            with self._lock:
                self._threads.pop(job_id, None)
                self._secrets.pop(job_id, None)
                self._latest_samples.pop(job_id, None)
                self._safety_events.pop(job_id, None)
            self._wake.set()

    def _cooperative_wait(self, job_id: str, duration: int, *, guard_callback=None) -> None:
        deadline = time.time() + max(0, duration)
        while time.time() < deadline:
            (guard_callback or (lambda: self._raise_if_stopped(job_id)))()
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    def _run_gpu_burn(
        self,
        ssh: SSHClient,
        duration: int,
        source: str,
        *,
        job_id: str = "",
        engine: str = "",
        container_image: str = "",
        gpu_devices: str = "",
        allow_busy_gpu: bool = False,
        gpu_memory_percent: int = 90,
        guard_callback=None,
    ) -> dict[str, Any]:
        activity = self._inspect_gpu_activity(ssh, gpu_devices)
        if not activity["safe_to_stress"] and not allow_busy_gpu:
            return {
                "status": "failed",
                "safety_blocked": True,
                "message": "GPU 当前存在业务负载或状态无法确认，已阻止压测；请在维护窗口释放目标 GPU 后重试",
                "gpu_activity": activity,
            }
        if engine == "gpu-burn-docker":
            return self._run_gpu_burn_container(
                ssh,
                duration,
                container_image,
                job_id=job_id,
                gpu_activity=activity,
                gpu_devices=gpu_devices,
                gpu_memory_percent=gpu_memory_percent,
                guard_callback=guard_callback,
            )

        remote_root = "/tmp/liema_gpu_burn"
        remote_zip = f"{remote_root}/gpu-burn-master.zip"
        ssh.exec(f"mkdir -p {remote_root}")
        source_path = Path(source).expanduser() if source else None
        uploaded = False
        if source_path and source_path.is_file() and not ssh.is_local:
            uploaded = ssh.sftp_put_file(source_path, remote_zip)
            if uploaded:
                unzip_out, unzip_err = ssh.exec(
                    f"mkdir -p {remote_root} && rm -rf {remote_root}/gpu-burn-master && "
                    f"(command -v unzip >/dev/null 2>&1 && unzip -oq {remote_zip} -d {remote_root} "
                    f"|| python3 -m zipfile -e {remote_zip} {remote_root})",
                    timeout=120,
                )
                if unzip_err and "No module named zipfile" not in unzip_err:
                    log.warning(f"gpu-burn 解压提示：{unzip_err[:500]}")

        installed, _ = ssh.exec("command -v gpu_burn 2>/dev/null || true")
        executable = installed.strip().splitlines()[-1].strip() if installed.strip() else ""
        if not executable:
            executable = f"{remote_root}/gpu-burn-master/gpu_burn"
        ready, _ = ssh.exec(f"test -x {shlex.quote(executable)} && echo READY || echo NEED_BUILD")
        if ready.strip() != "READY":
            build_cmd = (
                f"cd {remote_root}/gpu-burn-master && "
                f"CUDA_HOME=$(dirname $(dirname $(command -v nvcc 2>/dev/null || echo /usr/local/cuda/bin/nvcc))) && "
                f"(make CUDAPATH=\"$CUDA_HOME\" > {remote_root}/gpu_burn_build.log 2>&1 || true)"
            )
            ssh.exec(build_cmd, timeout=300)
            executable = f"{remote_root}/gpu-burn-master/gpu_burn"

        ready, _ = ssh.exec(f"test -x {shlex.quote(executable)} && echo READY || echo NOT_READY")
        if ready.strip() != "READY":
            build_log, _ = ssh.exec(f"tail -80 {remote_root}/gpu_burn_build.log 2>/dev/null")
            diagnosis = self._diagnose_gpu_build_failure(ssh, build_log)
            return {
                "status": "failed",
                "uploaded_source": uploaded,
                "message": "gpu-burn 未编译成功，请确认 CUDA/nvcc/make 环境",
                "build_log": build_log[-4000:],
                "diagnosis": diagnosis,
            }

        seconds = max(10, min(int(duration), 24 * 3600))
        run_cmd = (
            f"cd {remote_root}/gpu-burn-master 2>/dev/null || cd /tmp; "
            f"(nohup "
            f"{'CUDA_VISIBLE_DEVICES=' + shlex.quote(','.join(map(str, ServerCapabilityProbe.parse_gpu_devices(gpu_devices)))) + ' ' if gpu_devices else ''}"
            f"{shlex.quote(executable)} -m {max(1, min(int(gpu_memory_percent), 95))}% {shlex.quote(str(seconds))} "
            f"> {remote_root}/gpu_burn_output.txt 2>&1; "
            f"echo $? > {remote_root}/gpu_burn_exit_code.txt) & echo $!"
        )
        out, _ = ssh.exec(run_cmd)
        pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        start = time.time()
        while pid.isdigit() and time.time() - start < seconds + 60:
            if job_id:
                try:
                    (guard_callback or (lambda: self._raise_if_stopped(job_id)))()
                except InterruptedError:
                    ssh.exec(f"kill -TERM {pid} 2>/dev/null || true")
                    raise
            alive, _ = ssh.exec(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD")
            if "DEAD" in alive:
                break
            time.sleep(5)
        output, _ = ssh.exec(f"tail -120 {remote_root}/gpu_burn_output.txt 2>/dev/null")
        exit_code, _ = ssh.exec(f"cat {remote_root}/gpu_burn_exit_code.txt 2>/dev/null")
        elapsed = round(time.time() - start, 1)
        exit_code = exit_code.strip()
        if exit_code and exit_code != "0":
            return {
                "status": "failed",
                "uploaded_source": uploaded,
                "pid": pid,
                "elapsed_s": elapsed,
                "exit_code": exit_code,
                "message": "gpu-burn 运行失败",
                "output_tail": output[-4000:],
            }
        if elapsed < seconds * 0.8:
            return {
                "status": "failed",
                "uploaded_source": uploaded,
                "pid": pid,
                "elapsed_s": elapsed,
                "exit_code": exit_code or "unknown",
                "message": f"gpu-burn 提前退出，实际运行 {elapsed}s，低于预期 {seconds}s",
                "output_tail": output[-4000:],
            }
        return {
            "status": "succeeded",
            "engine": engine or "gpu-burn",
            "uploaded_source": uploaded,
            "pid": pid,
            "elapsed_s": elapsed,
            "exit_code": exit_code or "0",
            "memory_percent": max(1, min(int(gpu_memory_percent), 95)),
            "output_tail": output[-4000:],
        }

    @staticmethod
    def _inspect_gpu_activity(ssh: SSHClient, gpu_devices: str = "") -> dict[str, Any]:
        selected_indexes = ServerCapabilityProbe.parse_gpu_devices(gpu_devices)
        command = (
            "nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu "
            "--format=csv,noheader,nounits 2>/dev/null"
        )
        output, error = ssh.exec(command, timeout=30)
        gpus = []
        for line in output.splitlines():
            parts = [item.strip() for item in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                used = int(float(parts[-3]))
                total = int(float(parts[-2]))
                utilization = int(float(parts[-1]))
                index = int(parts[0])
            except ValueError:
                continue
            memory_threshold = max(1024, int(total * 0.10))
            busy = used >= memory_threshold or utilization >= 10
            gpus.append({
                "index": index,
                "name": ",".join(parts[1:-3]).strip(),
                "memory_used_mib": used,
                "memory_total_mib": total,
                "utilization_percent": utilization,
                "busy": busy,
            })
        busy_gpus = [gpu["index"] for gpu in gpus if gpu["busy"]]
        selected_gpus = (
            [gpu for gpu in gpus if gpu["index"] in selected_indexes]
            if selected_indexes
            else gpus
        )
        missing_indexes = [
            index for index in selected_indexes if not any(gpu["index"] == index for gpu in gpus)
        ]
        selected_busy_gpus = [gpu["index"] for gpu in selected_gpus if gpu["busy"]]
        return {
            "query_succeeded": bool(gpus),
            "safe_to_stress": bool(selected_gpus) and not selected_busy_gpus and not missing_indexes,
            "selected_gpu_indexes": selected_indexes,
            "busy_gpu_indexes": selected_busy_gpus,
            "all_busy_gpu_indexes": busy_gpus,
            "missing_gpu_indexes": missing_indexes,
            "thresholds": {"memory_percent": 10, "memory_min_mib": 1024, "utilization_percent": 10},
            "gpus": gpus,
            "error": (error or "")[-1000:] if not gpus else "",
        }

    def _run_gpu_burn_container(
        self,
        ssh: SSHClient,
        duration: int,
        image: str,
        *,
        job_id: str = "",
        gpu_activity: dict[str, Any] | None = None,
        gpu_devices: str = "",
        gpu_memory_percent: int = 90,
        guard_callback=None,
    ) -> dict[str, Any]:
        image = str(image or "").strip()
        if not image:
            return {"status": "failed", "message": "未配置 gpu-burn 容器镜像"}
        quoted_image = shlex.quote(image)
        ready, _ = ssh.exec(
            f"docker image inspect {quoted_image} >/dev/null 2>&1 && echo READY || echo NOT_READY",
            timeout=30,
        )
        if ready.strip() != "READY":
            return {"status": "failed", "engine": "gpu-burn-docker", "image": image, "message": "gpu-burn 容器镜像不存在"}

        seconds = max(10, min(int(duration), 24 * 3600))
        suffix = "".join(ch for ch in job_id.lower() if ch.isalnum())[:16] or str(int(time.time()))
        container_name = f"xiaoyi-gpu-burn-{suffix}"
        remote_root = "/tmp/liema_gpu_burn"
        output_path = f"{remote_root}/gpu_burn_container_output.txt"
        exit_path = f"{remote_root}/gpu_burn_container_exit_code.txt"
        ssh.exec(f"mkdir -p {remote_root}")
        selected_indexes = ServerCapabilityProbe.parse_gpu_devices(gpu_devices)
        gpu_request = (
            shlex.quote(f'"device={",".join(map(str, selected_indexes))}"')
            if selected_indexes
            else "all"
        )
        run_cmd = (
            f"(nohup docker run --rm --gpus {gpu_request} --name {shlex.quote(container_name)} "
            f"{quoted_image} -m {max(1, min(int(gpu_memory_percent), 95))}% {seconds} > {output_path} 2>&1; "
            f"echo $? > {exit_path}) & echo $!"
        )
        out, err = ssh.exec(run_cmd, timeout=10)
        pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not pid.isdigit():
            return {
                "status": "failed",
                "engine": "gpu-burn-docker",
                "image": image,
                "message": "gpu-burn 容器启动失败",
                "output_tail": (out + "\n" + err)[-2000:],
            }

        start = time.time()
        while time.time() - start < seconds + 60:
            if job_id:
                try:
                    (guard_callback or (lambda: self._raise_if_stopped(job_id)))()
                except InterruptedError:
                    ssh.exec(f"docker stop -t 10 {shlex.quote(container_name)} >/dev/null 2>&1 || true")
                    raise
            alive, _ = ssh.exec(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD")
            if "DEAD" in alive:
                break
            time.sleep(5)
        else:
            ssh.exec(f"docker stop -t 10 {shlex.quote(container_name)} >/dev/null 2>&1 || true")

        output, _ = ssh.exec(f"tail -120 {output_path} 2>/dev/null")
        exit_code, _ = ssh.exec(f"cat {exit_path} 2>/dev/null")
        elapsed = round(time.time() - start, 1)
        exit_code = exit_code.strip()
        result = {
            "status": "succeeded",
            "engine": "gpu-burn-docker",
            "image": image,
            "container_name": container_name,
            "pid": pid,
            "elapsed_s": elapsed,
            "exit_code": exit_code or "0",
            "gpu_activity_before_start": gpu_activity or {},
            "gpu_devices": selected_indexes,
            "memory_percent": max(1, min(int(gpu_memory_percent), 95)),
            "output_tail": output[-4000:],
        }
        if exit_code and exit_code != "0":
            result.update({"status": "failed", "message": "gpu-burn 容器运行失败"})
        elif elapsed < seconds * 0.8:
            result.update({
                "status": "failed",
                "message": f"gpu-burn 容器提前退出，实际运行 {elapsed}s，低于预期 {seconds}s",
            })
        return result

    def _start_python_cpu_stress(
        self,
        ssh: SSHClient,
        *,
        workers: int,
        cpu_load: int,
        duration: int,
    ) -> dict[str, Any]:
        script_path = "/tmp/xiaoyi_cpu_stress.py"
        wrapper_path = "/tmp/xiaoyi_cpu_stress_runner.sh"
        output_path = "/tmp/xiaoyi_cpu_stress_output.txt"
        exit_path = "/tmp/xiaoyi_cpu_stress_exit_code.txt"
        quoted_script = PYTHON_CPU_STRESS.replace("'", "'\"'\"'")
        ssh.exec(f"cat > {script_path} <<'PY'\n{quoted_script}\nPY\nchmod +x {script_path}", timeout=30)
        python_bin, _ = ssh.exec("command -v python3 2>/dev/null || command -v python 2>/dev/null || true")
        python_bin = python_bin.strip().splitlines()[0] if python_bin.strip() else ""
        if not python_bin:
            return {
                "status": "failed",
                "backend": "python-fallback",
                "message": "stress-ng 不可用，且服务器未找到 python/python3，无法降级压测",
            }
        wrapper = (
            "#!/bin/sh\n"
            f"{shlex.quote(python_bin)} {shlex.quote(script_path)} "
            f"--duration {int(duration)} --workers {int(workers)} --load {int(cpu_load)} "
            f"> {shlex.quote(output_path)} 2>&1\n"
            f"echo $? > {shlex.quote(exit_path)}\n"
        )
        ssh.exec(
            f"cat > {wrapper_path} <<'SH'\n{wrapper}\nSH\nchmod +x {wrapper_path}",
            timeout=30,
        )
        cmd = (
            f"rm -f {output_path} {exit_path}; "
            f"if command -v setsid >/dev/null 2>&1; then "
            f"setsid {wrapper_path} >/dev/null 2>&1 < /dev/null & echo $!; "
            f"else nohup {wrapper_path} >/dev/null 2>&1 < /dev/null & echo $!; fi"
        )
        out, err = ssh.exec(cmd, timeout=10)
        pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not pid.isdigit():
            return {
                "status": "failed",
                "backend": "python-fallback",
                "message": "Python CPU 压测启动失败",
                "output_tail": (out + "\n" + err)[-2000:],
            }
        return {
            "status": "running",
            "backend": "python-fallback",
            "pid": pid,
            "started_at": time.time(),
            "output_path": output_path,
            "exit_path": exit_path,
            "workers": int(workers),
            "cpu_load": int(cpu_load),
        }

    def _wait_python_cpu_stress(
        self,
        ssh: SSHClient,
        handle: dict[str, Any],
        duration: int,
        *,
        job_id: str = "",
        guard_callback=None,
    ) -> dict[str, Any]:
        if handle.get("status") == "failed":
            return handle
        pid = str(handle.get("pid") or "")
        started = float(handle.get("started_at") or time.time())
        while time.time() - started < duration + 30:
            if job_id or guard_callback:
                try:
                    (guard_callback or (lambda: self._raise_if_stopped(job_id)))()
                except InterruptedError:
                    ssh.exec(f"kill -TERM {pid} 2>/dev/null || true")
                    raise
            alive, _ = ssh.exec(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD")
            if "DEAD" in alive:
                break
            time.sleep(5)
        elapsed = round(time.time() - started, 1)
        output, _ = ssh.exec(f"tail -80 {handle['output_path']} 2>/dev/null")
        exit_code, _ = ssh.exec(f"cat {handle['exit_path']} 2>/dev/null")
        exit_code = exit_code.strip()
        result = {
            "status": "succeeded",
            "backend": "python-fallback",
            "elapsed_s": elapsed,
            "workers": handle.get("workers", 0),
            "cpu_load": handle.get("cpu_load", 0),
            "exit_code": exit_code or "0",
            "message": "stress-ng 不可用，已使用 Python 降级方案完成 CPU 压测",
            "output_tail": output[-2000:],
        }
        if exit_code and exit_code != "0":
            result.update({"status": "failed", "message": "Python CPU 压测运行失败"})
        elif elapsed < duration * 0.8:
            result.update({
                "status": "failed",
                "message": f"Python CPU 压测提前退出，实际运行 {elapsed}s，低于预期 {duration}s",
            })
        return result

    def _diagnose_gpu_build_failure(self, ssh: SSHClient, build_log: str) -> dict[str, Any]:
        commands = {
            "nvcc": "command -v nvcc 2>/dev/null || true",
            "nvidia_smi": "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -5 || true",
            "gcc": "gcc --version 2>/dev/null | head -1 || true",
            "gxx": "g++ --version 2>/dev/null | head -1 || true",
            "make": "command -v make 2>/dev/null || true",
            "unzip": "command -v unzip 2>/dev/null || true",
            "glibcxx_29": "strings /usr/lib64/libstdc++.so.6 2>/dev/null | grep GLIBCXX_3.4.29 || true",
            "glibcxx_30": "strings /usr/lib64/libstdc++.so.6 2>/dev/null | grep GLIBCXX_3.4.30 || true",
            "cuda_dirs": "ls -d /usr/local/cuda* 2>/dev/null || true",
        }
        details = {}
        for key, command in commands.items():
            out, err = ssh.exec(command)
            details[key] = (out or err or "").strip()[:1000]
        recommendations = []
        log_text = build_log.lower()
        if "glibcxx_3.4.29" in log_text or "glibcxx_3.4.30" in log_text:
            recommendations.append("当前 g++/cc1plus 依赖的 libstdc++ 版本高于系统 /usr/lib64/libstdc++.so.6，需安装匹配的 libstdc++ 或切换到系统自带 gcc/g++。")
        if not details.get("nvcc"):
            recommendations.append("未找到 nvcc，需安装 CUDA devel 工具包，或使用已编译好的 gpu_burn 二进制文件。")
        if not details.get("make"):
            recommendations.append("未找到 make，需安装 make。")
        if not details.get("unzip"):
            recommendations.append("未找到 unzip，源码包解压可能失败，需安装 unzip 或 python3。")
        if not recommendations:
            recommendations.append("请检查 CUDA、gcc/g++、make、libstdc++ 与 NVIDIA 驱动版本是否匹配。")
        return {
            "details": details,
            "recommendations": recommendations,
        }

    def _write_report(
        self,
        job_id: str,
        options: dict[str, Any],
        modes: set[str],
        duration: int,
        perf_summary: dict[str, Any],
        cpu_summary: dict[str, Any],
        gpu_result: dict[str, Any],
        capability_report: dict[str, Any],
        artifact_dir: Path,
        safety_event: str = "",
    ) -> Path:
        report_dir = artifact_dir / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job_id,
            "modes": sorted(modes),
            "duration_s": duration,
            "options": {key: value for key, value in options.items() if key != "password"},
            "perf_summary": perf_summary,
            "cpu_summary": cpu_summary,
            "gpu_result": gpu_result,
            "capability_report": capability_report,
            "safety_event": safety_event,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        samples = self.store.get_stress_samples(job_id, 0, 20000) if self.store else []
        artifacts = build_server_performance_artifacts(payload, samples, report_dir)
        payload["metric_summary"] = artifacts["summary"]
        payload["conclusion"] = artifacts["conclusion"]
        (report_dir / "server_stress_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "烈马自动化测试平台 - 服务器压测报告",
            "=" * 60,
            f"任务编号：{job_id}",
            f"压测模式：{', '.join(sorted(modes))}",
            f"压测时长：{duration}s",
            f"目标服务器：{options.get('host') or '未指定'}",
            "",
            "性能摘要",
            "-" * 60,
            json.dumps(perf_summary, ensure_ascii=False, indent=2),
            "",
            "服务器兼容性预检",
            "-" * 60,
            json.dumps(capability_report, ensure_ascii=False, indent=2),
            "",
            "CPU 压测摘要",
            "-" * 60,
            json.dumps(cpu_summary, ensure_ascii=False, indent=2),
            "",
            "GPU 压测摘要",
            "-" * 60,
            json.dumps(gpu_result, ensure_ascii=False, indent=2),
            "",
        ]
        (report_dir / "server_stress_diagnostics.txt").write_text("\n".join(lines), encoding="utf-8")
        return artifacts["docx_path"]
