import sqlite3
from tests import bootstrap  # noqa: F401
import json
import tempfile
import unittest
from pathlib import Path

from auto_test.common.paths import prepare_runtime_layout


class RuntimePathMigrationTests(unittest.TestCase):
    def test_migrates_legacy_layout_and_database_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy_logs = root / "logs"
            legacy_run = legacy_logs / "20260812_010203"
            legacy_stress = legacy_logs / "server_stress" / "stress-1"
            legacy_upload = legacy_logs / "uploads" / "upload-1"
            legacy_run.mkdir(parents=True)
            legacy_stress.mkdir(parents=True)
            legacy_upload.mkdir(parents=True)
            (legacy_run / "result.txt").write_text("run", encoding="utf-8")
            (legacy_stress / "report.txt").write_text("stress", encoding="utf-8")
            (legacy_upload / "input.txt").write_text("upload", encoding="utf-8")
            (legacy_logs / ".local_web_secrets.json").write_text("{}", encoding="utf-8")

            tasks_db = legacy_logs / "tasks.db"
            connection = sqlite3.connect(tasks_db)
            try:
                connection.execute(
                    "CREATE TABLE runs (id TEXT, run_dir TEXT, metadata_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?)",
                    (
                        "run-1",
                        str(legacy_run.resolve()),
                        '{"upload_path": "' + str(legacy_upload.resolve()).replace("\\", "\\\\") + '"}',
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            platform_db = legacy_logs / "platform.db"
            connection = sqlite3.connect(platform_db)
            try:
                connection.execute(
                    "CREATE TABLE report_jobs (options_json TEXT, snapshot_json TEXT, artifact_dir TEXT, docx_path TEXT, pdf_path TEXT)"
                )
                connection.execute(
                    "CREATE TABLE stress_jobs (options_json TEXT, artifact_dir TEXT, report_path TEXT)"
                )
                connection.execute(
                    "INSERT INTO stress_jobs VALUES (?, ?, ?)",
                    ("{}", str(legacy_stress.resolve()), str((legacy_stress / "report.txt").resolve())),
                )
                connection.commit()
            finally:
                connection.close()

            prepare_runtime_layout(root)
            prepare_runtime_layout(root)

            self.assertTrue((root / "artifacts" / "runs" / legacy_run.name / "result.txt").is_file())
            self.assertTrue((root / "artifacts" / "server_stress" / "stress-1" / "report.txt").is_file())
            self.assertTrue((root / "runtime" / "uploads" / "upload-1" / "input.txt").is_file())
            self.assertTrue((root / "instance" / "local_web_secrets.json").is_file())
            self.assertTrue((root / "data" / "tasks.db").is_file())
            self.assertTrue((root / "data" / "platform.db").is_file())

            connection = sqlite3.connect(root / "data" / "tasks.db")
            try:
                run_dir, metadata = connection.execute(
                    "SELECT run_dir, metadata_json FROM runs WHERE id='run-1'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(run_dir, str((root / "artifacts" / "runs" / legacy_run.name).resolve()))
            self.assertEqual(
                json.loads(metadata)["upload_path"],
                str((root / "runtime" / "uploads" / "upload-1").resolve()),
            )

            connection = sqlite3.connect(root / "data" / "platform.db")
            try:
                artifact_dir, report_path = connection.execute(
                    "SELECT artifact_dir, report_path FROM stress_jobs"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(artifact_dir, str((root / "artifacts" / "server_stress" / "stress-1").resolve()))
            self.assertEqual(report_path, str((root / "artifacts" / "server_stress" / "stress-1" / "report.txt").resolve()))


if __name__ == "__main__":
    unittest.main()
