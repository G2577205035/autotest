"""Short-lived, owner-scoped recording sessions in the platform database."""

import hashlib
import secrets
import time
import uuid
from urllib.parse import urlsplit

from auto_test.platform.secrets import encrypt_secret
from auto_test.ui_automation.store import UiStore, validate_suite


ACTIVE = ('queued', 'starting', 'recording', 'saving')
ACTIVE_SQL = "('queued','starting','recording','saving')"
LIFETIME = 900
IDLE_TIMEOUT = 90


def authorized(store, item):
    user, project = store.get_user(item['created_by']), store.get_project(item['project_id'])
    return bool(user and user.get('is_active') and project and project.get('is_active') and (
        user.get('is_superuser') or any(p['id'] == item['project_id'] and p.get('role') == 'project_admin'
                                      for p in store.list_user_projects(user['id']))))


def public_recording(item):
    return {key: item[key] for key in ('id', 'project_id', 'name', 'status', 'created_at', 'expires_at',
                                      'message', 'suite_id', 'suite_version', 'checks')}


class RecordingStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def initialize(connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS ui_recordings (
            id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, created_by VARCHAR(64) NOT NULL,
            name VARCHAR(120) NOT NULL, target_enc LONGTEXT NOT NULL, status VARCHAR(24) NOT NULL,
            owner VARCHAR(64) NOT NULL, created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL,
            heartbeat_at DOUBLE NOT NULL, gateway_port INTEGER NOT NULL, gateway_secret_enc LONGTEXT NOT NULL,
            message VARCHAR(300) NOT NULL, suite_id VARCHAR(64) NOT NULL, suite_version INTEGER NOT NULL,
            checks INTEGER NOT NULL)''')

    def get(self, identifier):
        with self.store._connection() as connection:
            row = connection.execute('SELECT * FROM ui_recordings WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def active(self):
        with self.store._connection() as connection:
            rows = connection.execute('SELECT * FROM ui_recordings WHERE status IN ' + ACTIVE_SQL + ' ORDER BY created_at,id').fetchall()
        return [dict(row) for row in rows]

    def create(self, project, actor, name, target, suite_id=''):
        name, target = name.strip(), target.strip()
        parsed = urlsplit(target)
        if not 1 <= len(name) <= 120 or not 1 <= len(target) <= 2000:
            raise ValueError('填写套件名称与测试网站地址')
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in target):
            raise ValueError('测试网站需为不含账号密码的 HTTP(S) 地址')
        # URLs can contain session tokens; keep them out of ordinary records too.
        encrypted = encrypt_secret(target)
        now, identifier = time.time(), uuid.uuid4().hex
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            rows = connection.execute('SELECT created_by FROM ui_recordings WHERE status IN ' + ACTIVE_SQL).fetchall()
            if len(rows) >= 2 or any(row['created_by'] == actor for row in rows):
                raise ValueError('每人同时只能录制一个会话，平台最多两个；请先结束已有录制')
            if suite_id and not connection.execute('SELECT 1 FROM ui_suite_versions WHERE id=? AND project_id=?', (suite_id, project)).fetchone():
                raise KeyError(suite_id)
            connection.execute('''INSERT INTO ui_recordings
                (id,project_id,created_by,name,target_enc,status,owner,created_at,expires_at,heartbeat_at,
                 gateway_port,gateway_secret_enc,message,suite_id,suite_version,checks)
                VALUES(?,?,?,?,?,'queued','',?,?,?,0,'','等待打开录制窗口',?,0,0)''',
                (identifier, project, actor, name, encrypted, now, now + LIFETIME, now, suite_id))
        return self.get(identifier)

    @staticmethod
    def lease(connection, owner):
        row = connection.execute("SELECT owner,expires_at FROM platform_worker_leases WHERE name='ui'").fetchone()
        return bool(row and row['owner'] == owner and row['expires_at'] > time.time())

    def claim(self, identifier, owner):
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            if not self.lease(connection, owner):
                return False
            return bool(connection.execute("UPDATE ui_recordings SET status='starting',owner=?,message='正在打开浏览器' WHERE id=? AND status='queued'", (owner, identifier)).rowcount)

    def ready(self, identifier, owner, port, secret):
        encrypted = encrypt_secret(secret)
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            if not self.lease(connection, owner):
                return False
            return bool(connection.execute("UPDATE ui_recordings SET status='recording',gateway_port=?,gateway_secret_enc=?,message='录制中：操作网页，并添加预期结果验证' WHERE id=? AND owner=? AND status='starting'", (port, encrypted, identifier, owner)).rowcount)

    def touch(self, identifier):
        with self.store._connection() as connection:
            return bool(connection.execute('UPDATE ui_recordings SET heartbeat_at=? WHERE id=? AND status IN ' + ACTIVE_SQL + ' AND expires_at>? AND heartbeat_at>?', (time.time(), identifier, time.time(), time.time() - IDLE_TIMEOUT)).rowcount)

    def request_save(self, identifier):
        with self.store._connection() as connection:
            connection.execute("UPDATE ui_recordings SET status='saving',message='正在保存录制' WHERE id=? AND status='recording' AND expires_at>? AND heartbeat_at>?", (identifier, time.time(), time.time() - IDLE_TIMEOUT))
        return self.get(identifier)

    def close(self, identifier, status, message, owner=None):
        with self.store._connection() as connection:
            # Only the lease holder may finish worker-owned state. User cancellation
            # is allowed immediately; the worker also scans closed containers.
            if owner is not None and not self.lease(connection, owner):
                return False
            return bool(connection.execute('UPDATE ui_recordings SET status=?,message=?,target_enc=\'\',gateway_port=0,gateway_secret_enc=\'\' WHERE id=? AND status IN ' + ACTIVE_SQL + (" AND (owner=? OR owner='')" if owner else ''),
                                           (status, message, identifier, *([owner] if owner else []))).rowcount)

    def retry(self, identifier, owner, message):
        with self.store._connection() as connection:
            if self.lease(connection, owner):
                connection.execute("UPDATE ui_recordings SET status='recording',message=? WHERE id=? AND owner=? AND status='saving'", (message, identifier, owner))

    def save(self, identifier, owner, source, checks):
        if not isinstance(source, str) or not 1 <= len(source.encode('utf-8')) <= 500_000 or '\0' in source:
            raise ValueError('录制脚本无效或超过 500 KB')
        if type(checks) is not int or not 1 <= checks <= 1000:
            raise ValueError('请至少添加一个可见性、文字或输入值检查点，再保存')
        encrypted = encrypt_secret(source)
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            item = connection.execute('SELECT * FROM ui_recordings WHERE id=?', (identifier,)).fetchone()
            if not item or item['owner'] != owner or item['status'] != 'saving' or not self.lease(connection, owner):
                return None
            if item['expires_at'] <= time.time() or item['heartbeat_at'] <= time.time() - IDLE_TIMEOUT:
                return None
            if not authorized(self.store, item):
                return None
            snapshot = validate_suite({'name': item['name'], 'files': [{'name': 'recorded.spec.js', 'content': '// 录制脚本已加密保存；如需修改操作，请重新录制。'}]})
            snapshot.update(recorded=True, recording_source_enc=encrypted, checks=checks,
                            sha256=hashlib.sha256(source.encode('utf-8')).hexdigest())
            suite_id, version = UiStore(self.store).insert_snapshot(connection, item['project_id'], item['created_by'], snapshot, item['suite_id'])
            connection.execute("UPDATE ui_recordings SET status='saved',suite_id=?,suite_version=?,checks=?,message='录制已保存为套件版本',target_enc='',gateway_port=0,gateway_secret_enc='' WHERE id=?", (suite_id, version, checks, identifier))
        return self.get(identifier)


def gateway_secret():
    return secrets.token_urlsafe(32)
