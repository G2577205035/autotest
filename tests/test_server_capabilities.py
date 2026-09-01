import unittest

from tests import bootstrap  # noqa: F401

from auto_test.monitoring.server_capabilities import ServerCapabilityProbe


class FakeSSH:
    def __init__(self, output):
        self.output = output
        self.commands = []

    def exec(self, command, timeout=30):
        self.commands.append(command)
        return self.output, ""


def probe_output(**overrides):
    values = {
        "probe_version": "5",
        "hostname": "gpu-01",
        "os_id": "ubuntu",
        "os_name": "Ubuntu",
        "os_version": "24.04",
        "os_pretty_name": "Ubuntu 24.04 LTS",
        "kernel": "6.8.0",
        "architecture": "x86_64",
        "user": "tester",
        "uid": "1000",
        "cpu_logical": "32",
        "memory_kb": "65536000",
        "tmp_available_kb": "2097152",
        "tmp_writable": "yes",
        "cgroup_version": "v2",
        "glibc_version": "glibc 2.39",
        "open_files_limit": "1024",
        "package_manager": "apt-get",
        "container_runtime": "docker",
        "container_version": "Docker version 27",
        "container_runtimes": '{"runc":{},"nvidia":{}}',
        "nvidia_container_runtime": "yes",
        "gpu_burn_container_image": "xiaoyi/gpu-burn:cuda11.8-universal",
        "gpu_burn_container_image_ready": "no",
        "gpu_burn_blackwell_image": "xiaoyi/gpu-burn:cuda12.8-blackwell",
        "gpu_burn_blackwell_image_ready": "no",
        "tool_python3": "/usr/bin/python3",
        "tool_stress_ng": "",
        "tool_top": "/usr/bin/top",
        "tool_free": "/usr/bin/free",
        "tool_iostat": "/usr/bin/iostat",
        "tool_sensors": "/usr/bin/sensors",
        "tool_nvidia_smi": "/usr/bin/nvidia-smi",
        "tool_nvcc": "",
        "tool_make": "/usr/bin/make",
        "tool_gcc": "/usr/bin/gcc",
        "tool_g++": "/usr/bin/g++",
        "tool_unzip": "/usr/bin/unzip",
        "tool_tar": "/usr/bin/tar",
        "tool_nohup": "/usr/bin/nohup",
        "tool_setsid": "/usr/bin/setsid",
        "tool_dcgmi": "",
        "tool_dcgm_exporter": "",
        "tool_gpu_burn": "/usr/local/bin/gpu_burn",
        "tool_all_reduce_perf": "",
        "python_version": "Python 3.12.3",
        "stress_ng_version": "",
        "sensors_readable": "yes",
        "dmesg_readable": "no",
        "gpu_burn_path": "/usr/local/bin/gpu_burn",
        "gpu_burn_source_ready": "no",
        "nccl_tests_path": "",
        "nvidia_smi_healthy": "yes",
        "gpu_count": "4",
        "gpu_compute_capabilities": "0:8.9,1:8.9,2:8.9,3:8.9",
        "gpu_devices": "0|NVIDIA L40S|46068|0|0|31|8.9;1|NVIDIA L40S|46068|0|0|32|8.9;2|NVIDIA L40S|46068|0|0|33|8.9;3|NVIDIA L40S|46068|0|0|34|8.9",
        "gpu_busy_count": "0",
        "gpu_busy_indexes": "",
        "gpu_name": "NVIDIA L40S",
        "driver_version": "550.54.15",
        "cuda_runtime_version": "12.4",
        "nvcc_version": "",
        "dcgm_version": "",
    }
    values.update(overrides)
    return "\n".join(f"{key}\t{value}" for key, value in values.items())


class ServerCapabilityProbeTests(unittest.TestCase):
    def test_dcgm_is_optional_and_does_not_block_gpu_stress(self):
        ssh = FakeSSH(probe_output())

        report = ServerCapabilityProbe().collect(ssh, ["monitor", "gpu"])

        self.assertEqual(report["overall"], "ready")
        self.assertEqual(report["capabilities"]["gpu"]["status"], "ready")
        self.assertFalse(report["gpu"]["dcgm"]["required_for_stress"])
        self.assertEqual(report["gpu"]["dcgm"]["role"], "optional_diagnostics")

    def test_python_is_an_explicit_cpu_fallback(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(probe_output(stress_ng_version="sh: stress-ng: command not found")),
            ["cpu"],
        )

        self.assertEqual(report["overall"], "degraded")
        self.assertEqual(report["cpu"]["stress_engine"], "python")
        self.assertTrue(report["capabilities"]["cpu"]["runnable"])
        self.assertEqual(report["versions"]["stress_ng"], "")

    def test_gpu_stress_is_blocked_without_a_healthy_nvidia_stack(self):
        output = probe_output(
            tool_nvidia_smi="",
            tool_gpu_burn="",
            gpu_burn_path="",
            nvidia_smi_healthy="no",
            gpu_count="0",
        )

        report = ServerCapabilityProbe().collect(FakeSSH(output), ["gpu"])

        self.assertEqual(report["overall"], "blocked")
        self.assertFalse(report["capabilities"]["gpu"]["runnable"])
        self.assertFalse(report["policy"]["automatic_driver_install"])

    def test_probe_command_contains_no_package_install_or_driver_change(self):
        ssh = FakeSSH(probe_output())

        ServerCapabilityProbe().collect(ssh, ["monitor"])

        command = ssh.commands[0].lower()
        self.assertNotIn("apt-get install", command)
        self.assertNotIn("dnf install", command)
        self.assertNotIn("yum install", command)
        self.assertNotIn("modprobe", command)

    def test_cpu_stress_is_blocked_when_tmp_is_not_writable(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(probe_output(tmp_writable="no")), ["cpu"]
        )

        self.assertEqual(report["overall"], "blocked")
        self.assertIn("/tmp", " ".join(report["capabilities"]["cpu"]["prerequisites"]))

    def test_gpu_source_build_requires_cpp_compiler_and_workspace(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    tool_nvcc="/usr/local/cuda/bin/nvcc",
                    gpu_burn_source_ready="yes",
                    **{"tool_g++": ""},
                )
            ),
            ["gpu"],
        )

        self.assertEqual(report["overall"], "blocked")
        self.assertIn("g++", " ".join(report["capabilities"]["gpu"]["prerequisites"]))

    def test_gpu_container_is_ready_without_host_cuda_toolkit(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    tool_nvcc="",
                    gpu_burn_container_image_ready="yes",
                )
            ),
            ["gpu"],
        )

        self.assertEqual(report["overall"], "ready")
        self.assertEqual(report["gpu"]["stress_engine"], "gpu-burn-docker")
        self.assertTrue(report["host"]["nvidia_container_runtime"])
        self.assertTrue(report["gpu"]["container_image_ready"])
        self.assertEqual(len(report["gpu"]["devices"]), 4)
        self.assertTrue(all(device["selectable"] for device in report["gpu"]["devices"]))

    def test_gpu_container_requires_nvidia_runtime(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    tool_nvcc="",
                    nvidia_container_runtime="no",
                    gpu_burn_container_image_ready="yes",
                    gpu_burn_source_ready="no",
                )
            ),
            ["gpu"],
        )

        self.assertEqual(report["overall"], "blocked")
        self.assertIn("NVIDIA Container Toolkit", " ".join(report["capabilities"]["gpu"]["prerequisites"]))

    def test_busy_gpu_blocks_a_ready_container_engine(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    gpu_burn_container_image_ready="yes",
                    gpu_busy_count="2",
                    gpu_busy_indexes="1,2",
                )
            ),
            ["gpu"],
        )

        self.assertEqual(report["overall"], "blocked")
        self.assertEqual(report["gpu"]["stress_engine"], "gpu-burn-docker")
        self.assertEqual(report["gpu"]["busy_count"], 2)
        self.assertIn("正在承载业务", report["capabilities"]["gpu"]["message"])
        self.assertEqual(report["gpu"]["test_strategy"]["recommended_preset"], "quick")
        self.assertEqual(report["gpu"]["test_strategy"]["full_stress_count"], 2)

    def test_all_busy_gpus_recommend_online_health_check(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    gpu_burn_container_image_ready="yes",
                    gpu_busy_count="4",
                    gpu_busy_indexes="0,1,2,3",
                    gpu_devices=(
                        "0|NVIDIA L40S|46068|42000|95|72|8.9;"
                        "1|NVIDIA L40S|46068|41000|90|71|8.9;"
                        "2|NVIDIA L40S|46068|40000|88|70|8.9;"
                        "3|NVIDIA L40S|46068|39000|85|69|8.9"
                    ),
                )
            ),
            ["monitor", "gpu"],
        )

        strategy = report["gpu"]["test_strategy"]
        self.assertEqual(strategy["recommended_preset"], "health")
        self.assertEqual(strategy["detected_count"], 4)
        self.assertEqual(strategy["full_stress_count"], 0)
        self.assertTrue(strategy["maintenance_required"])
        self.assertTrue(all(device["safe_test_modes"] == ["health"] for device in report["gpu"]["devices"]))

    def test_selected_idle_gpu_is_not_blocked_by_other_busy_gpus(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    gpu_burn_container_image_ready="yes",
                    gpu_busy_count="2",
                    gpu_busy_indexes="1,2",
                )
            ),
            ["gpu"],
            gpu_devices="0",
        )

        self.assertEqual(report["overall"], "ready")
        self.assertEqual(report["gpu"]["selected_indexes"], [0])
        self.assertEqual(report["gpu"]["busy_indexes"], [])

    def test_blackwell_prefers_native_cuda_12_8_image(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    gpu_count="1",
                    gpu_compute_capabilities="0:12.0",
                    gpu_burn_container_image_ready="yes",
                    gpu_burn_blackwell_image_ready="yes",
                )
            ),
            ["gpu"],
        )

        self.assertEqual(report["overall"], "ready")
        self.assertEqual(report["gpu"]["container_compatibility_mode"], "native-blackwell")
        self.assertEqual(report["gpu"]["container_image"], "xiaoyi/gpu-burn:cuda12.8-blackwell")

    def test_blackwell_can_fall_back_to_forward_compatible_ptx(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    tool_gpu_burn="",
                    gpu_burn_path="",
                    gpu_count="1",
                    gpu_compute_capabilities="0:12.0",
                    gpu_burn_container_image_ready="yes",
                    gpu_burn_blackwell_image_ready="no",
                )
            ),
            ["gpu"],
        )

        self.assertEqual(report["overall"], "degraded")
        self.assertEqual(report["gpu"]["container_compatibility_mode"], "ptx-fallback")
        self.assertEqual(report["gpu"]["container_image"], "xiaoyi/gpu-burn:cuda11.8-universal")

    def test_runtime_details_are_returned_for_page_diagnosis(self):
        report = ServerCapabilityProbe().collect(FakeSSH(probe_output()), ["monitor"])

        self.assertTrue(report["runtime"]["architecture_supported"])
        self.assertTrue(report["runtime"]["temporary_directory_writable"])
        self.assertEqual(report["host"]["cgroup_version"], "v2")

    def test_non_lts_ubuntu_release_is_marked_for_verification(self):
        report = ServerCapabilityProbe().collect(
            FakeSSH(
                probe_output(
                    os_version="25.10",
                    os_pretty_name="Ubuntu 25.10",
                )
            ),
            ["monitor"],
        )

        self.assertEqual(report["support"]["tier"], "compatible")
        self.assertFalse(report["support"]["release_verified"])
        self.assertTrue(any("尚未纳入" in item for item in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
