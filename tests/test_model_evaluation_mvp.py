import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import bootstrap  # noqa: F401

from auto_test.evaluation.backends.native import NativeEvaluationBackend
from auto_test.evaluation.catalog import ensure_builtin_evaluation_suites
from auto_test.evaluation.contracts import EvaluationRequest, assert_secret_free
from auto_test.evaluation.model_client import ObservedModelResponse
from auto_test.evaluation.result_mapper import EvalScopeResultMapper
from auto_test.evaluation.robustness import summarize_robustness
from auto_test.evaluation.scoring import estimate_token_count, score_response
from auto_test.platform.api import create_platform_api
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.identity import create_identity_api, install_identity_guard
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


ADMIN_PASSWORD = "StrongAdmin!2026"


class EvaluationMvpDomainTests(unittest.TestCase):
    def test_builtin_catalog_is_idempotent_and_hash_versioned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PlatformStore(Path(temp_dir) / "platform.db", recover_jobs=False)
            first = ensure_builtin_evaluation_suites(store)
            second = ensure_builtin_evaluation_suites(store)

            self.assertEqual(len(first), 8)
            self.assertEqual(
                [item["latest_version"]["id"] for item in first],
                [item["latest_version"]["id"] for item in second],
            )
            self.assertEqual(sum(item["case_count"] for item in second), 270)
            for item in second:
                self.assertEqual(len(item["latest_version"]["content_sha256"]), 64)

    def test_rule_scoring_token_estimate_and_robustness_guard(self):
        scored = score_response(
            '{"status":"ok","count":2}',
            {
                "json_schema": {
                    "required": ["status", "count"],
                    "types": {"status": "string", "count": "integer"},
                }
            },
        )
        self.assertTrue(scored["passed"])
        self.assertGreater(estimate_token_count("中文 token 123"), 0)

        robustness = summarize_robustness(
            [
                {
                    "case_id": "base",
                    "robustness_group": "g",
                    "variant_type": "baseline",
                    "score": {"score": 1.0},
                },
                {
                    "case_id": "variant",
                    "robustness_group": "g",
                    "variant_type": "typo",
                    "score": {"score": 0.8},
                },
                {
                    "case_id": "weak-base",
                    "robustness_group": "weak",
                    "variant_type": "baseline",
                    "score": {"score": 0.2},
                },
                {
                    "case_id": "weak-variant",
                    "robustness_group": "weak",
                    "variant_type": "noise",
                    "score": {"score": 0.2},
                },
            ]
        )
        self.assertEqual(robustness["average_retention"], 80.0)
        weak = next(item for item in robustness["groups"] if item["group"] == "weak")
        self.assertFalse(weak["available"])

    def test_native_backend_writes_jsonl_csv_and_token_provenance(self):
        class FakeClient:
            def call(self, **_kwargs):
                return ObservedModelResponse(
                    text="READY",
                    request_id="fake-1",
                    http_status=200,
                    finish_reason="stop",
                    input_tokens=8,
                    output_tokens=1,
                    token_source="api_usage",
                    ttft_ms=2.0,
                    latency_ms=5.0,
                    chunks=1,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = NativeEvaluationBackend(client=FakeClient())
            events = []

            def record_event(event):
                assert_secret_free(event.data, path="model_eval_event.data")
                events.append(event)

            result = backend.run(
                EvaluationRequest(
                    run_id="native-run",
                    project_id="project-a",
                    mode="eval",
                    task_config={
                        "api_url": "https://model.example.test/v1",
                        "model": "test-model",
                        "cases": [
                            {
                                "id": "case-1",
                                "category": "instruction",
                                "payload": {
                                    "messages": [{"role": "user", "content": "只回复 READY"}],
                                    "rules": {"exact_text": "READY"},
                                },
                            }
                        ],
                    },
                    work_dir=root,
                ),
                on_event=record_event,
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.summary["success_rate"], 100.0)
            self.assertTrue(result.summary["token_usage"]["exact"])
            progress_event = next(event for event in events if event.event_type == "progress")
            self.assertEqual(progress_event.data["output_tokens_per_second"], 200.0)
            self.assertEqual(progress_event.data["case_result"]["case_id"], "case-1")
            self.assertEqual(progress_event.data["partial_summary"]["completed_cases"], 1)
            self.assertTrue((root / "summary.json").is_file())
            self.assertTrue((root / "responses.jsonl").is_file())
            self.assertTrue((root / "performance_samples.csv").is_file())

    def test_evalscope_mapper_normalizes_perf_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage = root / "model" / "parallel_2_number_4"
            stage.mkdir(parents=True)
            (stage / "benchmark_summary.json").write_text(
                json.dumps(
                    {
                        "Concurrency": 2,
                        "Total Requests": 4,
                        "Success Requests": 3,
                        "Failed Requests": 1,
                        "Req Throughput (req/s)": 2.5,
                        "Output Throughput (tok/s)": 30.0,
                        "Total Throughput (tok/s)": 50.0,
                        "Avg TTFT (ms)": 10.0,
                        "Avg Latency (s)": 0.2,
                    }
                ),
                encoding="utf-8",
            )
            (stage / "benchmark_percentile.json").write_text(
                json.dumps(
                    [
                        {"Percentiles": "50%", "TTFT (ms)": 8.0, "Latency (s)": 0.1},
                        {"Percentiles": "95%", "TTFT (ms)": 15.0, "Latency (s)": 0.3},
                        {"Percentiles": "99%", "TTFT (ms)": 18.0, "Latency (s)": 0.4},
                    ]
                ),
                encoding="utf-8",
            )
            mapped = EvalScopeResultMapper().map_directory(root)

            self.assertEqual(mapped["summary"]["mode"], "perf")
            self.assertEqual(mapped["summary"]["success_rate"], 75.0)
            self.assertEqual(mapped["summary"]["performance"]["ttft_p95_ms"], 15.0)
            self.assertTrue((root / "summary.json").is_file())
            self.assertTrue((root / "performance_samples.csv").is_file())

    def test_evalscope_mapper_normalizes_open_loop_rate_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage = root / "model" / "rate_4.0_number_16"
            stage.mkdir(parents=True)
            (stage / "benchmark_summary.json").write_text(
                json.dumps(
                    {
                        "Concurrency": -1,
                        "Request Rate (req/s)": 4.0,
                        "Total Requests": 16,
                        "Success Requests": 16,
                        "Failed Requests": 0,
                        "Req Throughput (req/s)": 3.9,
                        "Output Throughput (tok/s)": 48.0,
                        "Avg TTFT (ms)": 12.0,
                        "Avg Latency (s)": 0.25,
                    }
                ),
                encoding="utf-8",
            )

            mapped = EvalScopeResultMapper().map_directory(root)

            self.assertIsNone(mapped["summary"]["max_concurrency"])
            self.assertEqual(mapped["summary"]["performance"]["stages"][0]["target_rps"], 4.0)


class EvaluationMvpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = PlatformStore(root / "platform.db", recover_jobs=False)
        self.app = FastAPI()
        self.artifact_storage = LocalArtifactStorage(root / "artifacts")
        platform_router, report_manager, _, _ = create_platform_api(
            TaskStore(root / "tasks.db"),
            platform_store=self.store,
            artifact_storage=self.artifact_storage,
        )
        self.evaluation_manager = report_manager.model_evaluation_manager
        identity_router, service = create_identity_api(self.store)
        self.app.include_router(platform_router)
        self.app.include_router(identity_router)
        install_identity_guard(self.app, service)
        self.client = TestClient(self.app)
        identity = self.client.post(
            "/api/auth/setup",
            json={
                "username": "admin.evaluation",
                "display_name": "评测管理员",
                "password": ADMIN_PASSWORD,
                "project_key": "EVALUATION",
                "project_name": "模型评测项目",
            },
        )
        self.assertEqual(identity.status_code, 201, identity.text)
        self.identity = identity.json()
        self.headers = {"X-CSRF-Token": self.identity["csrf_token"]}

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def test_mock_run_api_monitor_results_artifacts_and_project_boundary(self):
        with patch.dict("os.environ", {"LIEMA_EVALSCOPE_PYTHON": ""}):
            bootstrap_response = self.client.get("/api/model-evaluation/bootstrap")
        self.assertEqual(bootstrap_response.status_code, 200, bootstrap_response.text)
        bootstrap_payload = bootstrap_response.json()
        self.assertEqual(len(bootstrap_payload["suites"]), 8)
        self.assertFalse(bootstrap_payload["runtime"]["evalscope_configured"])

        submitted = self.client.post(
            "/api/model-evaluation/runs",
            headers=self.headers,
            json={"run_kind": "mock", "plan": "quick"},
        )
        self.assertEqual(submitted.status_code, 202, submitted.text)
        run_id = submitted.json()["id"]
        finished = self.evaluation_manager.run_once()
        self.assertEqual(finished["status"], "completed")

        detail = self.client.get(f"/api/model-evaluation/runs/{run_id}")
        results = self.client.get(f"/api/model-evaluation/runs/{run_id}/results")
        events = self.client.get(f"/api/model-evaluation/runs/{run_id}/events")
        summary = self.client.get(
            f"/api/model-evaluation/runs/{run_id}/artifacts/summary"
        )
        performance = self.client.get(
            f"/api/model-evaluation/runs/{run_id}/artifacts/performance"
        )
        readable_report = self.client.get(
            f"/api/model-evaluation/runs/{run_id}/report"
        )
        report_docx = self.client.get(
            f"/api/model-evaluation/runs/{run_id}/report/docx"
        )
        report_pdf = self.client.get(
            f"/api/model-evaluation/runs/{run_id}/report/pdf"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(results.status_code, 200, results.text)
        self.assertEqual(results.json()["total"], 10)
        self.assertGreaterEqual(len(events.json()["events"]), 3)
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(performance.status_code, 200, performance.text)
        self.assertEqual(readable_report.status_code, 200, readable_report.text)
        self.assertIn(
            "不代表任何真实模型",
            readable_report.json()["report"]["conclusion"]["summary"],
        )
        self.assertEqual(report_docx.status_code, 200, report_docx.text)
        self.assertTrue(report_docx.content.startswith(b"PK"))
        self.assertEqual(report_pdf.status_code, 200, report_pdf.text)
        self.assertTrue(report_pdf.content.startswith(b"%PDF"))
        self.assertNotIn("api_key_enc", detail.text)
        artifact_path = self.artifact_storage.resolve(detail.json()["artifact_ref"])
        self.assertTrue(artifact_path.is_dir())
        self.assertTrue((artifact_path / "model_evaluation_report.docx").is_file())
        self.assertTrue((artifact_path / "model_evaluation_report.pdf").is_file())

        other = self.store.create_project("OTHER-EVAL", "其他评测项目")
        hidden = self.client.get(
            f"/api/model-evaluation/runs/{run_id}",
            headers={"X-Project-ID": other["id"]},
        )
        self.assertEqual(hidden.status_code, 404)
        hidden_delete = self.client.delete(
            f"/api/model-evaluation/runs/{run_id}",
            headers={**self.headers, "X-Project-ID": other["id"]},
        )
        self.assertEqual(hidden_delete.status_code, 404)

        deleted = self.client.delete(
            f"/api/model-evaluation/runs/{run_id}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(artifact_path.exists())
        self.assertEqual(
            self.client.get(f"/api/model-evaluation/runs/{run_id}").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/model-evaluation/runs/{run_id}/events").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/model-evaluation/runs/{run_id}/results").status_code,
            404,
        )

    def test_queued_run_can_be_stopped(self):
        submitted = self.client.post(
            "/api/model-evaluation/runs",
            headers=self.headers,
            json={"run_kind": "mock", "plan": "quick"},
        )
        run_id = submitted.json()["id"]
        rejected = self.client.delete(
            f"/api/model-evaluation/runs/{run_id}", headers=self.headers
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        report_rejected = self.client.get(
            f"/api/model-evaluation/runs/{run_id}/report"
        )
        self.assertEqual(report_rejected.status_code, 409, report_rejected.text)
        stopped = self.client.post(
            f"/api/model-evaluation/runs/{run_id}/stop", headers=self.headers
        )
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["status"], "stopped")
        deleted = self.client.delete(
            f"/api/model-evaluation/runs/{run_id}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_report_analysis_protocol_failure_retry_and_downloads_keep_same_measurements(self):
        from tests.test_model_evaluation_report import ModelEvaluationReportTests
        submitted = self.client.post("/api/model-evaluation/runs", headers=self.headers, json={"run_kind": "mock", "plan": "quick"})
        run_id = submitted.json()["id"]
        self.evaluation_manager.run_once()
        url = f"/api/model-evaluation/runs/{run_id}/report"
        original = self.client.get(url).json()["report"]
        profile = {"id": "fixture", "base_url": "https://model.test", "model_name": "fixture", "api_key": "private-test-secret"}
        bad_gateway = Mock(status_code=200)
        bad_gateway.json.side_effect = json.JSONDecodeError("private-test-secret", "<html>", 0)
        valid = ModelEvaluationReportTests.analysis_response(original)
        responses = [bad_gateway,
                     Mock(status_code=200, json=Mock(return_value={"choices": [{"message": {"content": "格式不正确"}}]})),
                     Mock(status_code=200, json=Mock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": "结果如下：\n```json\n" + json.dumps(valid) + "\n```"}}]}))]
        with patch.object(self.store, "active_model_profile", return_value=profile), patch("auto_test.platform.models.requests.post", side_effect=responses) as post:
            failed = self.client.post(url + "/conclusion", headers=self.headers)
            self.assertEqual(failed.status_code, 200, failed.text)
            self.assertEqual(failed.json()["report"]["analysis"]["error_code"], "invalid_response_json")
            self.assertNotIn("private-test-secret", failed.text)
            self.assertEqual(post.call_count, 1)
            corrected = self.client.post(url + "/conclusion", headers=self.headers)
            self.assertEqual(corrected.status_code, 200, corrected.text)
            report = corrected.json()["report"]
            self.assertEqual(report["analysis"]["status"], "completed")
            self.assertEqual(report["analysis"]["generation"]["attempts"], 2)
            for key in ("conclusion", "metrics", "cases", "assessment_evidence"):
                self.assertEqual(report[key], original[key])
            self.assertEqual(self.client.get(url).json()["report"]["analysis"], report["analysis"])
            for extension, signature in (("pdf", b"%PDF"), ("docx", b"PK")):
                download = self.client.get(url + "/" + extension)
                self.assertEqual(download.status_code, 200)
                self.assertTrue(download.content.startswith(signature))
            self.assertEqual(post.call_count, 3)

    def test_report_analysis_requires_explicit_post_csrf_and_current_project(self):
        submitted = self.client.post("/api/model-evaluation/runs", headers=self.headers, json={"run_kind": "mock", "plan": "quick"})
        run_id = submitted.json()["id"]
        self.evaluation_manager.run_once()
        url = f"/api/model-evaluation/runs/{run_id}/report"
        with patch("auto_test.platform.models.call_model") as call:
            viewed = self.client.get(url)
            self.assertEqual(viewed.status_code, 200)
            call.assert_not_called()
            self.assertEqual(self.client.post(url + "/conclusion").status_code, 403)
            generated = self.client.post(url + "/conclusion", headers=self.headers)
            self.assertEqual(generated.status_code, 200, generated.text)
            self.assertEqual(generated.json()["report"]["analysis"]["status"], "unavailable")
            call.assert_not_called()
        from auto_test.platform.identity import _required_permission
        self.assertEqual(_required_permission("POST", url + "/conclusion"), "report:manage")
        project = self.store.create_project("OTHERREPORT", "另一项目")
        response = self.client.post(url + "/conclusion", headers={**self.headers, "X-Project-ID": project["id"]})
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
