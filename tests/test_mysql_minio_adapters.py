import tempfile
import unittest
from pathlib import Path

from tests import bootstrap  # noqa: F401

from auto_test.platform.artifact_storage import MinioArtifactStorage
from auto_test.platform.migration import (
    backup_sqlite_databases,
    restore_minio_to_local,
    restore_sqlite_backup,
)
from auto_test.platform.mysql_store import _mysql_sql
from auto_test.platform.persistence import _settings
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


class FakeMinio:
    def __init__(self):
        self.buckets = set()
        self.objects = {}

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def fput_object(self, bucket, key, path):
        self.objects[(bucket, key)] = Path(path).read_bytes()

    def fget_object(self, bucket, key, path):
        Path(path).write_bytes(self.objects[(bucket, key)])

    def list_objects(self, bucket, prefix="", recursive=False):
        class Item:
            def __init__(self, name):
                self.object_name = name

        return [Item(key) for stored_bucket, key in sorted(self.objects) if stored_bucket == bucket and key.startswith(prefix)]


class MySQLAdapterTests(unittest.TestCase):
    def test_sql_adapter_converts_qmark_and_insert_ignore(self):
        sql = _mysql_sql("INSERT OR IGNORE INTO sample(id, text) VALUES(?, 'why?')")

        self.assertIn("INSERT IGNORE", sql)
        self.assertIn("VALUES(%s, 'why?')", sql)

    def test_begin_immediate_maps_to_mysql_transaction(self):
        self.assertEqual(_mysql_sql("BEGIN IMMEDIATE"), "START TRANSACTION")

    def test_sql_adapter_escapes_literal_percent_for_pymysql(self):
        sql = _mysql_sql(
            "SELECT id FROM run_events WHERE run_id = ? "
            "AND (message LIKE '翻译进度%' OR message LIKE '翻译完成%') ORDER BY id"
        )

        self.assertEqual(
            sql,
            "SELECT id FROM run_events WHERE run_id = %s "
            "AND (message LIKE '翻译进度%%' OR message LIKE '翻译完成%%') ORDER BY id",
        )

    def test_translation_progress_sql_formats_with_single_parameter(self):
        sql = _mysql_sql(
            "SELECT id FROM run_events WHERE run_id = ? "
            "AND (message LIKE '翻译进度%' OR message LIKE '翻译完成%') ORDER BY id"
        )

        # Mirror PyMySQL's mogrify-style %-interpolation with one parameter.
        rendered = sql % ("run-1",)

        self.assertIn("run_id = run-1", rendered)
        self.assertIn("LIKE '翻译进度%'", rendered)
        self.assertIn("LIKE '翻译完成%'", rendered)

    def test_mysql_secrets_can_be_injected_by_environment(self):
        from unittest.mock import patch

        with patch.dict(
            "os.environ",
            {
                "LIEMA_DATABASE_BACKEND": "mysql",
                "LIEMA_MYSQL_HOST": "db.test",
                "LIEMA_MYSQL_PASSWORD": "secret",
            },
            clear=False,
        ):
            settings = _settings({"database": "blue", "user": "tester"})

        self.assertEqual(settings["backend"], "mysql")
        self.assertEqual(settings["host"], "db.test")
        self.assertEqual(settings["password"], "secret")


class MinioAdapterTests(unittest.TestCase):
    def test_publish_download_cache_and_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fake = FakeMinio()
            storage = MinioArtifactStorage(
                root / "staging",
                endpoint="minio.test:9000",
                access_key="access",
                secret_key="secret",
                bucket="test-artifacts",
                prefix="blue",
                create_bucket=True,
                client=fake,
            )
            job_dir = storage.workspace("server_stress", "job-1")
            report = job_dir / "report.txt"
            report.write_text("report-data", encoding="utf-8")

            references = storage.publish_tree(job_dir)
            import shutil
            shutil.rmtree(job_dir)
            materialized = storage.materialize_tree(storage.reference(job_dir))
            downloaded = storage.resolve_file(references[0], container=storage.reference(job_dir))
            restored = restore_minio_to_local(storage, root / "restored", dry_run=False)

            self.assertEqual(downloaded.read_text(encoding="utf-8"), "report-data")
            self.assertTrue((materialized / "report.txt").is_file())
            self.assertEqual(restored["restored"], 1)
            self.assertEqual(
                (root / "restored" / "server_stress" / "job-1" / "report.txt").read_text(encoding="utf-8"),
                "report-data",
            )

    def test_reference_cannot_escape_configured_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake = FakeMinio()
            storage = MinioArtifactStorage(
                Path(temp_dir) / "staging",
                endpoint="minio.test:9000",
                access_key="access",
                secret_key="secret",
                bucket="test-artifacts",
                prefix="blue",
                create_bucket=True,
                client=fake,
            )
            with self.assertRaises(ValueError):
                storage.resolve("minio://test-artifacts/other/report.txt")


class MigrationTests(unittest.TestCase):
    def test_backup_and_guarded_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tasks_path = root / "tasks.db"
            platform_path = root / "platform.db"
            task_store = TaskStore(tasks_path)
            platform_store = PlatformStore(platform_path)
            run = task_store.create_run({"source": "migration-test"})
            platform_store.create_stress_job({"host": "192.0.2.10", "modes": ["monitor"]})
            backup_dir = root / "backup"
            backup_sqlite_databases(
                backup_dir, tasks_path=tasks_path, platform_path=platform_path
            )
            for database in (tasks_path, platform_path):
                for suffix in ("-wal", "-shm"):
                    database.with_name(database.name + suffix).unlink(missing_ok=True)
            tasks_path.unlink()
            platform_path.unlink()

            result = restore_sqlite_backup(
                backup_dir,
                tasks_path=tasks_path,
                platform_path=platform_path,
                dry_run=False,
            )

            self.assertTrue(result["verified"])
            self.assertEqual(TaskStore(tasks_path).get_run(run["id"])["id"], run["id"])
            self.assertEqual(len(PlatformStore(platform_path).list_stress_jobs()), 1)

    def test_restore_rejects_tampered_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tasks_path = root / "tasks.db"
            platform_path = root / "platform.db"
            TaskStore(tasks_path)
            PlatformStore(platform_path)
            backup_dir = root / "backup"
            backup_sqlite_databases(
                backup_dir, tasks_path=tasks_path, platform_path=platform_path
            )
            with (backup_dir / "tasks.db").open("ab") as stream:
                stream.write(b"tampered")

            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                restore_sqlite_backup(
                    backup_dir,
                    tasks_path=root / "restored-tasks.db",
                    platform_path=root / "restored-platform.db",
                )


if __name__ == "__main__":
    unittest.main()
