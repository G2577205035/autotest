import unittest
from tests import bootstrap  # noqa: F401

from auto_test.core.automation import AutomationService


class AutomationServiceTests(unittest.TestCase):
    def test_pipeline_result_and_progress_are_structured(self):
        received = []

        def pipeline(progress_callback, run_id):
            progress_callback("upload", "uploading", 25)
            return {"run_id": run_id, "run_dir": "/tmp/example", "value": 7}

        result = AutomationService(pipeline=pipeline).run(
            run_id="run-1", progress_callback=received.append
        )

        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.run_dir, "/tmp/example")
        self.assertEqual(result.details["value"], 7)
        self.assertEqual([event.stage for event in received], ["preparing", "upload", "completed"])


if __name__ == "__main__":
    unittest.main()
