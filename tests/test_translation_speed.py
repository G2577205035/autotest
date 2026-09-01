from tests import bootstrap  # noqa: F401
import unittest

from auto_test.monitoring.translation_speed import format_duration, summarize_translation_speed


def event(event_id, created_at, message):
    return {"id": event_id, "created_at": created_at, "message": message}


class TranslationSpeedTests(unittest.TestCase):
    def test_uses_exact_progress_delta_and_event_timestamps(self):
        summary = summarize_translation_speed([
            event(1, 0, "翻译进度：90/9573"),
            event(2, 11, "翻译进度：92/9573"),
            event(3, 192, "翻译进度：117/9573"),
        ])

        self.assertEqual(summary["state"], "available")
        self.assertEqual(summary["translated_count"], 27)
        self.assertEqual(summary["elapsed_seconds"], 192.0)
        self.assertEqual(summary["items_per_minute"], 8.44)
        self.assertEqual(summary["current"], 117)
        self.assertEqual(summary["total"], 9573)

    def test_stalled_samples_count_toward_observed_wall_clock_time(self):
        summary = summarize_translation_speed([
            event(1, 100, "翻译进度：10/100"),
            event(2, 130, "翻译进度：20/100"),
            event(3, 160, "翻译进度：20/100"),
        ])

        self.assertEqual(summary["translated_count"], 10)
        self.assertEqual(summary["elapsed_seconds"], 60.0)
        self.assertEqual(summary["items_per_minute"], 10.0)

    def test_resets_form_independent_segments_without_counting_gap(self):
        summary = summarize_translation_speed([
            event(1, 0, "翻译进度：0/100"),
            event(2, 60, "翻译完成：60/100"),
            event(3, 600, "翻译进度：0/50"),
            event(4, 660, "翻译完成：30/50"),
        ])

        self.assertEqual(summary["segment_count"], 2)
        self.assertEqual(summary["translated_count"], 90)
        self.assertEqual(summary["elapsed_seconds"], 120.0)
        self.assertEqual(summary["items_per_minute"], 45.0)

    def test_single_valid_sample_is_measuring_and_noise_is_ignored(self):
        summary = summarize_translation_speed([
            event(1, 0, "解析进度：90/9573"),
            event(2, 10, "翻译进度：90/9573"),
        ])

        self.assertEqual(summary["state"], "measuring")
        self.assertIsNone(summary["items_per_minute"])
        self.assertEqual(summary["sample_count"], 1)

    def test_formats_report_duration(self):
        self.assertEqual(format_duration(192), "3分12秒")
        self.assertEqual(format_duration(3661), "1小时1分1秒")


if __name__ == "__main__":
    unittest.main()
