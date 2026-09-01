import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.monitoring.server_sessions import ServerSessionManager
from auto_test.platform.store import PlatformStore


class FakeSSH:
    instances = []

    def __init__(self, host, user, password, port=22):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.last_error = ""
        self.disconnected = False
        self.__class__.instances.append(self)

    def connect(self):
        return True

    def disconnect(self):
        self.disconnected = True


class FakeProbe:
    def collect(self, _ssh, modes, **_options):
        return {
            "overall": "ready",
            "requested_modes": list(modes),
            "host": {"hostname": "session-node", "cpu_logical": 16},
            "gpu": {"count": 0, "devices": []},
            "capabilities": {
                "monitor": {"status": "ready", "message": "ready"},
                "cpu": {"status": "ready", "message": "ready"},
                "gpu": {"status": "blocked", "message": "no gpu"},
            },
        }


class FailingProbe:
    def collect(self, *_args, **_kwargs):
        raise RuntimeError("unsupported probe")


class FakeMonitor:
    def __init__(self, *_args, sample_callback=None, **_kwargs):
        self.sample_callback = sample_callback
        self.stopped = False

    def start(self):
        self.sample_callback(
            "APP",
            {
                "time": "12:00:00",
                "cpu_pct": 12.5,
                "mem_used_gb": 4,
                "mem_total_gb": 16,
                "gpu_sampling_status": "unavailable",
            },
        )

    def stop_perf(self):
        self.stopped = True

    def stop_logs(self):
        self.stopped = True


class ServerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PlatformStore(Path(self.temp_dir.name) / "platform.db")
        FakeSSH.instances = []
        self.manager = ServerSessionManager(
            self.store,
            idle_timeout=300,
            reap_interval=0.1,
            capability_probe=FakeProbe(),
            monitor_factory=FakeMonitor,
            ssh_factory=FakeSSH,
        )

    def tearDown(self):
        self.manager.shutdown(timeout=0.2)
        self.temp_dir.cleanup()

    def test_unsaved_password_is_required_again_and_never_returned(self):
        profile = self.manager.save_profile(
            {
                "name": "Server A",
                "host": "192.0.2.10",
                "port": 22,
                "user": "tester",
                "password": "temporary-password",
                "save_password": False,
            }
        )

        self.assertFalse(profile["credential_saved"])
        self.assertNotIn("credential_enc", profile)
        with self.assertRaisesRegex(RuntimeError, "未保存凭据"):
            self.manager.connect({"profile_id": profile["id"]})

        session = self.manager.connect(
            {"profile_id": profile["id"], "password": "temporary-password"}
        )
        self.assertEqual(session["status"], "connected")
        self.assertEqual(FakeSSH.instances[-1].password, "temporary-password")
        metrics = self.manager.metrics(session["id"])
        self.assertEqual(metrics["metrics"][0]["data"]["cpu_pct"], 12.5)
        self.assertNotIn("password", str(session).lower())

    def test_saved_password_is_encrypted_and_reused_by_next_session(self):
        with patch.dict(os.environ, {"LIEMA_MASTER_KEY": "server-session-test-key"}):
            profile = self.manager.save_profile(
                {
                    "name": "Server B",
                    "host": "192.0.2.11",
                    "port": 22,
                    "user": "tester",
                    "password": "saved-password",
                    "save_password": True,
                }
            )
            raw = self.store.get_server_profile(profile["id"], include_secret=True)
            self.assertTrue(profile["credential_saved"])
            self.assertNotEqual(raw["credential_enc"], "saved-password")
            self.assertNotIn("saved-password", str(self.store.list_server_profiles()))

            session = self.manager.connect({"profile_id": profile["id"]})

        self.assertEqual(FakeSSH.instances[-1].password, "saved-password")
        self.assertEqual(session["profile_id"], profile["id"])

    def test_busy_session_must_stop_stress_before_manual_close(self):
        session = self.manager.connect(
            {
                "server_name": "Temporary",
                "host": "192.0.2.12",
                "port": 22,
                "user": "tester",
                "password": "one-time-password",
            }
        )

        ssh = self.manager.acquire(session["id"])
        self.assertIs(ssh, FakeSSH.instances[-1])
        with self.assertRaisesRegex(RuntimeError, "先停止压测"):
            self.manager.close(session["id"])
        self.manager.release(session["id"])

        closed = self.manager.close(session["id"])
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(ssh.disconnected)

    def test_probe_failure_keeps_connected_session_in_degraded_mode(self):
        self.manager.capability_probe = FailingProbe()

        session = self.manager.connect(
            {
                "server_name": "Degraded",
                "host": "192.0.2.13",
                "port": 22,
                "user": "tester",
                "password": "one-time-password",
            }
        )

        self.assertEqual(session["status"], "connected")
        self.assertEqual(session["capability"]["overall"], "degraded")
        self.assertEqual(
            session["capability"]["capabilities"]["cpu"]["status"], "blocked"
        )

    def test_identical_targets_use_separate_project_owned_sessions(self):
        common = {
            "host": "192.0.2.20",
            "port": 22,
            "user": "tester",
            "password": "one-time-password",
        }

        project_a = self.manager.connect({**common, "_project_id": "project-a"})
        project_b = self.manager.connect({**common, "_project_id": "project-b"})

        self.assertNotEqual(project_a["id"], project_b["id"])
        self.assertEqual(project_a["project_id"], "project-a")
        self.assertEqual(project_b["project_id"], "project-b")
        self.assertEqual(len(FakeSSH.instances), 2)


if __name__ == "__main__":
    unittest.main()
