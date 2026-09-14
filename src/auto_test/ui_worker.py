"""Dedicated UI Worker; imported scripts are never executed in Python/Web."""

import argparse
import json
import signal
import threading
import time
import uuid

from auto_test.common.paths import PROJECT_ROOT, prepare_runtime_layout
from auto_test.common.runtime_secrets import ensure_runtime_master_key
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.artifact_storage import create_artifact_storage
from auto_test.platform.worker_rpc import WorkerMailbox
from auto_test.reporting.tabular import build_tabular_report
from auto_test.ui_automation.runner import DockerUiRunner, settings
from auto_test.ui_automation.store import UiStore


class UiWorker:
    def __init__(self, store, artifacts, runner=None):
        self.store, self.artifacts = store, artifacts
        self.records = UiStore(store)
        self.mailbox = WorkerMailbox(store, 'ui')
        self.owner = uuid.uuid4().hex
        self.runner = runner or DockerUiRunner()
        self.managed_runner = runner is None
        self.stopping = threading.Event()

    def authorized(self, run):
        user, project = self.store.get_user(run['created_by']), self.store.get_project(run['project_id'])
        if not user or not user.get('is_active') or not project or not project.get('is_active'):
            return False
        return user.get('is_superuser') or any(p['id'] == run['project_id'] and p.get('role') in {'tester', 'project_admin'} for p in self.store.list_user_projects(user['id']))

    def run_once(self):
        run = self.records.claim(self.owner)
        if not run:
            return None
        directory = self.artifacts.workspace('ui-runs', run['project_id'], run['id'])
        status, result = 'failed', {'message': 'UI 执行未完成'}
        try:
            if not self.authorized(run):
                raise PermissionError('项目执行权限已撤销')
            def should_stop():
                current = self.records.run(run['project_id'], run['id'])
                return self.stopping.is_set() or not current or current['stop_requested'] or not self.authorized(run)
            status, result = self.runner.run(run, directory, should_stop)
        except Exception as exc:
            result = {'message': '执行环境、授权或产物检查失败', 'error_type': type(exc).__name__}
        try:
            display = {'succeeded': '通过', 'failed': '未通过', 'stopped': '已停止', 'passed': '通过', 'skipped': '跳过'}
            names = build_tabular_report(directory, 'UI 自动化测试报告', '记录本次 Playwright 脚本版本、执行状态与用例结果。结果来自本次导入脚本；跳过用例不计为通过，截图和 trace 可在运行详情下载。', [
                ('运行概况', ['项目', '内容'], [['套件', run['snapshot']['name']], ['版本', run['suite_version']], ['脚本 SHA-256', run['snapshot']['sha256']], ['状态', display.get(status, status)], ['说明', result.get('message')], ['通过 / 失败 / 跳过', f"{result.get('passed', 0)} / {result.get('failed', 0)} / {result.get('skipped', 0)}"]], [1, 4]),
                ('用例结果', ['用例', '结果', '耗时 ms'], [[case['name'], display.get(case['status'], case['status']), case.get('duration_ms')] for case in result.get('cases', [])] or [['无可用用例结果', '—', None]], [6, 1, 1]),
            ])
            result.setdefault('artifacts', []).extend({'name': name, 'size': (directory / name).stat().st_size} for name in names)
            (directory / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            self.artifacts.publish_tree(directory)
            reference = self.artifacts.reference(directory)
        except Exception as exc:
            status, reference = 'failed', ''
            result['report_error'] = type(exc).__name__
        if self.records.finish(run, self.owner, status, result, reference):
            self.store.add_audit_event(actor_user_id=run['created_by'], project_id=run['project_id'], action='ui.run.finish', target_type='ui_run', target_id=run['id'], outcome=status, detail={'suite_version': run['suite_version']})
        return self.records.run(run['project_id'], run['id'])

    def run(self, recover_stale=False):
        from auto_test.ui_automation.recorder_runtime import DockerRecorder, RecorderManager, recorder_settings
        if self.managed_runner:
            self.runner.config = settings()
        self.runner.preflight()
        recording_config = recorder_settings()
        recorder = RecorderManager(self.store, self.owner, self.stopping, runtime=DockerRecorder(recording_config)) if recording_config['enabled'] else None
        if recorder:
            recorder.runtime.preflight()
        if not self.mailbox.acquire(self.owner, recover_stale=recover_stale):
            raise RuntimeError('UI Worker 已被占用；异常退出后先确认旧进程已停止，再使用 --recover-stale')
        def heartbeat():
            while not self.stopping.wait(2):
                try:
                    if self.mailbox.renew(self.owner):
                        continue
                except Exception:
                    pass
                self.stopping.set()
        thread = threading.Thread(target=heartbeat, daemon=True)
        recorder_thread = None
        try:
            thread.start()
            # Configuration writes require an empty lease. If a write raced
            # preflight, retry startup rather than use an unverified snapshot.
            if (self.managed_runner and self.runner.config != settings()) or recording_config != recorder_settings():
                raise RuntimeError('UI 运行配置在启动期间发生变化，请重新启动 UI Worker')
            if recorder:
                recorder.recover()
                recorder_thread = threading.Thread(target=recorder.run, daemon=True)
            with self.store._connection() as connection:
                abandoned = connection.execute("SELECT id FROM ui_runs WHERE status='running'").fetchall()
            for run in abandoned:
                self.runner.remove(run['id'])
            with self.store._connection() as connection:
                connection.execute("UPDATE ui_runs SET status='interrupted',finished_at=?,result_json=? WHERE status='running'", (time.time(), json.dumps({'message': '旧 UI Worker 已结束，请重新提交运行'})))
            if recorder_thread:
                recorder_thread.start()
            while not self.stopping.is_set():
                if not self.run_once():
                    self.stopping.wait(1)
        finally:
            self.stopping.set()
            if thread.is_alive():
                thread.join(3)
            if recorder_thread and recorder_thread.is_alive():
                recorder_thread.join(55)
            self.mailbox.release(self.owner)


def main():
    parser = argparse.ArgumentParser(description='烈马独立 UI Worker')
    parser.add_argument('--recover-stale', action='store_true')
    args = parser.parse_args()
    prepare_runtime_layout(PROJECT_ROOT)
    ensure_runtime_master_key()
    store = create_platform_repository(PROJECT_ROOT, recover_jobs=False)
    worker = UiWorker(store, create_artifact_storage(PROJECT_ROOT))
    for name in ('SIGINT', 'SIGTERM'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: worker.stopping.set())
    try:
        worker.run(args.recover_stale)
    finally:
        if callable(getattr(store, 'close', None)):
            store.close()


if __name__ == '__main__':
    main()
