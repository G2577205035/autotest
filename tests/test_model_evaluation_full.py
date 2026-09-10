import io
import json
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests import bootstrap  # noqa: F401
from tests import test_model_evaluation_mvp as mvp
from auto_test.evaluation.backends.full import FullEvaluationBackend
from auto_test.evaluation.contracts import BackendEvent, BackendResult, EvaluationRequest, assert_secret_free
from auto_test.evaluation.full import build_full_configuration
from auto_test.evaluation.resources import align_stage_resources, EvaluationResourceSampler
from auto_test.platform.store import PlatformStore
from auto_test.reporting.model_evaluation import build_model_evaluation_report, generate_model_evaluation_report
from auto_test.reporting.model_evaluation_full import valid_rubric, build_evidence_archive


PROFILE = {"id": "model", "model_name": "test-model", "base_url": "http://127.0.0.1:19091/v1", "temperature": 0.3}


class FullEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PlatformStore(self.root / "store.db", recover_jobs=False)
        self.project = self.store.create_project("FULL", "全量测试")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, **kwargs):
        return build_full_configuration(self.store, self.project, profile=PROFILE, **kwargs)

    def test_bundle_covers_all_real_dimensions_and_locks_provenance(self):
        suite, version, config = self.bundle()
        self.assertEqual(len(config["stages"]), 9)
        self.assertEqual([s["stream"] for s in config["stages"][:2]], [False, True])
        self.assertEqual(sum(s["expected_cases"] for s in config["stages"]), 244)
        self.assertEqual(len(next(s for s in config["stages"] if s["id"] == "translation")["task_config"]["cases"]), 200)
        self.assertEqual(len({c["id"] for c in config["cases"]}), len(config["cases"]))
        for stage in config["stages"]:
            self.assertIn(stage["backend"], {"native", "evalscope"})
            self.assertEqual(len(stage["suite"]["content_sha256"]), 64)
        self.assertEqual(config["stages"][-1]["task_config"]["_safety"]["max_concurrency"], 16)
        self.assertEqual(config["stages"][-1]["task_config"]["max_tokens"], 512)
        self.assertEqual(config, self.bundle()[2])
        other = build_full_configuration(self.store, self.project, profile={**PROFILE, "model_name": "other"})
        self.assertEqual(version["content_sha256"], other[1]["content_sha256"])
        self.assertNotEqual(config["bundle_sha256"], other[2]["bundle_sha256"])
        assert_secret_free(config)

    def test_custom_bundle_preserves_all_rows_and_rejects_other_project(self):
        suite = self.store.save_model_eval_suite(self.project, {"name": "大测试集", "category": "custom"})
        version = self.store.publish_model_eval_suite_version(self.project, suite["id"], {}, [{"case_key": f"case-{n}", "payload": {"messages": [{"role": "user", "content": "Test"}], "rules": {"min_chars": 1}}} for n in range(1003)])
        config = self.bundle(suite_version_id=version["id"])[2]
        self.assertEqual(len(next(s for s in config["stages"] if s["id"] == "custom")["task_config"]["cases"]), 1003)
        other = self.store.create_project("OTHERFULL", "另一项目")["id"]
        with self.assertRaises(ValueError):
            build_full_configuration(self.store, other, profile=PROFILE, suite_version_id=version["id"])

    def test_sequential_results_do_not_overwrite_or_end_parent_early(self):
        config = self.bundle()[2]
        config["stages"] = config["stages"][:2]
        for stage in config["stages"]:
            stage["expected_cases"] = 1
        events, calls = [], []
        class Backend:
            def run(inner, request, on_event, should_stop):
                calls.append(request.task_config["stream"])
                case = {"case_id": request.task_config["cases"][0]["id"], "status": "passed", "score": {"score": 1}, "metrics": {"latency_ms": 12}}
                on_event(BackendEvent("progress", "一条已完成", "running", 50, {"case_result": case, "partial_summary": {"completed_cases": 1}}))
                on_event(BackendEvent("completed", "单项结束", "completed", 100))
                return BackendResult("completed", {"completed_cases": 1}, raw={"cases": [case]})
            def stop(inner, run_id):
                return True
        result = FullEvaluationBackend(lambda _: Backend()).run(EvaluationRequest("run", self.project, "eval", config, self.root / "full"), on_event=events.append)
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, [False, True])
        self.assertEqual(len(result.raw["cases"]), 2)
        self.assertEqual(len({r["case_id"] for r in result.raw["cases"]}), 2)
        self.assertTrue(all(e.phase == "running" and e.progress < 100 for e in events))
        self.assertEqual(result.summary["total_cases"], 2)
        self.assertEqual(result.summary["completed_cases"], 2)
        self.assertTrue((self.root / "full" / "full_evidence.json").is_file())

    def test_stop_and_perf_failure_preserve_partial_evidence(self):
        config = self.bundle()[2]
        calls = []
        backend = FullEvaluationBackend(lambda _: None)
        class StopBackend:
            def run(inner, request, on_event, should_stop):
                calls.append(request.work_dir.name)
                backend.stop(request.run_id)
                return BackendResult("stopped", {"completed_cases": 0})
            def stop(inner, run_id):
                return True
        backend.resolver = lambda _: StopBackend()
        result = backend.run(EvaluationRequest("stop", self.project, "eval", config, self.root / "stop"))
        self.assertEqual(result.status, "stopped")
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(s["status"] == "skipped" for s in result.summary["stages"]), 8)
        self.assertTrue((self.root / "stop" / "summary.json").is_file())
        class FailBackend:
            def run(inner, request, **kwargs):
                raise RuntimeError("must not persist credential-bearing exception text")
        config["stages"] = config["stages"][-2:]
        failed = FullEvaluationBackend(lambda _: FailBackend()).run(EvaluationRequest("failed", self.project, "eval", config, self.root / "failed"))
        self.assertEqual(failed.status, "failed")
        self.assertEqual([s["status"] for s in failed.summary["stages"]], ["failed", "skipped"])
        self.assertNotIn("credential-bearing", (self.root / "failed" / "summary.json").read_text(encoding="utf-8"))

    def test_resource_matching_separates_equal_loads_across_components(self):
        rows = [{"sampled_at": 10, "cpu_percent": 0, "memory_percent": None, "gpu_percent": 50, "gpu_memory_percent": 70}]
        stages = [{"evaluation_stage": "concurrency", "phase": "capacity", "concurrency": 16, "target_rps": None, "request_throughput": 3}, {"evaluation_stage": "deep_performance", "phase": "capacity", "concurrency": 16, "target_rps": 1, "request_throughput": 1}]
        execution = [{"evaluation_stage": "deep_performance", "phase": "capacity", "parallel": 16, "rate": 1, "started_at": 0, "finished_at": 20}]
        aligned = align_stage_resources(rows, stages, execution)
        self.assertEqual(aligned[0]["request_throughput"], 1)
        self.assertEqual(aligned[0]["cpu_percent"], 0)
        self.assertIsNone(aligned[0]["memory_percent"])
        sampler = EvaluationResourceSampler(self.store, {}, self.root)
        sampler.samples = rows
        data = sampler.finish({"performance": {"stages": stages}, "performance_execution": {"stages": execution}})
        self.assertEqual(data["statistics"]["cpu_percent"]["mean"], 0)
        self.assertIsNone(data["statistics"]["memory_percent"]["max"])

    def test_report_exposes_every_dimension_missing_judge_and_all_perf_metrics(self):
        suite, version, config = self.bundle()
        run = {"id": "full-report", "status": "failed", "snapshot": {"run_kind": "full", "plan": "deep", "model": PROFILE, "suite": {**suite, **version}, "task_config": config}, "summary": {"stages": [], "performance": {"stages": [{"phase": "recovery", "evaluation_stage": "deep_performance", "concurrency": 1, "latency_p50_ms": 123, "latency_p95_ms": 456, "latency_p99_ms": 789, "ttft_p99_ms": 321, "total_token_throughput": 654}]}}}
        report = build_model_evaluation_report(run, [])
        body = json.dumps(report, ensure_ascii=False)
        for text in ("123", "456", "789", "321", "654", "主观裁判有效结果不足", "流式", "BLEU", "全量规则指标", "未采集", "全部性能阶段", "Temperature", "SHA-256"):
            self.assertIn(text, body)
        self.assertIsNone(report["quality_score"])
        self.assertEqual(report["conclusion"]["level"], "risk")
        self.assertEqual(report["assessment_decision"]["code"], "insufficient")
        self.assertGreaterEqual(len(report["sections"]), 21)
        generated = generate_model_evaluation_report(run, [], self.root / "report")
        from docx import Document
        from pypdf import PdfReader
        doc = Document(generated["docx_path"])
        doc_text = "\n".join(p.text for p in doc.paragraphs)
        pdf_text = "\n".join(p.extract_text() or "" for p in PdfReader(generated["pdf_path"]).pages)
        for section in report["sections"]:
            self.assertIn(section["title"], doc_text)
            self.assertIn(section["title"], pdf_text)

    def test_rubric_validation_rejects_missing_wrong_and_nonfinite_scores(self):
        self.assertEqual(valid_rubric({"score": 1, "rubric": []}, ["忠实度"]), {})
        self.assertEqual(valid_rubric({"rubric": [{"name": "忠实度", "score": 0}, {"name": "虚构维度", "score": 1}, {"name": "其他", "score": float("nan")}]}, ["忠实度", "其他"]), {"忠实度": 0})

    def test_report_orders_historical_stages_and_names_actual_benchmark(self):
        suite, version, config = self.bundle()
        row = {"evaluation_stage": "deep_performance", "phase": "capacity", "concurrency": 16, "target_rps": 2, "request_throughput": 2}
        recovery = {**row, "phase": "recovery", "concurrency": 1, "target_rps": 1, "request_throughput": 1}
        windows = [{"evaluation_stage": "deep_performance", "phase": "capacity", "parallel": 16, "rate": 2}, {"evaluation_stage": "deep_performance", "phase": "recovery", "parallel": 1, "rate": 1}]
        run = {"status": "completed", "snapshot": {"run_kind": "full", "task_config": config, "suite": {**suite, **version}}, "summary": {"stages": [{"id": "standard_benchmark", "status": "completed", "summary": {"benchmarks": [{"primary_metric": {"name": "rouge", "dimensions": {"variant": "l", "statistic": "recall"}}, "score": 0.7654}]}}], "performance": {"stages": [recovery, row]}, "performance_execution": {"stages": windows}}}
        report = build_model_evaluation_report(run, [])
        table = next(s for s in report["sections"] if s["title"].startswith("全部性能阶段：负载"))
        self.assertIn("capacity", table["rows"][0][0])
        self.assertIn("recovery", table["rows"][1][0])
        benchmark = next(s for s in report["sections"] if s["title"] == "标准适配器指标")
        self.assertIn("ROUGE-L 召回", benchmark["rows"][0][3])
        self.assertEqual(benchmark["rows"][0][4], "0.7654")
        sample = {"case_id": "standard_benchmark:adapter-1", "status": "completed", "metrics": {"evaluation_stage": "standard_benchmark"}, "score": {"score": 1}}
        report = build_model_evaluation_report(run, [sample])
        self.assertEqual(report["cases"][0]["status_text"], "调用成功")
        self.assertIsNone(report["cases"][0]["score"])
        self.assertIn("全部明细 1 条", report["case_summary_text"])

    def test_evidence_includes_current_reviews_but_excludes_unrelated_files(self):
        (self.root / "summary.json").write_text('{}')
        (self.root / "not_for_export.txt").write_text("private")
        archive = build_evidence_archive(self.root, {"id": "full-run"}, [{"score": {"manual_review": {"comment": "最新人工意见"}}}])
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            self.assertNotIn("not_for_export.txt", z.namelist())
            self.assertIn("最新人工意见", z.read("reviewed_results.json").decode())
            self.assertIn("manifest.json", z.namelist())


class FullEvaluationApiTests(unittest.TestCase):
    setUp = mvp.EvaluationMvpApiTests.setUp
    tearDown = mvp.EvaluationMvpApiTests.tearDown

    def test_full_api_queues_deep_bundle_and_enforces_project_boundary(self):
        model = self.store.save_model_profile({**PROFILE, "name": "测试模型", "provider": "openai-compatible"}, api_key_enc=None)
        response = self.client.post("/api/model-evaluation/runs", headers=self.headers, json={"run_kind": "full", "plan": "quick", "model_profile_id": model["id"], "max_tokens": 4096})
        self.assertEqual(response.status_code, 202, response.text)
        run = response.json()
        self.assertEqual(run["snapshot"]["plan"], "deep")
        self.assertEqual(run["backend"], "full")
        self.assertEqual(len(run["snapshot"]["task_config"]["stages"]), 9)
        self.assertNotIn("api_key_enc", response.text)
        other = self.store.create_project("HIDDENFULL", "其他项目")
        for endpoint in ("report", "artifacts/evidence", "results"):
            hidden = self.client.get(f"/api/model-evaluation/runs/{run['id']}/{endpoint}", headers={"X-Project-ID": other["id"]})
            self.assertEqual(hidden.status_code, 404, hidden.text)

    def test_full_api_saved_resource_target_is_explicit(self):
        model = self.store.save_model_profile({**PROFILE, "name": "测试模型", "provider": "openai-compatible"}, api_key_enc=None)
        payload = {"run_kind": "full", "model_profile_id": model["id"], "server_profile_id": "nonexistent"}
        response = self.client.post("/api/model-evaluation/runs", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 404)

    def test_report_export_includes_rows_beyond_first_database_page(self):
        queued = self.client.post("/api/model-evaluation/runs", headers=self.headers, json={"run_kind": "mock"}).json()
        project, identity = queued["project_id"], queued["id"]
        folder = self.artifact_storage.workspace("model-evaluations", project, identity)
        folder.mkdir(parents=True, exist_ok=True)
        self.store.save_model_eval_case_results(project, identity, [{"case_id": str(n), "status": "passed", "metrics": {}, "score": {"score": 1}} for n in range(1003)])
        self.store.finish_model_eval_run(project, identity, status="completed", summary={}, artifact_ref=self.artifact_storage.reference(folder))
        captured = []
        def generate(run, results, root):
            captured.extend(results)
            return {"report": {"report_number": "test"}}
        with patch("auto_test.platform.api.generate_model_evaluation_report", side_effect=generate):
            response = self.client.get(f"/api/model-evaluation/runs/{identity}/report")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(captured), 1003)

    def test_artifact_download_prefers_whole_run_over_nested_component(self):
        queued = self.client.post("/api/model-evaluation/runs", headers=self.headers, json={"run_kind": "mock"}).json()
        project, identity = queued["project_id"], queued["id"]
        folder = self.artifact_storage.workspace("model-evaluations", project, identity)
        (folder / "concurrency").mkdir(parents=True)
        (folder / "summary.json").write_text('{"scope":"whole_run"}')
        (folder / "concurrency/summary.json").write_text('{"scope":"component"}')
        self.store.finish_model_eval_run(project, identity, status="completed", summary={}, artifact_ref=self.artifact_storage.reference(folder))
        response = self.client.get(f"/api/model-evaluation/runs/{identity}/artifacts/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scope"], "whole_run")


if __name__ == "__main__":
    unittest.main()
