"""Read-only resource sampling on the explicitly selected evaluation server."""

from __future__ import annotations

import csv
import json
import math
import threading
import time
from pathlib import Path
from statistics import mean

from auto_test.evaluation.advanced import correlate_resources
from auto_test.integrations.ssh import SSHClient
from auto_test.monitoring.docker_monitor import DockerMonitor
from auto_test.platform.secrets import decrypt_secret


def capture_execution_environment():
    """Capture only reproducibility metadata, never environment variables."""
    import platform
    from datetime import datetime, timezone
    from importlib.metadata import PackageNotFoundError, version
    packages = {}
    for name in ("auto-test", "evalscope", "requests", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "未安装 / 源码运行"
    return {"collected_at": datetime.now(timezone.utc).isoformat(), "os": platform.platform(), "python": platform.python_version(), "packages": packages}


def align_stage_resources(samples: list[dict], stages: list[dict], execution: list[dict]) -> list[dict]:
    aligned = []
    for window in execution:
        stage = next((item for item in stages if item.get("evaluation_stage") == window.get("evaluation_stage") and item.get("phase", "capacity") == window["phase"] and item.get("concurrency") == window["parallel"] and (item.get("target_rps") == window["rate"] or item.get("target_rps") is None and window["rate"] == -1)), None)
        if not stage:
            continue
        # Avoid samples collected across the preceding stage boundary.
        rows = [row for row in samples if window["started_at"] + 5 <= row["sampled_at"] <= window["finished_at"]]
        if not rows:
            continue
        item = {"phase": window["phase"], "started_at": window["started_at"], "finished_at": window["finished_at"], "request_throughput": stage.get("request_throughput"), "sample_count": len(rows)}
        if window.get("evaluation_stage"):
            item["evaluation_stage"] = window["evaluation_stage"]
        for field in ("cpu_percent", "gpu_percent", "memory_percent", "gpu_memory_percent"):
            values = [row[field] for row in rows if row.get(field) is not None]
            item[field] = mean(values) if values else None
        aligned.append(item)
    return aligned


class EvaluationResourceSampler:
    def __init__(self, store, binding: dict, work_dir: Path):
        self.store, self.binding, self.work_dir = store, binding, work_dir
        self.samples = []
        self.lock = threading.Lock()
        self.monitor = None
        self.ssh = None
        self.reason = "未绑定可供 Worker 使用的已保存服务器资产"
        self.environment = {}

    def start(self):
        profile_id = self.binding.get("server_profile_id")
        if not profile_id:
            return
        try:
            profile = self.store.get_server_profile(profile_id, include_secret=True)
            if not profile or profile.get("host") != self.binding.get("server_host"):
                self.reason = "服务器资产已改变，未使用与提交快照不一致的目标"
                return
            self.ssh = SSHClient(profile["host"], profile["user"], decrypt_secret(profile["credential_enc"]) if profile.get("credential_enc") else "", profile.get("port") or 22)
            if not self.ssh.connect():
                self.reason = "模型服务器只读监控连接失败"
                return
            # Capability discovery is read-only; failure must not prevent metrics.
            try:
                from auto_test.monitoring.server_capabilities import ServerCapabilityProbe
                from datetime import datetime, timezone
                capability = ServerCapabilityProbe().collect(self.ssh, ["monitor"])
                host, gpu, versions = capability["host"], capability["gpu"], capability["versions"]
                self.environment = {"source": "测试开始前只读采集", "collected_at": datetime.now(timezone.utc).isoformat(), "os": host.get("os_pretty_name") or host.get("os_name"), "cpu": f"{host.get('architecture')} / {host.get('cpu_logical')} 逻辑处理器", "memory": f"{host.get('memory_kb', 0) / 1024 / 1024:.2f} GiB", "gpu": f"{gpu.get('name')} / {gpu.get('count')} 张", "gpu_memory": "; ".join(f"GPU {d.get('index')}: {d.get('memory_total_mib', '未采集')} MiB" for d in gpu.get("devices", [])) or "未采集", "driver": versions.get("nvidia_driver") or "未采集", "cuda": versions.get("cuda_runtime") or "未采集"}
            except Exception:
                self.environment = {}
            self.monitor = DockerMonitor(self.ssh, self.ssh, "", enable_log=False, enable_perf=True, run_dir=str(self.work_dir), perf_prefix="evaluation_", sample_callback=self._sample)
            self.monitor.start()
            self.reason = ""
        except Exception as exc:
            self.reason = f"资源采集不可用：{type(exc).__name__}"

    def _sample(self, label, sample):
        if label != "APP":
            return
        def numeric(value):
            try:
                result = float(value)
                return result if math.isfinite(result) else None
            except (TypeError, ValueError):
                return None
        total, used = numeric(sample.get("mem_total_gb")), numeric(sample.get("mem_used_gb"))
        row = {"sampled_at": time.time(), "cpu_percent": numeric(sample.get("cpu_pct")), "gpu_percent": numeric(sample.get("gpu_pct")), "memory_percent": used / total * 100 if total and used is not None else None, "gpu_memory_percent": numeric(sample.get("gpu_mem_pct"))}
        with self.lock:
            self.samples.append(row)

    def finish(self, summary: dict) -> dict:
        self.close()
        with self.lock:
            samples = list(self.samples)
        (self.work_dir / "resource_observations.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
        execution = list((summary.get("performance_execution") or {}).get("stages") or [])
        stages = list((summary.get("performance") or {}).get("stages") or [])
        aligned = align_stage_resources(samples, stages, execution)
        with (self.work_dir / "resource_samples.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["evaluation_stage", "phase", "started_at", "finished_at", "request_throughput", "sample_count", "cpu_percent", "gpu_percent", "memory_percent", "gpu_memory_percent"])
            writer.writeheader()
            writer.writerows(aligned)
        result = correlate_resources(aligned)
        result.update({"server_name": self.binding.get("server_name", ""), "observation_count": len(samples), "scope": "selected_host_all_devices_stage_averages", "unavailable_reason": self.reason, "aligned_stages": aligned, "environment": self.environment})
        result["statistics"] = {}
        for field in ("cpu_percent", "gpu_percent", "memory_percent", "gpu_memory_percent"):
            values = [s[field] for s in samples if s.get(field) is not None and math.isfinite(s[field])]
            result["statistics"][field] = {"count": len(values), "mean": mean(values) if values else None, "min": min(values) if values else None, "max": max(values) if values else None}
        return result

    def close(self):
        if self.monitor:
            self.monitor.stop_perf()
            self.monitor.stop_logs()
            self.monitor = None
        if self.ssh:
            self.ssh.disconnect()
            self.ssh = None
