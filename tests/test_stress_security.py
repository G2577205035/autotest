import unittest
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.monitoring.server_stress import ServerStressManager
from auto_test.monitoring.cpu_stress import CpuStressRunner
from auto_test.platform.secrets import decrypt_secret


class StoreStub:
    def __init__(self):
        self.options = None
        self.secret_enc = ""
        self.secret_required = False

    def create_stress_job(self, options, *, secret_required=False, secret_enc=""):
        self.options = dict(options)
        self.secret_enc = secret_enc
        self.secret_required = secret_required
        return {"id": "job-1", "status": "authorized"}


class ThreadStub:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True


class StressSecretTests(unittest.TestCase):
    def test_submitted_ssh_password_is_kept_out_of_persistent_options(self):
        store = StoreStub()

        with patch.dict(
            "os.environ",
            {
                "LIEMA_MASTER_KEY": "test-master-key",
                "LIEMA_ARTIFACT_BACKEND": "local",
            },
        ):
            manager = ServerStressManager(store)
            with patch.object(manager, "start"):
                manager.submit({"host": "192.0.2.10", "password": "one-time-secret"})

        self.assertNotIn("password", store.options)
        self.assertTrue(store.secret_required)
        self.assertNotIn("one-time-secret", store.secret_enc)
        with patch.dict("os.environ", {"LIEMA_MASTER_KEY": "test-master-key"}):
            self.assertEqual(decrypt_secret(store.secret_enc), "one-time-secret")
        self.assertEqual(manager._secrets, {})

    def test_stress_ng_missing_does_not_auto_install_by_default(self):
        class SSHStub:
            def __init__(self):
                self.commands = []

            def exec(self, command, timeout=30):
                self.commands.append(command)
                if command.startswith("id -u"):
                    return "0\n", ""
                return "NOT_FOUND\n", ""

        ssh = SSHStub()
        runner = CpuStressRunner(ssh)

        self.assertFalse(runner.install(allow_install=False))
        commands = "\n".join(ssh.commands).lower()
        self.assertNotIn("yum install", commands)
        self.assertNotIn("dnf install", commands)
        self.assertNotIn("apt-get install", commands)

    def test_existing_stress_ng_can_run_as_non_root(self):
        class SSHStub:
            def __init__(self):
                self.commands = []

            def exec(self, command, timeout=30):
                self.commands.append(command)
                return "/usr/bin/stress-ng\n", ""

        ssh = SSHStub()

        self.assertTrue(CpuStressRunner(ssh).install(allow_install=False))
        self.assertFalse(any(command.startswith("id -u") for command in ssh.commands))


if __name__ == "__main__":
    unittest.main()
