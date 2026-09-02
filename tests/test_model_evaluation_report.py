import tempfile
import unittest
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from tests import bootstrap  # noqa: F401

from auto_test.reporting.model_evaluation import (
    build_model_evaluation_report,
    generate_model_evaluation_report,
)


def sample_run() -> dict:
    return {
        "id": "run-report-1",
        "status": "completed",
        "created_at": 1788318000,
        "finished_at": 1788318060,
        "snapshot": {
            "run_kind": "mock",
            "plan": "quick",
            "model": {"name": "Deterministic Mock", "provider": "mock"},
            "suite": {"name": "平台基础能力测试集", "version": 1},
            "task_config": {
                "cases": [
                    {
                        "id": "case-1",
                        "case_key": "fixed-expression",
                        "category": "基础能力",
                        "payload": {"name": "固定表达遵循"},
                    },
                    {
                        "id": "case-2",
                        "case_key": "structured-output",
                        "category": "结构化输出",
                        "payload": {"name": "JSON 结构化输出"},
                    },
                ]
            },
        },
        "summary": {
            "total_cases": 2,
            "completed_cases": 2,
            "passed_cases": 2,
            "failed_cases": 0,
            "error_cases": 0,
            "success_rate": 100.0,
            "quality_score": 100.0,
            "token_usage": {
                "input_tokens": 24,
                "output_tokens": 4,
                "total_tokens": 28,
                "source_counts": {"api_usage": 2},
                "exact": True,
            },
            "performance": {
                "latency_p95_ms": 12.0,
                "latency_p99_ms": 14.0,
                "output_tokens_per_second": 84.2,
            },
            "error_types": {},
        },
    }


def sample_results() -> list[dict]:
    return [
        {
            "case_id": "case-1",
            "status": "passed",
            "metrics": {"latency_ms": 10, "ttft_ms": 4, "total_tokens": 12, "token_source": "api_usage"},
            "score": {"score": 1.0},
        },
        {
            "case_id": "case-2",
            "status": "passed",
            "metrics": {"latency_ms": 14, "ttft_ms": 5, "total_tokens": 16, "token_source": "api_usage"},
            "score": {"score": 1.0},
        },
    ]


class ModelEvaluationReportTests(unittest.TestCase):
    def test_readable_report_explains_mock_scope_and_case_names(self):
        report = build_model_evaluation_report(sample_run(), sample_results())

        self.assertEqual(report["report_number"], "ME-20260902-110000")
        self.assertEqual(report["conclusion"]["title"], "评测流程验证完成")
        self.assertIn("不代表任何真实模型", report["conclusion"]["summary"])
        self.assertEqual(report["cases"][0]["name"], "固定表达遵循")
        self.assertEqual(report["metrics"][0]["value"], "100.0%")
        self.assertTrue(any("真实模型" in item for item in report["recommendations"]))

    def test_quality_case_uses_not_met_instead_of_failure_wording(self):
        run = sample_run()
        run["snapshot"]["run_kind"] = "intelligence"
        run["summary"].update(
            {
                "passed_cases": 1,
                "failed_cases": 1,
                "success_rate": 50.0,
                "quality_score": 85.0,
            }
        )
        results = sample_results()
        results[1]["status"] = "failed"
        results[1]["score"]["score"] = 0.7

        report = build_model_evaluation_report(run, results)

        self.assertEqual(report["cases"][0]["status_text"], "达标")
        self.assertEqual(report["cases"][1]["status_text"], "未达标")
        self.assertEqual(report["metrics"][0]["label"], "用例达标率")
        self.assertIn("未达标 1 条", report["metrics"][0]["explanation"])
        self.assertIn("用例达标率", report["conclusion"]["summary"])

    def test_generates_openable_docx_pdf_chart_and_readable_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            generated = generate_model_evaluation_report(
                sample_run(), sample_results(), temp_dir
            )

            for key in ("docx_path", "pdf_path", "chart_path", "json_path"):
                self.assertTrue(Path(generated[key]).is_file(), key)
                self.assertGreater(Path(generated[key]).stat().st_size, 100)

            document = Document(generated["docx_path"])
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            table_text = "\n".join(
                cell.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            )
            self.assertIn("模型评测报告", text)
            self.assertIn("不代表任何真实模型", table_text)
            self.assertIn("固定表达遵循", table_text)
            self.assertGreaterEqual(len(PdfReader(generated["pdf_path"]).pages), 2)
            self.assertIn(
                "评测流程验证完成",
                Path(generated["json_path"]).read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
