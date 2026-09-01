import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.evaluation.backends.evalscope import EvalScopeBackend, EvalScopeRuntimeConfig
from auto_test.evaluation.backends.mock import DeterministicMockBackend
from auto_test.evaluation.contracts import BackendResult, EvaluationRequest, assert_secret_free
from auto_test.evaluation.manager import (
    ModelEvaluationManager,
    create_model_evaluation_backend_resolver,
)
from auto_test.evaluation.result_mapper import EvalScopeResultMapper
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.identity import PROJECT_ROLES, _required_permission
from auto_test.platform.mysql_store import PLATFORM_SCHEMA
from auto_test.platform.store import PlatformStore


class ModelEvaluationContractTests(unittest.TestCase):
    def test_persisted_snapshots_reject_plaintext_credentials(self):
        with self.assertRaisesRegex(ValueError, "api_key"):
            assert_secret_free({"model": {"api_key": "must-not-persist"}})
        with self.assertRaisesRegex(ValueError, "URL"):
            assert_secret_free({"api_url": "https://user:password@example.test/v1"})
        with self.assertRaisesRegex(ValueError, "URL"):
            assert_secret_free({"api_url": "https://example.test/v1?access_token=secret"})

        assert_secret_free(
            {
                "model": {"api_key_env": "LIEMA_EVAL_MODEL_API_KEY"},
                "token_source": "api_usage",
                "max_tokens": 128,
            }
        )

    def test_project_roles_expose_evaluation_capabilities(self):
        self.assertIn("evaluation:view", PROJECT_ROLES["viewer"]["permissions"])
        self.assertIn("evaluation:operate", PROJECT_ROLES["tester"]["permissions"])
        self.assertIn("evaluation:manage", PROJECT_ROLES["project_admin"]["permissions"])
        self.assertEqual(
            _required_permission("POST", "/api/model-evaluation/runs"),
            "evaluation:operate",
        )
        self.assertEqual(
            _required_permission("POST", "/api/model-evaluation/suites"),
            "evaluation:manage",
        )


class ModelEvaluationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = PlatformStore(self.root / "platform.db", recover_jobs=False)
        self.project_a = self.store.create_project("EVA", "评测项目 A")
        self.project_b = self.store.create_project("EVB", "评测项目 B")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_suite_versions_are_immutable_and_project_scoped(self):
        suite = self.store.save_model_eval_suite(
            self.project_a["id"],
            {"name": "基础能力", "category": "capability"},
            created_by="user-a",
        )
        version_1 = self.store.publish_model_eval_suite_version(
            self.project_a["id"],
            suite["id"],
            {"name": "基础能力", "license": "internal"},
            [{"case_key": "case-1", "payload": {"prompt": "2+2=?"}}],
        )
        version_2 = self.store.publish_model_eval_suite_version(
            self.project_a["id"],
            suite["id"],
            {"name": "基础能力", "license": "internal", "revision": 2},
            [{"case_key": "case-2", "payload": {"prompt": "3+3=?"}}],
        )

        self.assertEqual(version_1["version"], 1)
        self.assertEqual(version_2["version"], 2)
        self.assertNotEqual(version_1["content_sha256"], version_2["content_sha256"])
        self.assertEqual(
            self.store.get_model_eval_suite_version(
                self.project_a["id"], version_1["id"]
            )["manifest"]["license"],
            "internal",
        )
        self.assertIsNone(
            self.store.get_model_eval_suite_version(
                self.project_b["id"], version_1["id"]
            )
        )
        self.assertEqual(len(self.store.list_model_eval_suites(self.project_a["id"])), 1)
        self.assertEqual(len(self.store.list_model_eval_suites(self.project_b["id"])), 0)

    def test_run_claim_events_stop_and_project_boundaries(self):
        queued = self.store.create_model_eval_run(
            self.project_a["id"],
            backend="mock",
            backend_version="1.0",
            snapshot={"mode": "mock", "task_config": {"cases": [{"id": "a"}]}},
        )
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(self.store.get_model_eval_run(self.project_b["id"], queued["id"]))

        claimed = self.store.claim_model_eval_run()
        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(claimed["status"], "preparing")
        stopping = self.store.request_stop_model_eval_run(
            self.project_a["id"], queued["id"]
        )
        self.assertTrue(stopping["stop_requested"])
        self.assertTrue(self.store.is_model_eval_run_stop_requested(queued["id"]))
        finished = self.store.finish_model_eval_run(
            self.project_a["id"], queued["id"], status="stopped", summary={}
        )
        self.assertEqual(finished["status"], "stopped")
        self.assertGreaterEqual(
            len(
                self.store.list_model_eval_run_events(
                    self.project_a["id"], queued["id"]
                )
            ),
            3,
        )

    def test_mysql_schema_contains_all_evaluation_tables(self):
        schema = "\n".join(PLATFORM_SCHEMA)
        for table in (
            "model_eval_suites",
            "model_eval_suite_versions",
            "model_eval_cases",
            "model_eval_runs",
            "model_eval_case_results",
            "model_eval_run_events",
            "model_eval_manual_reviews",
            "model_eval_comparisons",
        ):
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema)


class ModelEvaluationBackendTests(unittest.TestCase):
    def test_result_mapper_preserves_unknown_fields_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "summary.json").write_text(
                json.dumps(
                    {
                        "summary": {"score": 0.75, "total_tokens": 9},
                        "future_field": {"api_key": "top-secret", "value": 7},
                    }
                ),
                encoding="utf-8",
            )
            (root / "samples.jsonl").write_text(
                '{"case_id":"one","response":"top-secret"}\n', encoding="utf-8"
            )
            mapped = EvalScopeResultMapper().map_directory(
                root, secret_values=["top-secret"]
            )

        self.assertEqual(mapped["summary"]["score"], 0.75)
        self.assertEqual(mapped["summary"]["total_tokens"], 9)
        self.assertEqual(mapped["raw_primary"]["future_field"]["value"], 7)
        self.assertEqual(mapped["raw_primary"]["future_field"]["api_key"], "***")
        self.assertEqual(mapped["samples"][0]["response"], "***")

    def test_mock_manager_completes_persisted_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("MGR", "Manager")
            manager = ModelEvaluationManager(
                store,
                LocalArtifactStorage(root / "artifacts"),
                lambda _run: DeterministicMockBackend(),
                poll_interval=0.01,
            )
            queued = manager.submit(
                project["id"],
                snapshot={
                    "mode": "mock",
                    "task_config": {"cases": [{"id": "one"}, {"id": "two"}]},
                },
            )
            finished = manager.run_once()

            self.assertEqual(finished["id"], queued["id"])
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["summary"]["passed_cases"], 2)
            self.assertTrue(Path(finished["artifact_ref"]).is_dir())

    def test_manager_resolves_api_key_from_environment_only_at_runtime(self):
        class CapturingBackend:
            name = "capture"
            version = "test"

            def __init__(self):
                self.request = None

            def run(self, request, **_kwargs):
                self.request = request
                return BackendResult(status="completed", summary={"total_tokens": 1})

            def stop(self, _run_id):
                return True

        backend = CapturingBackend()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", {"MODEL_EVAL_TEST_KEY": "runtime-only-secret"}
        ):
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("SEC", "Secrets")
            manager = ModelEvaluationManager(
                store,
                LocalArtifactStorage(root / "artifacts"),
                lambda _run: backend,
            )
            queued = manager.submit(
                project["id"],
                snapshot={
                    "mode": "eval",
                    "task_config": {"api_key_env": "MODEL_EVAL_TEST_KEY"},
                },
                backend="capture",
            )
            finished = manager.run_once()

            self.assertEqual(finished["status"], "completed")
            self.assertEqual(
                backend.request.secret_env,
                {"LIEMA_EVAL_MODEL_API_KEY": "runtime-only-secret"},
            )
            self.assertNotIn(
                "runtime-only-secret",
                json.dumps(store.get_model_eval_run(project["id"], queued["id"])),
            )

    def test_backend_resolution_failure_finishes_run_as_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("BAD", "Bad runtime")
            manager = ModelEvaluationManager(
                store,
                LocalArtifactStorage(root / "artifacts"),
                create_model_evaluation_backend_resolver(
                    root,
                    {"evalscope_version": "1.11.1", "evalscope_python": ""},
                ),
            )
            manager.submit(
                project["id"],
                snapshot={"mode": "eval", "task_config": {}},
                backend="evalscope",
                backend_version="1.10.0",
            )

            finished = manager.run_once()

            self.assertEqual(finished["status"], "failed")
            self.assertIn("RuntimeError", finished["error"])

    def test_isolated_subprocess_runs_and_stops_without_leaking_secret(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = EvalScopeBackend(
                EvalScopeRuntimeConfig(
                    python_executable=Path(sys.executable),
                    version="test",
                    source_root=source_root,
                    stop_grace_seconds=0.5,
                )
            )
            request = EvaluationRequest(
                run_id="isolated-run",
                project_id="project-a",
                mode="mock",
                task_config={"sleep_seconds": 5},
                work_dir=root,
                secret_env={"LIEMA_EVAL_MODEL_API_KEY": "must-not-leak"},
            )
            stop = threading.Event()

            def request_stop():
                time.sleep(0.2)
                stop.set()

            stopper = threading.Thread(target=request_stop)
            stopper.start()
            result = backend.run(request, should_stop=stop.is_set)
            stopper.join()

            self.assertEqual(result.status, "stopped")
            persisted = (root / "evalscope_request.json").read_text(encoding="utf-8")
            self.assertNotIn("must-not-leak", persisted)
            self.assertIn("LIEMA_EVAL_MODEL_API_KEY", persisted)


if __name__ == "__main__":
    unittest.main()
