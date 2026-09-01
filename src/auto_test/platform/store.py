"""Persistent configuration and report-job storage for the Web console."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from auto_test.evaluation.persistence import ModelEvaluationStoreMixin


DEFAULT_REPORT_SECTIONS = [
    {"key": "overview", "title": "1. 测试背景与概述", "mode": "mixed", "enabled": True,
     "fixed_text": "本报告用于评估蓝鲨平台在目标环境中的功能完整性、处理效率与资源稳定性。"},
    {"key": "environment", "title": "2. 测试环境", "mode": "mixed", "enabled": True,
     "fixed_text": "环境信息由平台配置与本次运行快照自动汇总，并允许补充现场说明。"},
    {"key": "method", "title": "3. 测试方法与工具", "mode": "auto", "enabled": True,
     "fixed_text": "采用端到端批量导入、解析、翻译、断言、导出和资源监控方法。"},
    {"key": "cases", "title": "4. 核心测试用例", "mode": "auto", "enabled": True,
     "fixed_text": "测试用例根据本次启用的功能模块自动生成。"},
    {"key": "results", "title": "5. 测试结果汇总统计", "mode": "auto", "enabled": True,
     "fixed_text": "结果由运行日志、阶段统计、性能采样与图表自动生成。"},
    {"key": "conclusion", "title": "6. 综合测试结论", "mode": "mixed", "enabled": True,
     "fixed_text": "结论可使用规则或模型生成，并可在导出前人工修订。"},
]


class PlatformStore(ModelEvaluationStoreMixin):
    """SQLite store for versioned settings and retryable report jobs."""

    backend = "sqlite"

    def __init__(self, db_path: str | Path, *, recover_jobs: bool = True):
        self.db_path = Path(db_path)
        self.recover_jobs = recover_jobs
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    api_key_enc TEXT NOT NULL DEFAULT '',
                    temperature REAL NOT NULL DEFAULT 0.3,
                    max_tokens INTEGER NOT NULL DEFAULT 2000,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS server_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 22,
                    ssh_user TEXT NOT NULL,
                    credential_enc TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used_at REAL
                );

                CREATE TABLE IF NOT EXISTS report_templates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sections_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS report_template_versions (
                    template_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    sections_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(template_id, version)
                );

                CREATE TABLE IF NOT EXISTS endpoint_specs (
                    id TEXT PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    scheme TEXT NOT NULL DEFAULT 'http',
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER,
                    path TEXT NOT NULL,
                    default_path TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    spec_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at REAL NOT NULL,
                    UNIQUE(logical_name, version)
                );

                CREATE TABLE IF NOT EXISTS report_jobs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    template_version INTEGER NOT NULL,
                    options_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    artifact_dir TEXT NOT NULL DEFAULT '',
                    docx_path TEXT NOT NULL DEFAULT '',
                    pdf_path TEXT NOT NULL DEFAULT '',
                    docx_sha256 TEXT NOT NULL DEFAULT '',
                    pdf_sha256 TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS stress_jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_host TEXT NOT NULL DEFAULT '',
                    modes_json TEXT NOT NULL DEFAULT '[]',
                    modules_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '{}',
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    artifact_dir TEXT NOT NULL DEFAULT '',
                    report_path TEXT NOT NULL DEFAULT '',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    secret_required INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS stress_job_secrets (
                    job_id TEXT PRIMARY KEY,
                    secret_enc TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES stress_jobs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stress_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES stress_jobs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_superuser INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_login_at REAL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interface_modules (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, name),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interface_environments (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, name),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interface_variables (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL DEFAULT '',
                    variable_key TEXT NOT NULL,
                    value_text TEXT NOT NULL DEFAULT '',
                    secret_enc TEXT NOT NULL DEFAULT '',
                    is_secret INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, environment_id, variable_key),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interface_assets (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    module_id TEXT NOT NULL DEFAULT '',
                    environment_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    default_path TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    current_version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, module_id, name),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interface_asset_versions (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    definition_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at REAL NOT NULL,
                    UNIQUE(asset_id, version),
                    FOREIGN KEY(asset_id) REFERENCES interface_assets(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interface_scenarios (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    environment_id TEXT NOT NULL DEFAULT '',
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, name),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS interface_scenario_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL DEFAULT '',
                    scenario_name TEXT NOT NULL,
                    environment_id TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    concurrency_limit INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL,
                    scenario_json TEXT NOT NULL DEFAULT '{}',
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    artifact_dir TEXT NOT NULL DEFAULT '',
                    docx_path TEXT NOT NULL DEFAULT '',
                    pdf_path TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_suites (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    source TEXT NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_suite_versions (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    upstream_json TEXT NOT NULL DEFAULT '{}',
                    published_at REAL NOT NULL,
                    UNIQUE(suite_id, version),
                    FOREIGN KEY(suite_id) REFERENCES model_eval_suites(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_cases (
                    id TEXT PRIMARY KEY,
                    version_id TEXT NOT NULL,
                    case_key TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(version_id, case_key),
                    FOREIGN KEY(version_id) REFERENCES model_eval_suite_versions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    model_profile_id TEXT NOT NULL DEFAULT '',
                    suite_version_id TEXT NOT NULL DEFAULT '',
                    backend TEXT NOT NULL,
                    backend_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    artifact_ref TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_case_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    score_json TEXT NOT NULL DEFAULT '{}',
                    artifact_ref TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(run_id, case_id, attempt),
                    FOREIGN KEY(run_id) REFERENCES model_eval_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT '',
                    progress INTEGER,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES model_eval_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_manual_reviews (
                    id TEXT PRIMARY KEY,
                    result_id TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL DEFAULT '',
                    score_json TEXT NOT NULL DEFAULT '{}',
                    comment TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(result_id) REFERENCES model_eval_case_results(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS model_eval_comparisons (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    run_ids_json TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS project_memberships (
                    project_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(project_id, user_id),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    csrf_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id TEXT,
                    project_id TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL,
                    ip_address TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_models_active ON model_profiles(is_active);
                CREATE INDEX IF NOT EXISTS idx_server_profiles_updated
                    ON server_profiles(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_endpoint_active
                    ON endpoint_specs(status, default_path, created_at);
                CREATE INDEX IF NOT EXISTS idx_report_jobs_status
                    ON report_jobs(status, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_stress_jobs_status
                    ON stress_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_stress_samples_job
                    ON stress_samples(job_id, id);
                CREATE INDEX IF NOT EXISTS idx_users_active
                    ON users(is_active, username);
                CREATE INDEX IF NOT EXISTS idx_projects_active
                    ON projects(is_active, project_key);
                CREATE INDEX IF NOT EXISTS idx_interface_modules_project
                    ON interface_modules(project_id, sort_order, name);
                CREATE INDEX IF NOT EXISTS idx_interface_environments_project
                    ON interface_environments(project_id, is_default, name);
                CREATE INDEX IF NOT EXISTS idx_interface_variables_scope
                    ON interface_variables(project_id, environment_id, variable_key);
                CREATE INDEX IF NOT EXISTS idx_interface_assets_project
                    ON interface_assets(project_id, module_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_interface_versions_asset
                    ON interface_asset_versions(asset_id, version);
                CREATE INDEX IF NOT EXISTS idx_interface_scenarios_project
                    ON interface_scenarios(project_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interface_scenario_runs_project
                    ON interface_scenario_runs(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interface_scenario_runs_batch
                    ON interface_scenario_runs(batch_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_interface_scenario_runs_status
                    ON interface_scenario_runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_interface_scenario_runs_batch_status
                    ON interface_scenario_runs(batch_id, status);
                CREATE INDEX IF NOT EXISTS idx_model_eval_suites_project
                    ON model_eval_suites(project_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_model_eval_versions_suite
                    ON model_eval_suite_versions(suite_id, version);
                CREATE INDEX IF NOT EXISTS idx_model_eval_cases_version
                    ON model_eval_cases(version_id, category, sort_order);
                CREATE INDEX IF NOT EXISTS idx_model_eval_runs_project
                    ON model_eval_runs(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_model_eval_runs_status
                    ON model_eval_runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_model_eval_case_results_run
                    ON model_eval_case_results(run_id, status);
                CREATE INDEX IF NOT EXISTS idx_model_eval_events_run
                    ON model_eval_run_events(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_memberships_user
                    ON project_memberships(user_id, project_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_expiry
                    ON auth_sessions(user_id, expires_at);
                CREATE INDEX IF NOT EXISTS idx_audit_created
                    ON audit_events(created_at DESC);
                """
            )
            stress_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(stress_jobs)").fetchall()
            }
            if "stop_requested" not in stress_columns:
                connection.execute(
                    "ALTER TABLE stress_jobs ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "secret_required" not in stress_columns:
                connection.execute(
                    "ALTER TABLE stress_jobs ADD COLUMN secret_required INTEGER NOT NULL DEFAULT 0"
                )
            scenario_run_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(interface_scenario_runs)"
                ).fetchall()
            }
            for column_name, ddl in (
                ("scenario_json", "scenario_json TEXT NOT NULL DEFAULT '{}'"),
                ("parameters_json", "parameters_json TEXT NOT NULL DEFAULT '{}'"),
                ("stop_requested", "stop_requested INTEGER NOT NULL DEFAULT 0"),
                ("concurrency_limit", "concurrency_limit INTEGER NOT NULL DEFAULT 3"),
            ):
                if column_name not in scenario_run_columns:
                    connection.execute(
                        f"ALTER TABLE interface_scenario_runs ADD COLUMN {ddl}"
                    )
            self._initialize_records(connection)

    def _initialize_records(self, connection) -> None:
        """Seed defaults and recover jobs after a process restart."""
        existing = connection.execute(
                "SELECT * FROM report_templates WHERE id = 'default'"
            ).fetchone()
        if not existing:
            now = time.time()
            encoded = json.dumps(DEFAULT_REPORT_SECTIONS, ensure_ascii=False)
            connection.execute(
                """INSERT INTO report_templates(id, name, version, sections_json, updated_at)
                   VALUES('default', ?, 1, ?, ?)""",
                ("企业性能测试报告", encoded, now),
            )
            connection.execute(
                """INSERT INTO report_template_versions(
                   template_id, version, name, sections_json, created_at
                   ) VALUES('default', 1, ?, ?, ?)""",
                ("企业性能测试报告", encoded, now),
            )
        else:
            connection.execute(
                """INSERT OR IGNORE INTO report_template_versions(
                   template_id, version, name, sections_json, created_at
                   ) VALUES('default', ?, ?, ?, ?)""",
                (
                    existing["version"], existing["name"], existing["sections_json"],
                    existing["updated_at"],
                ),
            )
        if self.recover_jobs:
            connection.execute(
                """UPDATE report_jobs SET status='retrying', message='服务重启后恢复报告任务',
                   next_attempt_at=? WHERE status='running'""",
                (time.time(),),
            )
            connection.execute(
                """UPDATE stress_jobs SET status='interrupted', message='服务重启后任务已中断',
                   finished_at=? WHERE status='running'""",
                (time.time(),),
            )
            connection.execute(
                """UPDATE interface_scenario_runs
                   SET status='queued',started_at=NULL,stop_requested=0
                   WHERE status='running' AND COALESCE(scenario_json,'') NOT IN ('', '{}')"""
            )
            connection.execute(
                """UPDATE interface_scenario_runs
                   SET status='interrupted',finished_at=?
                   WHERE status='running' AND COALESCE(scenario_json,'') IN ('', '{}')""",
                (time.time(),),
            )
            connection.execute(
                """UPDATE model_eval_runs SET status='queued',phase='queued',progress=0,
                   message='服务重启后恢复评测任务',started_at=NULL,stop_requested=0
                   WHERE status IN ('preparing','running','scoring','reporting')"""
            )
            connection.execute(
                """DELETE FROM stress_job_secrets WHERE job_id IN (
                   SELECT id FROM stress_jobs WHERE status NOT IN ('authorized', 'queued')
                   )"""
            )
            connection.execute(
                """UPDATE stress_jobs SET status='interrupted',
                   message='排队任务缺少可用的加密凭据，请重新授权后提交', finished_at=?
                   WHERE status IN ('authorized', 'queued') AND secret_required=1
                   AND NOT EXISTS (
                       SELECT 1 FROM stress_job_secrets
                       WHERE stress_job_secrets.job_id=stress_jobs.id
                       AND stress_job_secrets.secret_enc!=''
                   )""",
                (time.time(),),
            )
        # Early versions persisted the SSH password inside options_json and
        # returned it from list/detail APIs.  Remove any legacy plaintext;
        # restart-safe queued credentials now live only in the encrypted vault.
        rows = connection.execute("SELECT id, options_json FROM stress_jobs").fetchall()
        for row in rows:
            try:
                options = json.loads(row["options_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if "password" in options:
                options.pop("password", None)
                connection.execute(
                    "UPDATE stress_jobs SET options_json=? WHERE id=?",
                    (json.dumps(options, ensure_ascii=False), row["id"]),
                )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # Identity, projects and audit ----------------------------------

    @staticmethod
    def _public_user(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item.pop("password_hash", None)
        item["is_active"] = bool(item.get("is_active"))
        item["is_superuser"] = bool(item.get("is_superuser"))
        return item

    @staticmethod
    def _public_project(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["is_active"] = bool(item.get("is_active"))
        return item

    def count_users(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"] if row else 0)

    def count_active_superusers(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE is_active=1 AND is_superuser=1"
            ).fetchone()
        return int(row["count"] if row else 0)

    def create_user(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        *,
        is_superuser: bool = False,
        is_active: bool = True,
    ) -> dict[str, Any]:
        user_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO users(
                   id,username,display_name,password_hash,is_active,is_superuser,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    username,
                    display_name,
                    password_hash,
                    1 if is_active else 0,
                    1 if is_superuser else 0,
                    now,
                    now,
                ),
            )
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str, *, include_password: bool = False) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            return None
        return dict(row) if include_password else self._public_user(row)

    def get_user_by_username(
        self, username: str, *, include_password: bool = False
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        if row is None:
            return None
        return dict(row) if include_password else self._public_user(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY is_superuser DESC, is_active DESC, username ASC"
            ).fetchall()
        return [self._public_user(row) for row in rows]

    def update_user(self, user_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "display_name": "display_name",
            "password_hash": "password_hash",
            "is_active": "is_active",
            "is_superuser": "is_superuser",
            "last_login_at": "last_login_at",
        }
        fields: list[str] = []
        values: list[Any] = []
        for key, column in allowed.items():
            if key not in changes:
                continue
            value = changes[key]
            if key in {"is_active", "is_superuser"}:
                value = 1 if value else 0
            fields.append(f"{column}=?")
            values.append(value)
        if not fields:
            user = self.get_user(user_id)
            if not user:
                raise KeyError(user_id)
            return user
        fields.append("updated_at=?")
        values.extend((time.time(), user_id))
        with self._connection() as connection:
            changed = connection.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE id=?", values
            ).rowcount
            if not changed:
                raise KeyError(user_id)
            if changes.get("is_active") is False or "password_hash" in changes:
                connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        return self.get_user(user_id) or {}

    def delete_user(self, user_id: str) -> bool:
        with self._connection() as connection:
            return bool(
                connection.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
            )

    def create_project(
        self, project_key: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        project_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO projects(
                   id,project_key,name,description,is_active,created_at,updated_at
                   ) VALUES(?,?,?,?,1,?,?)""",
                (project_id, project_key, name, description, now, now),
            )
        return self.get_project(project_id) or {}

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        return self._public_project(row)

    def list_projects(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        where = "" if include_inactive else " WHERE is_active=1"
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM projects" + where + " ORDER BY is_active DESC, project_key ASC"
            ).fetchall()
        return [self._public_project(row) for row in rows]

    def update_project(self, project_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"name": "name", "description": "description", "is_active": "is_active"}
        fields: list[str] = []
        values: list[Any] = []
        for key, column in allowed.items():
            if key in changes:
                value = 1 if key == "is_active" and changes[key] else changes[key]
                if key == "is_active" and not changes[key]:
                    value = 0
                fields.append(f"{column}=?")
                values.append(value)
        if not fields:
            project = self.get_project(project_id)
            if not project:
                raise KeyError(project_id)
            return project
        fields.append("updated_at=?")
        values.extend((time.time(), project_id))
        with self._connection() as connection:
            changed = connection.execute(
                f"UPDATE projects SET {', '.join(fields)} WHERE id=?", values
            ).rowcount
            if not changed:
                raise KeyError(project_id)
        return self.get_project(project_id) or {}

    def set_project_membership(
        self, project_id: str, user_id: str, role: str | None
    ) -> None:
        now = time.time()
        with self._connection() as connection:
            if role is None:
                connection.execute(
                    "DELETE FROM project_memberships WHERE project_id=? AND user_id=?",
                    (project_id, user_id),
                )
                return
            row = connection.execute(
                """SELECT 1 FROM project_memberships
                   WHERE project_id=? AND user_id=?""",
                (project_id, user_id),
            ).fetchone()
            if row:
                connection.execute(
                    """UPDATE project_memberships SET role=?, updated_at=?
                       WHERE project_id=? AND user_id=?""",
                    (role, now, project_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO project_memberships(
                       project_id,user_id,role,created_at,updated_at
                       ) VALUES(?,?,?,?,?)""",
                    (project_id, user_id, role, now, now),
                )

    def list_project_members(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT u.id AS user_id,u.username,u.display_name,u.is_active,
                          u.is_superuser,m.role,m.created_at,m.updated_at
                   FROM project_memberships m JOIN users u ON u.id=m.user_id
                   WHERE m.project_id=?
                   ORDER BY m.role ASC,u.username ASC""",
                (project_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_active"] = bool(item.get("is_active"))
            item["is_superuser"] = bool(item.get("is_superuser"))
            result.append(item)
        return result

    def list_user_projects(self, user_id: str, *, superuser: bool = False) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if superuser:
                rows = connection.execute(
                    """SELECT p.*,COALESCE(m.role,'project_admin') AS role
                       FROM projects p LEFT JOIN project_memberships m
                       ON m.project_id=p.id AND m.user_id=?
                       WHERE p.is_active=1 ORDER BY p.project_key ASC""",
                    (user_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT p.*,m.role FROM project_memberships m
                       JOIN projects p ON p.id=m.project_id
                       WHERE m.user_id=? AND p.is_active=1
                       ORDER BY p.project_key ASC""",
                    (user_id,),
                ).fetchall()
        result = []
        for row in rows:
            item = self._public_project(row) or {}
            item["role"] = str(dict(row).get("role") or "project_admin")
            result.append(item)
        return result

    def create_auth_session(
        self,
        token_hash: str,
        user_id: str,
        csrf_token: str,
        expires_at: float,
    ) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            connection.execute(
                """INSERT INTO auth_sessions(
                   token_hash,user_id,csrf_token,created_at,expires_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?)""",
                (token_hash, user_id, csrf_token, now, expires_at, now),
            )

    def get_auth_session(self, token_hash: str) -> dict[str, Any] | None:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """SELECT s.*,u.username,u.display_name,u.is_active,u.is_superuser
                   FROM auth_sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>?""",
                (token_hash, now),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?",
                    (now, token_hash),
                )
        if row is None:
            return None
        item = dict(row)
        item["is_active"] = bool(item.get("is_active"))
        item["is_superuser"] = bool(item.get("is_superuser"))
        return item

    def delete_auth_session(self, token_hash: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))

    def add_audit_event(
        self,
        *,
        actor_user_id: str | None,
        project_id: str | None,
        action: str,
        target_type: str = "",
        target_id: str = "",
        outcome: str = "success",
        ip_address: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO audit_events(
                   actor_user_id,project_id,action,target_type,target_id,outcome,
                   ip_address,detail_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    actor_user_id,
                    project_id,
                    action,
                    target_type,
                    target_id,
                    outcome,
                    ip_address,
                    json.dumps(detail or {}, ensure_ascii=False),
                    time.time(),
                ),
            )

    def list_audit_events(
        self, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        page_size = max(1, min(int(limit), 1000))
        page_offset = max(0, int(offset))
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT a.*,u.username AS actor_username,p.name AS project_name
                   FROM audit_events a
                   LEFT JOIN users u ON u.id=a.actor_user_id
                   LEFT JOIN projects p ON p.id=a.project_id
                   ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?""",
                (page_size, page_offset),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except (TypeError, ValueError):
                item["detail"] = {}
            result.append(item)
        return result

    def count_audit_events(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM audit_events").fetchone()
        return int(row["total"] if row else 0)

    # Model profiles -------------------------------------------------

    @staticmethod
    def _decode_server_profile(row, *, include_secret: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        encrypted = str(result.get("credential_enc") or "")
        result["credential_saved"] = bool(encrypted)
        result["user"] = result.pop("ssh_user", "")
        if not include_secret:
            result.pop("credential_enc", None)
        return result

    def list_server_profiles(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM server_profiles
                   ORDER BY COALESCE(last_used_at, updated_at) DESC, name ASC"""
            ).fetchall()
        return [self._decode_server_profile(row) for row in rows]

    def get_server_profile(
        self, profile_id: str, *, include_secret: bool = False
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM server_profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return self._decode_server_profile(row, include_secret=include_secret)

    def save_server_profile(
        self, data: dict[str, Any], credential_enc: str | None
    ) -> dict[str, Any]:
        profile_id = str(data.get("id") or uuid.uuid4().hex)
        existing = self.get_server_profile(profile_id, include_secret=True)
        encrypted = (
            str((existing or {}).get("credential_enc") or "")
            if credential_enc is None
            else str(credential_enc or "")
        )
        now = time.time()
        values = (
            str(data.get("name") or data.get("host") or "未命名服务器").strip(),
            str(data.get("host") or "").strip(),
            max(1, min(int(data.get("port") or 22), 65535)),
            str(data.get("user") or "").strip(),
            encrypted,
            now,
            profile_id,
        )
        with self._connection() as connection:
            if existing:
                connection.execute(
                    """UPDATE server_profiles
                       SET name=?, host=?, port=?, ssh_user=?, credential_enc=?, updated_at=?
                       WHERE id=?""",
                    values,
                )
            else:
                connection.execute(
                    """INSERT INTO server_profiles(
                       name,host,port,ssh_user,credential_enc,updated_at,id,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    values + (now,),
                )
        return self.get_server_profile(profile_id) or {}

    def delete_server_profile(self, profile_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM server_profiles WHERE id=?", (profile_id,)
            )
            return bool(cursor.rowcount)

    def touch_server_profile(self, profile_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE server_profiles SET last_used_at=? WHERE id=?",
                (time.time(), profile_id),
            )

    # Model profiles -------------------------------------------------

    def list_model_profiles(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_profiles ORDER BY is_active DESC, updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_model_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        return self._row(row)

    def active_model_profile(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_profiles WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return self._row(row)

    def save_model_profile(self, data: dict[str, Any], api_key_enc: str | None) -> dict[str, Any]:
        now = time.time()
        profile_id = str(data.get("id") or uuid.uuid4().hex)
        existing = self.get_model_profile(profile_id)
        encrypted = api_key_enc if api_key_enc is not None else (existing or {}).get("api_key_enc", "")
        active = 1 if data.get("is_active", False) else 0
        with self._connection() as connection:
            if active:
                connection.execute("UPDATE model_profiles SET is_active = 0")
            connection.execute(
                """
                INSERT INTO model_profiles(
                    id, name, provider, base_url, model_name, api_key_enc,
                    temperature, max_tokens, system_prompt, is_active, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, provider=excluded.provider,
                    base_url=excluded.base_url, model_name=excluded.model_name,
                    api_key_enc=excluded.api_key_enc, temperature=excluded.temperature,
                    max_tokens=excluded.max_tokens, system_prompt=excluded.system_prompt,
                    is_active=excluded.is_active, updated_at=excluded.updated_at
                """,
                (
                    profile_id, str(data["name"]).strip(), str(data.get("provider", "openai-compatible")),
                    str(data["base_url"]).strip().rstrip("/"), str(data["model_name"]).strip(),
                    encrypted, float(data.get("temperature", 0.3)), int(data.get("max_tokens", 2000)),
                    str(data.get("system_prompt", "")), active,
                    (existing or {}).get("created_at", now), now,
                ),
            )
        return self.get_model_profile(profile_id) or {}

    def activate_model(self, profile_id: str) -> None:
        with self._connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM model_profiles WHERE id = ?", (profile_id,)
            ).fetchone():
                raise KeyError(profile_id)
            connection.execute("UPDATE model_profiles SET is_active = 0")
            connection.execute(
                "UPDATE model_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
                (time.time(), profile_id),
            )

    # Report templates -----------------------------------------------

    def get_report_template(self, version: int | None = None) -> dict[str, Any]:
        with self._connection() as connection:
            if version is None:
                row = connection.execute(
                    "SELECT * FROM report_templates WHERE id = 'default'"
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT template_id AS id, name, version, sections_json,
                       created_at AS updated_at FROM report_template_versions
                       WHERE template_id='default' AND version=?""",
                    (int(version),),
                ).fetchone()
        if row is None:
            raise KeyError(f"report template version {version} not found")
        item = dict(row)
        item["sections"] = json.loads(item.pop("sections_json"))
        return item

    def save_report_template(self, name: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
        current = self.get_report_template()
        with self._connection() as connection:
            now = time.time()
            version = current["version"] + 1
            encoded = json.dumps(sections, ensure_ascii=False)
            connection.execute(
                """UPDATE report_templates
                   SET name = ?, version = ?, sections_json = ?, updated_at = ?
                   WHERE id = 'default'""",
                (name.strip(), version, encoded, now),
            )
            connection.execute(
                """INSERT INTO report_template_versions(
                   template_id, version, name, sections_json, created_at
                   ) VALUES('default', ?, ?, ?, ?)""",
                (version, name.strip(), encoded, now),
            )
        return self.get_report_template()

    # Project-scoped interface assets -------------------------------

    def list_interface_modules(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT m.*,
                          (SELECT COUNT(*) FROM interface_assets a WHERE a.module_id=m.id) AS asset_count
                   FROM interface_modules m
                   WHERE m.project_id=?
                   ORDER BY m.sort_order ASC, m.name ASC""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_interface_module(
        self, project_id: str, data: dict[str, Any], module_id: str = ""
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("模块名称不能为空")
        now = time.time()
        supplied_id = str(module_id or "")
        module_id = supplied_id or uuid.uuid4().hex
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT id FROM interface_modules WHERE id=? AND project_id=?",
                (module_id, project_id),
            ).fetchone()
            values = (
                name,
                str(data.get("description") or "").strip(),
                int(data.get("sort_order") or 0),
                now,
            )
            if existing:
                connection.execute(
                    """UPDATE interface_modules
                       SET name=?,description=?,sort_order=?,updated_at=?
                       WHERE id=? AND project_id=?""",
                    (*values, module_id, project_id),
                )
            elif supplied_id:
                raise KeyError(module_id)
            else:
                connection.execute(
                    """INSERT INTO interface_modules(
                       id,project_id,name,description,sort_order,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (module_id, project_id, *values[:3], now, now),
                )
        return next(
            item for item in self.list_interface_modules(project_id) if item["id"] == module_id
        )

    def delete_interface_module(self, project_id: str, module_id: str) -> bool:
        with self._connection() as connection:
            used = connection.execute(
                "SELECT COUNT(*) AS total FROM interface_assets WHERE project_id=? AND module_id=?",
                (project_id, module_id),
            ).fetchone()
            if used and int(used["total"] or 0):
                raise ValueError("模块下仍有接口，请先移动或删除接口")
            deleted = connection.execute(
                "DELETE FROM interface_modules WHERE id=? AND project_id=?",
                (module_id, project_id),
            ).rowcount
        return bool(deleted)

    def list_interface_environments(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT e.*,
                          (SELECT COUNT(*) FROM interface_variables v WHERE v.environment_id=e.id) AS variable_count,
                          (SELECT COUNT(*) FROM interface_assets a WHERE a.environment_id=e.id) AS asset_count
                   FROM interface_environments e
                   WHERE e.project_id=?
                   ORDER BY e.is_default DESC, e.name ASC""",
                (project_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_default"] = bool(item.get("is_default"))
            result.append(item)
        return result

    def save_interface_environment(
        self, project_id: str, data: dict[str, Any], environment_id: str = ""
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("环境名称不能为空")
        now = time.time()
        supplied_id = str(environment_id or "")
        environment_id = supplied_id or uuid.uuid4().hex
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT id FROM interface_environments WHERE id=? AND project_id=?",
                (environment_id, project_id),
            ).fetchone()
            is_default = 1 if data.get("is_default") else 0
            if is_default:
                connection.execute(
                    "UPDATE interface_environments SET is_default=0 WHERE project_id=?",
                    (project_id,),
                )
            values = (
                name,
                str(data.get("base_url") or "").strip().rstrip("/"),
                str(data.get("description") or "").strip(),
                is_default,
                now,
            )
            if existing:
                connection.execute(
                    """UPDATE interface_environments
                       SET name=?,base_url=?,description=?,is_default=?,updated_at=?
                       WHERE id=? AND project_id=?""",
                    (*values, environment_id, project_id),
                )
            elif supplied_id:
                raise KeyError(environment_id)
            else:
                connection.execute(
                    """INSERT INTO interface_environments(
                       id,project_id,name,base_url,description,is_default,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (environment_id, project_id, *values[:4], now, now),
                )
        return next(
            item
            for item in self.list_interface_environments(project_id)
            if item["id"] == environment_id
        )

    def delete_interface_environment(self, project_id: str, environment_id: str) -> bool:
        with self._connection() as connection:
            assets = connection.execute(
                "SELECT COUNT(*) AS total FROM interface_assets WHERE project_id=? AND environment_id=?",
                (project_id, environment_id),
            ).fetchone()
            variables = connection.execute(
                "SELECT COUNT(*) AS total FROM interface_variables WHERE project_id=? AND environment_id=?",
                (project_id, environment_id),
            ).fetchone()
            if assets and int(assets["total"] or 0):
                raise ValueError("环境仍被接口引用，请先调整接口环境")
            if variables and int(variables["total"] or 0):
                raise ValueError("环境下仍有变量，请先删除环境变量")
            scenarios = connection.execute(
                "SELECT name,environment_id,steps_json FROM interface_scenarios WHERE project_id=?",
                (project_id,),
            ).fetchall()
            for scenario in scenarios:
                try:
                    steps = json.loads(scenario["steps_json"] or "[]")
                except (TypeError, ValueError):
                    steps = []
                if str(scenario["environment_id"] or "") == environment_id or any(
                    str(step.get("environment_id") or "") == environment_id
                    for step in steps if isinstance(step, dict)
                ):
                    raise ValueError(f"环境仍被接口场景“{scenario['name']}”引用")
            deleted = connection.execute(
                "DELETE FROM interface_environments WHERE id=? AND project_id=?",
                (environment_id, project_id),
            ).rowcount
        return bool(deleted)

    @staticmethod
    def _public_interface_variable(row) -> dict[str, Any]:
        item = dict(row)
        encrypted = str(item.pop("secret_enc", "") or "")
        item["is_secret"] = bool(item.get("is_secret"))
        item["has_secret"] = bool(encrypted)
        item["value"] = "" if item["is_secret"] else str(item.pop("value_text", "") or "")
        item.pop("value_text", None)
        return item

    def list_interface_variables(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT v.*,e.name AS environment_name
                   FROM interface_variables v
                   LEFT JOIN interface_environments e ON e.id=v.environment_id
                   WHERE v.project_id=?
                   ORDER BY CASE WHEN v.environment_id='' THEN 0 ELSE 1 END,
                            e.name ASC,v.variable_key ASC""",
                (project_id,),
            ).fetchall()
        return [self._public_interface_variable(row) for row in rows]

    def get_interface_variable_records(
        self, project_id: str, environment_id: str = ""
    ) -> list[dict[str, Any]]:
        """Return private variable records for in-process request execution only."""
        normalized_environment_id = str(environment_id or "")
        with self._connection() as connection:
            if normalized_environment_id:
                rows = connection.execute(
                    """SELECT variable_key,value_text,secret_enc,is_secret,environment_id
                       FROM interface_variables
                       WHERE project_id=? AND environment_id IN ('',?)
                       ORDER BY CASE WHEN environment_id='' THEN 0 ELSE 1 END,variable_key ASC""",
                    (project_id, normalized_environment_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT variable_key,value_text,secret_enc,is_secret,environment_id
                       FROM interface_variables
                       WHERE project_id=? AND environment_id=''
                       ORDER BY variable_key ASC""",
                    (project_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def save_interface_variable(
        self,
        project_id: str,
        data: dict[str, Any],
        variable_id: str = "",
        *,
        secret_enc: str | None = None,
    ) -> dict[str, Any]:
        variable_key = str(data.get("key") or "").strip()
        if not variable_key:
            raise ValueError("变量名不能为空")
        environment_id = str(data.get("environment_id") or "")
        now = time.time()
        supplied_id = str(variable_id or "")
        variable_id = supplied_id or uuid.uuid4().hex
        with self._connection() as connection:
            if environment_id:
                environment = connection.execute(
                    "SELECT id FROM interface_environments WHERE id=? AND project_id=?",
                    (environment_id, project_id),
                ).fetchone()
                if not environment:
                    raise ValueError("选择的环境不存在或不属于当前项目")
            existing = connection.execute(
                "SELECT * FROM interface_variables WHERE id=? AND project_id=?",
                (variable_id, project_id),
            ).fetchone()
            is_secret = bool(data.get("is_secret"))
            encrypted = ""
            value_text = str(data.get("value") or "")
            if is_secret:
                encrypted = (
                    str(secret_enc)
                    if secret_enc is not None
                    else str(existing["secret_enc"] or "") if existing else ""
                )
                value_text = ""
                if not encrypted:
                    raise ValueError("密钥变量必须填写密钥值")
            values = (
                environment_id,
                variable_key,
                value_text,
                encrypted,
                1 if is_secret else 0,
                str(data.get("description") or "").strip(),
                now,
            )
            if existing:
                connection.execute(
                    """UPDATE interface_variables
                       SET environment_id=?,variable_key=?,value_text=?,secret_enc=?,
                           is_secret=?,description=?,updated_at=?
                       WHERE id=? AND project_id=?""",
                    (*values, variable_id, project_id),
                )
            elif supplied_id:
                raise KeyError(variable_id)
            else:
                connection.execute(
                    """INSERT INTO interface_variables(
                       id,project_id,environment_id,variable_key,value_text,secret_enc,
                       is_secret,description,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (variable_id, project_id, *values[:6], now, now),
                )
            row = connection.execute(
                """SELECT v.*,e.name AS environment_name
                   FROM interface_variables v
                   LEFT JOIN interface_environments e ON e.id=v.environment_id
                   WHERE v.id=? AND v.project_id=?""",
                (variable_id, project_id),
            ).fetchone()
        return self._public_interface_variable(row)

    def delete_interface_variable(self, project_id: str, variable_id: str) -> bool:
        with self._connection() as connection:
            deleted = connection.execute(
                "DELETE FROM interface_variables WHERE id=? AND project_id=?",
                (variable_id, project_id),
            ).rowcount
        return bool(deleted)

    @staticmethod
    def _decode_interface_asset(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            item["request"] = json.loads(item.pop("request_json") or "{}")
        except (TypeError, ValueError):
            item["request"] = {}
        item["version_count"] = int(item.get("version_count") or 0)
        return item

    def _interface_asset_query(self) -> str:
        return """SELECT a.*,m.name AS module_name,e.name AS environment_name,e.base_url,
                         (SELECT COUNT(*) FROM interface_asset_versions v WHERE v.asset_id=a.id) AS version_count
                  FROM interface_assets a
                  LEFT JOIN interface_modules m ON m.id=a.module_id
                  LEFT JOIN interface_environments e ON e.id=a.environment_id"""

    def list_interface_assets(self, project_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                self._interface_asset_query()
                + " WHERE a.project_id=? ORDER BY a.updated_at DESC LIMIT ?",
                (project_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._decode_interface_asset(row) or {} for row in rows]

    def get_interface_asset(self, project_id: str, asset_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                self._interface_asset_query() + " WHERE a.id=? AND a.project_id=?",
                (asset_id, project_id),
            ).fetchone()
        return self._decode_interface_asset(row)

    def save_interface_asset(
        self,
        project_id: str,
        data: dict[str, Any],
        asset_id: str = "",
        *,
        publish: bool = False,
        upsert_by_name: bool = False,
    ) -> dict[str, Any]:
        name = str(data.get("name") or data.get("logical_name") or "").strip()
        if not name:
            raise ValueError("接口名称不能为空")
        module_id = str(data.get("module_id") or "")
        environment_id = str(data.get("environment_id") or "")
        method = str(data.get("method") or "GET").upper()
        path = str(data.get("path") or "").strip()
        default_path = str(data.get("default_path") or "").strip()
        request_data = data.get("request") if isinstance(data.get("request"), dict) else {}
        now = time.time()
        supplied_id = str(asset_id or "")
        with self._connection() as connection:
            if module_id:
                module = connection.execute(
                    "SELECT id FROM interface_modules WHERE id=? AND project_id=?",
                    (module_id, project_id),
                ).fetchone()
                if not module:
                    raise ValueError("选择的模块不存在或不属于当前项目")
            if environment_id:
                environment = connection.execute(
                    "SELECT id FROM interface_environments WHERE id=? AND project_id=?",
                    (environment_id, project_id),
                ).fetchone()
                if not environment:
                    raise ValueError("选择的环境不存在或不属于当前项目")
            existing = None
            if supplied_id:
                existing = connection.execute(
                    "SELECT * FROM interface_assets WHERE id=? AND project_id=?",
                    (supplied_id, project_id),
                ).fetchone()
                if not existing:
                    raise KeyError(supplied_id)
            elif upsert_by_name:
                existing = connection.execute(
                    """SELECT * FROM interface_assets
                       WHERE project_id=? AND module_id=? AND name=?""",
                    (project_id, module_id, name),
                ).fetchone()
            asset_id = str(existing["id"]) if existing else uuid.uuid4().hex
            version = int(existing["current_version"] or 0) + 1 if existing else 1
            status = "published" if publish else "draft"
            definition = {
                "name": name,
                "description": str(data.get("description") or "").strip(),
                "module_id": module_id,
                "environment_id": environment_id,
                "method": method,
                "path": path,
                "default_path": default_path,
                "request": request_data,
                "source_type": str(data.get("source_type") or "manual"),
                "target": data.get("target") if isinstance(data.get("target"), dict) else {},
            }
            encoded_request = json.dumps(request_data, ensure_ascii=False)
            if publish:
                connection.execute(
                    """UPDATE interface_asset_versions SET status='archived'
                       WHERE asset_id=? AND status='published'""",
                    (asset_id,),
                )
            if existing:
                connection.execute(
                    """UPDATE interface_assets
                       SET module_id=?,environment_id=?,name=?,description=?,method=?,path=?,
                           default_path=?,request_json=?,current_version=?,status=?,updated_at=?
                       WHERE id=? AND project_id=?""",
                    (
                        module_id,
                        environment_id,
                        name,
                        definition["description"],
                        method,
                        path,
                        default_path,
                        encoded_request,
                        version,
                        status,
                        now,
                        asset_id,
                        project_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO interface_assets(
                       id,project_id,module_id,environment_id,name,description,method,path,
                       default_path,request_json,current_version,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        asset_id,
                        project_id,
                        module_id,
                        environment_id,
                        name,
                        definition["description"],
                        method,
                        path,
                        default_path,
                        encoded_request,
                        version,
                        status,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """INSERT INTO interface_asset_versions(
                   id,asset_id,version,definition_json,status,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    asset_id,
                    version,
                    json.dumps(definition, ensure_ascii=False),
                    status,
                    now,
                ),
            )
        return self.get_interface_asset(project_id, asset_id) or {}

    def publish_interface_asset(self, project_id: str, asset_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            asset = connection.execute(
                "SELECT current_version FROM interface_assets WHERE id=? AND project_id=?",
                (asset_id, project_id),
            ).fetchone()
            if not asset:
                raise KeyError(asset_id)
            connection.execute(
                """UPDATE interface_asset_versions SET status='archived'
                   WHERE asset_id=? AND status='published'""",
                (asset_id,),
            )
            connection.execute(
                """UPDATE interface_asset_versions SET status='published'
                   WHERE asset_id=? AND version=?""",
                (asset_id, int(asset["current_version"])),
            )
            connection.execute(
                "UPDATE interface_assets SET status='published',updated_at=? WHERE id=?",
                (time.time(), asset_id),
            )
        return self.get_interface_asset(project_id, asset_id) or {}

    def delete_interface_asset(self, project_id: str, asset_id: str) -> bool:
        with self._connection() as connection:
            scenarios = connection.execute(
                "SELECT name,steps_json FROM interface_scenarios WHERE project_id=?",
                (project_id,),
            ).fetchall()
            for scenario in scenarios:
                try:
                    steps = json.loads(scenario["steps_json"] or "[]")
                except (TypeError, ValueError):
                    steps = []
                if any(
                    str(step.get("asset_id") or "") == asset_id
                    for step in steps if isinstance(step, dict)
                ):
                    raise ValueError(f"接口仍被场景“{scenario['name']}”引用，请先调整场景步骤")
            deleted = connection.execute(
                "DELETE FROM interface_assets WHERE id=? AND project_id=?",
                (asset_id, project_id),
            ).rowcount
        return bool(deleted)

    def list_interface_asset_versions(
        self, project_id: str, asset_id: str
    ) -> list[dict[str, Any]]:
        if not self.get_interface_asset(project_id, asset_id):
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM interface_asset_versions
                   WHERE asset_id=? ORDER BY version DESC""",
                (asset_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["definition"] = json.loads(item.pop("definition_json") or "{}")
            except (TypeError, ValueError):
                item["definition"] = {}
            result.append(item)
        return result

    # Project-scoped interface automation scenarios ----------------

    @staticmethod
    def _decode_interface_scenario(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target, fallback in (
            ("parameters_json", "parameters", {}),
            ("steps_json", "steps", []),
        ):
            try:
                item[target] = json.loads(item.pop(source) or json.dumps(fallback))
            except (TypeError, ValueError):
                item[target] = fallback
        item["step_count"] = len(item.get("steps") or [])
        item["run_count"] = int(item.get("run_count") or 0)
        return item

    def _interface_scenario_query(self) -> str:
        return """SELECT s.*,e.name AS environment_name,
                         (SELECT COUNT(*) FROM interface_scenario_runs r
                          WHERE r.scenario_id=s.id AND r.project_id=s.project_id) AS run_count,
                         (SELECT r.status FROM interface_scenario_runs r
                          WHERE r.scenario_id=s.id AND r.project_id=s.project_id
                          ORDER BY r.created_at DESC LIMIT 1) AS last_run_status,
                         (SELECT r.created_at FROM interface_scenario_runs r
                          WHERE r.scenario_id=s.id AND r.project_id=s.project_id
                          ORDER BY r.created_at DESC LIMIT 1) AS last_run_at
                  FROM interface_scenarios s
                  LEFT JOIN interface_environments e ON e.id=s.environment_id"""

    def list_interface_scenarios(
        self, project_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                self._interface_scenario_query()
                + " WHERE s.project_id=? ORDER BY s.updated_at DESC LIMIT ?",
                (project_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._decode_interface_scenario(row) or {} for row in rows]

    def get_interface_scenario(
        self, project_id: str, scenario_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                self._interface_scenario_query() + " WHERE s.id=? AND s.project_id=?",
                (scenario_id, project_id),
            ).fetchone()
        return self._decode_interface_scenario(row)

    def save_interface_scenario(
        self,
        project_id: str,
        data: dict[str, Any],
        scenario_id: str = "",
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("场景名称不能为空")
        parameters = data.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("场景用例参数必须是对象")
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("场景至少需要一个接口步骤")
        if len(steps) > 50:
            raise ValueError("单个场景最多支持 50 个步骤")
        environment_id = str(data.get("environment_id") or "")
        supplied_id = str(scenario_id or "")
        scenario_id = supplied_id or uuid.uuid4().hex
        now = time.time()
        normalized_steps: list[dict[str, Any]] = []
        with self._connection() as connection:
            if environment_id:
                environment = connection.execute(
                    "SELECT id FROM interface_environments WHERE id=? AND project_id=?",
                    (environment_id, project_id),
                ).fetchone()
                if not environment:
                    raise ValueError("选择的场景环境不存在或不属于当前项目")
            existing = connection.execute(
                "SELECT id FROM interface_scenarios WHERE id=? AND project_id=?",
                (scenario_id, project_id),
            ).fetchone()
            if supplied_id and not existing:
                raise KeyError(scenario_id)
            for index, raw_step in enumerate(steps, start=1):
                if not isinstance(raw_step, dict):
                    raise ValueError(f"第 {index} 个场景步骤格式无效")
                asset_id = str(raw_step.get("asset_id") or "")
                asset = connection.execute(
                    "SELECT id,name FROM interface_assets WHERE id=? AND project_id=?",
                    (asset_id, project_id),
                ).fetchone()
                if not asset:
                    raise ValueError(f"第 {index} 个步骤引用的接口不存在或不属于当前项目")
                step_environment_id = str(raw_step.get("environment_id") or "")
                if step_environment_id:
                    step_environment = connection.execute(
                        "SELECT id FROM interface_environments WHERE id=? AND project_id=?",
                        (step_environment_id, project_id),
                    ).fetchone()
                    if not step_environment:
                        raise ValueError(f"第 {index} 个步骤选择的环境不存在或不属于当前项目")
                normalized_steps.append(
                    {
                        "id": str(raw_step.get("id") or uuid.uuid4().hex),
                        "name": str(raw_step.get("name") or asset["name"] or f"步骤 {index}").strip(),
                        "asset_id": asset_id,
                        "environment_id": step_environment_id,
                        "enabled": bool(raw_step.get("enabled", True)),
                        "continue_on_failure": bool(raw_step.get("continue_on_failure", False)),
                        "extractions": list(raw_step.get("extractions") or []),
                        "assertions": list(raw_step.get("assertions") or []),
                    }
                )
            values = (
                name,
                str(data.get("description") or "").strip(),
                environment_id,
                json.dumps(parameters, ensure_ascii=False),
                json.dumps(normalized_steps, ensure_ascii=False),
                now,
            )
            if existing:
                connection.execute(
                    """UPDATE interface_scenarios
                       SET name=?,description=?,environment_id=?,parameters_json=?,
                           steps_json=?,updated_at=?
                       WHERE id=? AND project_id=?""",
                    (*values, scenario_id, project_id),
                )
            else:
                connection.execute(
                    """INSERT INTO interface_scenarios(
                       id,project_id,name,description,environment_id,parameters_json,
                       steps_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (scenario_id, project_id, *values[:5], now, now),
                )
        return self.get_interface_scenario(project_id, scenario_id) or {}

    def delete_interface_scenario(self, project_id: str, scenario_id: str) -> bool:
        with self._connection() as connection:
            deleted = connection.execute(
                "DELETE FROM interface_scenarios WHERE id=? AND project_id=?",
                (scenario_id, project_id),
            ).rowcount
        return bool(deleted)

    @staticmethod
    def _decode_interface_scenario_run(row, *, include_result: bool = True) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target in (
            ("summary_json", "summary"),
            ("result_json", "result"),
            ("scenario_json", "scenario_snapshot"),
            ("parameters_json", "parameters"),
        ):
            if source not in item:
                continue
            encoded = item.pop(source)
            try:
                item[target] = json.loads(encoded or "{}")
            except (TypeError, ValueError):
                item[target] = {}
        if not include_result:
            item.pop("result", None)
            item.pop("scenario_snapshot", None)
            item.pop("parameters", None)
        item["stop_requested"] = bool(item.get("stop_requested"))
        item["concurrency_limit"] = max(
            1, min(int(item.get("concurrency_limit") or 3), 5)
        )
        return item

    def create_interface_scenario_run(
        self,
        project_id: str,
        scenario: dict[str, Any],
        *,
        batch_id: str = "",
        created_by: str = "",
        status: str = "running",
        parameters: dict[str, Any] | None = None,
        concurrency_limit: int = 3,
    ) -> dict[str, Any]:
        if status not in {"queued", "running"}:
            raise ValueError("场景运行初始状态无效")
        run_id = uuid.uuid4().hex
        now = time.time()
        concurrency_limit = max(1, min(int(concurrency_limit), 5))
        summary = {
            "total_steps": len([step for step in scenario.get("steps") or [] if step.get("enabled", True)]),
            "passed_steps": 0,
            "failed_steps": 0,
            "skipped_steps": 0,
        }
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO interface_scenario_runs(
                   id,project_id,scenario_id,scenario_name,environment_id,batch_id,
                   concurrency_limit,status,
                   scenario_json,parameters_json,summary_json,result_json,
                   artifact_dir,docx_path,pdf_path,created_by,
                   stop_requested,created_at,started_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    project_id,
                    str(scenario.get("id") or ""),
                    str(scenario.get("name") or "接口场景"),
                    str(scenario.get("environment_id") or ""),
                    str(batch_id or ""),
                    concurrency_limit,
                    status,
                    json.dumps(scenario, ensure_ascii=False),
                    json.dumps(parameters or {}, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    "{}",
                    "",
                    "",
                    "",
                    str(created_by or ""),
                    0,
                    now,
                    now if status == "running" else None,
                ),
            )
        return self.get_interface_scenario_run(project_id, run_id) or {}

    def claim_interface_scenario_run(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT queued.id,queued.project_id
                   FROM interface_scenario_runs AS queued
                   WHERE queued.status='queued'
                   AND (
                       queued.batch_id='' OR
                       (SELECT COUNT(*) FROM interface_scenario_runs AS active
                        WHERE active.batch_id=queued.batch_id
                        AND active.project_id=queued.project_id
                        AND active.status='running') < queued.concurrency_limit
                   )
                   ORDER BY queued.created_at ASC LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE interface_scenario_runs
                   SET status='running',started_at=?,stop_requested=0
                   WHERE id=? AND project_id=? AND status='queued'""",
                (time.time(), row["id"], row["project_id"]),
            ).rowcount
            if not updated:
                return None
            claimed = connection.execute(
                "SELECT * FROM interface_scenario_runs WHERE id=? AND project_id=?",
                (row["id"], row["project_id"]),
            ).fetchone()
        return self._decode_interface_scenario_run(claimed)

    def request_stop_interface_scenario_run(
        self, project_id: str, run_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            current = connection.execute(
                """SELECT status FROM interface_scenario_runs
                   WHERE id=? AND project_id=?""",
                (run_id, project_id),
            ).fetchone()
            if not current:
                return None
            if current["status"] == "queued":
                connection.execute(
                    """UPDATE interface_scenario_runs
                       SET status='interrupted',stop_requested=1,finished_at=?
                       WHERE id=? AND project_id=? AND status='queued'""",
                    (time.time(), run_id, project_id),
                )
            elif current["status"] == "running":
                connection.execute(
                    """UPDATE interface_scenario_runs SET stop_requested=1
                       WHERE id=? AND project_id=?""",
                    (run_id, project_id),
                )
        return self.get_interface_scenario_run(project_id, run_id)

    def is_interface_scenario_stop_requested(
        self, project_id: str, run_id: str
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT stop_requested FROM interface_scenario_runs
                   WHERE id=? AND project_id=?""",
                (run_id, project_id),
            ).fetchone()
        return bool(row and row["stop_requested"])

    def finish_interface_scenario_run(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "interrupted"}:
            raise ValueError("场景运行结束状态无效")
        artifact_values = artifacts or {}
        with self._connection() as connection:
            updated = connection.execute(
                """UPDATE interface_scenario_runs
                   SET status=?,summary_json=?,result_json=?,artifact_dir=?,docx_path=?,
                       pdf_path=?,finished_at=? WHERE id=? AND project_id=?""",
                (
                    status,
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    str(artifact_values.get("artifact_dir") or ""),
                    str(artifact_values.get("docx_path") or ""),
                    str(artifact_values.get("pdf_path") or ""),
                    time.time(),
                    run_id,
                    project_id,
                ),
            ).rowcount
            if not updated:
                raise KeyError(run_id)
        return self.get_interface_scenario_run(project_id, run_id) or {}

    def get_interface_scenario_run(
        self, project_id: str, run_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM interface_scenario_runs WHERE id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()
        return self._decode_interface_scenario_run(row)

    def list_interface_scenario_runs(
        self, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id,project_id,scenario_id,scenario_name,environment_id,batch_id,
                          concurrency_limit,status,summary_json,artifact_dir,docx_path,
                          pdf_path,created_by,
                          created_at,started_at,finished_at
                   FROM interface_scenario_runs WHERE project_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (project_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            self._decode_interface_scenario_run(row, include_result=False) or {}
            for row in rows
        ]

    def list_interface_scenario_batch_runs(
        self, project_id: str, batch_id: str
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM interface_scenario_runs
                   WHERE project_id=? AND batch_id=? ORDER BY created_at ASC""",
                (project_id, batch_id),
            ).fetchall()
        return [
            self._decode_interface_scenario_run(row, include_result=False) or {}
            for row in rows
        ]

    # Legacy endpoint override specs --------------------------------

    def save_endpoint_spec(self, data: dict[str, Any], publish: bool = False) -> dict[str, Any]:
        logical_name = str(data["logical_name"]).strip()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM endpoint_specs WHERE logical_name = ?",
                (logical_name,),
            ).fetchone()
            version = int(row["version"]) + 1
            if publish:
                connection.execute(
                    "UPDATE endpoint_specs SET status = 'archived' WHERE logical_name = ? AND status = 'published'",
                    (logical_name,),
                )
            spec_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO endpoint_specs(
                    id, logical_name, version, method, scheme, host, port, path,
                    default_path, source_type, spec_json, status, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    spec_id, logical_name, version, str(data.get("method", "GET")).upper(),
                    str(data.get("scheme", "http")), str(data.get("host", "")),
                    int(data["port"]) if data.get("port") not in (None, "") else None,
                    str(data["path"]), str(data.get("default_path", "")),
                    str(data.get("source_type", "json")),
                    json.dumps(data.get("spec", {}), ensure_ascii=False),
                    "published" if publish else "draft", time.time(),
                ),
            )
        return self.get_endpoint_spec(spec_id) or {}

    def get_endpoint_spec(self, spec_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM endpoint_specs WHERE id = ?", (spec_id,)
            ).fetchone()
        item = self._row(row)
        if item:
            item["spec"] = json.loads(item.pop("spec_json") or "{}")
        return item

    def list_endpoint_specs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM endpoint_specs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["spec"] = json.loads(item.pop("spec_json") or "{}")
            result.append(item)
        return result

    def resolve_endpoint(self, default_path: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM endpoint_specs
                   WHERE status = 'published' AND default_path = ?
                   ORDER BY version DESC LIMIT 1""",
                (default_path,),
            ).fetchone()
        return self._row(row)

    # Report jobs ----------------------------------------------------

    def create_report_job(
        self, run_id: str, template_version: int, options: dict[str, Any]
    ) -> dict[str, Any]:
        canonical = json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idem = hashlib.sha256(f"{run_id}:{template_version}:{canonical}".encode("utf-8")).hexdigest()
        now = time.time()
        job_id = uuid.uuid4().hex
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM report_jobs WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if existing:
                return self._decode_report_job(existing)
            connection.execute(
                """INSERT INTO report_jobs(
                    id, run_id, status, idempotency_key, template_version,
                    options_json, next_attempt_at, message, created_at
                ) VALUES(?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                (job_id, run_id, idem, template_version, canonical, now, "报告任务已进入队列", now),
            )
        return self.get_report_job(job_id) or {}

    @staticmethod
    def _decode_report_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["options"] = json.loads(item.pop("options_json") or "{}")
        item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
        return item

    def get_report_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM report_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_report_job(row)

    def list_report_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM report_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [self._decode_report_job(row) for row in rows]

    def claim_report_job(self) -> dict[str, Any] | None:
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM report_jobs
                   WHERE status IN ('queued', 'retrying') AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            changed = connection.execute(
                """UPDATE report_jobs SET status = 'running', attempts = attempts + 1,
                   started_at = ?, message = '正在生成报告' WHERE id = ?
                   AND status IN ('queued', 'retrying')""",
                (now, row["id"]),
            ).rowcount
            connection.commit()
        return self.get_report_job(row["id"]) if changed else None

    def complete_report_job(self, job_id: str, snapshot: dict[str, Any], artifacts: dict[str, str]) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """UPDATE report_jobs SET status='succeeded', message='报告生成完成', error='',
                   snapshot_json=?, artifact_dir=?, docx_path=?, pdf_path=?,
                   docx_sha256=?, pdf_sha256=?, finished_at=? WHERE id=?""",
                (
                    json.dumps(snapshot, ensure_ascii=False), artifacts.get("artifact_dir", ""),
                    artifacts.get("docx_path", ""), artifacts.get("pdf_path", ""),
                    artifacts.get("docx_sha256", ""), artifacts.get("pdf_sha256", ""), now, job_id,
                ),
            )

    def fail_report_job(self, job_id: str, error: str) -> None:
        job = self.get_report_job(job_id)
        if not job:
            return
        retry = int(job["attempts"]) < int(job["max_attempts"])
        delay = min(60, 2 ** max(0, int(job["attempts"])))
        with self._connection() as connection:
            connection.execute(
                """UPDATE report_jobs SET status=?, message=?, error=?, next_attempt_at=?, finished_at=?
                   WHERE id=?""",
                (
                    "retrying" if retry else "failed",
                    f"生成失败，{delay}s 后重试" if retry else "报告生成失败",
                    error[:4000], time.time() + delay,
                    None if retry else time.time(), job_id,
                ),
            )

    # Server stress jobs --------------------------------------------

    @staticmethod
    def _decode_stress_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["modes"] = json.loads(item.pop("modes_json") or "[]")
        item["modules"] = json.loads(item.pop("modules_json") or "[]")
        item["options"] = json.loads(item.pop("options_json") or "{}")
        return item

    def create_stress_job(
        self,
        options: dict[str, Any],
        *,
        secret_required: bool = False,
        secret_enc: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        job_id = uuid.uuid4().hex
        persisted_options = dict(options)
        persisted_options.pop("password", None)
        modes = [
            str(item).strip()
            for item in persisted_options.get("modes", [])
            if str(item).strip()
        ]
        modules = [
            str(item).strip()
            for item in persisted_options.get("modules", [])
            if str(item).strip()
        ]
        target_name = str(
            persisted_options.get("target_name")
            or persisted_options.get("host")
            or "未命名服务器"
        ).strip()
        target_host = str(persisted_options.get("host") or "").strip()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO stress_jobs(
                    id, status, target_name, target_host, modes_json, modules_json,
                    options_json, message, secret_required, created_at
                ) VALUES(?, 'authorized', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, target_name, target_host,
                    json.dumps(modes, ensure_ascii=False),
                    json.dumps(modules, ensure_ascii=False),
                    json.dumps(persisted_options, ensure_ascii=False, sort_keys=True),
                    "任务已安全授权，等待新版执行器领取",
                    1 if secret_required or secret_enc else 0,
                    now,
                ),
            )
            if secret_enc:
                connection.execute(
                    """INSERT INTO stress_job_secrets(job_id, secret_enc, created_at)
                       VALUES(?, ?, ?)""",
                    (job_id, secret_enc, now),
                )
        return self.get_stress_job(job_id) or {}

    def claim_stress_job(self, excluded_hosts: set[str] | None = None) -> dict[str, Any] | None:
        excluded = sorted(host for host in (excluded_hosts or set()) if host)
        host_clause = ""
        values: list[Any] = []
        if excluded:
            host_clause = f" AND target_host NOT IN ({','.join('?' for _ in excluded)})"
            values.extend(excluded)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM stress_jobs WHERE status IN ('authorized', 'queued') AND stop_requested=0"
                + host_clause
                + " ORDER BY created_at LIMIT 1",
                values,
            ).fetchone()
            if not row:
                connection.commit()
                return None
            secret_row = connection.execute(
                "SELECT secret_enc FROM stress_job_secrets WHERE job_id=?",
                (row["id"],),
            ).fetchone()
            changed = connection.execute(
                """UPDATE stress_jobs SET status='running', message='正在准备压测任务',
                   started_at=?, error='' WHERE id=?
                   AND status IN ('authorized', 'queued') AND stop_requested=0""",
                (time.time(), row["id"]),
            ).rowcount
            if changed:
                connection.execute(
                    "DELETE FROM stress_job_secrets WHERE job_id=?",
                    (row["id"],),
                )
            connection.commit()
        if not changed:
            return None
        job = self.get_stress_job(row["id"])
        if job is not None and secret_row is not None:
            job["_secret_enc"] = str(secret_row["secret_enc"] or "")
        return job

    def get_stress_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM stress_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_stress_job(row)

    def list_stress_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM stress_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._decode_stress_job(row) for row in rows]

    def update_stress_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        message: str | None = None,
        error: str | None = None,
        artifact_dir: str | None = None,
        report_path: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        fields = []
        values: list[Any] = []
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if message is not None:
            fields.append("message=?")
            values.append(message)
        if error is not None:
            fields.append("error=?")
            values.append(error[:4000])
        if artifact_dir is not None:
            fields.append("artifact_dir=?")
            values.append(artifact_dir)
        if report_path is not None:
            fields.append("report_path=?")
            values.append(report_path)
        if started:
            fields.append("started_at=?")
            values.append(time.time())
        if finished:
            fields.append("finished_at=?")
            values.append(time.time())
        if not fields:
            return
        values.append(job_id)
        with self._connection() as connection:
            connection.execute(f"UPDATE stress_jobs SET {', '.join(fields)} WHERE id=?", values)
            if finished or status in {"succeeded", "failed", "interrupted"}:
                connection.execute(
                    "DELETE FROM stress_job_secrets WHERE job_id=?",
                    (job_id,),
                )

    def request_stop_stress_job(self, job_id: str) -> dict[str, Any]:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM stress_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["status"] in {"authorized", "queued"}:
                connection.execute(
                    """UPDATE stress_jobs SET status='interrupted', stop_requested=1,
                       message='压测任务已取消', finished_at=? WHERE id=?""",
                    (now, job_id),
                )
                connection.execute(
                    "DELETE FROM stress_job_secrets WHERE job_id=?",
                    (job_id,),
                )
            elif row["status"] == "running":
                connection.execute(
                    """UPDATE stress_jobs SET stop_requested=1,
                       message='正在安全停止压测任务' WHERE id=?""",
                    (job_id,),
                )
        return self.get_stress_job(job_id) or {}

    def is_stress_stop_requested(self, job_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT stop_requested FROM stress_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bool(row and row["stop_requested"])

    def add_stress_sample(self, job_id: str, source: str, sample: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO stress_samples(job_id, source, data_json, created_at)
                   VALUES(?, ?, ?, ?)""",
                (job_id, source, json.dumps(sample, ensure_ascii=False), time.time()),
            )

    def get_stress_samples(
        self,
        job_id: str,
        after_id: int = 0,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM stress_samples
                   WHERE job_id=? AND id>? ORDER BY id LIMIT ?""",
                (job_id, max(0, int(after_id)), max(1, min(int(limit), 20000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json") or "{}")
            result.append(item)
        return result
