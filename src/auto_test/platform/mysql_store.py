"""MySQL repositories compatible with the existing SQLite business stores."""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

# MySQL server error codes that mean the connection itself died; only these
# trigger an automatic reconnect-and-retry inside MySQLConnection.execute.
_LOST_CONNECTION_CODES = (2006, 2013, 2055)


def _placeholders(sql: str) -> str:
    """Convert qmark placeholders without touching quoted question marks.

    PyMySQL interpolates parameters with %-style formatting, so any literal
    percent in the SQL (e.g. ``LIKE '翻译进度%'``) must be doubled to ``%%``;
    PyMySQL restores it to a single ``%`` before sending the query.
    """
    result: list[str] = []
    quote = ""
    escaped = False
    for char in sql:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and quote:
            result.append(char)
            escaped = True
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            result.append(char)
            continue
        if char == "%":
            result.append("%%")
            continue
        result.append("%s" if char == "?" and not quote else char)
    return "".join(result)


def _mysql_sql(sql: str) -> str:
    statement = sql.strip()
    if statement.upper() == "BEGIN IMMEDIATE":
        return "START TRANSACTION"
    statement = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", statement, flags=re.I)
    return _placeholders(statement)


class MySQLConnection:
    """Small DB-API adapter exposing sqlite-style ``connection.execute``.

    Statements are retried once when the server reports a lost connection
    (2006/2013/2055): the pool keeps connections alive across network flaps,
    and a dead socket self-heals on the next use instead of failing the
    whole operation.
    """

    def __init__(self, connection):
        self.raw = connection
        self.broken = False

    def execute(self, sql: str, parameters=()):
        translated = _mysql_sql(sql)
        args = tuple(parameters or ())
        cursor = self.raw.cursor()
        try:
            cursor.execute(translated, args)
        except Exception as exc:
            code = (getattr(exc, "args", None) or [None])[0]
            if code not in _LOST_CONNECTION_CODES:
                raise
            # Connection died mid-flight: reconnect once and replay.  A
            # statement that failed with a lost-connection error did not
            # reach a durable commit, so replay is safe for our usage.
            self.raw.ping(reconnect=True)
            cursor = self.raw.cursor()
            cursor.execute(translated, args)
        return cursor

    def commit(self) -> None:
        try:
            self.raw.commit()
        except Exception:
            self.broken = True
            raise

    def rollback(self) -> None:
        try:
            self.raw.rollback()
        except Exception:
            self.broken = True
            raise

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:
            self.broken = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class _ConnectionPool:
    """Bounded connection pool shared across threads.

    The store previously opened a fresh MySQL connection for every query,
    which turned any database stall into a connect-timeout storm.  The pool
    reuses idle connections, discards broken ones on release, and fails
    fast (instead of hanging) when the pool is saturated.
    """

    def __init__(self, factory, *, max_connections: int = 12, acquire_timeout: float = 5.0):
        self._factory = factory
        self._max_connections = max_connections
        self._acquire_timeout = acquire_timeout
        self._condition = threading.Condition()
        self._idle: list[MySQLConnection] = []
        self._created = 0

    def acquire(self) -> MySQLConnection:
        deadline = time.monotonic() + self._acquire_timeout
        with self._condition:
            while True:
                if self._idle:
                    return self._idle.pop()
                if self._created < self._max_connections:
                    self._created += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"MySQL 连接池已满（{self._max_connections}），等待空闲连接超时"
                    )
                self._condition.wait(remaining)
        try:
            return self._factory()
        except Exception:
            with self._condition:
                self._created -= 1
                self._condition.notify()
            raise

    def release(self, connection: MySQLConnection) -> None:
        with self._condition:
            if getattr(connection, "broken", False):
                try:
                    connection.close()
                except Exception:
                    pass
                self._created -= 1
            else:
                self._idle.append(connection)
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            idle, self._idle = self._idle, []
            for connection in idle:
                try:
                    connection.close()
                except Exception:
                    pass
            self._created = 0


def mysql_connection_factory(settings: dict[str, Any]) -> Callable[[], MySQLConnection]:
    database = str(settings.get("database") or "").strip()
    if not database or not _IDENTIFIER.fullmatch(database):
        raise ValueError("database.database 必须是有效的MySQL库名")

    def connect() -> MySQLConnection:
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("启用MySQL需要安装PyMySQL依赖") from exc
        raw = pymysql.connect(
            host=str(settings.get("host") or "127.0.0.1"),
            port=int(settings.get("port") or 3306),
            user=str(settings.get("user") or ""),
            password=str(settings.get("password") or ""),
            database=database,
            charset="utf8mb4",
            connect_timeout=int(settings.get("connect_timeout") or 5),
            read_timeout=int(settings.get("read_timeout") or 30),
            write_timeout=int(settings.get("write_timeout") or 30),
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
        return MySQLConnection(raw)

    return connect


TASK_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        id VARCHAR(64) PRIMARY KEY, status VARCHAR(32) NOT NULL, stage VARCHAR(64) NOT NULL,
        progress INT NOT NULL DEFAULT 0, message TEXT NOT NULL, metadata_json LONGTEXT NOT NULL,
        run_dir VARCHAR(2048) NULL, error LONGTEXT NULL, created_at DOUBLE NOT NULL,
        started_at DOUBLE NULL, finished_at DOUBLE NULL, heartbeat_at DOUBLE NULL,
        stale_marked_at DOUBLE NULL,
        KEY idx_runs_status_created(status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS run_events (
        id BIGINT PRIMARY KEY AUTO_INCREMENT, run_id VARCHAR(64) NOT NULL, created_at DOUBLE NOT NULL,
        level VARCHAR(32) NOT NULL, stage VARCHAR(64) NOT NULL DEFAULT '', message LONGTEXT NOT NULL,
        progress INT NULL, event_type VARCHAR(32) NOT NULL DEFAULT '',
        KEY idx_events_run_id_id(run_id, id),
        KEY idx_events_run_type_id(run_id, event_type, id),
        CONSTRAINT fk_events_run FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS run_metrics (
        id BIGINT PRIMARY KEY AUTO_INCREMENT, run_id VARCHAR(64) NOT NULL, created_at DOUBLE NOT NULL,
        source VARCHAR(64) NOT NULL, data_json LONGTEXT NOT NULL,
        KEY idx_metrics_run_id_id(run_id, id),
        CONSTRAINT fk_metrics_run FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS run_secrets (
        run_id VARCHAR(64) PRIMARY KEY, secret_enc LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
        CONSTRAINT fk_run_secret_run FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


PLATFORM_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS server_profiles (
        id VARCHAR(64) PRIMARY KEY, name VARCHAR(255) NOT NULL, host VARCHAR(500) NOT NULL,
        port INT NOT NULL DEFAULT 22, ssh_user VARCHAR(255) NOT NULL,
        credential_enc LONGTEXT NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        last_used_at DOUBLE NULL, KEY idx_server_profiles_updated(updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_profiles (
        id VARCHAR(64) PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, provider VARCHAR(100) NOT NULL,
        base_url TEXT NOT NULL, model_name VARCHAR(255) NOT NULL, api_key_enc LONGTEXT NOT NULL,
        temperature DOUBLE NOT NULL DEFAULT 0.3, max_tokens INT NOT NULL DEFAULT 2000,
        system_prompt LONGTEXT NOT NULL, is_active TINYINT NOT NULL DEFAULT 0,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, KEY idx_models_active(is_active)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS report_templates (
        id VARCHAR(64) PRIMARY KEY, name VARCHAR(255) NOT NULL, version INT NOT NULL,
        sections_json LONGTEXT NOT NULL, updated_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS report_template_versions (
        template_id VARCHAR(64) NOT NULL, version INT NOT NULL, name VARCHAR(255) NOT NULL,
        sections_json LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
        PRIMARY KEY(template_id, version)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS endpoint_specs (
        id VARCHAR(64) PRIMARY KEY, logical_name VARCHAR(255) NOT NULL, version INT NOT NULL,
        method VARCHAR(16) NOT NULL, scheme VARCHAR(16) NOT NULL DEFAULT 'http', host VARCHAR(500) NOT NULL,
        port INT NULL, path TEXT NOT NULL, default_path VARCHAR(500) NOT NULL, source_type VARCHAR(64) NOT NULL,
        spec_json LONGTEXT NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'draft', created_at DOUBLE NOT NULL,
        UNIQUE KEY uk_endpoint_version(logical_name, version),
        KEY idx_endpoint_active(status, default_path, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS report_jobs (
        id VARCHAR(64) PRIMARY KEY, run_id VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,
        idempotency_key VARCHAR(64) NOT NULL UNIQUE, template_version INT NOT NULL,
        options_json LONGTEXT NOT NULL, snapshot_json LONGTEXT NULL, attempts INT NOT NULL DEFAULT 0,
        max_attempts INT NOT NULL DEFAULT 3, next_attempt_at DOUBLE NOT NULL DEFAULT 0,
        message TEXT NOT NULL, error LONGTEXT NULL, artifact_dir TEXT NULL,
        docx_path TEXT NULL, pdf_path TEXT NULL, docx_sha256 VARCHAR(64) NULL,
        pdf_sha256 VARCHAR(64) NULL, created_at DOUBLE NOT NULL, started_at DOUBLE NULL,
        finished_at DOUBLE NULL, KEY idx_report_jobs_status(status, next_attempt_at, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS stress_jobs (
        id VARCHAR(64) PRIMARY KEY, status VARCHAR(32) NOT NULL, target_name VARCHAR(255) NOT NULL,
        target_host VARCHAR(500) NOT NULL, modes_json LONGTEXT NOT NULL, modules_json LONGTEXT NOT NULL,
        options_json LONGTEXT NOT NULL, message TEXT NOT NULL, error LONGTEXT NULL,
        artifact_dir TEXT NULL, report_path TEXT NULL, stop_requested TINYINT NOT NULL DEFAULT 0,
        secret_required TINYINT NOT NULL DEFAULT 0, created_at DOUBLE NOT NULL,
        started_at DOUBLE NULL, finished_at DOUBLE NULL,
        KEY idx_stress_jobs_status(status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS stress_job_secrets (
        job_id VARCHAR(64) PRIMARY KEY, secret_enc LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
        CONSTRAINT fk_stress_secret_job FOREIGN KEY(job_id) REFERENCES stress_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS stress_samples (
        id BIGINT PRIMARY KEY AUTO_INCREMENT, job_id VARCHAR(64) NOT NULL, source VARCHAR(64) NOT NULL,
        data_json LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
        KEY idx_stress_samples_job(job_id, id),
        CONSTRAINT fk_samples_job FOREIGN KEY(job_id) REFERENCES stress_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS users (
        id VARCHAR(64) PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE,
        display_name VARCHAR(120) NOT NULL, password_hash VARCHAR(500) NOT NULL,
        is_active TINYINT NOT NULL DEFAULT 1, is_superuser TINYINT NOT NULL DEFAULT 0,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, last_login_at DOUBLE NULL,
        KEY idx_users_active(is_active, username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS projects (
        id VARCHAR(64) PRIMARY KEY, project_key VARCHAR(40) NOT NULL UNIQUE,
        name VARCHAR(120) NOT NULL, description TEXT NOT NULL,
        is_active TINYINT NOT NULL DEFAULT 1, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        KEY idx_projects_active(is_active, project_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_modules (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        name VARCHAR(120) NOT NULL, description TEXT NOT NULL, sort_order INT NOT NULL DEFAULT 0,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        UNIQUE KEY uk_interface_module(project_id, name),
        KEY idx_interface_modules_project(project_id, sort_order, name),
        CONSTRAINT fk_interface_module_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_environments (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        name VARCHAR(120) NOT NULL, base_url VARCHAR(1000) NOT NULL DEFAULT '',
        description TEXT NOT NULL, is_default TINYINT NOT NULL DEFAULT 0,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        UNIQUE KEY uk_interface_environment(project_id, name),
        KEY idx_interface_environments_project(project_id, is_default, name),
        CONSTRAINT fk_interface_environment_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_variables (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        environment_id VARCHAR(64) NOT NULL DEFAULT '', variable_key VARCHAR(128) NOT NULL,
        value_text LONGTEXT NOT NULL, secret_enc LONGTEXT NOT NULL,
        is_secret TINYINT NOT NULL DEFAULT 0, description TEXT NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        UNIQUE KEY uk_interface_variable(project_id, environment_id, variable_key),
        KEY idx_interface_variables_scope(project_id, environment_id, variable_key),
        CONSTRAINT fk_interface_variable_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_assets (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        module_id VARCHAR(64) NOT NULL DEFAULT '', environment_id VARCHAR(64) NOT NULL DEFAULT '',
        name VARCHAR(120) NOT NULL, description TEXT NOT NULL, method VARCHAR(16) NOT NULL,
        path TEXT NOT NULL, default_path VARCHAR(500) NOT NULL DEFAULT '', request_json LONGTEXT NOT NULL,
        current_version INT NOT NULL DEFAULT 1, status VARCHAR(32) NOT NULL DEFAULT 'draft',
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        UNIQUE KEY uk_interface_asset(project_id, module_id, name),
        KEY idx_interface_assets_project(project_id, module_id, updated_at),
        CONSTRAINT fk_interface_asset_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_asset_versions (
        id VARCHAR(64) PRIMARY KEY, asset_id VARCHAR(64) NOT NULL, version INT NOT NULL,
        definition_json LONGTEXT NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'draft',
        created_at DOUBLE NOT NULL, UNIQUE KEY uk_interface_asset_version(asset_id, version),
        KEY idx_interface_versions_asset(asset_id, version),
        CONSTRAINT fk_interface_version_asset FOREIGN KEY(asset_id) REFERENCES interface_assets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_scenarios (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        name VARCHAR(120) NOT NULL, description TEXT NOT NULL,
        environment_id VARCHAR(64) NOT NULL DEFAULT '', parameters_json LONGTEXT NOT NULL,
        steps_json LONGTEXT NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        UNIQUE KEY uk_interface_scenario(project_id, name),
        KEY idx_interface_scenarios_project(project_id, updated_at),
        CONSTRAINT fk_interface_scenario_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS interface_scenario_runs (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        scenario_id VARCHAR(64) NOT NULL DEFAULT '', scenario_name VARCHAR(120) NOT NULL,
        environment_id VARCHAR(64) NOT NULL DEFAULT '', batch_id VARCHAR(64) NOT NULL DEFAULT '',
        concurrency_limit INT NOT NULL DEFAULT 3,
        status VARCHAR(32) NOT NULL, scenario_json LONGTEXT NOT NULL,
        parameters_json LONGTEXT NOT NULL, summary_json LONGTEXT NOT NULL,
        result_json LONGTEXT NOT NULL,
        artifact_dir TEXT NOT NULL, docx_path TEXT NOT NULL, pdf_path TEXT NOT NULL,
        created_by VARCHAR(64) NOT NULL DEFAULT '', stop_requested TINYINT NOT NULL DEFAULT 0,
        created_at DOUBLE NOT NULL,
        started_at DOUBLE NULL, finished_at DOUBLE NULL,
        KEY idx_interface_scenario_runs_project(project_id, created_at),
        KEY idx_interface_scenario_runs_batch(batch_id, created_at),
        KEY idx_interface_scenario_runs_status(status, created_at),
        KEY idx_interface_scenario_runs_batch_status(batch_id, status),
        CONSTRAINT fk_interface_scenario_run_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_suites (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NULL,
        source VARCHAR(64) NOT NULL, name VARCHAR(160) NOT NULL,
        category VARCHAR(64) NOT NULL DEFAULT 'general', description TEXT NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'draft', created_by VARCHAR(64) NOT NULL DEFAULT '',
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        KEY idx_model_eval_suites_project(project_id, updated_at),
        CONSTRAINT fk_model_eval_suite_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_suite_versions (
        id VARCHAR(64) PRIMARY KEY, suite_id VARCHAR(64) NOT NULL, version INT NOT NULL,
        manifest_json LONGTEXT NOT NULL, content_sha256 VARCHAR(64) NOT NULL,
        upstream_json LONGTEXT NOT NULL, published_at DOUBLE NOT NULL,
        UNIQUE KEY uk_model_eval_suite_version(suite_id, version),
        KEY idx_model_eval_versions_suite(suite_id, version),
        CONSTRAINT fk_model_eval_version_suite FOREIGN KEY(suite_id) REFERENCES model_eval_suites(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_cases (
        id VARCHAR(64) PRIMARY KEY, version_id VARCHAR(64) NOT NULL,
        case_key VARCHAR(160) NOT NULL, category VARCHAR(64) NOT NULL DEFAULT 'general',
        tags_json LONGTEXT NOT NULL, payload_json LONGTEXT NOT NULL,
        weight DOUBLE NOT NULL DEFAULT 1, sort_order INT NOT NULL DEFAULT 0,
        UNIQUE KEY uk_model_eval_case(version_id, case_key),
        KEY idx_model_eval_cases_version(version_id, category, sort_order),
        CONSTRAINT fk_model_eval_case_version FOREIGN KEY(version_id) REFERENCES model_eval_suite_versions(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_runs (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        model_profile_id VARCHAR(64) NOT NULL DEFAULT '', suite_version_id VARCHAR(64) NOT NULL DEFAULT '',
        backend VARCHAR(64) NOT NULL, backend_version VARCHAR(64) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL, phase VARCHAR(64) NOT NULL, progress INT NOT NULL DEFAULT 0,
        message TEXT NOT NULL, error LONGTEXT NOT NULL, snapshot_json LONGTEXT NOT NULL,
        summary_json LONGTEXT NOT NULL, artifact_ref TEXT NOT NULL,
        created_by VARCHAR(64) NOT NULL DEFAULT '', stop_requested TINYINT NOT NULL DEFAULT 0,
        created_at DOUBLE NOT NULL, started_at DOUBLE NULL, finished_at DOUBLE NULL,
        KEY idx_model_eval_runs_project(project_id, created_at),
        KEY idx_model_eval_runs_status(status, created_at),
        CONSTRAINT fk_model_eval_run_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_case_results (
        id VARCHAR(64) PRIMARY KEY, run_id VARCHAR(64) NOT NULL,
        case_id VARCHAR(64) NOT NULL DEFAULT '', attempt INT NOT NULL DEFAULT 1,
        status VARCHAR(32) NOT NULL, metrics_json LONGTEXT NOT NULL,
        score_json LONGTEXT NOT NULL, artifact_ref TEXT NOT NULL, created_at DOUBLE NOT NULL,
        UNIQUE KEY uk_model_eval_case_result(run_id, case_id, attempt),
        KEY idx_model_eval_case_results_run(run_id, status),
        CONSTRAINT fk_model_eval_result_run FOREIGN KEY(run_id) REFERENCES model_eval_runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_run_events (
        id BIGINT PRIMARY KEY AUTO_INCREMENT, run_id VARCHAR(64) NOT NULL,
        event_type VARCHAR(64) NOT NULL, message TEXT NOT NULL,
        phase VARCHAR(64) NOT NULL DEFAULT '', progress INT NULL,
        data_json LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
        KEY idx_model_eval_events_run(run_id, id),
        CONSTRAINT fk_model_eval_event_run FOREIGN KEY(run_id) REFERENCES model_eval_runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_manual_reviews (
        id VARCHAR(64) PRIMARY KEY, result_id VARCHAR(64) NOT NULL,
        reviewer_id VARCHAR(64) NOT NULL DEFAULT '', score_json LONGTEXT NOT NULL,
        comment TEXT NOT NULL, created_at DOUBLE NOT NULL,
        KEY idx_model_eval_reviews_result(result_id, created_at),
        CONSTRAINT fk_model_eval_review_result FOREIGN KEY(result_id) REFERENCES model_eval_case_results(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_eval_comparisons (
        id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
        run_ids_json LONGTEXT NOT NULL, config_json LONGTEXT NOT NULL,
        created_by VARCHAR(64) NOT NULL DEFAULT '', created_at DOUBLE NOT NULL,
        KEY idx_model_eval_comparisons_project(project_id, created_at),
        CONSTRAINT fk_model_eval_comparison_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS project_memberships (
        project_id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL,
        role VARCHAR(32) NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        PRIMARY KEY(project_id, user_id), KEY idx_memberships_user(user_id, project_id),
        CONSTRAINT fk_membership_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
        CONSTRAINT fk_membership_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS auth_sessions (
        token_hash VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
        csrf_token VARCHAR(128) NOT NULL, created_at DOUBLE NOT NULL,
        expires_at DOUBLE NOT NULL, last_seen_at DOUBLE NOT NULL,
        KEY idx_sessions_user_expiry(user_id, expires_at),
        CONSTRAINT fk_session_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        id BIGINT PRIMARY KEY AUTO_INCREMENT, actor_user_id VARCHAR(64) NULL,
        project_id VARCHAR(64) NULL, action VARCHAR(160) NOT NULL,
        target_type VARCHAR(64) NOT NULL DEFAULT '', target_id VARCHAR(160) NOT NULL DEFAULT '',
        outcome VARCHAR(32) NOT NULL, ip_address VARCHAR(100) NOT NULL DEFAULT '',
        detail_json LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
        KEY idx_audit_created(created_at), KEY idx_audit_actor(actor_user_id, created_at),
        CONSTRAINT fk_audit_actor FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE SET NULL,
        CONSTRAINT fk_audit_project FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def _ensure_column(connection: MySQLConnection, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing MySQL table when it is missing."""
    existing = {
        str(row["Field"])
        for row in connection.execute(f"SHOW COLUMNS FROM {table}").fetchall()
    }
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _ensure_index(connection: MySQLConnection, table: str, index: str, ddl: str) -> None:
    """Create an index when it is missing (MySQL has no IF NOT EXISTS)."""
    found = connection.execute(
        """
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?
        """,
        (table, index),
    ).fetchone()
    if not found:
        connection.execute(f"CREATE INDEX {index} ON {table} {ddl}")


class _MySQLBase:
    backend = "mysql"

    def __init__(
        self,
        settings: dict[str, Any],
        connection_factory=None,
        *,
        initialize_schema: bool = True,
        pool_max_connections: int = 12,
    ):
        self.settings = dict(settings)
        self.db_path = None
        self._factory = connection_factory or mysql_connection_factory(self.settings)
        self._pool = _ConnectionPool(
            self._factory,
            max_connections=max(2, int(pool_max_connections)),
        )
        if initialize_schema:
            self._initialize()

    def _connect(self) -> MySQLConnection:
        return self._factory()

    def close(self) -> None:
        """Close all pooled connections; the pool remains reusable."""
        self._pool.close()

    @contextmanager
    def _connection(self):
        connection = self._pool.acquire()
        try:
            with connection:
                yield connection
        finally:
            self._pool.release(connection)


class MySQLTaskStore(_MySQLBase, TaskStore):
    def _initialize(self) -> None:
        with self._connection() as connection:
            for statement in TASK_SCHEMA:
                connection.execute(statement)
            # Migrate databases created before the heartbeat-grace and
            # structured-event columns existed.
            _ensure_column(
                connection, "runs", "stale_marked_at", "stale_marked_at DOUBLE NULL"
            )
            _ensure_column(
                connection,
                "run_events",
                "event_type",
                "event_type VARCHAR(32) NOT NULL DEFAULT ''",
            )
            _ensure_index(
                connection,
                "run_events",
                "idx_events_run_type_id",
                "(run_id, event_type, id)",
            )

    def claim_next(
        self,
        stale_after_seconds: float = 120,
        stale_grace_seconds: float = 120,
    ) -> dict[str, Any] | None:
        """Serialize legacy pipeline execution across all application processes."""
        with self._connection() as lock_connection:
            lock = lock_connection.execute(
                "SELECT GET_LOCK('liema_automation_claim', 5) AS acquired"
            ).fetchone()
            if not lock or not lock["acquired"]:
                return None
            try:
                return super().claim_next(stale_after_seconds, stale_grace_seconds)
            finally:
                lock_connection.execute("SELECT RELEASE_LOCK('liema_automation_claim')")


class MySQLPlatformStore(_MySQLBase, PlatformStore):
    def __init__(
        self,
        settings: dict[str, Any],
        connection_factory=None,
        *,
        initialize_schema: bool = True,
        recover_jobs: bool = True,
    ):
        self.recover_jobs = recover_jobs
        super().__init__(
            settings,
            connection_factory,
            initialize_schema=initialize_schema,
        )

    def _initialize(self) -> None:
        with self._connection() as connection:
            for statement in PLATFORM_SCHEMA:
                connection.execute(statement)
            _ensure_column(
                connection,
                "interface_scenario_runs",
                "scenario_json",
                "scenario_json LONGTEXT NULL",
            )
            _ensure_column(
                connection,
                "interface_scenario_runs",
                "parameters_json",
                "parameters_json LONGTEXT NULL",
            )
            _ensure_column(
                connection,
                "interface_scenario_runs",
                "stop_requested",
                "stop_requested TINYINT NOT NULL DEFAULT 0",
            )
            _ensure_column(
                connection,
                "interface_scenario_runs",
                "concurrency_limit",
                "concurrency_limit INT NOT NULL DEFAULT 3",
            )
            _ensure_index(
                connection,
                "interface_scenario_runs",
                "idx_interface_scenario_runs_status",
                "(status, created_at)",
            )
            _ensure_index(
                connection,
                "interface_scenario_runs",
                "idx_interface_scenario_runs_batch_status",
                "(batch_id, status)",
            )
            self._initialize_records(connection)

    def save_model_profile(self, data: dict[str, Any], api_key_enc: str | None) -> dict[str, Any]:
        now = time.time()
        import uuid

        profile_id = str(data.get("id") or uuid.uuid4().hex)
        existing = self.get_model_profile(profile_id)
        encrypted = api_key_enc if api_key_enc is not None else (existing or {}).get("api_key_enc", "")
        active = 1 if data.get("is_active", False) else 0
        with self._connection() as connection:
            if active:
                connection.execute("UPDATE model_profiles SET is_active=0")
            connection.execute(
                """INSERT INTO model_profiles(
                    id,name,provider,base_url,model_name,api_key_enc,temperature,max_tokens,
                    system_prompt,is_active,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON DUPLICATE KEY UPDATE name=VALUES(name),provider=VALUES(provider),
                    base_url=VALUES(base_url),model_name=VALUES(model_name),api_key_enc=VALUES(api_key_enc),
                    temperature=VALUES(temperature),max_tokens=VALUES(max_tokens),
                    system_prompt=VALUES(system_prompt),is_active=VALUES(is_active),updated_at=VALUES(updated_at)""",
                (
                    profile_id, str(data["name"]).strip(), str(data.get("provider", "openai-compatible")),
                    str(data["base_url"]).strip().rstrip("/"), str(data["model_name"]).strip(),
                    encrypted, float(data.get("temperature", 0.3)), int(data.get("max_tokens", 2000)),
                    str(data.get("system_prompt", "")), active, (existing or {}).get("created_at", now), now,
                ),
            )
        return self.get_model_profile(profile_id) or {}
