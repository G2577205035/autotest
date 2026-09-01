"""Configured repository factories.

The factory is deliberately strict: selecting MySQL before its repository and
driver are installed fails at startup instead of silently writing to SQLite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_test.common.config_loader import database_cfg
from auto_test.common.env import get_env
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


def _settings(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    values = dict(database_cfg())
    values.update(overrides or {})
    values["backend"] = str(
        get_env("DATABASE_BACKEND", values.get("backend", "sqlite"))
    ).strip().lower()
    environment_keys = {
        "host": "MYSQL_HOST",
        "port": "MYSQL_PORT",
        "database": "MYSQL_DATABASE",
        "user": "MYSQL_USER",
        "password": "MYSQL_PASSWORD",
        "tasks_path": "SQLITE_TASKS_PATH",
        "platform_path": "SQLITE_PLATFORM_PATH",
    }
    for key, environment_name in environment_keys.items():
        environment_value = get_env(environment_name)
        if environment_value is not None:
            values[key] = environment_value
    return values


def database_backend(overrides: dict[str, Any] | None = None) -> str:
    return str(_settings(overrides)["backend"] or "sqlite")


def create_task_repository(base_dir: str | Path, overrides: dict[str, Any] | None = None):
    base = Path(base_dir).resolve()
    settings = _settings(overrides)
    if settings["backend"] == "mysql":
        from auto_test.platform.mysql_store import MySQLTaskStore

        return MySQLTaskStore(settings)
    if settings["backend"] != "sqlite":
        raise ValueError(f"不支持的数据库后端：{settings['backend']}")
    configured = str(settings.get("tasks_path") or "data/tasks.db")
    path = Path(configured)
    return TaskStore(path if path.is_absolute() else base / path)


def create_platform_repository(
    base_dir: str | Path,
    overrides: dict[str, Any] | None = None,
    *,
    recover_jobs: bool = True,
):
    base = Path(base_dir).resolve()
    settings = _settings(overrides)
    if settings["backend"] == "mysql":
        from auto_test.platform.mysql_store import MySQLPlatformStore

        return MySQLPlatformStore(settings, recover_jobs=recover_jobs)
    if settings["backend"] != "sqlite":
        raise ValueError(f"不支持的数据库后端：{settings['backend']}")
    configured = str(settings.get("platform_path") or "data/platform.db")
    path = Path(configured)
    return PlatformStore(
        path if path.is_absolute() else base / path,
        recover_jobs=recover_jobs,
    )
