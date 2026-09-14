"""Encrypted database mailbox and exclusive process ownership for server workers."""

from __future__ import annotations

import json
import time
import uuid

from auto_test.platform.secrets import decrypt_secret, encrypt_secret


class WorkerMailbox:
    def __init__(self, store, name="server"):
        self.store, self.name = store, name

    @staticmethod
    def initialize(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS platform_worker_leases (
            name VARCHAR(64) PRIMARY KEY, owner VARCHAR(64) NOT NULL,
            expires_at DOUBLE NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS platform_worker_commands (
            id VARCHAR(64) PRIMARY KEY, worker_name VARCHAR(64) NOT NULL, owner VARCHAR(64) NOT NULL,
            operation VARCHAR(64) NOT NULL, payload_enc LONGTEXT NOT NULL,
            result_enc LONGTEXT NOT NULL, status VARCHAR(24) NOT NULL,
            created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL)""")
        connection.execute("INSERT OR IGNORE INTO platform_worker_leases(name,owner,expires_at) VALUES('server','',0)")

    def status(self):
        with self.store._connection() as connection:
            row = connection.execute("SELECT owner,expires_at FROM platform_worker_leases WHERE name=?", (self.name,)).fetchone()
        return {"available": bool(row and row['owner'] and row['expires_at'] > time.time()), "owner": row['owner'] if row else '', "expires_at": row['expires_at'] if row else 0}

    def acquire(self, owner, *, ttl=30, recover_stale=False):
        now = time.time()
        with self.store._connection() as connection:
            sql = "UPDATE platform_worker_leases SET owner=?,expires_at=? WHERE name=? AND (owner=''"
            if recover_stale:
                sql += " OR expires_at<?"
            sql += ")"
            return bool(connection.execute(sql, (owner, now + ttl, self.name, *([now] if recover_stale else []))).rowcount)

    def renew(self, owner, ttl=30):
        now = time.time()
        with self.store._connection() as connection:
            return bool(connection.execute("UPDATE platform_worker_leases SET expires_at=? WHERE name=? AND owner=? AND expires_at>?", (now + ttl, self.name, owner, now)).rowcount)

    def release(self, owner):
        with self.store._connection() as connection:
            connection.execute("UPDATE platform_worker_leases SET owner='',expires_at=0 WHERE name=? AND owner=?", (self.name, owner))
            connection.execute("DELETE FROM platform_worker_commands WHERE worker_name=? AND owner=?", (self.name, owner))

    def submit(self, operation, args, kwargs, timeout=120):
        worker = self.status()
        if not worker['available']:
            raise RuntimeError("独立服务器 Worker 未在线，请先启动 server-worker")
        identifier = uuid.uuid4().hex
        encoded = json.dumps({'args': args, 'kwargs': kwargs}, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 1_000_000:
            raise ValueError("服务器操作参数过大")
        now = time.time()
        with self.store._connection() as connection:
            connection.execute("INSERT INTO platform_worker_commands(id,worker_name,owner,operation,payload_enc,result_enc,status,created_at,expires_at) VALUES(?,?,?,?,?,?,'queued',?,?)", (identifier, self.name, worker['owner'], operation, encrypt_secret(encoded), '', now, now + timeout))
        return identifier

    def claim(self, owner):
        now = time.time()
        with self.store._connection() as connection:
            # UPDATE obtains an exclusive row lock without extending the lease.
            connection.execute("UPDATE platform_worker_leases SET owner=owner WHERE name=?", (self.name,))
            lease = connection.execute("SELECT owner,expires_at FROM platform_worker_leases WHERE name=?", (self.name,)).fetchone()
            if not lease or lease['owner'] != owner or lease['expires_at'] <= now:
                return None
            connection.execute("DELETE FROM platform_worker_commands WHERE worker_name=? AND expires_at<?", (self.name, now))
            row = connection.execute("SELECT * FROM platform_worker_commands WHERE worker_name=? AND owner=? AND status='queued' ORDER BY created_at,id LIMIT 1", (self.name, owner)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE platform_worker_commands SET status='running',payload_enc='' WHERE id=? AND status='queued'", (row['id'],))
        item = dict(row)
        item['payload'] = json.loads(decrypt_secret(item.pop('payload_enc')))
        return item

    def finish(self, owner, identifier, result):
        encoded = encrypt_secret(json.dumps(result, ensure_ascii=False, allow_nan=False))
        now = time.time()
        with self.store._connection() as connection:
            updated = connection.execute("""UPDATE platform_worker_commands SET result_enc=?,status='done'
                WHERE id=? AND owner=? AND status='running' AND expires_at>?
                AND EXISTS(SELECT 1 FROM platform_worker_leases WHERE name=? AND owner=? AND expires_at>?)""", (encoded, identifier, owner, now, self.name, owner, now)).rowcount
        return bool(updated)

    def receive(self, identifier):
        with self.store._connection() as connection:
            row = connection.execute("SELECT result_enc FROM platform_worker_commands WHERE id=? AND status='done'", (identifier,)).fetchone()
            if not row:
                return None
            connection.execute("DELETE FROM platform_worker_commands WHERE id=?", (identifier,))
        return json.loads(decrypt_secret(row['result_enc']))

    def cancel(self, identifier):
        with self.store._connection() as connection:
            connection.execute("DELETE FROM platform_worker_commands WHERE id=?", (identifier,))

    def call(self, operation, *args, timeout=120, **kwargs):
        identifier = self.submit(operation, args, kwargs, timeout)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                result = self.receive(identifier)
                if result is not None:
                    if result.get('error') == 'KeyError':
                        raise KeyError("服务器会话或资产不存在")
                    if result.get('error'):
                        raise RuntimeError(result.get('message') or "服务器 Worker 操作失败")
                    return result.get('value')
                if not self.status()['available']:
                    raise RuntimeError("服务器 Worker 已离线，请重新连接会话")
                time.sleep(.1)
            raise RuntimeError("服务器 Worker 操作超时，请刷新检查结果后重试")
        finally:
            self.cancel(identifier)
