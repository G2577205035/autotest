import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests import bootstrap  # noqa: F401
from auto_test.platform.store import PlatformStore
from auto_test.platform.worker_rpc import WorkerMailbox
from auto_test.monitoring.remote_server import RemoteServerSessions, RemoteServerStress
from auto_test.server_worker import ServerWorker


class ServerWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.dict(os.environ, {'LIEMA_MASTER_KEY': secrets.token_urlsafe(32)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.path = Path(self.temp.name) / 'platform.db'
        self.store = PlatformStore(self.path, recover_jobs=False)
        self.mailbox = WorkerMailbox(self.store)

    def test_exclusive_ownership_renewal_and_explicit_stale_recovery(self):
        self.assertTrue(self.mailbox.acquire('first'))
        other = WorkerMailbox(PlatformStore(self.path, recover_jobs=False))
        self.assertFalse(other.acquire('second'))
        self.assertTrue(self.mailbox.renew('first'))
        with self.store._connection() as connection:
            connection.execute("UPDATE platform_worker_leases SET expires_at=0 WHERE name='server'")
        self.assertFalse(other.acquire('second'))
        self.assertFalse(self.mailbox.renew('first'))
        self.assertTrue(other.acquire('second', recover_stale=True))
        self.mailbox.release('first')
        self.assertEqual(other.status()['owner'], 'second')

    def test_mailbox_encrypts_payload_destroys_on_claim_and_consumes_result(self):
        self.mailbox.acquire('owner')
        secret = secrets.token_urlsafe(24)
        identifier = self.mailbox.submit('sessions.connect', [{'password': secret}], {})
        with self.store._connection() as connection:
            row = dict(connection.execute('SELECT * FROM platform_worker_commands WHERE id=?', (identifier,)).fetchone())
        self.assertNotIn(secret, json.dumps(row))
        command = self.mailbox.claim('owner')
        self.assertEqual(command['payload']['args'][0]['password'], secret)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute('SELECT payload_enc FROM platform_worker_commands WHERE id=?', (identifier,)).fetchone()['payload_enc'], '')
        self.assertTrue(self.mailbox.finish('owner', identifier, {'value': {'id': 'session'}}))
        self.assertEqual(self.mailbox.receive(identifier), {'value': {'id': 'session'}})
        self.assertIsNone(self.mailbox.receive(identifier))

    def test_expired_cancelled_and_old_owner_commands_cannot_complete(self):
        self.mailbox.acquire('owner')
        identifier = self.mailbox.submit('sessions.list_sessions', [], {})
        self.assertIsNone(self.mailbox.claim('intruder'))
        self.mailbox.claim('owner')
        self.assertFalse(self.mailbox.finish('intruder', identifier, {'value': []}))
        self.mailbox.cancel(identifier)
        self.assertFalse(self.mailbox.finish('owner', identifier, {'value': []}))
        expired = self.mailbox.submit('sessions.list_sessions', [], {}, timeout=-1)
        self.assertIsNone(self.mailbox.claim('owner'))
        self.assertIsNone(self.mailbox.receive(expired))

    def test_two_web_clients_share_live_session_via_worker(self):
        sessions, stress = Mock(), Mock()
        sessions.connect.return_value = {'id': 'shared-session', 'project_id': 'project'}
        sessions.get.return_value = sessions.connect.return_value
        sessions.metrics.return_value = {'metrics': [{'id': 1, 'cpu_pct': 12}], 'last_id': 1}
        worker = ServerWorker(self.store, sessions=sessions, stress=stress)
        thread = threading.Thread(target=worker.run)
        thread.start()
        self.addCleanup(lambda: (worker.stopping.set(), thread.join(timeout=10)))
        deadline = time.monotonic() + 5
        while not self.mailbox.status()['available'] and time.monotonic() < deadline:
            time.sleep(.01)
        web_a = RemoteServerSessions(self.store)
        web_b = RemoteServerSessions(PlatformStore(self.path, recover_jobs=False))
        self.assertEqual(web_a.connect({'_project_id': 'project'})['id'], 'shared-session')
        self.assertEqual(web_b.get('shared-session')['project_id'], 'project')
        self.assertEqual(web_b.metrics('shared-session')['last_id'], 1)
        sessions.connect.assert_called_once()
        self.assertIsNone(web_a._reaper)
        worker.stopping.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.mailbox.status()['available'])

    def test_unknown_operation_and_exception_messages_are_not_exposed(self):
        sessions, stress = Mock(), Mock()
        worker = ServerWorker(self.store, sessions=sessions, stress=stress)
        self.addCleanup(worker.pool.shutdown)
        self.mailbox.acquire(worker.owner)
        secret = secrets.token_urlsafe(20)
        sessions.connect.side_effect = RuntimeError(secret)
        for operation in ('sessions.connect', '__import__'):
            identifier = self.mailbox.submit(operation, [{}], {})
            worker.execute(self.mailbox.claim(worker.owner))
            result = self.mailbox.receive(identifier)
            self.assertIn('error', result)
            self.assertNotIn(secret, json.dumps(result))

    def test_external_web_restart_does_not_interrupt_owned_stress_job(self):
        run = self.store.create_stress_job({'host': 'example.test', 'user': 'tester', 'modes': ['monitor']})
        with self.store._connection() as connection:
            connection.execute("UPDATE stress_jobs SET status='running' WHERE id=?", (run['id'],))
        with patch.dict(os.environ, {'LIEMA_SERVER_EXECUTION_MODE': 'external'}):
            second = PlatformStore(self.path, recover_jobs=True)
        self.assertEqual(second.get_stress_job(run['id'])['status'], 'running')

    def test_unavailable_worker_does_not_open_local_ssh(self):
        proxy = RemoteServerSessions(self.store)
        with patch('auto_test.monitoring.server_sessions.SSHClient') as ssh:
            with self.assertRaisesRegex(RuntimeError, '未在线'):
                proxy.connect({'host': 'example.test'})
            ssh.assert_not_called()
        self.assertEqual(proxy.list_sessions(), [])
