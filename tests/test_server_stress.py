import os
import unittest
from unittest.mock import patch
from tests import bootstrap  # noqa: F401

from auto_test.monitoring.server_stress import ServerStressManager


class FakeSSH:
    is_local = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def exec(self, command, timeout=30):
        self.commands.append(command)
        if not self.responses:
            return "", ""
        return self.responses.pop(0)


class ServerStressManagerTests(unittest.TestCase):
    def setUp(self):
        self.artifact_backend = patch.dict(
            os.environ, {"LIEMA_ARTIFACT_BACKEND": "local"}
        )
        self.artifact_backend.start()

    def tearDown(self):
        self.artifact_backend.stop()

    def test_target_is_only_resolved_from_request(self):
        manager = ServerStressManager(None)

        empty = manager._resolve_target({"target": "gpu"})
        explicit = manager._resolve_target({
            "host": "server.example.internal",
            "port": 2202,
            "user": "tester",
            "password": "secret",
        })

        self.assertEqual(empty, {"host": "", "port": 22, "user": "", "password": ""})
        self.assertEqual(explicit["host"], "server.example.internal")
        self.assertEqual(explicit["port"], 2202)
        self.assertEqual(explicit["user"], "tester")

    def test_gpu_burn_not_ready_is_not_treated_as_ready(self):
        ssh = FakeSSH([
            ("0, NVIDIA RTX 3090, 0, 24576, 0\n", ""),
            ("", ""),
            ("", ""),
            ("NEED_BUILD\n", ""),
            ("", ""),
            ("NOT_READY\n", ""),
            ("nvcc not found", ""),
        ])
        manager = ServerStressManager(None)

        result = manager._run_gpu_burn(ssh, 60, "")

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("nohup ./gpu_burn", "\n".join(ssh.commands))

    def test_gpu_burn_non_zero_exit_is_failed(self):
        ssh = FakeSSH([
            ("0, NVIDIA RTX 3090, 0, 24576, 0\n", ""),
            ("", ""),
            ("", ""),
            ("READY\n", ""),
            ("READY\n", ""),
            ("12345\n", ""),
            ("DEAD\n", ""),
            ("nohup: cannot run command './gpu_burn'", ""),
            ("127\n", ""),
        ])
        manager = ServerStressManager(None)

        result = manager._run_gpu_burn(ssh, 60, "")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], "127")

    def test_busy_gpu_is_blocked_before_start(self):
        ssh = FakeSSH([("0, NVIDIA RTX 3090, 22000, 24576, 95\n", "")])
        manager = ServerStressManager(None)

        result = manager._run_gpu_burn(
            ssh,
            60,
            "",
            engine="gpu-burn-docker",
            container_image="xiaoyi/gpu-burn:cuda11.8-sm86",
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["safety_blocked"])
        self.assertEqual(result["gpu_activity"]["busy_gpu_indexes"], [0])
        self.assertNotIn("docker run", "\n".join(ssh.commands))

    def test_container_gpu_burn_uses_prebuilt_image(self):
        ssh = FakeSSH([
            ("0, NVIDIA RTX 3090, 0, 24576, 0\n", ""),
            ("READY\n", ""),
            ("", ""),
            ("12345\n", ""),
            ("DEAD\n", ""),
            ("GPU 0: NVIDIA RTX 3090\n", ""),
            ("0\n", ""),
        ])
        manager = ServerStressManager(None)

        result = manager._run_gpu_burn(
            ssh,
            10,
            "",
            engine="gpu-burn-docker",
            container_image="xiaoyi/gpu-burn:cuda11.8-sm86",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["engine"], "gpu-burn-docker")
        self.assertTrue(any("docker run --rm --gpus all" in command for command in ssh.commands))

    def test_container_gpu_burn_can_target_one_idle_gpu(self):
        ssh = FakeSSH([
            (
                "0, NVIDIA RTX 3090, 0, 24576, 0\n"
                "1, NVIDIA RTX 3090, 22000, 24576, 95\n",
                "",
            ),
            ("READY\n", ""),
            ("", ""),
            ("12345\n", ""),
            ("DEAD\n", ""),
            ("GPU 0: NVIDIA RTX 3090\n", ""),
            ("0\n", ""),
        ])
        manager = ServerStressManager(None)

        result = manager._run_gpu_burn(
            ssh,
            10,
            "",
            engine="gpu-burn-docker",
            container_image="xiaoyi/gpu-burn:cuda11.8-sm86",
            gpu_devices="0",
        )

        self.assertFalse(result.get("safety_blocked", False))
        launch = next(command for command in ssh.commands if "docker run" in command)
        self.assertIn("device=0", launch)
        self.assertNotIn("--gpus all", launch)

    def test_container_gpu_burn_passes_configured_memory_percent(self):
        ssh = FakeSSH([
            ("0, NVIDIA RTX 4090, 0, 24576, 0\n", ""),
            ("READY\n", ""),
            ("", ""),
            ("12345\n", ""),
            ("DEAD\n", ""),
            ("GPU 0: NVIDIA RTX 4090\n", ""),
            ("0\n", ""),
        ])
        manager = ServerStressManager(None)

        manager._run_gpu_burn(
            ssh,
            10,
            "",
            engine="gpu-burn-docker",
            container_image="xiaoyi/gpu-burn:cuda12.8-universal",
            gpu_memory_percent=50,
        )

        launch = next(command for command in ssh.commands if "docker run" in command)
        self.assertIn("-m 50% 10", launch)

    def test_safety_guard_stops_on_temperature_and_memory_thresholds(self):
        manager = ServerStressManager(None)
        manager._latest_samples["job-1"] = {
            "APP": {
                "cpu_temp_c": 91,
                "gpu0_temp_c": 70,
                "mem_used_gb": 97,
                "mem_total_gb": 100,
                "disk_util_pct": 30,
            }
        }

        with self.assertRaisesRegex(InterruptedError, "CPU 温度"):
            manager._raise_if_unsafe("job-1", {
                "safety_enabled": True,
                "cpu_temp_limit": 90,
                "gpu_temp_limit": 85,
                "memory_usage_limit": 95,
                "disk_usage_limit": 95,
            })

    def test_python_cpu_fallback_detaches_from_ssh_channel(self):
        ssh = FakeSSH([
            ("", ""),
            ("/usr/bin/python3\n", ""),
            ("", ""),
            ("34567\n", ""),
        ])
        manager = ServerStressManager(None)

        result = manager._start_python_cpu_stress(ssh, workers=2, cpu_load=70, duration=60)

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["pid"], "34567")
        launch_command = ssh.commands[-1]
        self.assertIn("setsid", launch_command)
        self.assertIn("nohup", launch_command)
        self.assertIn("< /dev/null", launch_command)
        self.assertIn(">/dev/null 2>&1", launch_command)


if __name__ == "__main__":
    unittest.main()
