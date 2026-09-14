import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from tests import bootstrap  # noqa: F401
from tests import test_ui_automation
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from auto_test.platform.identity import IdentityService, install_identity_guard
from auto_test.platform.secrets import decrypt_secret
from auto_test.platform.worker_rpc import WorkerMailbox
from auto_test.ui_automation.api import register_ui_api
from auto_test.ui_automation.recorder_api import availability, register_recorder_gateway
from auto_test.ui_automation.recordings import RecordingStore, public_recording
from auto_test.ui_automation.recorder_runtime import DockerRecorder, RecorderManager, recorder_settings
from auto_test.ui_automation.runner import DockerUiRunner, decrypt_artifact, extract_artifacts, prepare_input, summarize_result
from auto_test.ui_automation import environment
from auto_test.ui_worker import UiWorker


class UiRecordingTests(unittest.TestCase):
    def setUp(self):
        test_ui_automation.UiAutomationTests.setUp(self)
        env = patch.dict(os.environ, {'LIEMA_UI_RECORDER_IMAGE': ''})
        env.start(); self.addCleanup(env.stop)
        self.recordings = RecordingStore(self.store)
        self.owner = secrets.token_hex(16)
        self.assertTrue(WorkerMailbox(self.store, 'ui').acquire(self.owner))
        self.secret = secrets.token_urlsafe(32)
        self.source = "import { test, expect } from '@playwright/test';\ntest('recorded', async ({page}) => { await page.getByLabel('Password').fill(" + json.dumps(self.secret) + "); await expect(page.getByRole('heading')).toHaveText('Ready'); });"

    def recording(self, ready=True, suite_id=''):
        item = self.recordings.create(self.project, self.user, '页面录制', 'https://app.example.test/path?ticket=' + self.secret, suite_id)
        if ready:
            self.assertTrue(self.recordings.claim(item['id'], self.owner))
            self.assertTrue(self.recordings.ready(item['id'], self.owner, 45678, self.secret))
        return self.recordings.get(item['id'])

    def client(self):
        app, service = FastAPI(), IdentityService(self.store)
        install_identity_guard(app, service)
        app.include_router(register_ui_api(self.store, self.artifacts), prefix='/api')
        register_recorder_gateway(app, self.store, service)
        token, csrf = service.create_session(self.user)
        client = TestClient(app)
        client.cookies.set('liema_session', token)
        client.headers.update({'X-Project-ID': self.project, 'X-CSRF-Token': csrf})
        return client, service

    def saved(self):
        item = self.recording()
        self.recordings.request_save(item['id'])
        return self.recordings.save(item['id'], self.owner, self.source, 1)

    def prepare_environment(self):
        WorkerMailbox(self.store, 'ui').release(self.owner)
        self.store.update_user(self.user, is_superuser=True)
        # Restore by the outer setUp patch; exercise file-based setup with no
        # shell overrides, without touching the user's actual local settings.
        for key in environment.ENVIRONMENT_KEYS.values():
            os.environ.pop(key, None)
        return {'runner_image': 'sha256:' + 'a' * 64, 'recorder_image': 'sha256:' + 'b' * 64, 'network': 'ui-isolated-test'}

    def test_environment_roundtrip_shared_by_web_and_worker_is_not_reported_ready(self):
        value = self.prepare_environment()
        client, _ = self.client()
        with client, patch('auto_test.ui_automation.recorder_api.shutil.which', return_value=None):
            response = client.put('/api/ui-recordings/environment', json=value)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['saved'])
            result = client.get('/api/ui-recordings/environment').json()
        self.assertEqual(result['configuration'], value)
        self.assertTrue(result['configured'])
        self.assertFalse(result['available'])
        checks = {item['key']: item['status'] for item in result['checks']}
        self.assertEqual(checks['docker'], 'missing')
        self.assertEqual(checks['worker'], 'missing')
        self.assertEqual(checks['recorder_image'], 'configured')
        self.assertEqual(DockerUiRunner().config['image'], value['runner_image'])
        self.assertEqual(DockerRecorder().config['image'], value['recorder_image'])
        self.assertEqual(DockerRecorder().config['network'], value['network'])
        self.assertEqual(json.loads(environment.CONFIG_PATH.read_text(encoding='utf-8')), value)
        self.assertEqual(list(environment.CONFIG_PATH.parent.glob('.ui-runtime-*.tmp')), [])

    def test_container_recording_uses_validated_docker_dns_without_published_port(self):
        from auto_test.ui_automation.recorder_runtime import endpoint
        with patch.dict(os.environ, {'LIEMA_UI_RECORDER_CONNECT_MODE': 'network'}):
            self.assertEqual(endpoint({'id': 'a' * 32, 'gateway_port': 6080}), 'http://liema-recorder-' + 'a' * 32 + ':6080')
            for item in [{'id': 'other-host', 'gateway_port': 6080}, {'id': 'a' * 32, 'gateway_port': 12345}]:
                with self.subTest(item=item), self.assertRaises(ValueError):
                    endpoint(item)
            runner = DockerRecorder({'image': 'sha256:' + 'a' * 64, 'network': 'ui-test', 'enabled': True, 'connection_mode': 'network'})
            self.assertNotIn('--publish', runner.create_arguments('liema-recorder-' + 'a' * 32))
        with patch.dict(os.environ, {'LIEMA_UI_RECORDER_CONNECT_MODE': 'other'}), self.assertRaises(ValueError):
            environment.connection_mode()

    def test_browser_proxy_validation_and_replay_propagation(self):
        for invalid in ['socks5://proxy:1234', 'http://user:secret@proxy:80', 'http://proxy', 'http://proxy:0', 'http://proxy:99999', 'http://proxy:80/path', 'http://proxy:80?token=x', 'http://bad host:80']:
            with self.subTest(proxy=invalid), patch.dict(os.environ, {'LIEMA_UI_BROWSER_PROXY': invalid}), self.assertRaises(ValueError):
                environment.browser_proxy()
        with patch.dict(os.environ, {'LIEMA_UI_BROWSER_PROXY': 'http://ui-proxy:3128/'}):
            proxy = environment.browser_proxy()
            self.assertEqual(proxy, 'http://ui-proxy:3128')
            snapshot = {'files': [], 'parameters': {}, 'base_url': '', 'timeout_seconds': 60}
            prepare_input(snapshot, self.root, proxy)
            config = (self.root / 'playwright.config.cjs').read_text(encoding='utf-8')
            self.assertIn('"proxy": {"server": "http://ui-proxy:3128"}', config)
            self.assertNotIn('baseURL', config)
            prepare_input({**snapshot, 'base_url': 'https://app.example.test'}, self.root, proxy)
            self.assertIn('"baseURL": "https://app.example.test"', (self.root / 'playwright.config.cjs').read_text(encoding='utf-8'))

    def test_server_managed_environment_exposes_deployment_mode(self):
        self.prepare_environment()
        client, _ = self.client()
        with client, patch.dict(os.environ, {'LIEMA_UI_RECORDER_CONNECT_MODE': 'network'}):
            self.assertEqual(client.get('/api/ui-recordings/environment').json()['deployment_mode'], 'container')

    def test_browser_exit_ends_recording_and_removes_desktop(self):
        item = self.recording()
        runtime = Mock()
        runtime.healthy.return_value = False
        manager = RecorderManager(self.store, self.owner, threading.Event(), runtime=runtime)
        manager.containers.add(item['id'])
        manager.tick()
        self.assertEqual(self.recordings.get(item['id'])['status'], 'failed')
        runtime.remove.assert_called_once_with(item['id'])

    def test_browser_affinity_is_bounded_on_large_linux_hosts(self):
        from auto_test.ui_automation.runner import browser_cpu_arguments
        with patch('auto_test.ui_automation.runner.os.sched_getaffinity', return_value=set(range(24, 192)), create=True):
            self.assertEqual(browser_cpu_arguments(), ['--cpuset-cpus', '24,25'])

    def test_environment_global_changes_require_superuser_and_csrf(self):
        value = self.prepare_environment()
        self.store.update_user(self.user, is_superuser=False)
        client, _ = self.client()
        with client:
            view = client.get('/api/ui-recordings/environment')
            self.assertEqual(view.status_code, 200)
            self.assertFalse(view.json()['can_edit'])
            self.assertIsNone(view.json()['configuration'])
            self.assertEqual(client.put('/api/ui-recordings/environment', json=value).status_code, 403)
            self.store.update_user(self.user, is_superuser=True)
            client.headers.pop('X-CSRF-Token')
            self.assertEqual(client.put('/api/ui-recordings/environment', json=value).status_code, 403)
        self.assertFalse(environment.CONFIG_PATH.exists())

    def test_environment_env_overrides_are_visible_and_cannot_be_overwritten(self):
        value = self.prepare_environment()
        environment.save_options(self.store, value)
        override = 'sha256:' + 'c' * 64
        client, _ = self.client()
        with client, patch.dict(os.environ, {'LIEMA_UI_RECORDER_IMAGE': override}):
            result = client.get('/api/ui-recordings/environment').json()
            self.assertEqual(result['configuration']['recorder_image'], override)
            self.assertEqual(result['managed_fields'], ['recorder_image'])
            self.assertEqual(DockerRecorder().config['image'], override)
            self.assertEqual(client.put('/api/ui-recordings/environment', json=value).status_code, 400)
        self.assertEqual(environment.options(), value)
        with patch.dict(os.environ, {'LIEMA_UI_RECORDER_IMAGE': ''}):
            self.assertFalse(recorder_settings()['enabled'])

    def test_environment_rejects_unsafe_or_incomplete_values_without_changing_file(self):
        value = self.prepare_environment()
        environment.save_options(self.store, value)
        original = environment.CONFIG_PATH.read_bytes()
        client, _ = self.client()
        with client:
            for change in [{'runner_image': 'latest'}, {'recorder_image': 'image:tag'}, {'network': 'host'},
                           {'network': 'bridge'}, {'network': 'bad;network'}, {'network': 'none'}, {'runner_image': ''}]:
                with self.subTest(change=change):
                    self.assertEqual(client.put('/api/ui-recordings/environment', json={**value, **change}).status_code, 400)
                    self.assertEqual(environment.CONFIG_PATH.read_bytes(), original)

    def test_environment_blocks_both_live_and_expired_worker_owners(self):
        value = self.prepare_environment()
        self.assertTrue(WorkerMailbox(self.store, 'ui').acquire(self.owner))
        client, _ = self.client()
        with client:
            self.assertEqual(client.put('/api/ui-recordings/environment', json=value).status_code, 400)
            with self.store._connection() as connection:
                connection.execute("UPDATE platform_worker_leases SET expires_at=0 WHERE name='ui'")
            self.assertEqual(client.put('/api/ui-recordings/environment', json=value).status_code, 400)
        self.assertFalse(environment.CONFIG_PATH.exists())

    def test_environment_blocks_pending_runs_and_recordings(self):
        value = self.prepare_environment()
        run = self.records.enqueue(self.project, self.user, self.suite['id'])
        with self.assertRaisesRegex(ValueError, '未结束'):
            environment.save_options(self.store, value)
        self.records.stop(self.project, run['id'])
        item = self.recording(ready=False)
        with self.assertRaisesRegex(ValueError, '未结束'):
            environment.save_options(self.store, value)
        self.recordings.close(item['id'], 'cancelled', 'cancelled')
        environment.save_options(self.store, value)
        self.assertEqual(environment.options(), value)

    def test_environment_bad_file_and_failed_atomic_replace_are_recoverable(self):
        value = self.prepare_environment()
        environment.CONFIG_PATH.parent.mkdir()
        environment.CONFIG_PATH.write_text('invalid json', encoding='utf-8')
        client, _ = self.client()
        with client:
            response = client.get('/api/ui-recordings/environment')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['available'])
            self.assertTrue(response.json()['configuration_error'])
            self.assertEqual(client.put('/api/ui-recordings/environment', json=value).status_code, 200)
            original = environment.CONFIG_PATH.read_bytes()
            with patch('auto_test.ui_automation.environment.os.replace', side_effect=OSError('test write failure')):
                self.assertEqual(client.put('/api/ui-recordings/environment', json={**value, 'network': 'another-test'}).status_code, 503)
        self.assertEqual(environment.CONFIG_PATH.read_bytes(), original)
        self.assertEqual(list(environment.CONFIG_PATH.parent.glob('.ui-runtime-*.tmp')), [])

    def test_worker_detects_configuration_changed_during_preflight_and_releases_lease(self):
        value = self.prepare_environment()
        environment.save_options(self.store, {**value, 'recorder_image': ''})
        worker = UiWorker(self.store, self.artifacts)
        with patch.object(worker.runner, 'preflight', side_effect=lambda: environment.save_options(self.store, {**value, 'recorder_image': '', 'network': 'changed-network'})):
            with self.assertRaisesRegex(RuntimeError, '启动期间发生变化'):
                worker.run()
        self.assertFalse(worker.mailbox.status()['available'])
        self.assertTrue(worker.mailbox.acquire('replacement'))

    def test_worker_checks_recorder_before_marking_it_online(self):
        value = self.prepare_environment()
        environment.save_options(self.store, value)
        worker = UiWorker(self.store, self.artifacts)
        with patch.object(worker.runner, 'preflight'), patch.object(DockerRecorder, 'preflight', side_effect=ValueError('missing recorder image')):
            with self.assertRaisesRegex(ValueError, 'missing recorder image'):
                worker.run()
        self.assertFalse(worker.mailbox.status()['available'])
        self.assertTrue(worker.mailbox.acquire('replacement'))

    def test_web_launcher_loads_in_fresh_process_without_recording_dependencies(self):
        # Fresh import catches eager imports even when this test process already
        # loaded httpx for TestClient. No server socket or real platform DB.
        script = '''import asyncio, importlib.abc, json, sys
class MissingRecordingDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'httpx', 'websockets'}:
            raise ModuleNotFoundError('Recording dependency deliberately absent', name=fullname)
sys.meta_path.insert(0, MissingRecordingDependencies())
from uvicorn.importer import import_from_string
app = import_from_string('web_main:app')
assert 'httpx' not in sys.modules and 'websockets' not in sys.modules
async def check():
    messages = []
    async def receive():
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    scope = {'type':'http', 'asgi':{'version':'3.0'}, 'http_version':'1.1',
             'method':'GET', 'scheme':'http', 'path':'/api/auth/status',
             'raw_path':b'/api/auth/status', 'root_path':'', 'query_string':b'',
             'headers':[], 'server':('localhost',0), 'client':('127.0.0.1',1)}
    await asyncio.wait_for(app(scope, receive, send), 10)
    assert next(m for m in messages if m['type']=='http.response.start')['status'] == 200
    payload = json.loads(b''.join(m.get('body', b'') for m in messages if m['type']=='http.response.body'))
    assert payload['setup_required'] and not payload['authenticated']
asyncio.run(check())
print('isolated launcher and auth status OK without recording dependencies')
'''
        env = {**os.environ, 'LIEMA_DATABASE_BACKEND': 'sqlite',
               'LIEMA_SQLITE_TASKS_PATH': str(self.root / 'startup-tasks.db'),
               'LIEMA_SQLITE_PLATFORM_PATH': str(self.root / 'startup-platform.db'),
               'LIEMA_ARTIFACT_BACKEND': 'local', 'LIEMA_TASK_QUEUE_BACKEND': 'local',
               'LIEMA_TASK_EXECUTION_MODE': 'external', 'LIEMA_UI_RECORDER_IMAGE': ''}
        process = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                                 cwd=Path(__file__).resolve().parents[1], env=env,
                                 capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('isolated launcher and auth status OK', process.stdout)

    def test_missing_dependencies_disable_recording_without_breaking_ui_or_cancel(self):
        item = self.recording()
        client, _ = self.client()
        with client, patch('auto_test.ui_automation.recorder_api.recorder_settings', return_value={'enabled': True}):
            for missing in ['httpx', 'websockets.asyncio.client']:
                with self.subTest(missing=missing), patch.dict(sys.modules, {missing: None}):
                    response = client.get('/api/ui-recordings')
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()['configured'])
                    self.assertFalse(response.json()['available'])
                    self.assertIn('依赖', response.json()['message'])
                    self.assertEqual(client.get('/api/ui-automation').status_code, 200)
                    self.assertEqual(client.get('/api/ui-suites/' + self.suite['id']).status_code, 200)
                    self.assertEqual(client.post('/api/ui-recordings', json={'name': '录制', 'target_url': 'https://example.test'}).status_code, 503)
                    self.assertEqual(client.get('/ui-recorder/' + item['id'] + '/desktop').status_code, 503)
                    self.assertEqual(client.get('/ui-recorder/' + item['id'] + '/assets/core/rfb.js').status_code, 503)
                    with self.assertRaises(WebSocketDisconnect):
                        with client.websocket_connect('/ui-recorder/' + item['id'] + '/socket', headers={'Origin': 'http://testserver'}):
                            self.fail('Missing recording dependency must deny desktop access')
            with patch.dict(sys.modules, {'httpx': None, 'websockets.asyncio.client': None}):
                self.assertEqual(client.post('/api/ui-recordings/' + item['id'] + '/cancel').json()['status'], 'cancelled')

    def test_disabled_recording_does_not_load_transport_dependencies(self):
        with patch('auto_test.ui_automation.recorder_api.gateway_dependencies', side_effect=AssertionError('must stay lazy')):
            result = availability(self.store)
        self.assertFalse(result['configured'])
        self.assertFalse(result['available'])

    def test_recording_source_and_url_are_encrypted_and_public_responses_are_clean(self):
        saved = self.saved()
        snapshot = self.records.suite(self.project, saved['suite_id'])['snapshot']
        self.assertEqual(decrypt_secret(snapshot['recording_source_enc']), self.source)
        self.assertNotIn(self.secret, json.dumps(snapshot))
        self.assertEqual(saved['target_enc'], '')
        self.assertEqual(saved['gateway_secret_enc'], '')
        with self.store._connection() as connection:
            raw = connection.execute('SELECT snapshot_json FROM ui_suite_versions WHERE id=?', (saved['suite_id'],)).fetchone()['snapshot_json']
        self.assertNotIn(self.secret, raw)
        client, _ = self.client()
        with client, patch('auto_test.ui_automation.api.settings', return_value={'enabled': True}):
            for response in [client.get('/api/ui-suites/' + saved['suite_id']), client.post('/api/ui-runs', json={'suite_id': saved['suite_id']}), client.get('/api/ui-recordings/' + saved['id'])]:
                self.assertLess(response.status_code, 300)
                self.assertNotIn('recording_source_enc', response.text)
                self.assertNotIn('gateway_secret_enc', response.text)
                self.assertNotIn(self.secret, response.text)
            run = self.records.runs(self.project)['items'][0]
            self.assertNotIn('recording_source_enc', client.get('/api/ui-runs/' + run['id']).text)

    def test_save_is_idempotent_and_rerecording_retains_queued_version(self):
        saved = self.saved()
        self.assertIsNone(self.recordings.save(saved['id'], self.owner, self.source, 1))
        run = self.records.enqueue(self.project, self.user, saved['suite_id'])
        item = self.recording(suite_id=saved['suite_id'])
        self.recordings.request_save(item['id'])
        second = self.recordings.save(item['id'], self.owner, self.source + '\n// second', 2)
        self.assertEqual(second['suite_version'], 2)
        self.assertEqual(self.records.run(self.project, run['id'])['suite_version'], 1)
        with self.assertRaises(ValueError):
            self.records.save(self.project, self.user, self.data, saved['suite_id'])

    def test_no_checks_and_cancelled_capture_do_not_create_versions(self):
        item = self.recording()
        self.recordings.request_save(item['id'])
        for checks in [0, -1, True, 1001]:
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                self.recordings.save(item['id'], self.owner, self.source, checks)
        self.recordings.close(item['id'], 'cancelled', 'cancelled')
        self.assertIsNone(self.recordings.save(item['id'], self.owner, self.source, 1))
        self.assertEqual(len(self.records.suites(self.project)), 1)

    def test_lease_expiry_blocks_ready_save_and_worker_activity(self):
        item = self.recording()
        self.recordings.request_save(item['id'])
        with self.store._connection() as connection:
            connection.execute("UPDATE platform_worker_leases SET expires_at=0 WHERE name='ui'")
        self.assertIsNone(self.recordings.save(item['id'], self.owner, self.source, 1))
        runtime, stopping = Mock(), threading.Event()
        RecorderManager(self.store, self.owner, stopping, runtime).tick()
        self.assertTrue(stopping.is_set())
        runtime.capture.assert_not_called()

    def test_late_heartbeat_cannot_revive_idle_session(self):
        item = self.recording()
        with self.store._connection() as connection:
            connection.execute('UPDATE ui_recordings SET heartbeat_at=? WHERE id=?', (time.time() - 100, item['id']))
        self.assertFalse(self.recordings.touch(item['id']))
        manager = RecorderManager(self.store, self.owner, threading.Event(), Mock())
        manager.tick()
        self.assertEqual(self.recordings.get(item['id'])['status'], 'expired')

    def test_roles_are_rechecked_at_capture_commit(self):
        item = self.recording()
        self.recordings.request_save(item['id'])
        self.store.set_project_membership(self.project, self.user, 'tester')
        self.assertIsNone(self.recordings.save(item['id'], self.owner, self.source, 1))

    def test_owner_and_global_limits_and_invalid_urls(self):
        item = self.recording()
        self.store.set_project_membership(self.other, self.user, 'project_admin')
        with self.assertRaises(ValueError):
            self.recordings.create(self.other, self.user, 'duplicate', 'https://example.test')
        second = self.store.create_user('ui.recorder2', 'second', 'unusable-hash')['id']
        self.recordings.create(self.other, second, 'second', 'https://example.test')
        third = self.store.create_user('ui.recorder3', 'third', 'unusable-hash')['id']
        with self.assertRaises(ValueError):
            self.recordings.create(self.other, third, 'third', 'https://example.test')
        for url in ['file:///etc/passwd', 'javascript:alert(1)', 'https://user:pass@example.test', 'http://example.test/\nextra']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.recordings.create(self.project, self.user, 'bad', url)
        self.assertNotIn('target_enc', public_recording(item))

    def test_manager_starts_saves_and_removes_owned_container(self):
        item = self.recording(ready=False)
        runtime = Mock(); runtime.start.return_value = (45678, self.secret)
        runtime.capture.return_value = {'source': self.source, 'checks': 1}
        manager = RecorderManager(self.store, self.owner, threading.Event(), runtime)
        manager.tick()
        self.assertEqual(self.recordings.get(item['id'])['status'], 'recording')
        self.recordings.request_save(item['id']); manager.tick()
        self.assertEqual(self.recordings.get(item['id'])['status'], 'saved')
        runtime.remove.assert_called_with(item['id'])
        self.assertEqual(manager.containers, set())

    def test_manager_retries_missing_checks_and_cleans_cancelled_browser(self):
        item = self.recording()
        runtime = Mock(); runtime.capture.return_value = {'source': self.source, 'checks': 0}
        manager = RecorderManager(self.store, self.owner, threading.Event(), runtime)
        manager.containers.add(item['id'])
        self.recordings.request_save(item['id']); manager.tick()
        runtime.resume.assert_called_once()
        self.assertEqual(self.recordings.get(item['id'])['status'], 'recording')
        self.recordings.close(item['id'], 'cancelled', 'cancelled'); manager.tick()
        runtime.remove.assert_called_with(item['id'])

    def test_recording_can_be_saved_after_adding_a_missing_checkpoint(self):
        item = self.recording()
        runtime = Mock(); runtime.capture.return_value = {'source': self.source, 'checks': 0}
        manager = RecorderManager(self.store, self.owner, threading.Event(), runtime)
        manager.containers.add(item['id'])
        self.recordings.request_save(item['id']); manager.tick()
        retry = self.recordings.get(item['id'])
        self.assertEqual(retry['status'], 'recording')
        self.assertIn('如何添加验证', retry['message'])
        self.assertFalse(retry['suite_id'])
        runtime.remove.assert_not_called()
        runtime.capture.return_value = {'source': self.source, 'checks': 1}
        self.recordings.request_save(item['id']); manager.tick()
        saved = self.recordings.get(item['id'])
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['suite_version'], 1)
        self.assertEqual(saved['checks'], 1)
        self.assertEqual(runtime.capture.call_count, 2)
        runtime.resume.assert_called_once()
        runtime.remove.assert_called_once_with(item['id'])

    def test_http_owner_project_csrf_permissions_origin_and_offline_gates(self):
        item = self.recording(); prefix = '/api/ui-recordings/' + item['id']
        client, _ = self.client()
        with client:
            self.assertEqual(client.post('/api/ui-recordings', json={'name': '录制', 'target_url': 'https://example.test'}).status_code, 503)
            self.assertEqual(client.post(prefix + '/save', headers={'X-CSRF-Token': ''}).status_code, 403)
            self.assertEqual(client.get(prefix).status_code, 200)
            page = client.get('/ui-recorder/' + item['id'] + '/desktop')
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(self.secret, page.text)
            self.assertNotIn('45678', page.text)
            self.assertIn('no-store', page.headers['cache-control'])
            self.assertEqual(client.get('/ui-recorder/' + item['id'] + '/desktop', headers={'Origin': 'https://evil.test'}).status_code, 403)
            self.store.set_project_membership(self.other, self.user, 'project_admin')
            self.assertEqual(client.get(prefix, headers={'X-Project-ID': self.other}).status_code, 404)
            self.store.set_project_membership(self.project, self.user, 'tester')
            self.assertEqual(client.get(prefix).status_code, 403)
            self.assertEqual(client.get('/ui-recorder/' + item['id'] + '/desktop').status_code, 403)
            client.cookies.clear()
            self.assertEqual(client.get('/ui-recorder/' + item['id'] + '/desktop').status_code, 401)

    def test_other_member_cannot_open_recording_even_if_superuser(self):
        item = self.recording()
        client, service = self.client()
        other_user = self.store.create_user('ui.other', '其他管理员', 'unusable-hash', is_superuser=True)['id']
        token, _ = service.create_session(other_user)
        client.cookies.set('liema_session', token)
        with client:
            self.assertEqual(client.get('/api/ui-recordings/' + item['id']).status_code, 404)
            self.assertEqual(client.get('/ui-recorder/' + item['id'] + '/desktop').status_code, 404)

    def test_websocket_requires_same_origin_and_authorized_session(self):
        item = self.recording(); client, _ = self.client()
        with client:
            for headers in [{}, {'Origin': 'https://evil.test'}]:
                with self.subTest(headers=headers), self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect('/ui-recorder/' + item['id'] + '/socket', headers=headers):
                        self.fail('unauthorized websocket accepted')

    def test_websocket_forwards_binary_and_revokes_open_connection(self):
        from websockets.sync.server import serve
        seen = []
        def echo(connection):
            seen.append(connection.request.headers.get('Authorization'))
            try:
                for message in connection: connection.send(message)
            except Exception:
                pass
        server = serve(echo, '127.0.0.1', 0, subprotocols=['binary'])
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(3)))
        item = self.recording()
        with self.store._connection() as connection:
            connection.execute('UPDATE ui_recordings SET gateway_port=? WHERE id=?', (server.socket.getsockname()[1], item['id']))
        client, _ = self.client()
        with client:
            with client.websocket_connect('/ui-recorder/' + item['id'] + '/socket', headers={'Origin': 'http://testserver'}, subprotocols=['binary']) as socket:
                socket.send_bytes(b'RFB synthetic frame')
                self.assertEqual(socket.receive_bytes(), b'RFB synthetic frame')
                self.store.set_project_membership(self.project, self.user, 'viewer')
                with self.assertRaises(WebSocketDisconnect):
                    socket.receive_bytes()
        self.assertEqual(seen, ['Bearer ' + self.secret])

    def archive(self):
        archive = io.BytesIO()
        raw = json.dumps({'suites': [{'specs': [{'title': self.secret, 'tests': [{'results': [{'status': 'passed', 'duration': 12}]}]}]}]}).encode()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name, payload in [('playwright.json', raw), ('test-results/screen.png', self.secret.encode())]:
                member = tarfile.TarInfo(name); member.size = len(payload); tar.addfile(member, io.BytesIO(payload))
        archive.seek(0)
        return archive

    def test_recorded_artifacts_are_encrypted_and_report_names_are_sanitized(self):
        output = self.root / 'encrypted'; output.mkdir()
        manifest = extract_artifacts(self.archive(), output, encrypted=True)
        self.assertTrue(all(entry['encrypted'] for entry in manifest))
        for file in output.iterdir(): self.assertNotIn(self.secret.encode(), file.read_bytes())
        status, result = summarize_result(output / 'playwright.json.enc', 0, encrypted=True)
        self.assertEqual(status, 'succeeded')
        self.assertEqual(result['cases'][0]['name'], '录制用例 1')
        self.assertIn(self.secret.encode(), decrypt_artifact(output / 'playwright.json.enc'))

    def test_runner_injects_recording_only_through_stdin_and_keeps_host_encrypted(self):
        saved = self.saved()
        run = self.records.enqueue(self.project, self.user, saved['suite_id'])
        runner = DockerUiRunner({'enabled': True, 'image': 'sha256:' + '1' * 64, 'network': 'none'})
        commands, inputs = [], []
        archive = self.archive().getvalue()
        def command(args, **kwargs):
            commands.append(args)
            if 'input' in kwargs: inputs.append(kwargs['input'])
            if args[0] == 'exec' and args[2] == 'tar': return SimpleNamespace(returncode=0, stdout=archive)
            return SimpleNamespace(returncode=0, stdout=b'0')
        with patch.object(runner, 'command', side_effect=command):
            status, result = runner.run(run, self.root / 'run', lambda: False)
        self.assertEqual(status, 'succeeded')
        self.assertEqual(inputs[-1], self.source.encode())
        self.assertEqual(len(inputs), 2)
        self.assertNotIn(self.secret.encode(), inputs[0])
        self.assertIn('playwright.config.cjs', json.loads(inputs[0]))
        self.assertNotIn(self.secret, str(commands))
        for path in (self.root / 'run').rglob('*'):
            if path.is_file(): self.assertNotIn(self.secret.encode(), path.read_bytes())
        prepare_input(run['snapshot'], self.root)
        self.assertNotIn(self.secret, (self.root / 'recorded.spec.js').read_text('utf-8'))
        self.assertIn('/tmp/liema-tests', (self.root / 'playwright.config.cjs').read_text())

    def test_encrypted_artifact_download_is_project_scoped_and_decrypts_on_demand(self):
        saved = self.saved(); run = self.records.enqueue(self.project, self.user, saved['suite_id'])
        claimed = self.records.claim(self.owner)
        output = self.artifacts.workspace('ui-runs', self.project, run['id'])
        manifest = extract_artifacts(self.archive(), output, encrypted=True)
        self.records.finish(claimed, self.owner, 'succeeded', {'artifacts': manifest}, self.artifacts.reference(output))
        client, _ = self.client()
        with client:
            url = '/api/ui-runs/' + run['id'] + '/download?name=playwright.json'
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(self.secret, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.store.set_project_membership(self.other, self.user, 'viewer')
            self.assertEqual(client.get(url, headers={'X-Project-ID': self.other}).status_code, 404)

    def test_recorder_docker_boundary_and_stdin_secret_transport(self):
        config = {'image': 'sha256:' + '1' * 64, 'network': 'test-internal', 'enabled': True}
        runtime = DockerRecorder(config)
        item = self.recording(ready=False); calls, inputs = [], []
        def command(args, **kwargs):
            calls.append(args)
            if 'input' in kwargs: inputs.append(json.loads(kwargs['input']))
            if args[0] == 'inspect': return SimpleNamespace(stdout=b'{"6080/tcp":[{"HostIp":"127.0.0.1","HostPort":"45678"}]}')
            return SimpleNamespace(stdout=b'true')
        with patch.object(runtime, 'command', side_effect=command), patch.object(runtime, 'request', return_value={'ready': True}):
            port, secret = runtime.start(item, lambda: False)
        self.assertEqual(port, 45678)
        self.assertNotIn(secret, str(calls)); self.assertNotIn(self.secret, str(calls))
        self.assertEqual(inputs[0]['secret'], secret)
        create = next(args for args in calls if args[0] == 'create')
        for arg in ['127.0.0.1::6080', '--read-only', '--cap-drop', 'ALL', 'no-new-privileges', '--memory']:
            self.assertIn(arg, create)
        self.assertNotIn('--mount', create)
        self.assertNotIn('--privileged', create)
        with patch.dict(os.environ, {'LIEMA_UI_RECORDER_IMAGE': config['image']}), self.assertRaises(ValueError):
            recorder_settings()

    def test_recorder_start_failure_removes_only_its_container(self):
        item = self.recording(ready=False)
        runtime = DockerRecorder({'image': 'sha256:' + '1' * 64, 'network': 'test-internal', 'enabled': True})
        with patch.object(runtime, 'preflight'), patch.object(runtime, 'command', side_effect=RuntimeError), patch.object(runtime, 'remove') as remove:
            with self.assertRaises(RuntimeError): runtime.start(item, lambda: False)
        remove.assert_called_once_with(item['id'])
        name = 'liema-recorder-' + item['id']
        unrelated = 'liema-recorder-' + secrets.token_hex(16)
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout=(name + '\n' + unrelated + '\n').encode())
        with patch.object(runtime, 'command', side_effect=command):
            runtime.recover([item['id']])
        self.assertEqual([args for args in calls if args[0] == 'rm'], [['rm', '-f', name]])
