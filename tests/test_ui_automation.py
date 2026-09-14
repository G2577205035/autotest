import io
import json
import os
import secrets
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import bootstrap  # noqa: F401
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auto_test.platform.store import PlatformStore
from auto_test.platform.identity import IdentityService, install_identity_guard
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.worker_rpc import WorkerMailbox
from auto_test.ui_automation.api import register_ui_api
from auto_test.ui_automation.store import UiStore, validate_suite
from auto_test.ui_automation.runner import DockerUiRunner, extract_artifacts, summarize_result, settings
from auto_test.ui_worker import UiWorker


class UiAutomationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = patch('auto_test.ui_automation.environment.CONFIG_PATH', self.root / 'config' / 'ui-runtime.local.json')
        config.start(); self.addCleanup(config.stop)
        env = patch.dict(os.environ, {'LIEMA_MASTER_KEY': secrets.token_urlsafe(32), 'LIEMA_UI_RUNNER_IMAGE': '', 'LIEMA_UI_RUNNER_NETWORK': 'none'})
        env.start(); self.addCleanup(env.stop)
        self.store = PlatformStore(self.root / 'db.sqlite', recover_jobs=False)
        self.project = self.store.create_project('UI1', 'UI 测试')['id']
        self.other = self.store.create_project('UI2', '其他项目')['id']
        self.user = self.store.create_user('ui.tester', 'UI 测试员', 'unusable-hash')['id']
        self.store.set_project_membership(self.project, self.user, 'project_admin')
        self.records = UiStore(self.store)
        self.artifacts = LocalArtifactStorage(self.root / 'artifacts')
        self.data = {'name': '页面检查', 'files': [{'name': 'smoke.spec.js', 'content': "const { test } = require('@playwright/test'); test('example', async () => {});"}], 'parameters': {}}
        self.suite = self.records.save(self.project, self.user, self.data)

    def test_versions_snapshots_history_and_owner_claim(self):
        run = self.records.enqueue(self.project, self.user, self.suite['id'])
        new = self.records.save(self.project, self.user, {**self.data, 'name': '新版'}, self.suite['id'])
        self.assertEqual(new['version'], 2)
        self.assertEqual(self.records.run(self.project, run['id'])['snapshot']['name'], '页面检查')
        self.assertIsNone(self.records.run(self.other, run['id']))
        self.assertIsNone(self.records.claim('non-owner'))
        with self.assertRaises(ValueError):
            self.records.delete_suite(self.project, self.suite['id'])
        mailbox = WorkerMailbox(self.store, 'ui')
        self.assertTrue(mailbox.acquire('owner'))
        claimed = self.records.claim('owner')
        self.assertEqual(claimed['id'], run['id'])
        self.assertIsNone(self.records.claim('owner'))
        self.assertFalse(self.records.finish(claimed, 'other', 'succeeded', {}))
        with self.store._connection() as connection:
            connection.execute("UPDATE platform_worker_leases SET expires_at=0 WHERE name='ui'")
        self.assertFalse(self.records.finish(claimed, 'owner', 'succeeded', {}))
        self.assertTrue(mailbox.acquire('owner', recover_stale=True))
        self.assertTrue(self.records.finish(claimed, 'owner', 'failed', {'message': 'failed'}))
        self.records.delete_suite(self.project, self.suite['id'])
        self.assertEqual(self.records.runs(self.project)['total'], 1)
        self.assertEqual(self.records.run(self.project, run['id'])['snapshot']['name'], '页面检查')

    def test_queued_stop_and_import_guards(self):
        run = self.records.enqueue(self.project, self.user, self.suite['id'])
        self.assertTrue(self.records.stop(self.project, run['id']))
        self.assertEqual(self.records.run(self.project, run['id'])['status'], 'stopped')
        self.assertGreater(self.records.run(self.project, run['id'])['finished_at'], 0)
        for changes in [{'files': [{'name': '../smoke.spec.js', 'content': 'x'}]}, {'files': [{'name': 'playwright.config.js', 'content': 'x'}]}, {'parameters': {'password': secrets.token_urlsafe(20)}}, {'base_url': 'file:///etc/passwd'}, {'files': self.data['files'] * 2}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_suite({**self.data, **changes})

    def test_docker_boundary_defaults_and_fixed_arguments(self):
        self.assertFalse(settings()['enabled'])
        with patch.dict(os.environ, {'LIEMA_UI_RUNNER_IMAGE': 'latest'}), self.assertRaises(ValueError):
            settings()
        with patch.dict(os.environ, {'LIEMA_UI_RUNNER_NETWORK': 'host'}), self.assertRaises(ValueError):
            settings()
        runner = DockerUiRunner({'enabled': True, 'image': 'sha256:' + '1' * 64, 'network': 'none'})
        args = runner.create_arguments('liema-ui-' + '1' * 32, self.root)
        for value in ['--read-only', '--cap-drop', '--pids-limit', '--memory', '--cpus', 'no-new-privileges', '1000:1000', 'none']:
            self.assertIn(value, args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('docker.sock', str(args))
        self.assertFalse(any(value.startswith('type=bind,') for value in args))
        self.assertIn('/work/tests:rw,nosuid,nodev,size=8m,mode=1777', args)

    def test_artifact_path_and_link_escape_are_rejected(self):
        for name, kind in [('../escape.txt', tarfile.REGTYPE), ('/escape.txt', tarfile.REGTYPE), ('link.png', tarfile.SYMTYPE), ('C:/escape.txt', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode='w') as tar:
                    member = tarfile.TarInfo(name); member.type = kind; member.size = 1 if kind == tarfile.REGTYPE else 0
                    tar.addfile(member, io.BytesIO(b'x') if member.size else None)
                archive.seek(0)
                with self.assertRaises(ValueError):
                    extract_artifacts(archive, self.root / 'output')
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_zero_tests_skips_expected_failure_and_process_errors_do_not_pass(self):
        path = self.root / 'result.json'
        for status, expected_status, exit_code, expected in [('passed', 'passed', 0, 'succeeded'), ('skipped', 'passed', 0, 'failed'), ('failed', 'failed', 0, 'failed'), ('passed', 'passed', 1, 'failed')]:
            path.write_text(json.dumps({'suites': [{'specs': [{'title': 'case', 'tests': [{'expectedStatus': expected_status, 'results': [{'status': status, 'duration': 20}]}]}]}]}))
            self.assertEqual(summarize_result(path, exit_code)[0], expected)
        path.write_text('{"suites": []}')
        self.assertEqual(summarize_result(path, 0)[0], 'failed')

    def test_runner_copies_artifacts_then_removes_container_on_success_and_stop(self):
        payload = json.dumps({'suites': [{'specs': [{'title': '合成用例', 'tests': [{'results': [{'status': 'passed', 'duration': 12}]}]}]}]}).encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            member = tarfile.TarInfo('playwright.json'); member.size = len(payload); tar.addfile(member, io.BytesIO(payload))
        runner = DockerUiRunner({'enabled': True, 'image': 'sha256:' + '1' * 64, 'network': 'none'})
        run = self.records.enqueue(self.project, self.user, self.suite['id'])
        commands = []
        def command(args, **kwargs):
            commands.append(args)
            if args[0] == 'exec' and args[2] == 'tar': kwargs['stdout'].write(archive.getvalue())
            return SimpleNamespace(returncode=0, stdout=b'0')
        with patch.object(runner, 'command', side_effect=command):
            status, result = runner.run(run, self.root / 'output', lambda: False)
            self.assertEqual(status, 'succeeded')
            self.assertEqual(result['passed'], 1)
            self.assertEqual(commands[-1][0:2], ['rm', '-f'])
            self.assertTrue(any(args[0] == 'exec' and args[2:7] == ['tar', '-C', '/results', '-cf', '-'] for args in commands))
            self.assertFalse(any(args[0] == 'cp' for args in commands))
            status, _ = runner.run(run, self.root / 'stopped', lambda: True)
            self.assertEqual(status, 'stopped')
            self.assertEqual(commands[-1][0:2], ['rm', '-f'])

    def test_worker_rechecks_permission_and_generates_readable_failure_report(self):
        run = self.records.enqueue(self.project, self.user, self.suite['id'])
        runner = Mock()
        worker = UiWorker(self.store, self.artifacts, runner)
        self.assertTrue(worker.mailbox.acquire(worker.owner))
        self.store.set_project_membership(self.project, self.user, 'viewer')
        completed = worker.run_once()
        runner.run.assert_not_called()
        self.assertEqual(completed['id'], run['id'])
        self.assertEqual(completed['status'], 'failed')
        self.assertEqual(completed['result']['error_type'], 'PermissionError')
        root = self.artifacts.resolve(completed['artifact_ref'])
        self.assertTrue((root / 'report.pdf').read_bytes().startswith(b'%PDF'))
        self.assertTrue((root / 'report.docx').is_file())

    def test_api_csrf_roles_offline_submission_and_cross_project_download(self):
        app = FastAPI(); service = IdentityService(self.store)
        install_identity_guard(app, service)
        app.include_router(register_ui_api(self.store, self.artifacts), prefix='/api')
        token, csrf = service.create_session(self.user)
        with TestClient(app) as client:
            client.cookies.set('liema_session', token)
            client.headers.update({'X-Project-ID': self.project, 'X-CSRF-Token': csrf})
            self.assertEqual(client.post('/api/ui-suites', json=self.data, headers={'X-CSRF-Token': ''}).status_code, 403)
            self.assertEqual(client.post('/api/ui-runs', json={'suite_id': self.suite['id']}).status_code, 503)
            self.assertEqual(client.get('/api/ui-automation').json()['runner']['available'], False)
            self.store.set_project_membership(self.project, self.user, 'tester')
            self.assertEqual(client.post('/api/ui-suites', json=self.data).status_code, 403)
            run = self.records.enqueue(self.project, self.user, self.suite['id'])
            self.assertEqual(client.post('/api/ui-runs/' + run['id'] + '/stop').status_code, 200)
            self.store.set_project_membership(self.other, self.user, 'viewer')
            client.headers['X-Project-ID'] = self.other
            self.assertEqual(client.get('/api/ui-runs/' + run['id']).status_code, 404)
            self.assertEqual(client.get('/api/ui-runs/' + run['id'] + '/download?name=report.pdf').status_code, 404)
