import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import bootstrap  # noqa: F401
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from auto_test.platform.store import PlatformStore
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.identity import IdentityService, install_identity_guard
from auto_test.platform.performance_api import register_performance_api
from auto_test.monitoring.performance_comparison import PerformanceComparisons, comparison_result
from auto_test.monitoring.cpu_benchmark import BENCHMARK_SCRIPT, run_cpu_benchmark, validate_benchmark


class PerformanceComparisonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = PlatformStore(self.root / 'db.sqlite', recover_jobs=False)
        self.artifacts = LocalArtifactStorage(self.root / 'artifacts')
        self.project = self.store.create_project('PERF', '性能测试')['id']
        self.other = self.store.create_project('OTHER', '其他项目')['id']
        self.benchmark = dict(status='succeeded', benchmark_version='liema-sha256-v1', block_bytes=1048576, duration_s=60, workers=1, warmup_s=1, python_version='3.test', openssl_version='test', mib_per_second=100.0, elapsed_s=60.1, score=100.0)
        self.entry = {'id': 'a', 'name': '节点甲', 'status': 'succeeded', 'options': {'duration': 60, 'workers': 1, 'cpu_load': 100, 'cpu_benchmark': True}, 'modes': ['cpu'], 'metrics': {'sample_count': 5, 'cpu_pct': {'avg': 99}}, 'benchmark': self.benchmark}

    def test_comparison_baseline_and_missing_metrics(self):
        second = copy.deepcopy(self.entry); second['id'] = 'b'; second['benchmark']['mib_per_second'] = 150
        result = comparison_result([self.entry, second])
        self.assertTrue(result['rankable'])
        self.assertEqual([row['relative_index'] for row in result['rows']], [100, 150])
        self.assertIsNone(result['rows'][0]['cpu_peak_c'])
        second['benchmark'] = {}
        result = comparison_result([self.entry, second])
        self.assertFalse(result['rankable'])
        self.assertIsNone(result['rows'][1]['throughput_mib_s'])
        self.assertTrue(all(row['relative_index'] is None for row in result['rows']))

    def test_mismatch_requires_opt_in_and_failure_never_ranks(self):
        second = copy.deepcopy(self.entry); second['options']['workers'] = 2
        with self.assertRaises(ValueError): comparison_result([self.entry, second])
        self.assertFalse(comparison_result([self.entry, second], True)['rankable'])
        for change in ({'status': 'failed'}, {'benchmark': {**self.benchmark, 'openssl_version': 'other'}}, {'benchmark': {**self.benchmark, 'mib_per_second': float('nan')}}):
            with self.subTest(change=change):
                self.assertFalse(comparison_result([self.entry, {**self.entry, **change}])['rankable'])

    def test_benchmark_result_guards_and_stop_kills_only_owned_group(self):
        for field, value in [('score', 999), ('duration_s', 10), ('mib_per_second', float('nan')), ('elapsed_s', 59), ('workers', 0)]:
            with self.subTest(field=field), self.assertRaises(ValueError): validate_benchmark({**self.benchmark, field: value}, 60, 1)
        ssh = Mock(); ssh.exec.return_value = ('12345', '')
        with self.assertRaises(InterruptedError):
            run_cpu_benchmark(ssh, guard_callback=Mock(side_effect=InterruptedError()))
        commands = [call.args[0] for call in ssh.exec.call_args_list]
        self.assertTrue(any('/proc/12345/cmdline' in command and 'kill -TERM -- -12345' in command for command in commands))
        self.assertIn('rmdir /tmp/liema-bench-', commands[-1])
        self.assertNotIn('rm -rf', str(commands))

    def test_benchmark_script_real_local_single_process_smoke(self):
        path = self.root / 'benchmark.py'; path.write_text(BENCHMARK_SCRIPT, encoding='utf-8')
        completed = subprocess.run([sys.executable, str(path), '--duration', '10', '--workers', '1'], capture_output=True, text=True, timeout=35, check=True)
        result = validate_benchmark(json.loads(completed.stdout), 10, 1)
        self.assertGreater(result['mib_per_second'], 0)
        self.assertEqual(result['reference_mib_per_second'], 100)

    def create_job(self, project, name='测试节点'):
        options = {**self.entry['options'], 'host': 'test-node.invalid', 'target_name': name, 'server_name': name, 'modes': ['cpu'], '_project_id': project}
        job = self.store.create_stress_job(options)
        self.store.update_stress_job(job['id'], status='succeeded', finished=True)
        self.store.add_stress_sample(job['id'], 'app', {'cpu_pct': 50})
        return self.store.get_stress_job(job['id'])

    def test_persisted_report_does_not_invent_historical_benchmark(self):
        jobs = [self.create_job(self.project), self.create_job(self.project)]
        service = PerformanceComparisons(self.store, self.artifacts)
        saved = service.save(self.project, '', jobs)
        self.assertFalse(saved['result']['rankable'])
        self.assertIsNone(saved['result']['rows'][0]['throughput_mib_s'])
        self.assertIsNone(service.get(self.other, saved['id']))
        item = service.get(self.project, saved['id'])
        root = self.artifacts.resolve(item['artifact_ref'])
        self.assertTrue((root / 'report.pdf').read_bytes().startswith(b'%PDF'))
        self.assertTrue((root / 'report.docx').is_file())

    def test_api_permissions_cross_project_comparison_download_and_trend(self):
        user = self.store.create_user('perf.tester', '性能测试员', 'unusable-hash')['id']
        self.store.set_project_membership(self.project, user, 'tester')
        self.store.set_project_membership(self.other, user, 'viewer')
        service = IdentityService(self.store); token, csrf = service.create_session(user)
        router = APIRouter(); app = FastAPI()
        register_performance_api(router, self.store, self.artifacts, lambda job, request: job['options'].get('_project_id') == request.state.identity['current_project']['id'])
        app.include_router(router, prefix='/api'); install_identity_guard(app, service)
        jobs = [self.create_job(self.project), self.create_job(self.project)]
        alien = self.create_job(self.other)
        with TestClient(app) as client:
            client.cookies.set('liema_session', token); client.headers.update({'X-Project-ID': self.project, 'X-CSRF-Token': csrf})
            self.assertEqual(client.post('/api/stress-comparisons', json={'job_ids': [jobs[0]['id'], alien['id']]}).status_code, 404)
            response = client.post('/api/stress-comparisons', json={'job_ids': [job['id'] for job in jobs]})
            self.assertEqual(response.status_code, 201, response.text)
            identifier = response.json()['id']
            self.assertEqual(client.get('/api/stress-comparisons/' + identifier + '/download/pdf').status_code, 200)
            points = client.get('/api/stress-trends', params={'job_id': jobs[0]['id']}).json()['points']
            self.assertEqual(len(points), 2)
            client.headers['X-Project-ID'] = self.other
            self.assertEqual(client.get('/api/stress-comparisons/' + identifier + '/download/pdf').status_code, 404)
            self.assertEqual(client.post('/api/stress-comparisons', json={'job_ids': [job['id'] for job in jobs]}).status_code, 403)
