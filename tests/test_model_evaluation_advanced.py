import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from tests import bootstrap  # noqa: F401

from auto_test.evaluation.advanced import (
    analyze_capacity,
    build_run_comparison,
    correlate_resources,
    merge_rule_and_judge_score,
    parse_evaluation_import,
    parse_judge_response,
    select_manual_review_indices,
    validate_evaluation_cases,
)
from auto_test.evaluation.catalog import ensure_builtin_evaluation_suites
from auto_test.evaluation.presets import build_task_configuration
from auto_test.evaluation.scoring import score_response
from auto_test.platform.api import create_platform_api
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.identity import create_identity_api, install_identity_guard
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


ADMIN_PASSWORD = "StrongAdmin!2026"


def sample_case(case_id="case-1"):
    return {
        "id": case_id,
        "case_key": case_id,
        "category": "custom",
        "payload": {
            "name": "项目验收用例",
            "messages": [{"role": "user", "content": "只回复 READY"}],
            "rules": {"exact_text": "READY"},
        },
    }


class AdvancedEvaluationDomainTests(unittest.TestCase):
    def test_builtin_capability_packs_are_versioned_and_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = PlatformStore(Path(temp_dir) / "platform.db", recover_jobs=False)
            suites = ensure_builtin_evaluation_suites(store)
            by_category = {item["category"]: item for item in suites}

            self.assertEqual(len(suites), 8)
            self.assertGreaterEqual(by_category["translation"]["case_count"], 200)
            self.assertGreaterEqual(by_category["report_writing"]["case_count"], 8)
            self.assertGreaterEqual(by_category["intelligence"]["case_count"], 8)
            self.assertEqual(sum(item["case_count"] for item in suites), 270)

    def test_rule_judge_and_manual_review_boundaries(self):
        rules = {
            "required_terms": ["Beichen"],
            "required_entities": ["Atlas"],
            "required_numbers": ["2026-09-02", "99%"],
            "required_units": ["ms"],
            "protected_spans": ["{{owner}}"],
            "required_format": ["`status=ready`"],
            "required_sections": ["事实", "风险", "建议"],
            "references": ["Beichen Atlas 2026-09-02 99% 12 ms {{owner}} `status=ready`"],
            "min_reference_similarity": 0.2,
        }
        response = (
            "## 事实\nBeichen 与 Atlas 于 2026-09-02 达到 99%，耗时 12 ms，"
            "保留 {{owner}} 和 `status=ready`。\n## 风险\n暂无。\n## 建议\n继续复核。"
        )
        score = score_response(response, rules)
        self.assertTrue(score["passed"])
        judge = parse_judge_response(
            '```json\n{"score":0.9,"confidence":0.8,"reason":"有依据","rubric":[]}\n```',
            ["忠实度"],
        )
        self.assertEqual(judge["status"], "completed")
        merged = merge_rule_and_judge_score(score, judge)
        self.assertTrue(merged["passed"])
        self.assertEqual(merged["scoring_source"], "rules_and_independent_judge")

        deterministic_failure = score_response("完全错误", {"exact_text": "READY"})
        guarded = merge_rule_and_judge_score(
            deterministic_failure,
            {"status": "completed", "score": 1.0, "confidence": 1.0},
        )
        self.assertFalse(guarded["passed"])
        self.assertEqual(guarded["score"], 0.0)

        self.assertEqual(select_manual_review_indices(24, 25), {0, 5, 9, 14, 18, 23})
        self.assertEqual(select_manual_review_indices(24, 0), set())

    def test_import_jsonl_csv_documents_and_safe_zip(self):
        jsonl = (
            json.dumps({"case_key": "a", "prompt": "只回复 A", "rules": {"exact_text": "A"}}, ensure_ascii=False)
            + "\n"
            + json.dumps({"case_key": "b", "prompt": "只回复 B", "rules": {"exact_text": "B"}}, ensure_ascii=False)
        ).encode("utf-8")
        self.assertEqual(len(parse_evaluation_import("cases.jsonl", jsonl)), 2)
        csv_data = "case_key,prompt,reference,required_terms\nc,翻译 service,服务,服务\n".encode("utf-8")
        self.assertEqual(parse_evaluation_import("cases.csv", csv_data)[0]["case_key"], "c")
        self.assertEqual(parse_evaluation_import("material.md", "事实\n风险\n建议".encode("utf-8"))[0]["category"], "report_writing")

        docx_buffer = io.BytesIO()
        document = Document()
        document.add_paragraph("事实：测试通过。风险：暂无。建议：复核。")
        document.save(docx_buffer)
        self.assertEqual(len(parse_evaluation_import("material.docx", docx_buffer.getvalue())), 1)

        pdf_buffer = io.BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(50, 800, "Facts, risks and recommendations")
        pdf.save()
        self.assertEqual(len(parse_evaluation_import("material.pdf", pdf_buffer.getvalue())), 1)

        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("suite/cases.jsonl", jsonl)
        self.assertEqual(len(parse_evaluation_import("suite.zip", archive_buffer.getvalue())), 2)

        unsafe_buffer = io.BytesIO()
        with zipfile.ZipFile(unsafe_buffer, "w") as archive:
            archive.writestr("../cases.jsonl", jsonl)
        with self.assertRaisesRegex(ValueError, "不安全路径"):
            parse_evaluation_import("unsafe.zip", unsafe_buffer.getvalue())

        duplicate = [
            {"case_key": "same", "prompt": "A"},
            {"case_key": "same", "prompt": "B"},
        ]
        validation = validate_evaluation_cases(duplicate)
        self.assertFalse(validation["valid"])
        self.assertIn("重复", validation["errors"][0])

    def test_presets_capacity_resources_and_comparison_contract(self):
        profile = {
            "id": "model-a",
            "base_url": "https://model.example.test/v1",
            "model_name": "model-a",
            "temperature": 0.2,
        }
        judge = {
            "id": "judge-b",
            "base_url": "https://judge.example.test/v1",
            "model_name": "judge-b",
        }
        backend, _, mode, task = build_task_configuration(
            run_kind="translation",
            plan="deep",
            profile=profile,
            cases=[sample_case(str(index)) for index in range(250)],
            stream=True,
            max_tokens=512,
            timeout=30,
            judge_profile=judge,
        )
        self.assertEqual((backend, mode), ("native", "eval"))
        self.assertEqual(len(task["cases"]), 200)
        self.assertEqual(task["judge"]["profile_id"], "judge-b")

        perf = build_task_configuration(
            run_kind="deep_performance",
            plan="deep",
            profile=profile,
            cases=[sample_case()],
            stream=False,
            max_tokens=128,
            timeout=30,
        )[3]
        self.assertEqual(perf["parallel"], [1])
        self.assertTrue(perf["open_loop"])
        self.assertEqual(perf["rate"], [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(perf["number"], [60, 120, 240, 480])
        self.assertEqual(perf["duration"], 60)
        self.assertEqual(perf["_load_profile"]["recovery_observation_seconds"], 60)

        stages = [
            {"concurrency": 1, "request_throughput": 1.0, "success_rate": 100},
            {"concurrency": 2, "request_throughput": 1.9, "success_rate": 100},
            {"concurrency": 4, "request_throughput": 2.0, "success_rate": 90},
        ]
        capacity = analyze_capacity(stages)
        self.assertEqual(capacity["max_stable_concurrency"], 2)
        self.assertEqual(capacity["capacity_knee"]["concurrency"], 4)
        correlation = correlate_resources(
            [
                {"request_throughput": 1, "cpu_percent": 10, "gpu_percent": 20, "memory_percent": 30, "gpu_memory_percent": 40},
                {"request_throughput": 2, "cpu_percent": 20, "gpu_percent": 30, "memory_percent": 40, "gpu_memory_percent": 50},
                {"request_throughput": 3, "cpu_percent": 30, "gpu_percent": 40, "memory_percent": 50, "gpu_memory_percent": 60},
            ]
        )
        self.assertTrue(correlation["available"])
        self.assertEqual(correlation["throughput_correlations"]["cpu_percent"], 1.0)

        base_snapshot = {
            "suite": {"content_sha256": "a" * 64},
            "plan": "quick",
            "parameters": {"max_tokens": 128},
            "model": {"name": "A"},
            "run_kind": "translation",
        }
        runs = [
            {"id": "a", "status": "completed", "snapshot": base_snapshot, "summary": {"quality_score": 95, "success_rate": 100, "performance": {"latency_p95_ms": 20}}},
            {"id": "b", "status": "completed", "snapshot": {**base_snapshot, "model": {"name": "B"}}, "summary": {"quality_score": 90, "success_rate": 100, "performance": {"latency_p95_ms": 10}}},
        ]
        comparison = build_run_comparison(runs)
        self.assertTrue(comparison["comparable"])
        self.assertEqual(comparison["ranking"][0]["run_id"], "a")
        runs[1]["snapshot"] = {**runs[1]["snapshot"], "plan": "deep"}
        self.assertFalse(build_run_comparison(runs)["comparable"])


class AdvancedEvaluationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = PlatformStore(root / "platform.db", recover_jobs=False)
        self.app = FastAPI()
        router, report_manager, _, _ = create_platform_api(
            TaskStore(root / "tasks.db"),
            platform_store=self.store,
            artifact_storage=LocalArtifactStorage(root / "artifacts"),
        )
        self.manager = report_manager.model_evaluation_manager
        identity_router, service = create_identity_api(self.store)
        self.app.include_router(router)
        self.app.include_router(identity_router)
        install_identity_guard(self.app, service)
        self.client = TestClient(self.app)
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "username": "admin.advanced",
                "display_name": "进阶评测管理员",
                "password": ADMIN_PASSWORD,
                "project_key": "ADVANCED-EVAL",
                "project_name": "进阶模型评测",
            },
        )
        self.assertEqual(setup.status_code, 201, setup.text)
        self.identity = setup.json()
        self.headers = {"X-CSRF-Token": self.identity["csrf_token"]}

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def _run_mock_full(self):
        response = self.client.post(
            "/api/model-evaluation/runs",
            headers=self.headers,
            json={"run_kind": "mock_full", "plan": "standard", "manual_review_percent": 25},
        )
        self.assertEqual(response.status_code, 202, response.text)
        finished = self.manager.run_once()
        self.assertEqual(finished["status"], "completed")
        return response.json()["id"]

    def test_project_suite_import_publish_and_isolation(self):
        created = self.client.post(
            "/api/model-evaluation/suites",
            headers=self.headers,
            json={"name": "项目脱敏验收集", "category": "custom", "description": "仅含脱敏素材"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        suite_id = created.json()["id"]
        payload = json.dumps(
            {"case_key": "project-1", "prompt": "只回复 READY", "rules": {"exact_text": "READY"}},
            ensure_ascii=False,
        ).encode("utf-8")
        imported = self.client.post(
            f"/api/model-evaluation/suites/{suite_id}/import",
            headers=self.headers,
            files={"file": ("cases.jsonl", payload, "application/x-ndjson")},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.json()["validation"]["total"], 1)
        listed = self.client.get("/api/model-evaluation/suites")
        suite = next(item for item in listed.json()["suites"] if item["id"] == suite_id)
        self.assertEqual(suite["latest_version"]["case_count"], 1)

        other = self.store.create_project("OTHER-ADVANCED", "其他项目")
        hidden = self.client.get(
            f"/api/model-evaluation/suites/{suite_id}/versions/{suite['latest_version']['id']}",
            headers={"X-Project-ID": other["id"]},
        )
        self.assertEqual(hidden.status_code, 404)

    def test_full_mock_review_comparison_and_mismatch_confirmation(self):
        first_id = self._run_mock_full()
        first_detail = self.client.get(f"/api/model-evaluation/runs/{first_id}").json()
        self.assertEqual(first_detail["summary"]["total_cases"], 24)
        self.assertEqual(first_detail["summary"]["scoring"]["pending_manual_review_cases"], 6)
        self.assertIn("translation_zh_en", first_detail["summary"]["dimensions"])
        results = self.client.get(f"/api/model-evaluation/runs/{first_id}/results?page_size=200").json()["results"]
        pending = next(item for item in results if item["score"]["manual_review"]["status"] == "pending")
        reviewed = self.client.post(
            f"/api/model-evaluation/runs/{first_id}/results/{pending['id']}/reviews",
            headers=self.headers,
            json={"score": 92, "comment": "事实、格式与译文均符合验收要求", "rubric": {"事实覆盖": 95}},
        )
        self.assertEqual(reviewed.status_code, 201, reviewed.text)
        after_review = self.client.get(f"/api/model-evaluation/runs/{first_id}").json()
        self.assertEqual(after_review["summary"]["scoring"]["manual_reviewed_cases"], 1)
        self.assertEqual(after_review["summary"]["scoring"]["pending_manual_review_cases"], 5)
        reviews = self.client.get(f"/api/model-evaluation/runs/{first_id}/reviews").json()["reviews"]
        self.assertEqual(len(reviews), 1)

        second_id = self._run_mock_full()
        compared = self.client.post(
            "/api/model-evaluation/comparisons",
            headers=self.headers,
            json={"run_ids": [first_id, second_id]},
        )
        self.assertEqual(compared.status_code, 201, compared.text)
        self.assertTrue(compared.json()["result"]["comparable"])
        self.assertEqual(len(compared.json()["result"]["ranking"]), 2)

        basic = self.client.post(
            "/api/model-evaluation/runs",
            headers=self.headers,
            json={"run_kind": "mock", "plan": "quick"},
        )
        self.assertEqual(basic.status_code, 202, basic.text)
        self.manager.run_once()
        mismatch = self.client.post(
            "/api/model-evaluation/comparisons",
            headers=self.headers,
            json={"run_ids": [first_id, basic.json()["id"]]},
        )
        self.assertEqual(mismatch.status_code, 409, mismatch.text)
        confirmed = self.client.post(
            "/api/model-evaluation/comparisons",
            headers=self.headers,
            json={"run_ids": [first_id, basic.json()["id"]], "allow_mismatch": True},
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        self.assertFalse(confirmed.json()["result"]["comparable"])
        self.assertEqual(confirmed.json()["result"]["ranking"], [])


if __name__ == "__main__":
    unittest.main()
