import unittest
from tests import bootstrap  # noqa: F401
from unittest.mock import patch

from auto_test.pipeline import runner


class RuntimeServerResolutionTests(unittest.TestCase):
    def test_custom_account_uses_platform_default_translation_language(self):
        previous = runner.config.TRANSLATE_NAME
        try:
            with patch("auto_test.pipeline.runner.default_translate_name", return_value="es:zh-CHS"):
                runner._apply_user_config({"translate_name": ""}, {"translate_name": ""})
            self.assertEqual(runner.config.TRANSLATE_NAME, "es:zh-CHS")
        finally:
            runner.config.TRANSLATE_NAME = previous

    def test_gpu_follows_runtime_app_server_when_no_independent_host_submitted(self):
        app_srv, gpu_srv = runner._runtime_servers(
            "172.16.102.73", "deploy", "one-time-secret"
        )

        self.assertEqual(app_srv["host"], "172.16.102.73")
        self.assertEqual(gpu_srv["host"], "172.16.102.73")
        self.assertEqual(app_srv["user"], "deploy")
        self.assertEqual(gpu_srv["user"], "deploy")
        self.assertEqual(app_srv["password"], "one-time-secret")
        self.assertEqual(gpu_srv["password"], "one-time-secret")

    def test_same_gpu_host_reuses_runtime_app_credentials(self):
        app_srv, gpu_srv = runner._runtime_servers(
            "172.16.102.73",
            "deploy",
            "app-secret",
            "172.16.102.73",
        )

        self.assertEqual(gpu_srv, app_srv)

    def test_runtime_uses_page_gpu_credentials_for_explicit_independent_host(self):
        app_srv, gpu_srv = runner._runtime_servers(
            "172.16.102.73",
            "deploy",
            "app-secret",
            "172.16.102.59",
            "gpu-user",
            "gpu-secret",
        )

        self.assertEqual(app_srv["host"], "172.16.102.73")
        self.assertEqual(gpu_srv["host"], "172.16.102.59")
        self.assertEqual(gpu_srv["user"], "gpu-user")
        self.assertEqual(gpu_srv["password"], "gpu-secret")

    def test_runtime_monitor_upload_export_ai_and_stress_options_override_yaml(self):
        options = {
            "monitor_modules": ["systemadmin", "translateadmin"],
            "monitor_enable_perf": False,
            "monitor_enable_log": True,
            "upload_mode": "server",
            "upload_size_limit_mb": 2048,
            "export_enable": True,
            "export_types": ["tran"],
            "ai_checks_enable": False,
            "ai_sample_path": r"E:\sample.txt",
            "ai_max_file_ids": 3,
            "stress_enable": True,
            "stress_workers": 4,
            "stress_cpu_load": 70,
            "stress_duration": 90,
        }

        with (
            patch("auto_test.pipeline.runner.monitor_cfg", return_value={"containers": ["old"], "enable_perf": True, "enable_log_monitor": False}),
            patch("auto_test.pipeline.runner.upload_cfg", return_value={"mode": "auto", "size_limit_mb": 100}),
            patch("auto_test.pipeline.runner.export_cfg", return_value={"enable": False, "types": "original"}),
            patch("auto_test.pipeline.runner.ai_checks_cfg", return_value={"enable": True, "sample_path": "", "max_file_ids": 1}),
            patch("auto_test.pipeline.runner.stress_cfg", return_value={"enable": False, "workers": 0, "cpu_load": 80, "duration": 30}),
        ):
            self.assertEqual(runner._runtime_monitor_cfg(options)["containers"], ["systemadmin", "translateadmin"])
            self.assertFalse(runner._runtime_monitor_cfg(options)["enable_perf"])
            self.assertEqual(runner._runtime_upload_cfg(options)["mode"], "server")
            self.assertEqual(runner._runtime_upload_cfg(options)["size_limit_mb"], 2048)
            self.assertEqual(runner._runtime_export_cfg(options)["types"], "tran")
            self.assertFalse(runner._runtime_ai_checks_cfg(options)["enable"])
            self.assertEqual(runner._runtime_ai_checks_cfg(options)["max_file_ids"], 3)
            self.assertTrue(runner._runtime_stress_cfg(options)["enable"])
            self.assertEqual(runner._runtime_stress_cfg(options)["workers"], 4)
            self.assertEqual(runner._runtime_stress_cfg(options)["cpu_load"], 70)
            self.assertEqual(runner._runtime_stress_cfg(options)["duration"], 90)


if __name__ == "__main__":
    unittest.main()
