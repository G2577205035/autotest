import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.pipeline import runner, upload


class _FakeSftp:
    def __init__(self, size):
        self.size = size
        self.closed = False
        self.put_calls = []

    def stat(self, _path):
        return SimpleNamespace(st_size=self.size)

    def put(self, local_path, remote_path, callback=None):
        self.put_calls.append((local_path, remote_path))
        if callback:
            callback(self.size, self.size)

    def close(self):
        self.closed = True


class _FakeUploadSsh:
    def __init__(self, sftp):
        self.sftp = sftp
        self._client = self
        self.exec_calls = []

    def open_sftp(self):
        return self.sftp

    def exec(self, command, timeout=30):
        self.exec_calls.append((command, timeout))
        if command.startswith("ls -d"):
            return "/home/sftp/测试20260819091428\n", ""
        if command.startswith("md5sum"):
            return "a" * 32 + "\n", ""
        return "", ""


class _RunnerSsh:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self._client = object()
        self.disconnected = False
        self.__class__.instances.append(self)

    def connect(self):
        return True

    def disconnect(self):
        self.disconnected = True


class _RunnerMonitor:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.started = False
        self.perf_stopped = False
        self.logs_stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop_perf(self):
        self.perf_stopped = True

    def stop_logs(self):
        self.logs_stopped = True
        return []


class ServerUploadTests(unittest.TestCase):
    def test_large_file_checksum_timeout_scales_beyond_legacy_30_seconds(self):
        file_size = 6_921_179_102
        self.assertGreaterEqual(upload._checksum_timeout(file_size), 300)
        self.assertLessEqual(upload._checksum_timeout(file_size), 3600)

    def test_existing_large_file_uses_size_aware_checksum_and_skips_transfer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "sample.rar"
            local_file.write_bytes(b"same-content")
            sftp = _FakeSftp(local_file.stat().st_size)
            ssh = _FakeUploadSsh(sftp)

            with (
                patch("auto_test.integrations.api.check_upload_disk", return_value={"allowed": True}),
                patch(
                    "auto_test.integrations.api.listen_multi_fast_upload",
                    return_value=("upload-id", 1, "batch-name"),
                ),
                patch.object(upload, "_local_md5", return_value="a" * 32),
                patch.object(upload.config, "HOST", "192.0.2.10"),
                patch.object(upload.config, "CASE_NAME", "case"),
                patch.object(upload.config, "TRANSLATE_NAME", "es:zh-CHS"),
                patch.object(upload.config, "ANALYSIS", "summary"),
            ):
                batches, uploaded, unsupported = upload.upload_folder_server(
                    "token", str(local_file), "case-id", ssh,
                )

        checksum_call = next(call for call in ssh.exec_calls if call[0].startswith("md5sum"))
        self.assertGreaterEqual(checksum_call[1], 300)
        self.assertEqual(sftp.put_calls, [])
        self.assertTrue(sftp.closed)
        self.assertEqual((uploaded, unsupported), (1, 0))
        self.assertEqual(batches[0]["ul_id"], "upload-id")

    def test_upload_failure_propagates_and_releases_monitor_connections(self):
        _RunnerSsh.instances = []
        _RunnerMonitor.instances = []
        events = []
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_file = Path(temp_dir) / "sample.rar"
            upload_file.write_bytes(b"payload")
            with (
                patch.object(runner, "RUNS_DIR", Path(temp_dir) / "runs"),
                patch.object(runner, "prepare_runtime_layout"),
                patch.object(runner, "_cleanup_old_runs"),
                patch.object(runner, "login", return_value="token"),
                patch.object(runner, "resolve_case", return_value="case-id"),
                patch.object(runner, "SSHClient", _RunnerSsh),
                patch.object(runner, "DockerMonitor", _RunnerMonitor),
                patch.object(runner, "upload_folder_server", side_effect=TimeoutError()),
            ):
                with self.assertRaisesRegex(RuntimeError, "TimeoutError: 等待服务器响应超时"):
                    runner.run_pipeline(
                        progress_callback=lambda stage, message, progress: events.append(
                            (stage, message, progress)
                        ),
                        options={
                            "username": "test",
                            "password": "secret",
                            "host": "192.0.2.10",
                            "ssh_user": "root",
                            "ssh_password": "secret",
                            "upload_path": str(upload_file),
                            "upload_mode": "server",
                            "stress_enable": False,
                            "monitor_enable_perf": True,
                            "monitor_enable_log": True,
                            "translate_name": "es:zh-CHS",
                        },
                    )

        self.assertEqual(len(_RunnerSsh.instances), 2)
        self.assertTrue(all(item.disconnected for item in _RunnerSsh.instances))
        self.assertEqual(len(_RunnerMonitor.instances), 1)
        self.assertTrue(_RunnerMonitor.instances[0].perf_stopped)
        self.assertTrue(_RunnerMonitor.instances[0].logs_stopped)
        self.assertTrue(any(stage == "upload" for stage, _message, _progress in events))


if __name__ == "__main__":
    unittest.main()
