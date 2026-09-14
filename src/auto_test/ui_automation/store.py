"""Platform-owned UI suite versions and immutable queued run snapshots."""

import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from auto_test.platform.interface_data import normalize_parameters


def validate_suite(data):
    name = str(data.get('name') or '').strip()
    files = data.get('files') or []
    if not 1 <= len(name) <= 120 or not isinstance(files, list) or not 1 <= len(files) <= 20:
        raise ValueError('填写套件名称，并导入 1～20 个 Playwright 测试文件')
    seen, total, normalized = set(), 0, []
    for item in files:
        filename, content = str(item.get('name') or ''), str(item.get('content') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}\.(spec|test)\.(js|ts|cjs)', filename) or filename.casefold() in seen:
            raise ValueError('文件名需为唯一的 name.spec.js / name.spec.ts / name.test.cjs，不允许目录或配置文件')
        total += len(content.encode('utf-8'))
        if not content.strip() or '\0' in content or total > 500_000:
            raise ValueError('测试脚本不能为空，总大小不能超过 500 KB')
        seen.add(filename.casefold())
        normalized.append({'name': filename, 'content': content})
    base_url = str(data.get('base_url') or '').strip()
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('测试地址需为不含凭据的 HTTP(S) 地址')
    parameters = normalize_parameters(data.get('parameters') or {})
    timeout = int(data.get('timeout_seconds') or 120)
    if not 10 <= timeout <= 900:
        raise ValueError('运行时间需为 10～900 秒')
    result = {'name': name, 'files': normalized, 'base_url': base_url, 'parameters': parameters, 'timeout_seconds': timeout}
    result['sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


class UiStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def initialize(connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS ui_suite_versions (
            id VARCHAR(64) NOT NULL, project_id VARCHAR(64) NOT NULL, version INTEGER NOT NULL,
            name VARCHAR(120) NOT NULL, snapshot_json LONGTEXT NOT NULL, created_by VARCHAR(64) NOT NULL,
            created_at DOUBLE NOT NULL, PRIMARY KEY(id,version))''')
        connection.execute('''CREATE TABLE IF NOT EXISTS ui_runs (
            id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, suite_id VARCHAR(64) NOT NULL,
            suite_version INTEGER NOT NULL, snapshot_json LONGTEXT NOT NULL,
            status VARCHAR(24) NOT NULL, owner VARCHAR(64) NOT NULL, stop_requested INTEGER NOT NULL,
            created_by VARCHAR(64) NOT NULL, created_at DOUBLE NOT NULL, started_at DOUBLE,
            finished_at DOUBLE, result_json LONGTEXT NOT NULL, artifact_ref LONGTEXT NOT NULL)''')
        connection.execute("INSERT OR IGNORE INTO platform_worker_leases(name,owner,expires_at) VALUES('ui','',0)")
        from auto_test.ui_automation.recordings import RecordingStore
        RecordingStore.initialize(connection)

    @staticmethod
    def decode(row):
        if not row:
            return None
        item = dict(row)
        for field in ('snapshot', 'result'):
            if field + '_json' in item:
                item[field] = json.loads(item.pop(field + '_json'))
        return item

    def save(self, project, actor, data, identifier=''):
        snapshot = validate_suite(data)
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            current = connection.execute('SELECT snapshot_json FROM ui_suite_versions WHERE id=? AND project_id=? ORDER BY version DESC LIMIT 1', (identifier, project)).fetchone() if identifier else None
            if current and json.loads(current['snapshot_json']).get('recorded'):
                raise ValueError('此套件来自加密录制，请使用重新录制保存新版本')
            identifier, version = self.insert_snapshot(connection, project, actor, snapshot, identifier)
        return self.suite(project, identifier, version)

    def insert_snapshot(self, connection, project, actor, snapshot, identifier=''):
        """Caller owns the dispatch lock and transaction (also used by recording save)."""
        if identifier:
            row = connection.execute('SELECT MAX(version) AS version FROM ui_suite_versions WHERE id=? AND project_id=?', (identifier, project)).fetchone()
            if not row or not row['version']:
                raise KeyError(identifier)
            version = row['version'] + 1
        else:
            if connection.execute('SELECT COUNT(DISTINCT id) AS total FROM ui_suite_versions WHERE project_id=?', (project,)).fetchone()['total'] >= 100:
                raise ValueError('每个项目最多保存 100 个套件')
            identifier, version = uuid.uuid4().hex, 1
        connection.execute('INSERT INTO ui_suite_versions(id,project_id,version,name,snapshot_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)', (identifier, project, version, snapshot['name'], json.dumps(snapshot, ensure_ascii=False), actor, time.time()))
        return identifier, version

    @staticmethod
    def public(item):
        if not item:
            return item
        result = {key: value for key, value in item.items() if key not in {'owner', 'artifact_ref'}}
        if 'snapshot' in result:
            result['snapshot'] = {key: value for key, value in result['snapshot'].items() if key != 'recording_source_enc'}
        return result

    def suite(self, project, identifier, version=None):
        with self.store._connection() as connection:
            row = connection.execute('SELECT * FROM ui_suite_versions WHERE project_id=? AND id=?' + (' AND version=?' if version else '') + ' ORDER BY version DESC LIMIT 1', (project, identifier, *([version] if version else []))).fetchone()
        return self.decode(row)

    def suites(self, project):
        with self.store._connection() as connection:
            rows = connection.execute('''SELECT s.id,s.name,s.version,s.created_at FROM ui_suite_versions s
                WHERE s.project_id=? AND s.version=(SELECT MAX(v.version) FROM ui_suite_versions v WHERE v.id=s.id)
                ORDER BY s.created_at DESC,s.id LIMIT 100''', (project,)).fetchall()
        return [dict(row) for row in rows]

    def delete_suite(self, project, identifier):
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            if connection.execute("SELECT 1 FROM ui_runs WHERE project_id=? AND suite_id=? AND status IN ('queued','running') LIMIT 1", (project, identifier)).fetchone():
                raise ValueError('此套件还有未结束的任务')
            return bool(connection.execute('DELETE FROM ui_suite_versions WHERE project_id=? AND id=?', (project, identifier)).rowcount)

    def enqueue(self, project, actor, identifier, version=None):
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            row = connection.execute('SELECT * FROM ui_suite_versions WHERE project_id=? AND id=?' + (' AND version=?' if version else '') + ' ORDER BY version DESC LIMIT 1', (project, identifier, *([version] if version else []))).fetchone()
            if not row:
                raise KeyError(identifier)
            if connection.execute("SELECT COUNT(*) AS total FROM ui_runs WHERE project_id=? AND status IN ('queued','running')", (project,)).fetchone()['total'] >= 20:
                raise ValueError('项目已有 20 个未结束的 UI 任务')
            run_id = uuid.uuid4().hex
            connection.execute("INSERT INTO ui_runs(id,project_id,suite_id,suite_version,snapshot_json,status,owner,stop_requested,created_by,created_at,result_json,artifact_ref) VALUES(?,?,?,?,?,'queued','',0,?,?,'{}','')", (run_id, project, identifier, row['version'], row['snapshot_json'], actor, time.time()))
        return self.run(project, run_id)

    def runs(self, project, page=1, size=20):
        with self.store._connection() as connection:
            total = connection.execute('SELECT COUNT(*) AS total FROM ui_runs WHERE project_id=?', (project,)).fetchone()['total']
            rows = connection.execute('SELECT id,project_id,suite_id,suite_version,status,created_at,started_at,finished_at,result_json FROM ui_runs WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (project, size, (page - 1) * size)).fetchall()
        return {'items': [self.decode(row) for row in rows], 'total': total, 'page': page, 'page_size': size}

    def run(self, project, identifier):
        with self.store._connection() as connection:
            return self.decode(connection.execute('SELECT * FROM ui_runs WHERE project_id=? AND id=?', (project, identifier)).fetchone())

    def stop(self, project, identifier):
        with self.store._connection() as connection:
            # Set the timestamp before status: MySQL evaluates assignments in order.
            return bool(connection.execute("UPDATE ui_runs SET stop_requested=1,finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END,status=CASE WHEN status='queued' THEN 'stopped' ELSE status END WHERE project_id=? AND id=? AND status IN ('queued','running')", (time.time(), project, identifier)).rowcount)

    def claim(self, owner):
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            lease = connection.execute("SELECT owner,expires_at FROM platform_worker_leases WHERE name='ui'").fetchone()
            if not lease or lease['owner'] != owner or lease['expires_at'] <= time.time():
                return None
            row = connection.execute("SELECT * FROM ui_runs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                return None
            connection.execute("UPDATE ui_runs SET status='running',owner=?,started_at=? WHERE id=? AND status='queued'", (owner, time.time(), row['id']))
        return self.run(row['project_id'], row['id'])

    def finish(self, run, owner, status, result, artifact_ref=''):
        with self.store._connection() as connection:
            now = time.time()
            return bool(connection.execute("""UPDATE ui_runs SET status=?,result_json=?,artifact_ref=?,finished_at=?
                WHERE id=? AND owner=? AND status='running'
                AND EXISTS(SELECT 1 FROM platform_worker_leases WHERE name='ui' AND owner=? AND expires_at>?)""",
                (status, json.dumps(result, ensure_ascii=False, allow_nan=False), artifact_ref, now, run['id'], owner, owner, now)).rowcount)
