import csv
import tempfile
import unittest
from pathlib import Path
from tests import bootstrap  # noqa: F401
from unittest.mock import patch

from auto_test.monitoring.docker_monitor import DockerMonitor
from auto_test.monitoring.cpu_temperature import SENSORS_MARKER


class FakeSSH:
    def exec(self, command):
        if command.startswith("top "):
            return "%Cpu(s):  5.0 us,  2.0 sy, 93.0 id", ""
        if command.startswith("free "):
            return "Mem: 102400 51200 0 0 0 51200", ""
        if "nvidia-smi --query-gpu" in command:
            return (
                "0, NVIDIA RTX 4090, 40, 8192, 24576, 55, 120.5, 450, 2100, 35, P2\n"
                "1, NVIDIA L40S, 60, 4096, 46068, 65, N/A, 350, 1800, N/A, P0",
                "",
            )
        if command == "nvidia-smi":
            return "NVIDIA-SMI", ""
        return "", ""

    def connect(self):
        return True


class DockerMonitorTargetTests(unittest.TestCase):
    def test_missing_independent_gpu_connection_reuses_app_connection(self):
        app_ssh = FakeSSH()

        monitor = DockerMonitor(app_ssh, None, [], enable_perf=True)

        self.assertIs(monitor.gpu_ssh, app_ssh)
        self.assertFalse(monitor._gpu_separate)

    def test_same_server_collection_includes_nvidia_smi_metrics(self):
        app_ssh = FakeSSH()
        monitor = DockerMonitor(app_ssh, None, [], enable_perf=True)
        samples = []
        monitor._running = True

        def stop_after_first_sample(_seconds):
            monitor._running = False

        with patch("auto_test.monitoring.docker_monitor.time.sleep", side_effect=stop_after_first_sample):
            monitor._collect_perf(app_ssh, samples, include_gpu=True, label="APP")

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["gpu_count"], 2)
        self.assertEqual(samples[0]["gpu_pct"], 50.0)
        self.assertEqual(samples[0]["gpu0_pct"], 40)
        self.assertEqual(samples[0]["gpu1_temp_c"], 65)
        self.assertEqual(samples[0]["gpu_mem_mb"], 12288)
        self.assertEqual(samples[0]["gpu0_mem_total_mb"], 24576)
        self.assertEqual(samples[0]["gpu0_mem_pct"], 33.3)
        self.assertEqual(samples[0]["gpu0_power_w"], 120.5)
        self.assertEqual(samples[0]["gpu0_sm_clock_mhz"], 2100)
        self.assertEqual(samples[0]["gpu0_pstate"], "P2")
        self.assertIsNone(samples[0]["gpu1_fan_pct"])
        self.assertEqual(samples[0]["gpu_sampling_status"], "partial")

    def test_collection_persists_each_cpu_package_reading(self):
        class TemperatureSSH(FakeSSH):
            def exec(self, command):
                if SENSORS_MARKER in command:
                    return f"""{SENSORS_MARKER}
coretemp-isa-0000
Adapter: ISA adapter
Package id 0: +36.0°C (high = +88.0°C, crit = +98.0°C)
coretemp-isa-0001
Adapter: ISA adapter
Package id 1: +33.0°C (high = +88.0°C, crit = +98.0°C)
""", ""
                return super().exec(command)

        ssh = TemperatureSSH()
        monitor = DockerMonitor(ssh, None, [], enable_perf=True)
        samples = []
        monitor._running = True

        def stop_after_first_sample(_seconds):
            monitor._running = False

        with patch("auto_test.monitoring.docker_monitor.time.sleep", side_effect=stop_after_first_sample):
            monitor._collect_perf(ssh, samples, include_gpu=False, label="APP")

        self.assertEqual(samples[0]["cpu_temp_c"], 36.0)
        self.assertEqual([reading["label"] for reading in samples[0]["cpu_temp_readings"]], ["Package id 0", "Package id 1"])

    def test_extended_query_failure_falls_back_to_base_metrics(self):
        class FallbackSSH(FakeSSH):
            def exec(self, command):
                if "power.draw" in command:
                    return "", "Field power.draw is not a valid field to query"
                if "nvidia-smi --query-gpu" in command:
                    return "0, NVIDIA RTX 3090, 50, 1024, 24576, 61", ""
                return super().exec(command)

        ssh = FallbackSSH()
        monitor = DockerMonitor(ssh, None, [], enable_perf=True)
        samples = []
        monitor._running = True

        def stop_after_first_sample(_seconds):
            monitor._running = False

        with patch("auto_test.monitoring.docker_monitor.time.sleep", side_effect=stop_after_first_sample):
            monitor._collect_perf(ssh, samples, include_gpu=True, label="APP")

        self.assertEqual(samples[0]["gpu0_pct"], 50)
        self.assertEqual(samples[0]["gpu0_mem_total_mb"], 24576)
        self.assertIsNone(samples[0]["gpu0_power_w"])
        self.assertEqual(samples[0]["gpu_sampling_status"], "partial")
        self.assertIn("回退", samples[0]["gpu_sampling_message"])

    def test_unavailable_gpu_state_is_persisted_without_gpu_indexes(self):
        samples = [{
            "time": "10:00:00",
            "cpu_pct": 8,
            "mem_used_gb": 16,
            "mem_total_gb": 64,
            "cpu_temp_c": "",
            "disk_util_pct": 2,
            "gpu_sampling_status": "unavailable",
            "gpu_sampling_message": "nvidia-smi 未返回可解析的 GPU 指标",
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor = DockerMonitor(FakeSSH(), None, [], enable_perf=True, run_dir=temp_dir)
            monitor._write_perf_csv(samples, "perf.csv", has_gpu=True)
            monitor._write_perf_summary(samples, "summary.csv", has_gpu=True)

            with (Path(temp_dir) / "report" / "perf.csv").open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertIn("gpu_sampling_status", rows[0])
            self.assertEqual(rows[1][rows[0].index("gpu_sampling_status")], "unavailable")
            summary_text = (Path(temp_dir) / "report" / "summary.csv").read_text(encoding="utf-8")
            self.assertIn("GPU采集状态,unavailable", summary_text)
            self.assertIn("GPU采集说明,nvidia-smi 未返回可解析的 GPU 指标", summary_text)


if __name__ == "__main__":
    unittest.main()
