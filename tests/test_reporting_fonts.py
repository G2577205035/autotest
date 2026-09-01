import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.reporting.charts import (
    CHART_FONT_RENDER_MARKER,
    CHART_FONT_RENDER_VERSION,
    charts_have_current_cjk_font,
    regenerate_performance_charts,
)
from auto_test.reporting.font_support import configure_matplotlib_cjk


class ReportingFontTests(unittest.TestCase):
    def test_configure_matplotlib_registers_bundled_linux_cjk_font(self):
        from matplotlib import rcParams

        self.addCleanup(configure_matplotlib_cjk)
        with (
            patch.object(Path, "is_file", return_value=True),
            patch("auto_test.reporting.font_support._register_font", return_value="Noto Sans CJK JP") as register,
        ):
            selected = configure_matplotlib_cjk()

        self.assertEqual(selected, "Noto Sans CJK JP")
        self.assertEqual(rcParams["font.sans-serif"][0], "Noto Sans CJK JP")
        self.assertEqual(
            str(register.call_args.args[0]).replace("\\", "/"),
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        )

    def test_historical_performance_csv_can_be_redrawn_for_a_new_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            report_dir = run_dir / "report"
            report_dir.mkdir(parents=True)
            with (report_dir / "perf_app.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["time", "cpu_pct(%)", "cpu_temp(°C)", "mem_used_gb"],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"time": "10:00:00", "cpu_pct(%)": 20, "cpu_temp(°C)": 48, "mem_used_gb": 8},
                        {"time": "10:00:05", "cpu_pct(%)": 35, "cpu_temp(°C)": 52, "mem_used_gb": 9},
                    ]
                )

            generated = regenerate_performance_charts(run_dir)

            names = {path.name for path in generated}
            self.assertIn("perf_app_cpu.png", names)
            self.assertIn("perf_app_mem.png", names)
            self.assertTrue(all(path.stat().st_size > 1000 for path in generated))
            self.assertFalse(charts_have_current_cjk_font(run_dir))

    def test_current_chart_set_is_identified_by_font_render_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            charts_dir = run_dir / "charts"
            charts_dir.mkdir(parents=True)
            marker = charts_dir / CHART_FONT_RENDER_MARKER
            marker.write_text(CHART_FONT_RENDER_VERSION, encoding="ascii")

            self.assertTrue(charts_have_current_cjk_font(run_dir))


if __name__ == "__main__":
    unittest.main()
