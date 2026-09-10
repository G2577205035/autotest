import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from tests import bootstrap  # noqa: F401
from auto_test.core.task_queue import redis_connection_settings
from auto_test.evaluation.advanced import correlate_resources
from auto_test.evaluation.catalog import TRANSLATION_CASES, CORE_CASES
from auto_test.evaluation.model_client import EvaluationModelClient
from auto_test.evaluation.presets import build_task_configuration
from auto_test.evaluation.scoring import score_response
from auto_test.evaluation.value_matching import dates_in, has_quantity, has_sequence
from auto_test.evaluation.evalscope_runner import _run_perf_stages
from auto_test.evaluation.resources import align_stage_resources, EvaluationResourceSampler
from auto_test.evaluation.result_mapper import _normalized_summary
from auto_test.evaluation.advanced import analyze_capacity, parse_judge_response


class FactualEvaluationTests(unittest.TestCase):
    def test_real_report_headings_and_presentational_facts(self):
        rules = {'required_sections': ['事实摘要', '风险判断', '改进建议'], 'required_facts': ['20 项', '来源A'], 'fact_normalization': 'presentation', 'pass_threshold': 1.0}
        response = '## 1. 事实摘要\n来源 A 的 **20** 项测试。\n**二、风险判断**\n待核实。\n### 三、改进建议\n复测。'
        self.assertTrue(score_response(response, rules)['passed'])
        self.assertTrue(score_response(response.replace('## 1. 事实摘要', '## 1. 事实摘要（Facts）'), rules)['passed'])
        self.assertFalse(score_response(response.replace('## 1. 事实摘要', '正文只提到事实摘要（Facts）'), rules)['passed'])
        for wrong in (response.replace('**20**', '**120**'), response.replace('来源 A', '来源 AB'), response.replace('### 三、改进建议', '正文提到了改进建议')):
            self.assertFalse(score_response(wrong, rules)['passed'])
        self.assertFalse(score_response('20项', {'required_facts': ['20 项'], 'pass_threshold': 1.0})['passed'])

    def test_core_fact_extraction_accepts_chinese_date_but_not_wrong_date(self):
        case = next(row for row in CORE_CASES if row['payload']['name']=='关键事实抽取')
        rules = case['payload']['rules']
        self.assertTrue(score_response('验收时间为2026年9月2日，负责人是林岚。', rules)['passed'])
        self.assertFalse(score_response('验收时间为2026年9月3日，负责人是林岚。', rules)['passed'])

    def test_date_equivalence_and_invalid_dates(self):
        for text in ("2026-01-09", "2026年1月9日", "January 9, 2026", "9th January 2026"):
            with self.subTest(text=text):
                self.assertIn("2026-01-09", dates_in(text))
        for text in ("2026-02-30", "2026-01-19", "12026-01-09", "01/09/2026"):
            with self.subTest(text=text):
                self.assertNotIn("2026-01-09", dates_in(text))

    def test_exact_currency_scale_and_percent(self):
        expected = {"value": 12010000, "currency": "CNY"}
        for text in ("RMB 12,010,000", "12.01 million RMB", "预算为1201万元。", "CNY 12010 thousand"):
            with self.subTest(text=text):
                self.assertTrue(has_quantity(text, expected))
        for text in ("USD 12,010,000", "12.01 billion RMB", "12010000", "RMB 120100001", "RMB -12010000", "RMB 12010000 USD"):
            with self.subTest(text=text):
                self.assertFalse(has_quantity(text, expected))
        for text in ("81%", "81 percent", "百分之81", "８１％"):
            self.assertTrue(has_quantity(text, {"value": 81, "unit": "%"}))
        self.assertFalse(has_quantity("181%", {"value": 81, "unit": "%"}))

    def test_sequence_cannot_match_date_identifier_or_wrong_round(self):
        for text in ("first batch", "batch 1", "1st round", "第 一 轮", "第1批"):
            self.assertTrue(has_sequence(text, 1, []), text)
        for text in ("[CASE-001] on January 1, 2026", "batch 11", "budget 1 million", "[round 1]"):
            self.assertFalse(has_sequence(text, 1, ["[round 1]"]), text)
        self.assertTrue(has_sequence("ninety-ninth batch", 99, []))
        self.assertTrue(has_sequence("第九十九轮", 99, []))
        self.assertTrue(has_sequence("验收轮次39将于", 39, []))
        self.assertTrue(has_sequence("Test Batch No. 87", 87, []))
        self.assertTrue(has_sequence("88th test batch", 88, []))
        self.assertFalse(has_sequence("验收轮次399", 39, []))

    def test_every_new_translation_reference_passes_and_facts_are_mandatory(self):
        for case in TRANSLATION_CASES:
            payload = case["payload"]
            result = score_response(payload["mock_response"], payload["rules"])
            self.assertTrue(result["passed"], (case["case_key"], result["failed_checks"]))
        payload = TRANSLATION_CASES[0]["payload"]
        for old, new in (("RMB 12010000", "USD 12010000"), ("2026-01-01", "2026-01-02"), ("batch 1", "batch 11"), ("{{owner}}", "{{Owner}}")):
            result = score_response(payload["mock_response"].replace(old, new), payload["rules"])
            self.assertFalse(result["passed"], (old, result))

    def test_translation_sampling_balances_directions_and_fills_short_groups(self):
        profile = {"model_name": "test", "base_url": "http://example.test"}
        def selected(cases, plan):
            return build_task_configuration(run_kind="translation", plan=plan, profile=profile, cases=list(cases), stream=True, max_tokens=512, timeout=30)[3]["cases"]
        for plan, per_direction in (("quick", 10), ("standard", 50), ("deep", 100)):
            rows = selected(TRANSLATION_CASES, plan)
            self.assertEqual(Counter(row["category"] for row in rows), {"translation_zh_en": per_direction, "translation_en_zh": per_direction})
            self.assertEqual(len({row["case_key"] for row in rows}), 2 * per_direction)
        self.assertEqual(len(selected(TRANSLATION_CASES[:2] + TRANSLATION_CASES[100:], "quick")), 20)

    def test_json_field_values_are_checked(self):
        rules = {"json_schema": {"values": {"status": "ok", "count": 2}}, "pass_threshold": 1.0}
        self.assertTrue(score_response('{"status":"ok","count":2}', rules)["passed"])
        self.assertFalse(score_response('{"status":"no","count":2}', rules)["passed"])

    def test_resource_missing_and_constant_values_are_unavailable(self):
        rows = [{"request_throughput": x, "cpu_percent": ""} for x in (1, 2, 3)]
        self.assertFalse(correlate_resources(rows)["available"])
        for row in rows:
            row["cpu_percent"] = 30
        self.assertFalse(correlate_resources(rows)["available"])
        rows[0]["cpu_percent"] = 10
        result = correlate_resources(rows)
        self.assertTrue(result["available"])
        self.assertIsNone(result["throughput_correlations"]["gpu_percent"])
        self.assertEqual(result["valid_sample_counts"]["gpu_percent"], 0)

    def test_redis_password_optional_and_acl_username_requires_password(self):
        with patch.dict(os.environ, {"LIEMA_REDIS_USERNAME": "worker", "LIEMA_REDIS_PASSWORD": ""}, clear=True):
            result = redis_connection_settings({"password_env": "UNUSED"})
            self.assertIsNone(result["username"])
            self.assertIsNone(result["password"])
        with patch.dict(os.environ, {"LIEMA_REDIS_USERNAME": "worker", "LIEMA_REDIS_PASSWORD": "test-only"}, clear=True):
            result = redis_connection_settings({})
            self.assertEqual(result["username"], "worker")
            self.assertEqual(result["password"], "test-only")


class ObservedClientTests(unittest.TestCase):
    def client(self, document=None, lines=None):
        response = Mock(status_code=200)
        response.json.return_value = document
        response.iter_lines.return_value = iter(lines or [])
        session = Mock()
        session.post.return_value = response
        return EvaluationModelClient(session=session), session, response

    def call(self, client, stream=False):
        return client.call(base_url="http://example.test", model="test", messages=[{"role": "user", "content": "hello"}], stream=stream)

    def test_nonstream_has_no_observable_ttft_and_closes_response(self):
        client, session, response = self.client({"id": "x", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1}})
        result = self.call(client)
        self.assertTrue(result.succeeded)
        self.assertIsNone(result.ttft_ms)
        self.assertEqual(result.token_source, "api_usage")
        response.close.assert_called_once()
        self.assertNotIn("stream_options", session.post.call_args.kwargs["json"])

    def test_stream_usage_and_finish_marker(self):
        chunks = [{"id": "x", "choices": [{"delta": {"content": "中文"}, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}, {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}]
        lines = ["data: " + json.dumps(item) for item in chunks]
        client, session, response = self.client(lines=lines + ["data: [DONE]"])
        result = self.call(client, True)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.text, "中文")
        self.assertEqual(result.token_source, "api_usage")
        self.assertEqual(result.output_tokens, 2)
        self.assertIsNotNone(result.ttft_ms)
        self.assertTrue(session.post.call_args.kwargs["json"]["stream_options"]["include_usage"])
        response.close.assert_called_once()
        client, _, _ = self.client(lines=lines)
        self.assertEqual(self.call(client, True).error_type, "stream_interrupted")

    def test_empty_truncation_and_timeout_never_pass_and_next_call_recovers(self):
        for content, reason, expected in (("", "stop", "empty_response"), ("partial", "length", "output_truncated")):
            client, _, response = self.client({"choices": [{"message": {"content": content}, "finish_reason": reason}]})
            result = self.call(client)
            self.assertFalse(result.succeeded)
            self.assertEqual(result.error_type, expected)
            response.close.assert_called_once()
        client, session, response = self.client({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        session.post.side_effect = [requests.Timeout("isolated test"), response]
        self.assertEqual(self.call(client).error_type, "timeout")
        self.assertTrue(self.call(client).succeeded)


class PerformanceSafetyTests(unittest.TestCase):
    def test_unlimited_rate_is_not_a_negative_rps_capacity_dimension(self):
        summary = _normalized_summary([{'path': f'stage-{index:02d}-capacity/benchmark_summary.json', 'payload': {'Total Requests': 10, 'Success Requests': 10, 'Concurrency': concurrency, 'Request Rate (req/s)': -1, 'Req Throughput (req/s)': throughput}} for index, (concurrency, throughput) in enumerate(((1, 1.0), (2, 1.9), (4, 2.0)))])
        stages = summary['performance']['stages']
        self.assertTrue(all(stage['target_rps'] is None for stage in stages))
        capacity = analyze_capacity(stages)
        self.assertEqual(capacity['capacity_knee']['concurrency'], 4)
        self.assertIsNone(capacity['max_stable_rps'])

    def test_judge_malformed_or_nonfinite_scores_are_invalid(self):
        for text in ('[]', '{"score":NaN}', '{"score":2}', '{"score":true}'):
            self.assertEqual(parse_judge_response(text, [])['status'], 'invalid', text)
        answer = '<think>{"score": 0.1} is a draft.</think>\n```json\n{"score":0.9,"confidence":0.8,"reason":"matches evidence","rubric":[]}\n```'
        self.assertEqual(parse_judge_response(answer, [])['score'], 0.9)
        self.assertEqual(parse_judge_response('<think>```json\n{"score":1}\n```', [])['status'], 'invalid')
        self.assertEqual(parse_judge_response('prefix ```json\n{"score":1}\n``` suffix', [])['status'], 'invalid')

    def test_stage_timeouts_capacity_limit_and_error_stop_with_recovery(self):
        calls = []
        def benchmark(options):
            calls.append(options)
            self.assertLessEqual(options["parallel"], 2)
            self.assertEqual(options["total_timeout"], 7)
            self.assertFalse(options["open_loop"])
            root = Path(options["outputs_dir"])
            root.mkdir(parents=True)
            (root / "benchmark_summary.json").write_text(json.dumps({"Total Requests": 10, "Failed Requests": 3 if len(calls) == 1 else 0}), encoding="utf-8")
            return {}
        with tempfile.TemporaryDirectory() as folder:
            payload = {"work_dir": folder, "safety": {"max_concurrency": 2, "request_timeout_seconds": 7, "stop_on_error_rate": 0.2}, "load_profile": {"kind": "deep", "burst": {"requests": 20}, "sustained": {"duration_seconds": 2}, "recovery_observation_seconds": 3}}
            with patch("auto_test.evaluation.evalscope_runner._emit"):
                result = _run_perf_stages(payload, {"number": [10, 20], "parallel": [2, 4], "rate": [1, 2]}, benchmark)
            stages = result["performance_execution"]["stages"]
            self.assertEqual([stage["phase"] for stage in stages], ["capacity", "recovery"])
            self.assertTrue(result["performance_execution"]["safety_stopped"])
            self.assertEqual(calls[-1]["parallel"], 1)

    def test_resource_alignment_excludes_other_time_windows_and_missing_gpu(self):
        samples = [{"sampled_at": timestamp, "cpu_percent": cpu, "gpu_percent": None} for timestamp, cpu in ((1, 99), (16, 10), (21, 20), (31, 99))]
        stages = [{"phase": "capacity", "concurrency": 2, "target_rps": 1, "request_throughput": 0.9}]
        windows = [{"phase": "capacity", "parallel": 2, "rate": 1, "started_at": 10, "finished_at": 25}]
        result = align_stage_resources(samples, stages, windows)
        self.assertEqual(result[0]["cpu_percent"], 15)
        self.assertIsNone(result[0]["gpu_percent"])
        self.assertEqual(result[0]["sample_count"], 2)

    def test_edited_server_asset_cannot_redirect_resource_connection(self):
        store = Mock()
        store.get_server_profile.return_value = {"host": "changed.example.test"}
        with tempfile.TemporaryDirectory() as folder, patch("auto_test.evaluation.resources.SSHClient") as ssh:
            sampler = EvaluationResourceSampler(store, {"server_profile_id": "p", "server_host": "original.example.test"}, Path(folder))
            sampler.start()
            result = sampler.finish({})
            ssh.assert_not_called()
            self.assertFalse(result["available"])
            self.assertIn("改变", result["unavailable_reason"])


if __name__ == "__main__":
    unittest.main()
