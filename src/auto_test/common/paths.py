"""Runtime directory layout and migration from the legacy ``logs`` layout."""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from auto_test.common.env import get_env

_SOURCE_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = Path(
    get_env(
        "HOME",
        _SOURCE_ROOT if (_SOURCE_ROOT / "pyproject.toml").is_file() else Path.cwd(),
    )
).resolve()

DATA_DIR = PROJECT_ROOT / "data"
PLATFORM_DB_PATH = DATA_DIR / "platform.db"
TASKS_DB_PATH = DATA_DIR / "tasks.db"

RUNTIME_DIR = PROJECT_ROOT / "runtime"
UPLOADS_DIR = RUNTIME_DIR / "uploads"

ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
RUNS_DIR = ARTIFACTS_DIR / "runs"
SERVER_STRESS_DIR = ARTIFACTS_DIR / "server_stress"

LOGS_DIR = PROJECT_ROOT / "logs"
WEB_LOGS_DIR = LOGS_DIR / "web"

INSTANCE_DIR = PROJECT_ROOT / "instance"
LOCAL_WEB_SECRETS_PATH = INSTANCE_DIR / "local_web_secrets.json"

_RUN_DIR_RE = re.compile(r"^\d{8}_\d{6}$")


def _move_without_overwrite(source: Path, target: Path) -> None:
    """Move a path while preserving anything already present at the target."""
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.move(str(source), str(target))
        return
    if not source.is_dir() or not target.is_dir():
        return
    for child in source.iterdir():
        _move_without_overwrite(child, target / child.name)
    try:
        source.rmdir()
    except OSError:
        pass


def _move_sqlite_database(source: Path, target: Path) -> None:
    """Copy a consistent SQLite snapshot, then remove the legacy files."""
    if not source.exists() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source), timeout=30)
    target_connection = sqlite3.connect(str(target), timeout=30)
    try:
        source_connection.backup(target_connection)
    except Exception:
        target_connection.close()
        source_connection.close()
        target.unlink(missing_ok=True)
        raise
    else:
        target_connection.close()
        source_connection.close()

    for path in (
        source.with_name(source.name + "-wal"),
        source.with_name(source.name + "-shm"),
        source,
    ):
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            # A legacy process can still hold a Windows file handle. The new
            # database is already a consistent snapshot; retry cleanup later.
            pass


def _path_replacements(root: Path) -> list[tuple[str, str]]:
    legacy = root / "logs"
    mappings = [
        (legacy / "server_stress", root / "artifacts" / "server_stress"),
        (legacy / "uploads", root / "runtime" / "uploads"),
        (legacy, root / "artifacts" / "runs"),
    ]
    replacements: list[tuple[str, str]] = []
    for old, new in mappings:
        old_native = str(old.resolve())
        new_native = str(new.resolve())
        replacements.append((old_native, new_native))
        replacements.append((old_native.replace("\\", "\\\\"), new_native.replace("\\", "\\\\")))
        replacements.append((old.resolve().as_posix(), new.resolve().as_posix()))
    return replacements


def _replace_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    replacements: list[tuple[str, str]],
) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return
    for column in columns:
        for old, new in replacements:
            connection.execute(
                f'UPDATE "{table}" SET "{column}"=replace("{column}", ?, ?) '
                f'WHERE instr("{column}", ?) > 0',
                (old, new, old),
            )


def _migrate_database_paths(root: Path) -> None:
    replacements = _path_replacements(root)
    tasks_db = root / "data" / "tasks.db"
    if tasks_db.is_file():
        connection = sqlite3.connect(tasks_db)
        try:
            _replace_columns(
                connection,
                "runs",
                ("run_dir", "metadata_json"),
                replacements,
            )
            connection.commit()
        finally:
            connection.close()

    platform_db = root / "data" / "platform.db"
    if platform_db.is_file():
        connection = sqlite3.connect(platform_db)
        try:
            _replace_columns(
                connection,
                "report_jobs",
                ("options_json", "snapshot_json", "artifact_dir", "docx_path", "pdf_path"),
                replacements,
            )
            _replace_columns(
                connection,
                "stress_jobs",
                ("options_json", "artifact_dir", "report_path"),
                replacements,
            )
            connection.commit()
        finally:
            connection.close()


def prepare_runtime_layout(project_root: str | Path | None = None) -> None:
    """Create the current layout and migrate legacy runtime data in place.

    The operation is idempotent and never overwrites an existing destination.
    It is safe to call at every application startup.
    """
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    legacy_logs = root / "logs"
    data_dir = root / "data"
    uploads_dir = root / "runtime" / "uploads"
    runs_dir = root / "artifacts" / "runs"
    stress_dir = root / "artifacts" / "server_stress"
    web_logs_dir = root / "logs" / "web"
    instance_dir = root / "instance"

    _move_sqlite_database(legacy_logs / "tasks.db", data_dir / "tasks.db")
    _move_sqlite_database(legacy_logs / "platform.db", data_dir / "platform.db")
    _move_without_overwrite(
        legacy_logs / ".local_web_secrets.json",
        instance_dir / "local_web_secrets.json",
    )
    _move_without_overwrite(legacy_logs / "uploads", uploads_dir)
    _move_without_overwrite(legacy_logs / "server_stress", stress_dir)
    _move_without_overwrite(legacy_logs / "web_start", web_logs_dir / "startup")

    if legacy_logs.is_dir():
        for item in list(legacy_logs.iterdir()):
            if item.is_dir() and _RUN_DIR_RE.fullmatch(item.name):
                _move_without_overwrite(item, runs_dir / item.name)
            elif item.is_file() and item.name.startswith("web_server_") and item.suffix == ".log":
                _move_without_overwrite(item, web_logs_dir / item.name)

    for directory in (
        data_dir,
        uploads_dir,
        runs_dir,
        stress_dir,
        web_logs_dir,
        instance_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    _migrate_database_paths(root)
