import tempfile
from tests import bootstrap  # noqa: F401
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from auto_test.reporting.enterprise import (
    _pdf_font_name,
    build_docx,
    build_report_artifacts,
    collect_snapshot,
)
from auto_test.reporting.charts import CHART_FONT_RENDER_MARKER, CHART_FONT_RENDER_VERSION


class EnterpriseReportSnapshotTests(unittest.TestCase):
    def test_pdf_font_detects_linux_noto_cjk_before_windows_fonts(self):
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase import ttfonts

        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(ttfonts, "TTFont", return_value=object()) as font_type,
            patch.object(pdfmetrics, "registerFont") as register_font,
        ):
            font_name = _pdf_font_name()

        self.assertEqual(font_name, "LiemaCJK")
        font_type.assert_called_once()
        self.assertEqual(font_type.call_args.args[0], "LiemaCJK")
        self.assertEqual(
            font_type.call_args.args[1].replace("\\", "/"),
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        )
        self.assertEqual(font_type.call_args.kwargs, {"subfontIndex": 0})
        register_font.assert_called_once()

    def test_pdf_font_uses_builtin_chinese_cid_when_cff_fonts_are_unsupported(self):
        from reportlab.pdfbase import cidfonts
        from reportlab.pdfbase import pdfmetrics

        with (
            patch.object(Path, "is_file", return_value=False),
            patch.object(cidfonts, "UnicodeCIDFont", return_value=object()) as cid_font,
            patch.object(pdfmetrics, "registerFont") as register_font,
        ):
            font_name = _pdf_font_name()

        self.assertEqual(font_name, "STSong-Light")
        cid_font.assert_called_once_with("STSong-Light")
        register_font.assert_called_once()

    def test_minio_run_is_materialized_before_snapshot_collects_charts(self):
        class FakeArtifactStorage:
            backend = "minio"

            def __init__(self, root, run_dir):
                self.root = root
                self.run_dir = run_dir
                self.references = []

            def materialize_tree(self, reference):
                self.references.append(reference)
                return self.run_dir

            def workspace(self, *parts):
                path = self.root.joinpath(*parts)
                path.mkdir(parents=True, exist_ok=True)
                return path

            def reference(self, path):
                return f"minio://test/{Path(path).relative_to(self.root).as_posix()}"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "artifacts"
            run_dir = root / "runs" / "run-1"
            chart_dir = run_dir / "translate" / "charts"
            report_dir = run_dir / "translate" / "report"
            chart_dir.mkdir(parents=True)
            report_dir.mkdir(parents=True)
            (chart_dir / "cpu.png").write_bytes(b"chart")
            (chart_dir / CHART_FONT_RENDER_MARKER).write_text(
                CHART_FONT_RENDER_VERSION, encoding="ascii"
            )
            (report_dir / "run_report.txt").write_text("测试汇总", encoding="utf-8")
            storage = FakeArtifactStorage(root, run_dir)
            run = {
                "id": "run-1",
                "status": "succeeded",
                "stage": "completed",
                "created_at": time.time(),
                "run_dir": "minio://test/runs/run-1",
                "metadata": {"options": {}},
            }
            template = {"name": "default", "version": 1, "sections": []}

            def write_stub(_snapshot, output_path):
                Path(output_path).write_bytes(b"artifact")

            with (
                patch("auto_test.reporting.enterprise.build_docx", side_effect=write_stub),
                patch("auto_test.reporting.enterprise.build_pdf", side_effect=write_stub),
            ):
                snapshot, artifacts = build_report_artifacts(
                    run, [], template, {}, "job-1", artifact_storage=storage
                )

        self.assertEqual(storage.references, ["minio://test/runs/run-1"])
        self.assertEqual(snapshot["charts"], [str((chart_dir / "cpu.png").resolve())])
        self.assertEqual(snapshot["raw_report"], "测试汇总")
        self.assertIn("enterprise_reports/job-1", artifacts["artifact_dir"].replace("\\", "/"))

    def test_environment_uses_run_hosts_without_yaml_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            report_dir = run_dir / "translate" / "report"
            report_dir.mkdir(parents=True)
            (report_dir / "run_report.txt").write_text("上传 1 1 0 0 0\n", encoding="utf-8")
            run = {
                "id": "run123",
                "status": "succeeded",
                "stage": "completed",
                "created_at": time.time(),
                "run_dir": str(run_dir),
                "metadata": {
                    "options": {
                        "host": "172.16.102.73",
                        "gpu_host": "172.16.102.190",
                    }
                },
            }
            template = {"name": "default", "version": 1, "sections": []}

            with (
                patch("auto_test.reporting.enterprise.monitor_cfg", return_value={"enable_perf": True, "enable_log_monitor": True}),
                patch("auto_test.reporting.enterprise.export_cfg", return_value={"enable": False}),
                patch("auto_test.reporting.enterprise.ai_checks_cfg", return_value={"enable": False}),
                patch("auto_test.reporting.enterprise.stress_cfg", return_value={"enable": False}),
            ):
                snapshot = collect_snapshot(run, [], template, {})

        self.assertEqual(snapshot["environment"]["蓝鲨 APP 地址"], "172.16.102.73")
        self.assertEqual(snapshot["environment"]["GPU 服务地址"], "172.16.102.190")

    def test_snapshot_preserves_translation_speed_for_report_rendering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            report_dir = run_dir / "translate" / "report"
            report_dir.mkdir(parents=True)
            (report_dir / "run_report.txt").write_text("翻译 117 0 0 117 100.0%\n", encoding="utf-8")
            run = {
                "id": "run-speed", "status": "succeeded", "stage": "completed",
                "created_at": time.time(), "run_dir": str(run_dir), "metadata": {"options": {}},
            }
            template = {"name": "default", "version": 1, "sections": []}
            speed = {
                "state": "available", "items_per_minute": 8.44,
                "translated_count": 27, "elapsed_seconds": 192.0,
                "sample_count": 18, "segment_count": 1,
                "current": 117, "total": 9573,
            }

            snapshot = collect_snapshot(run, [], template, {}, translation_speed=speed)

        self.assertEqual(snapshot["schema_version"], "1.1")
        self.assertEqual(snapshot["translation_speed"]["items_per_minute"], 8.44)
        self.assertEqual(snapshot["translation_speed"]["elapsed_seconds"], 192.0)

    def test_docx_removes_xml_incompatible_control_characters(self):
        snapshot = {
            "schema_version": "1.0",
            "title": "测试\x00报告😀",
            "report_number": "BS-\x01-001",
            "generated_at": "2026-08-11 21:05:02",
            "prepared_by": "质量\x0b团队",
            "run": {"id": "run\x0c123"},
            "template": {
                "name": "default",
                "version": 1,
                "sections": [
                    {
                        "key": "results",
                        "title": "结果\x0e汇总",
                        "fixed_text": "保留\t制表符，移除\x1f控制符。",
                    }
                ],
            },
            "environment": {},
            "environment_notes": "",
            "methods": [],
            "stage_rows": [],
            "metrics_summary": {},
            "metric_count": 0,
            "translation_speed": {
                "state": "available", "items_per_minute": 8.44,
                "translated_count": 27, "elapsed_seconds": 192,
                "sample_count": 18, "segment_count": 1,
                "current": 117, "total": 9573,
            },
            "charts": [],
            "raw_report": "msg测试\x00  [other]  过滤后无中文字符",
            "custom_sections": {},
            "conclusion": "",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "report.docx"
            build_docx(snapshot, output_path)
            with ZipFile(output_path) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                core_xml = archive.read("docProps/core.xml").decode("utf-8")

        self.assertIn("测试报告😀", document_xml)
        self.assertIn("msg测试  [other]", document_xml)
        self.assertIn("翻译吞吐量", document_xml)
        self.assertIn("8.44 个/分钟", document_xml)
        self.assertIn("测试报告😀", core_xml)
        for codepoint in (0, 1, 11, 12, 14, 31):
            self.assertNotIn(chr(codepoint), document_xml)
            self.assertNotIn(chr(codepoint), core_xml)


if __name__ == "__main__":
    unittest.main()
