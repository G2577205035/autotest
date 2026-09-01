import sqlite3
from contextlib import closing
from tests import bootstrap  # noqa: F401
import tempfile
import time
import unittest
from pathlib import Path

from auto_test.platform.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_lifecycle_is_persisted(self):
        created = self.store.create_run({"source": "test"})
        claimed = self.store.claim_next()

        self.assertEqual(claimed["id"], created["id"])
        self.assertEqual(claimed["status"], "running")

        self.store.update_progress(created["id"], "upload", "uploading", 25)
        self.store.complete(created["id"], "/tmp/run")
        completed = self.store.get_run(created["id"])

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(completed["run_dir"], "/tmp/run")
        self.assertGreaterEqual(len(self.store.get_events(created["id"])), 3)

    def test_translation_progress_query_returns_complete_filtered_series(self):
        created = self.store.create_run({"source": "test"})
        self.store.add_event(created["id"], "INFO", "解析进度：20/100")
        self.store.add_event(created["id"], "INFO", "翻译进度：90/9573")
        self.store.add_event(created["id"], "INFO", "翻译完成：117/9573")

        events = self.store.get_translation_progress_events(created["id"])

        self.assertEqual([event["message"] for event in events], [
            "翻译进度：90/9573", "翻译完成：117/9573",
        ])

    def test_translation_progress_query_falls_back_to_legacy_untyped_rows(self):
        created = self.store.create_run({"source": "test"})
        # Simulate rows written before the structured event_type column
        # existed: raw insert with the default empty type.
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO run_events
                        (run_id, created_at, level, stage, message, progress, event_type)
                    VALUES (?, ?, 'INFO', '', '翻译进度：7/9573', NULL, '')
                    """,
                    (created["id"], time.time()),
                )

        events = self.store.get_translation_progress_events(created["id"])

        self.assertEqual([event["message"] for event in events], ["翻译进度：7/9573"])

    def test_list_runs_omits_heavy_columns_while_get_run_returns_them(self):
        created = self.store.create_run({"source": "test"})
        self.store.claim_next()
        self.store.complete(created["id"], "/data/runs/example")

        listed = self.store.list_runs(10)[0]
        detailed = self.store.get_run(created["id"])

        self.assertNotIn("run_dir", listed)
        self.assertNotIn("error", listed)
        self.assertNotIn("stale_marked_at", listed)
        self.assertEqual(listed["has_run_dir"], 1)
        self.assertEqual(detailed["run_dir"], "/data/runs/example")
        self.assertIn("error", detailed)
        self.assertEqual(listed["id"], created["id"])

    def test_stale_running_task_enters_grace_window_before_interruption(self):
        stale = self.store.create_run()
        self.store.claim_next()
        queued = self.store.create_run()
        connection = sqlite3.connect(self.store.db_path)
        try:
            with connection:
                connection.execute(
                    "UPDATE runs SET heartbeat_at = ? WHERE id = ?",
                    (time.time() - 300, stale["id"]),
                )
        finally:
            connection.close()

        first_claim = self.store.claim_next(stale_after_seconds=120)

        # First stale sighting only marks the grace window; the executor
        # process may simply be blocked by a network flap, so nothing is
        # interrupted and the queue keeps waiting.
        self.assertIsNone(first_claim)
        current = self.store.get_run(stale["id"])
        self.assertEqual(current["status"], "running")
        self.assertIn("宽限", current["message"])
        events = self.store.get_events(stale["id"])
        self.assertTrue(any("宽限" in event["message"] for event in events))

        # Push the grace marker beyond its window; only then is the task
        # interrupted and the queued task claimed.
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE runs SET stale_marked_at = ? WHERE id = ?",
                    (time.time() - 300, stale["id"]),
                )

        second_claim = self.store.claim_next(stale_after_seconds=120)

        self.assertEqual(self.store.get_run(stale["id"])["status"], "interrupted")
        self.assertEqual(second_claim["id"], queued["id"])

    def test_heartbeat_recovery_clears_grace_marker_without_interrupting(self):
        run = self.store.create_run()
        self.store.claim_next()
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE runs SET heartbeat_at = ? WHERE id = ?",
                    (time.time() - 300, run["id"]),
                )

        self.assertIsNone(self.store.claim_next(stale_after_seconds=120))
        self.assertEqual(self.store.get_run(run["id"])["status"], "running")

        # Heartbeat catches up (executor was alive all along): the marker is
        # cleared on the next claim and the task keeps running.
        self.store.heartbeat(run["id"])
        self.assertIsNone(self.store.claim_next(stale_after_seconds=120))

        current = self.store.get_run(run["id"])
        self.assertEqual(current["status"], "running")
        self.assertIn("心跳已恢复", current["message"])

    def test_run_has_human_readable_display_name(self):
        created = self.store.create_run({
            "options": {
                "username": "test",
                "host": "172.16.102.73",
                "case_name": "测试20260810",
            }
        })

        self.assertIn("测试-", created["display_name"])
        self.assertIn("test", created["display_name"])
        self.assertIn("73", created["display_name"])
        self.assertIn("测试20260810", created["display_name"])

    def test_existing_database_is_migrated_with_new_columns(self):
        path = Path(self.temp_dir.name) / "legacy.db"
        legacy_schema = """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}', run_dir TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            started_at REAL, finished_at REAL, heartbeat_at REAL
        );
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            created_at REAL NOT NULL, level TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL, progress INTEGER
        );
        CREATE TABLE run_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            created_at REAL NOT NULL, source TEXT NOT NULL, data_json TEXT NOT NULL
        );
        CREATE TABLE run_secrets (
            run_id TEXT PRIMARY KEY, secret_enc TEXT NOT NULL, created_at REAL NOT NULL
        );
        """
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.executescript(legacy_schema)

        migrated = TaskStore(path)

        with closing(sqlite3.connect(path)) as connection:
            runs_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(runs)")
            }
            events_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(run_events)")
            }
        self.assertIn("stale_marked_at", runs_columns)
        self.assertIn("event_type", events_columns)

        created = migrated.create_run({"source": "test"})
        migrated.claim_next()
        migrated.add_event(created["id"], "INFO", "翻译进度：1/10")
        self.assertEqual(
            len(migrated.get_translation_progress_events(created["id"])), 1
        )

    def test_can_cancel_queued_run(self):
        created = self.store.create_run({"source": "test"})

        stopped = self.store.request_stop(created["id"])

        self.assertEqual(stopped["status"], "interrupted")
        self.assertEqual(stopped["message"], "任务已取消")

    def test_running_stop_request_is_cooperative(self):
        created = self.store.create_run({"source": "test"})
        self.store.claim_next()

        stopped = self.store.request_stop(created["id"])

        self.assertEqual(stopped["status"], "running")
        self.assertTrue(self.store.is_stop_requested(created["id"]))

    def test_secret_is_not_public_and_is_consumed_when_task_is_claimed(self):
        created = self.store.create_run(
            {"options": {"username": "tester"}},
            secret_enc="encrypted-once",
        )

        public = TaskStore(self.store.db_path).get_run(created["id"])
        self.assertNotIn("_secret_enc", public)
        self.assertNotIn("encrypted-once", str(public))

        claimed = TaskStore(self.store.db_path).claim_next()
        self.assertEqual(claimed["_secret_enc"], "encrypted-once")
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM run_secrets WHERE run_id=?", (created["id"],)
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_cancelled_queued_task_deletes_pending_secret(self):
        created = self.store.create_run({}, secret_enc="encrypted-once")

        self.store.request_stop(created["id"])

        with closing(sqlite3.connect(self.store.db_path)) as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM run_secrets WHERE run_id=?", (created["id"],)
            ).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
