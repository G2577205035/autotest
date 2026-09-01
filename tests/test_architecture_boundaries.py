import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.monitoring.server_stress import ServerStressManager
from auto_test.platform.artifact_storage import LocalArtifactStorage, create_artifact_storage
from auto_test.platform.persistence import create_platform_repository, create_task_repository
from auto_test.platform.secrets import decrypt_secret, encrypt_secret
from auto_test.platform.store import PlatformStore


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_repository_factories_keep_sqlite_paths_configurable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_store = create_task_repository(
                root,
                {"backend": "sqlite", "tasks_path": "state/tasks.sqlite"},
            )
            platform_store = create_platform_repository(
                root,
                {"backend": "sqlite", "platform_path": "state/platform.sqlite"},
            )

            self.assertEqual(task_store.db_path, root / "state" / "tasks.sqlite")
            self.assertEqual(platform_store.db_path, root / "state" / "platform.sqlite")

    def test_unknown_backends_fail_instead_of_silently_using_local_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "数据库"):
                create_platform_repository(temp_dir, {"backend": "unknown"})
            with self.assertRaisesRegex(ValueError, "产物"):
                create_artifact_storage(temp_dir, {"backend": "unknown"})

    def test_local_artifact_storage_rejects_escape_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = LocalArtifactStorage(Path(temp_dir) / "artifacts")
            job_dir = storage.workspace("server_stress", "job-1")
            report = job_dir / "report.txt"
            report.write_text("ok", encoding="utf-8")

            self.assertEqual(storage.resolve_file(report, container=job_dir), report)
            with self.assertRaises(ValueError):
                storage.workspace("..", "escape")


class StressQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = PlatformStore(root / "platform.db")
        self.storage = LocalArtifactStorage(root / "artifacts")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_claim_respects_active_target_host(self):
        first = self.store.create_stress_job({"host": "192.0.2.10", "modes": ["monitor"]})
        second = self.store.create_stress_job({"host": "192.0.2.10", "modes": ["monitor"]})

        claimed = self.store.claim_stress_job()

        self.assertEqual(claimed["id"], first["id"])
        self.assertIsNone(self.store.claim_stress_job({"192.0.2.10"}))
        self.assertEqual(self.store.get_stress_job(second["id"])["status"], "authorized")

    def test_authorized_job_is_invisible_to_legacy_queue_claimers(self):
        job = self.store.create_stress_job(
            {"host": "192.0.2.10", "modes": ["monitor"]},
            secret_required=True,
            secret_enc="encrypted-placeholder",
        )

        with self.store._connection() as connection:
            legacy_claim = connection.execute(
                "SELECT id FROM stress_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()

        self.assertEqual(job["status"], "authorized")
        self.assertIsNone(legacy_claim)
        self.assertEqual(self.store.claim_stress_job()["id"], job["id"])

    def test_queued_stress_job_can_be_cancelled(self):
        job = self.store.create_stress_job(
            {"host": "192.0.2.10", "modes": ["monitor"]},
            secret_required=True,
            secret_enc="encrypted-placeholder",
        )

        stopped = self.store.request_stop_stress_job(job["id"])

        self.assertEqual(stopped["status"], "interrupted")
        self.assertTrue(stopped["stop_requested"])
        self.assertIsNone(self.store.claim_stress_job())
        with self.store._connection() as connection:
            secret = connection.execute(
                "SELECT secret_enc FROM stress_job_secrets WHERE job_id=?",
                (job["id"],),
            ).fetchone()
        self.assertIsNone(secret)

    def test_encrypted_queued_secret_survives_restart_and_is_consumed_on_claim(self):
        database = self.store.db_path
        with patch.dict(os.environ, {"LIEMA_MASTER_KEY": "restart-safe-test-key"}):
            encrypted = encrypt_secret("queued-password")
            job = self.store.create_stress_job(
                {
                    "host": "192.0.2.10",
                    "password": "must-never-persist",
                    "modes": ["monitor"],
                },
                secret_required=True,
                secret_enc=encrypted,
            )

            public_job = self.store.get_stress_job(job["id"])
            self.assertNotIn("_secret_enc", public_job)
            self.assertNotIn("queued-password", str(public_job))
            self.assertNotIn("password", public_job["options"])
            self.assertNotIn("must-never-persist", str(public_job))
            self.assertNotIn("_secret_enc", self.store.list_stress_jobs()[0])
            self.assertNotIn(encrypted, str(self.store.list_stress_jobs()))

            restarted = PlatformStore(database)
            self.assertEqual(restarted.get_stress_job(job["id"])["status"], "authorized")
            claimed = restarted.claim_stress_job()

            self.assertEqual(claimed["id"], job["id"])
            self.assertEqual(decrypt_secret(claimed.pop("_secret_enc")), "queued-password")
            self.assertNotIn("_secret_enc", restarted.get_stress_job(job["id"]))
            with restarted._connection() as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM stress_job_secrets WHERE job_id=?",
                    (job["id"],),
                ).fetchone()["count"]
            self.assertEqual(remaining, 0)

    def test_restart_only_interrupts_queued_job_when_encrypted_secret_is_missing(self):
        job = PlatformStore(self.store.db_path, recover_jobs=False).create_stress_job(
            {"host": "192.0.2.10", "modes": ["monitor"]},
            secret_required=True,
        )

        recovered = PlatformStore(self.store.db_path)
        recovered_job = recovered.get_stress_job(job["id"])

        self.assertEqual(recovered_job["status"], "interrupted")
        self.assertIn("重新授权", recovered_job["message"])

    def test_manager_dispatches_encrypted_credential_after_store_restart(self):
        manager_before_restart = ServerStressManager(
            self.store,
            self.storage,
            max_concurrent_jobs=1,
            poll_interval=0.01,
        )
        with patch.dict(os.environ, {"LIEMA_MASTER_KEY": "dispatcher-restart-key"}), patch.object(
            manager_before_restart, "start"
        ):
            job = manager_before_restart.submit(
                {
                    "host": "192.0.2.10",
                    "user": "tester",
                    "password": "restart-password",
                    "modes": ["monitor"],
                }
            )

        restarted_store = PlatformStore(self.store.db_path)
        restarted_manager = ServerStressManager(
            restarted_store,
            self.storage,
            max_concurrent_jobs=1,
            poll_interval=0.01,
        )
        dispatched_passwords = []

        def fake_run(job_id):
            with restarted_manager._lock:
                dispatched_passwords.append(restarted_manager._secrets.get(job_id))
            restarted_store.update_stress_job(
                job_id, status="succeeded", message="done", finished=True
            )

        with patch.dict(os.environ, {"LIEMA_MASTER_KEY": "dispatcher-restart-key"}), patch.object(
            restarted_manager, "_run_job", side_effect=fake_run
        ):
            restarted_manager.start()
            deadline = time.time() + 1
            while not dispatched_passwords and time.time() < deadline:
                time.sleep(0.01)
            restarted_manager.stop()

        self.assertEqual(dispatched_passwords, ["restart-password"])
        self.assertEqual(restarted_store.get_stress_job(job["id"])["status"], "succeeded")

    def test_manager_dispatches_no_more_than_configured_concurrency(self):
        manager = ServerStressManager(
            self.store,
            self.storage,
            max_concurrent_jobs=1,
            poll_interval=0.01,
        )
        gate = __import__("threading").Event()
        started = []

        def fake_run(job_id):
            started.append(job_id)
            gate.wait(1)
            manager.store.update_stress_job(
                job_id, status="succeeded", message="done", finished=True
            )
            with manager._lock:
                manager._threads.pop(job_id, None)
            manager._wake.set()

        with patch.object(manager, "_run_job", side_effect=fake_run):
            first = manager.submit({"host": "192.0.2.10", "modes": ["monitor"]})
            second = manager.submit({"host": "192.0.2.11", "modes": ["monitor"]})
            deadline = time.time() + 1
            while not started and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(started), 1)
            self.assertEqual(self.store.get_stress_job(second["id"])["status"], "authorized")
            gate.set()
            deadline = time.time() + 1
            while len(started) < 2 and time.time() < deadline:
                time.sleep(0.01)
            manager.stop()

        self.assertEqual(started, [first["id"], second["id"]])


if __name__ == "__main__":
    unittest.main()
