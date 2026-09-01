import json
import logging
import os
import tempfile
from tests import bootstrap  # noqa: F401
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_test.core.automation import RunEvent
from auto_test.core.task_manager import TaskManager
from auto_test.platform.task_store import TaskStore
from auto_test.platform.secrets import encrypt_secret


class FakeService:
    def run(self, *, run_id, progress_callback):
        progress_callback(RunEvent("upload", "uploading", 30, time.time()))
        return SimpleNamespace(run_dir=f"/tmp/{run_id}")

class SlowService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, *, run_id, progress_callback):
        self.started.set()
        self.release.wait(1)
        progress_callback(RunEvent("upload", "uploading", 30, time.time()))
        return SimpleNamespace(run_dir=f"/tmp/{run_id}")


class SecretCaptureService:
    def __init__(self):
        self.options = None

    def run(self, *, run_id, progress_callback, options):
        self.options = dict(options)
        return SimpleNamespace(run_dir=f"/tmp/{run_id}")


class TaskManagerTests(unittest.TestCase):
    def test_worker_executes_queued_task_and_persists_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskStore(Path(temp_dir) / "tasks.db")
            manager = TaskManager(store, FakeService(), poll_interval=0.01)
            manager.start()
            try:
                task = manager.submit({"source": "test"})
                deadline = time.time() + 2
                while time.time() < deadline:
                    current = store.get_run(task["id"])
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("task did not complete before deadline")

                self.assertEqual(current["progress"], 100)
                self.assertTrue(current["run_dir"].endswith(task["id"]))
            finally:
                manager.stop()

    def test_restart_copies_metadata_into_new_queued_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskStore(Path(temp_dir) / "tasks.db")
            manager = TaskManager(store, FakeService(), poll_interval=0.01)
            original = store.create_run({"options": {"username": "test", "host": "172.16.102.73"}})
            store.request_stop(original["id"])

            restarted = manager.restart_run(original["id"])

            self.assertNotEqual(restarted["id"], original["id"])
            self.assertEqual(restarted["status"], "queued")
            self.assertEqual(restarted["metadata"]["options"]["username"], "test")
            self.assertEqual(restarted["metadata"]["restarted_from"], original["id"])

    def test_stop_running_task_interrupts_at_next_progress_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskStore(Path(temp_dir) / "tasks.db")
            service = SlowService()
            manager = TaskManager(store, service, poll_interval=0.01)
            manager.start()
            try:
                task = manager.submit({"source": "test"})
                self.assertTrue(service.started.wait(1))
                manager.stop_run(task["id"])
                service.release.set()
                deadline = time.time() + 2
                while time.time() < deadline:
                    current = store.get_run(task["id"])
                    if current["status"] == "interrupted":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("task did not interrupt before deadline")
            finally:
                service.release.set()
                manager.stop()

    def test_worker_injects_decrypted_secret_without_persisting_it_in_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"LIEMA_MASTER_KEY": "task-secret-test-key"}
        ):
            store = TaskStore(Path(temp_dir) / "tasks.db")
            service = SecretCaptureService()
            manager = TaskManager(store, service)
            secret_enc = encrypt_secret(json.dumps({
                "password": "business-secret",
                "ssh_password": "ssh-secret",
            }))
            submitted = manager.submit(
                {"options": {"username": "tester", "host": "192.0.2.10"}},
                secret_enc=secret_enc,
            )

            public = store.get_run(submitted["id"])
            self.assertNotIn("password", public["metadata"]["options"])
            manager._execute(store.claim_next())

        self.assertEqual(service.options["password"], "business-secret")
        self.assertEqual(service.options["ssh_password"], "ssh-secret")

    def test_log_handler_degrades_and_buffers_while_store_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskStore(Path(temp_dir) / "tasks.db")
            manager = TaskManager(store, FakeService())
            handler = manager._log_handler
            run = store.create_run({"source": "test"})
            manager._set_active(run["id"])

            def make_record(message: str) -> logging.LogRecord:
                return logging.LogRecord(
                    "liema", logging.INFO, __file__, 1, message, (), None
                )

            with patch.object(store, "add_event", side_effect=RuntimeError("db down")):
                handler.emit(make_record("翻译进度：1/10"))
                handler.emit(make_record("翻译进度：1/10"))

            # Failures never raise into logging and back off from the store.
            self.assertGreater(handler._backoff_until, time.monotonic())
            self.assertEqual(len(handler._buffer), 2)

            # Store recovers: the next flush persists buffered events.
            handler._backoff_until = 0.0
            handler._failures = 0
            handler.emit(make_record("翻译进度：2/10"))

            events = store.get_events(run["id"])
            progress = sorted(
                event["message"]
                for event in events
                if event["message"].startswith("翻译进度")
            )
            self.assertEqual(
                progress,
                ["翻译进度：1/10", "翻译进度：1/10", "翻译进度：2/10"],
            )
            self.assertEqual(len(handler._buffer), 0)


if __name__ == "__main__":
    unittest.main()
