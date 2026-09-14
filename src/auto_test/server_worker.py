"""Dedicated server worker; no HTTP listener and no business credentials in RPC rows."""

from __future__ import annotations

import argparse
import concurrent.futures
import signal
import threading
import time
import uuid

from auto_test.common.logging import log
from auto_test.common.paths import PROJECT_ROOT, prepare_runtime_layout
from auto_test.common.runtime_secrets import ensure_runtime_master_key
from auto_test.monitoring.server_sessions import ServerSessionManager
from auto_test.monitoring.server_stress import ServerStressManager
from auto_test.platform.artifact_storage import create_artifact_storage
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.worker_rpc import WorkerMailbox


class ServerWorker:
    def __init__(self, store, artifacts=None, *, sessions=None, stress=None):
        self.store = store
        self.mailbox = WorkerMailbox(store)
        self.owner = uuid.uuid4().hex
        self.sessions = sessions or ServerSessionManager(store)
        self.stress = stress or ServerStressManager(store, artifacts, session_manager=self.sessions)
        self.stopping = threading.Event()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='server-command')
        self.futures = set()
        self.connect_lock = threading.Lock()

    def execute(self, command):
        operation = command['operation']
        allowed = {
            'sessions.connect': self.sessions.connect, 'sessions.list_sessions': self.sessions.list_sessions,
            'sessions.get': self.sessions.get, 'sessions.metrics': self.sessions.metrics,
            'sessions.capability': self.sessions.capability, 'sessions.close': self.sessions.close,
            'sessions.delete_profile': self.sessions.delete_profile,
            'stress.submit': self.stress.submit, 'stress.probe': self.stress.probe,
        }
        value = None
        try:
            if self.stopping.is_set() or operation not in allowed:
                raise RuntimeError('操作不可用')
            payload = command['payload']
            if operation in {'sessions.connect', 'sessions.delete_profile'}:
                with self.connect_lock:
                    if self.stopping.is_set():
                        raise RuntimeError('Worker 正在停止')
                    value = allowed[operation](*payload['args'], **payload['kwargs'])
            else:
                value = allowed[operation](*payload['args'], **payload['kwargs'])
            response = {'value': value}
        except Exception as exc:
            # Never copy upstream exception strings: SSH errors may include credentials.
            response = {'error': type(exc).__name__, 'message': '服务器操作未完成，请检查连接、会话占用和服务器配置'}
        try:
            accepted = self.mailbox.finish(self.owner, command['id'], response)
        except Exception:
            accepted = False
        # An unacknowledged connection may be a reused session. Leave its regular
        # idle expiry in charge rather than closing another request's session.
        if not accepted and value and operation == 'stress.submit':
            self.stress.request_stop(value['id'])

    def run(self, *, recover_stale=False):
        if not self.mailbox.acquire(self.owner, recover_stale=recover_stale):
            raise RuntimeError('服务器 Worker 已被另一进程持有；异常退出后确认旧进程停止，再使用 --recover-stale')
        def heartbeat():
            while not self.stopping.wait(2):
                try:
                    if self.mailbox.renew(self.owner):
                        continue
                except Exception:
                    pass
                self.stopping.set()
                break
        thread = threading.Thread(target=heartbeat, daemon=True, name='server-worker-lease')
        try:
            # Only the exclusive owner recovers abandoned stress execution.
            with self.store._connection() as connection:
                connection.execute("UPDATE stress_jobs SET status='interrupted',message='服务器 Worker 重启，需重新连接并授权',finished_at=? WHERE status='running'", (time.time(),))
            self.stress.start()
            thread.start()
            log.info('独立服务器 Worker 已启动，负责 SSH 会话、采样和压测')
            while not self.stopping.is_set():
                self.futures = {future for future in self.futures if not future.done()}
                if len(self.futures) < 4:
                    command = self.mailbox.claim(self.owner)
                    if command:
                        self.futures.add(self.pool.submit(self.execute, command))
                        continue
                self.stopping.wait(.1)
        finally:
            self.stopping.set()
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.stress.stop()
            if thread.is_alive():
                thread.join(timeout=3)
            self.mailbox.release(self.owner)


def main():
    parser = argparse.ArgumentParser(description='烈马独立服务器 Worker')
    parser.add_argument('--recover-stale', action='store_true', help='仅在确认旧 Worker 已停止后，回收已过期的所有权')
    args = parser.parse_args()
    prepare_runtime_layout(PROJECT_ROOT)
    ensure_runtime_master_key()
    store = create_platform_repository(PROJECT_ROOT, recover_jobs=False)
    worker = ServerWorker(store, create_artifact_storage(PROJECT_ROOT))
    for name in ('SIGINT', 'SIGTERM'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: worker.stopping.set())
    try:
        worker.run(recover_stale=args.recover_stale)
    finally:
        close = getattr(store, 'close', None)
        if close:
            close()


if __name__ == '__main__':
    main()
