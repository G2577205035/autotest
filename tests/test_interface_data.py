import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests import bootstrap  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auto_test.platform.interface_data import parse_dataset
from auto_test.platform.interface_data_api import create_interface_data_api
from auto_test.platform.identity import IdentityService, install_identity_guard
from auto_test.platform.store import PlatformStore


class InterfaceDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'platform.db'
        self.store = PlatformStore(self.path, recover_jobs=False)
        self.project = self.store.create_project('DATA', '数据测试')['id']
        self.other = self.store.create_project('OTHER', '其他项目')['id']
        self.user = self.store.create_user('data.user', '测试用户', 'unusable-hash')['id']
        self.store.set_project_membership(self.project, self.user, 'project_admin')
        self.asset = self.store.save_interface_asset(self.project, {'name': '读接口', 'method': 'GET', 'path': 'https://example.test/{{ITEM}}'})
        self.scenario = self.store.save_interface_scenario(self.project, {'name': '数据测试', 'parameters': {}, 'steps': [{'asset_id': self.asset['id']}]})

    def dataset(self):
        return self.store.save_interface_dataset(self.project, self.scenario['id'], '两组参数', '[{"ITEM":"one"},{"ITEM":"two"}]', 'json', self.user)

    def schedule(self, dataset_id=''):
        return self.store.save_interface_schedule(self.project, self.scenario['id'], dataset_id, 60, 1, self.user)

    def test_csv_json_and_validation(self):
        self.assertEqual(parse_dataset('ITEM,COUNT\n"a,b",2\n', 'csv'), [{'ITEM': 'a,b', 'COUNT': '2'}])
        for source, kind in [('[{"TOKEN":"x"}]', 'json'), ('[{"N":{"password":"x"}}]', 'json'), ('[{"N":NaN}]', 'json'), ('[{"ITEM":1,"ITEM":2}]', 'json'), ('A,A\n1,2', 'csv'), ('A,B\n1', 'csv'), ('[]', 'json'), ('{}', 'json')]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_dataset(source, kind)

    def test_data_batch_snapshots_and_cross_project_rejection(self):
        data = self.dataset()
        batch = self.store.enqueue_interface_dataset(self.project, self.scenario['id'], data['id'], self.user, 1)
        self.assertEqual(batch['queued'], 2)
        runs = [self.store.get_interface_scenario_run(self.project, identifier) for identifier in batch['run_ids']]
        self.assertEqual([r['parameters']['ITEM'] for r in runs], ['one', 'two'])
        self.assertTrue(all(r['status'] == 'queued' and r['concurrency_limit'] == 1 for r in runs))
        self.store.delete_interface_dataset(self.project, data['id'])
        self.assertEqual(self.store.get_interface_scenario_run(self.project, batch['run_ids'][1])['parameters'], {'ITEM': 'two'})
        with self.assertRaises(KeyError):
            self.store.enqueue_interface_dataset(self.other, self.scenario['id'], data['id'], self.user)

    def test_two_instances_schedule_only_once_and_skip_overlapping_runs(self):
        data = self.dataset()
        plan = self.schedule(data['id'])
        second = PlatformStore(self.path, recover_jobs=False)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda store: store.dispatch_interface_schedule(now=plan['next_run_at']), [self.store, second]))
        self.assertEqual(sum(bool(result and result.get('queued')) for result in results), 1)
        self.assertEqual(len(self.store.list_interface_scenario_runs(self.project)), 2)
        skipped = second.dispatch_interface_schedule(now=plan['next_run_at'] + 3600)
        self.assertIn('skipped', skipped)
        self.assertGreater(second.list_interface_schedules(self.project, self.scenario['id'])[0]['next_run_at'], plan['next_run_at'] + 3600)
        self.assertEqual(len(self.store.list_interface_scenario_runs(self.project)), 2)

    def test_claim_concurrency_is_shared_across_store_instances(self):
        data = self.dataset()
        self.store.enqueue_interface_dataset(self.project, self.scenario['id'], data['id'], self.user, 1)
        second = PlatformStore(self.path, recover_jobs=False)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claimed = list(pool.map(lambda store: store.claim_interface_scenario_run(), [self.store, second]))
        self.assertEqual(sum(run is not None for run in claimed), 1)

    def test_schedule_pauses_when_creator_loses_permission(self):
        plan = self.schedule()
        self.store.set_project_membership(self.project, self.user, 'viewer')
        self.assertIn('paused', self.store.dispatch_interface_schedule(now=plan['next_run_at']))
        self.assertEqual(self.store.list_interface_schedules(self.project, self.scenario['id'])[0]['enabled'], 0)
        self.assertEqual(self.store.list_interface_scenario_runs(self.project), [])

    def test_schedule_pause_delete_and_reference_protection(self):
        data = self.dataset()
        plan = self.schedule(data['id'])
        with self.assertRaises(ValueError):
            self.store.delete_interface_dataset(self.project, data['id'])
        self.assertFalse(self.store.change_interface_schedule(self.other, plan['id'], None, self.user))
        self.store.change_interface_schedule(self.project, plan['id'], False, self.user)
        self.assertIsNone(self.store.dispatch_interface_schedule(now=plan['next_run_at'] + 120))
        self.store.delete_interface_scenario(self.project, self.scenario['id'])
        self.assertEqual(self.store.list_interface_schedules(self.project, self.scenario['id']), [])
        self.assertEqual(self.store.list_interface_datasets(self.project, self.scenario['id']), [])

    def test_trend_missing_metrics_and_failed_runs_are_not_hidden(self):
        for status, summary in [('succeeded', {'elapsed_ms': 12}), ('failed', {'elapsed_ms': 40}), ('interrupted', {})]:
            run = self.store.create_interface_scenario_run(self.project, self.scenario)
            self.store.finish_interface_scenario_run(self.project, run['id'], status=status, summary=summary, result={})
        trend = self.store.interface_scenario_trend(self.project, self.scenario['id'])
        self.assertEqual(trend['success_rate'], 33.33)
        self.assertEqual(trend['p95_ms'], 40)
        self.assertEqual(trend['points'][-1]['elapsed_ms'], None)
        self.assertEqual(self.store.interface_scenario_trend(self.other, self.scenario['id'])['total'], 0)

    def test_api_permissions_csrf_and_project_boundary(self):
        app = FastAPI()
        service = IdentityService(self.store)
        install_identity_guard(app, service)
        app.include_router(create_interface_data_api(self.store, Mock()), prefix='/api')
        token, csrf = service.create_session(self.user)
        with TestClient(app) as client:
            client.cookies.set('liema_session', token)
            client.headers.update({'X-Project-ID': self.project, 'X-CSRF-Token': csrf})
            path = '/api/interface-scenarios/' + self.scenario['id']
            self.assertEqual(client.post(path + '/datasets', headers={'X-CSRF-Token': ''}, json={'name': '参数', 'content': '[{}]'}).status_code, 403)
            response = client.post(path + '/datasets', json={'name': '参数', 'content': '[{"ITEM":"x"}]'})
            self.assertEqual(response.status_code, 201, response.text)
            data_id = response.json()['id']
            self.store.set_project_membership(self.project, self.user, 'tester')
            self.assertEqual(client.post(path + '/datasets', json={'name': '参数', 'content': '[{}]'}).status_code, 403)
            self.assertEqual(client.post(path + '/data-execute', json={'dataset_id': data_id}).status_code, 202)
            self.store.set_project_membership(self.project, self.user, 'viewer')
            self.assertEqual(client.post(path + '/data-execute', json={'dataset_id': data_id}).status_code, 403)
            self.store.set_project_membership(self.other, self.user, 'project_admin')
            client.headers['X-Project-ID'] = self.other
            self.assertEqual(client.get(path + '/automation').status_code, 404)
