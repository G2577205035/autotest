"""Web-side proxies: all live SSH and stress execution belongs to server-worker."""

from auto_test.common.env import get_env
from auto_test.monitoring.server_sessions import ServerSessionManager
from auto_test.platform.worker_rpc import WorkerMailbox


def server_execution_mode():
    mode = str(get_env('SERVER_EXECUTION_MODE', 'embedded')).strip().lower()
    if mode not in {'embedded', 'external'}:
        raise ValueError('LIEMA_SERVER_EXECUTION_MODE 必须为 embedded 或 external')
    return mode


class RemoteServerSessions(ServerSessionManager):
    def __init__(self, store):
        super().__init__(store)
        self.mailbox = WorkerMailbox(store)

    def start(self):
        pass

    def shutdown(self, timeout=5):
        pass

    def delete_profile(self, profile_id):
        return self.mailbox.call('sessions.delete_profile', profile_id, timeout=10)

    def connect(self, options):
        return self.mailbox.call('sessions.connect', options, timeout=180)

    def list_sessions(self):
        if not self.mailbox.status()['available']:
            return []
        return self.mailbox.call('sessions.list_sessions', timeout=10)

    def get(self, session_id, *, touch=False):
        return self.mailbox.call('sessions.get', session_id, touch=touch, timeout=10)

    def metrics(self, session_id, after_id=0, limit=5000):
        return self.mailbox.call('sessions.metrics', session_id, after_id=after_id, limit=limit, timeout=15)

    def capability(self, session_id):
        return self.mailbox.call('sessions.capability', session_id, timeout=10)

    def acquire(self, session_id):
        raise RuntimeError('SSH 连接只能由独立服务器 Worker 领取')

    def close(self, session_id, *, force=False, reason='用户关闭连接'):
        return self.mailbox.call('sessions.close', session_id, force=force, reason=reason, timeout=30)


class RemoteServerStress:
    def __init__(self, store, sessions):
        self.store, self.session_manager = store, sessions
        self.mailbox = WorkerMailbox(store)

    def start(self):
        pass

    def stop(self, timeout=5):
        pass

    def submit(self, options):
        return self.mailbox.call('stress.submit', options, timeout=30)

    def probe(self, options):
        return self.mailbox.call('stress.probe', options, timeout=180)

    def request_stop(self, job_id):
        # Stop is durable even if the owning process is temporarily unavailable.
        return self.store.request_stop_stress_job(job_id)
