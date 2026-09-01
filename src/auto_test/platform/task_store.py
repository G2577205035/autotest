"""SQLite-backed persistent state for automation runs."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class TaskNotFoundError(KeyError):
    pass


_TRANSLATION_PROGRESS_TYPE = "translation_progress"
_TRANSLATION_PROGRESS_PREFIXES = ("翻译进度", "翻译完成")


def event_type_for(message: str) -> str:
    """Derive the structured event type from a raw log message.

    Translation throughput statistics previously required a full ``LIKE``
    scan over every run event.  Typing the relevant events at write time
    lets the query use the ``(run_id, event_type, id)`` index instead.
    """
    text = str(message or "")
    if text.startswith(_TRANSLATION_PROGRESS_PREFIXES):
        return _TRANSLATION_PROGRESS_TYPE
    return ""


class TaskStore:
    """Persist run state and events with short, transaction-scoped connections."""

    backend = "sqlite"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
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
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    run_dir TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    heartbeat_at REAL,
                    stale_marked_at REAL
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    level TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    progress INTEGER,
                    event_type TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS run_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    source TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS run_secrets (
                    run_id TEXT PRIMARY KEY,
                    secret_enc TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_runs_status_created
                    ON runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_run_id_id
                    ON run_events(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_metrics_run_id_id
                    ON run_metrics(run_id, id);
                """
            )
            runs_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "stale_marked_at" not in runs_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN stale_marked_at REAL")
            events_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(run_events)").fetchall()
            }
            if "event_type" not in events_columns:
                connection.execute(
                    "ALTER TABLE run_events ADD COLUMN event_type TEXT NOT NULL DEFAULT ''"
                )
            # Created after the column migration: it references event_type,
            # which legacy databases only gain through the ALTER above.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_run_type_id"
                " ON run_events(run_id, event_type, id)"
            )

    @staticmethod
    def _make_display_name(item: dict[str, Any]) -> str:
        metadata = item.get("metadata") or {}
        options = metadata.get("options") if isinstance(metadata, dict) else {}
        options = options if isinstance(options, dict) else {}
        created = datetime.fromtimestamp(float(item.get("created_at") or time.time()))
        stamp = created.strftime("%Y%m%d-%H%M%S")
        username = str(options.get("username") or "全部账号").strip()
        host = str(options.get("host") or "").strip()
        case_name = str(options.get("case_name") or "").strip()
        host_tail = host.split(".")[-1] if host else "默认环境"
        parts = ["测试", stamp, username]
        if host_tail:
            parts.append(host_tail)
        if case_name:
            parts.append(case_name)
        return "-".join(parts)

    @staticmethod
    def _decode_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item.pop("stale_marked_at", None)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json"))
        except (TypeError, ValueError):
            item["metadata"] = {}
            item.pop("metadata_json", None)
        item["display_name"] = TaskStore._make_display_name(item)
        return item

    def create_run(
        self,
        metadata: dict[str, Any] | None = None,
        *,
        secret_enc: str = "",
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        now = time.time()
        encoded = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, status, stage, progress, message, metadata_json, created_at
                ) VALUES (?, 'queued', 'queued', 0, ?, ?, ?)
                """,
                (run_id, "任务已进入队列", encoded, now),
            )
            connection.execute(
                """
                INSERT INTO run_events (run_id, created_at, level, stage, message, progress, event_type)
                VALUES (?, ?, 'INFO', 'queued', ?, 0, '')
                """,
                (run_id, now, "任务已进入队列"),
            )
            if secret_enc:
                connection.execute(
                    """INSERT INTO run_secrets(run_id, secret_enc, created_at)
                       VALUES(?, ?, ?)""",
                    (run_id, str(secret_enc), now),
                )
        return self.get_run(run_id)

    def claim_next(
        self,
        stale_after_seconds: float = 120,
        stale_grace_seconds: float = 120,
    ) -> dict[str, Any] | None:
        """Atomically claim one queued task while enforcing global serialization.

        A running task whose heartbeat lags behind is no longer interrupted
        on sight: network flaps, machine sleep and database outages stall
        heartbeats without killing the executor process.  The first stale
        sighting only marks ``stale_marked_at`` and enters a grace window;
        the task is interrupted only when the heartbeat stays stale beyond
        ``stale_grace_seconds``.  A successful heartbeat clears the marker,
        so a healthy worker is never misjudged.
        """
        now = time.time()
        stale_before = now - stale_after_seconds
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale_rows = connection.execute(
                "SELECT id, stale_marked_at FROM runs WHERE status = 'running' AND heartbeat_at < ?",
                (stale_before,),
            ).fetchall()
            for row in stale_rows:
                marked = row["stale_marked_at"] or 0
                if not marked:
                    connection.execute(
                        """
                        UPDATE runs SET message = ?, stale_marked_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        ("心跳未更新，进入宽限期", now, row["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO run_events (run_id, created_at, level, stage, message, event_type)
                        VALUES (?, ?, 'WARNING', 'running', ?, '')
                        """,
                        (
                            row["id"],
                            now,
                            f"心跳已超过 {int(stale_after_seconds)} 秒未更新，进入 {int(stale_grace_seconds)} 秒宽限期，超时仍未恢复将中断任务",
                        ),
                    )
                elif now - marked > stale_grace_seconds:
                    connection.execute(
                        """
                        UPDATE runs
                        SET status = 'interrupted', stage = 'interrupted',
                            message = ?, error = ?, finished_at = ?, stale_marked_at = NULL
                        WHERE id = ? AND status = 'running'
                        """,
                        ("执行进程心跳超时", "worker heartbeat expired", now, row["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO run_events (run_id, created_at, level, stage, message, event_type)
                        VALUES (?, ?, 'ERROR', 'interrupted', ?, '')
                        """,
                        (row["id"], now, "执行进程心跳超时，任务已中断"),
                    )
                # else: still inside the grace window, keep running untouched

            recovered_rows = connection.execute(
                """
                SELECT id FROM runs
                WHERE status = 'running' AND heartbeat_at >= ? AND stale_marked_at IS NOT NULL
                """,
                (stale_before,),
            ).fetchall()
            for row in recovered_rows:
                connection.execute(
                    """
                    UPDATE runs SET message = ?, stale_marked_at = NULL
                    WHERE id = ?
                    """,
                    ("心跳已恢复，任务继续执行", row["id"]),
                )

            active = connection.execute(
                "SELECT 1 FROM runs WHERE status = 'running' LIMIT 1"
            ).fetchone()
            if active:
                connection.commit()
                return None

            row = connection.execute(
                "SELECT * FROM runs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            changed = connection.execute(
                """
                UPDATE runs
                SET status = 'running', stage = 'preparing', progress = 1,
                    message = ?, started_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                ("任务开始执行", now, now, row["id"]),
            ).rowcount
            secret_row = None
            if changed == 1:
                secret_row = connection.execute(
                    "SELECT secret_enc FROM run_secrets WHERE run_id=?",
                    (row["id"],),
                ).fetchone()
                connection.execute(
                    "DELETE FROM run_secrets WHERE run_id=?",
                    (row["id"],),
                )
            connection.commit()
            task = self.get_run(row["id"]) if changed == 1 else None
            if task is not None and secret_row is not None:
                task["_secret_enc"] = str(secret_row["secret_enc"] or "")
            return task

    def heartbeat(self, run_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET heartbeat_at = ?, stale_marked_at = NULL,
                    message = CASE WHEN stale_marked_at IS NOT NULL
                                   THEN '心跳已恢复，任务继续执行' ELSE message END
                WHERE id = ? AND status = 'running'
                """,
                (time.time(), run_id),
            )

    def update_progress(self, run_id: str, stage: str, message: str, progress: int) -> None:
        progress = max(0, min(99, int(progress)))
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE runs
                SET stage = ?, progress = ?, message = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (stage, progress, message, now, run_id),
            ).rowcount
            if changed != 1:
                raise TaskNotFoundError(run_id)
            connection.execute(
                """
                INSERT INTO run_events (run_id, created_at, level, stage, message, progress, event_type)
                VALUES (?, ?, 'INFO', ?, ?, ?, ?)
                """,
                (run_id, now, stage, message, progress, event_type_for(message)),
            )

    def complete(self, run_id: str, run_dir: str = "") -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'succeeded', stage = 'completed', progress = 100,
                    message = ?, run_dir = ?, finished_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'running'
                """,
                ("任务执行完成", run_dir, now, now, run_id),
            )
            connection.execute(
                """
                INSERT INTO run_events (run_id, created_at, level, stage, message, progress, event_type)
                VALUES (?, ?, 'INFO', 'completed', ?, 100, '')
                """,
                (run_id, now, "任务执行完成"),
            )
            connection.execute("DELETE FROM run_secrets WHERE run_id=?", (run_id,))

    def fail(self, run_id: str, error: str) -> None:
        now = time.time()
        safe_error = error[:4000]
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', stage = 'failed', message = ?, error = ?,
                    finished_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'running'
                """,
                ("任务执行失败", safe_error, now, now, run_id),
            )
            connection.execute(
                """
                INSERT INTO run_events (run_id, created_at, level, stage, message, event_type)
                VALUES (?, ?, 'ERROR', 'failed', ?, '')
                """,
                (run_id, now, safe_error),
            )
            connection.execute("DELETE FROM run_secrets WHERE run_id=?", (run_id,))

    def add_event(self, run_id: str, level: str, message: str, stage: str = "") -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO run_events (run_id, created_at, level, stage, message, event_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, time.time(), level, stage, message[:10000], event_type_for(message)),
            )

    def add_metric(self, run_id: str, source: str, data: dict[str, Any]) -> None:
        """Persist one live performance sample for charts and report snapshots."""
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO run_metrics(run_id, created_at, source, data_json)
                   VALUES(?, ?, ?, ?)""",
                (run_id, time.time(), source[:40], json.dumps(data, ensure_ascii=False)),
            )

    def _update_metadata(self, connection: sqlite3.Connection, run_id: str, updater) -> dict[str, Any]:
        row = connection.execute("SELECT metadata_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(run_id)
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        updater(metadata)
        connection.execute(
            "UPDATE runs SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), run_id),
        )
        return metadata

    def request_stop(self, run_id: str) -> dict[str, Any]:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(run_id)
            status = row["status"]
            if status == "queued":
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'interrupted', stage = 'interrupted', progress = 100,
                        message = ?, finished_at = ?, heartbeat_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    ("任务已取消", now, now, run_id),
                )
                connection.execute(
                    """
                    INSERT INTO run_events (run_id, created_at, level, stage, message, progress, event_type)
                    VALUES (?, ?, 'WARNING', 'interrupted', ?, 100, '')
                    """,
                    (run_id, now, "任务已取消"),
                )
                connection.execute("DELETE FROM run_secrets WHERE run_id=?", (run_id,))
            elif status == "running":
                self._update_metadata(connection, run_id, lambda metadata: metadata.update({"stop_requested": True}))
                connection.execute(
                    """
                    UPDATE runs SET message = ?, heartbeat_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    ("停止请求已发送，任务将在下一个安全检查点中断", now, run_id),
                )
                connection.execute(
                    """
                    INSERT INTO run_events (run_id, created_at, level, stage, message, event_type)
                    VALUES (?, ?, 'WARNING', 'running', ?, '')
                    """,
                    (run_id, now, "停止请求已发送"),
                )
        return self.get_run(run_id)

    def is_stop_requested(self, run_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT metadata_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(run_id)
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            return False
        return bool(metadata.get("stop_requested"))

    def interrupt(self, run_id: str, message: str = "任务已停止") -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'interrupted', stage = 'interrupted', progress = 100,
                    message = ?, finished_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (message, now, now, run_id),
            )
            connection.execute(
                """
                INSERT INTO run_events (run_id, created_at, level, stage, message, progress, event_type)
                VALUES (?, ?, 'WARNING', 'interrupted', ?, 100, '')
                """,
                (run_id, now, message),
            )
            connection.execute("DELETE FROM run_secrets WHERE run_id=?", (run_id,))

    def wake_queued(self, run_id: str) -> dict[str, Any]:
        now = time.time()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE runs SET message = ?, heartbeat_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                ("已唤醒执行队列，等待调度", now, run_id),
            ).rowcount
            if changed != 1 and connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise TaskNotFoundError(run_id)
            if changed == 1:
                connection.execute(
                    """
                    INSERT INTO run_events (run_id, created_at, level, stage, message, event_type)
                    VALUES (?, ?, 'INFO', 'queued', ?, '')
                    """,
                    (run_id, now, "已唤醒执行队列，等待调度"),
                )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        item = self._decode_run(row)
        if item is None:
            raise TaskNotFoundError(run_id)
        return item

    def latest_run(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._decode_run(row)

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, status, stage, progress, message, metadata_json,
                       created_at, started_at, finished_at, heartbeat_at,
                       CASE WHEN COALESCE(run_dir, '') <> '' THEN 1 ELSE 0 END AS has_run_dir
                FROM runs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        self.get_run(run_id)
        limit = max(1, min(5000, int(limit)))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, created_at, level, stage, message, progress
                FROM run_events
                WHERE run_id = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (run_id, max(0, int(after_id)), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_translation_progress_events(self, run_id: str) -> list[dict[str, Any]]:
        """Load the complete translation progress series for rate calculation.

        New events are typed as ``translation_progress`` at write time and
        resolve through the ``(run_id, event_type, id)`` index; the legacy
        ``LIKE`` branch only covers rows written before the structured
        ``event_type`` column existed, so the scan disappears as new data
        accumulates.
        """
        self.get_run(run_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, created_at, level, stage, message, progress
                FROM run_events
                WHERE run_id = ?
                  AND (event_type = 'translation_progress'
                       OR (event_type = ''
                           AND (message LIKE '翻译进度%' OR message LIKE '翻译完成%')))
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_metrics(self, run_id: str, after_id: int = 0, limit: int = 5000) -> list[dict[str, Any]]:
        self.get_run(run_id)
        limit = max(1, min(100000, int(limit)))
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, run_id, created_at, source, data_json
                   FROM run_metrics WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?""",
                (run_id, max(0, int(after_id)), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json"))
            except (TypeError, ValueError):
                item["data"] = {}
                item.pop("data_json", None)
            result.append(item)
        return result
