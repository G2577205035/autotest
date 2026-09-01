"""Dry-run friendly MySQL and MinIO migration utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from auto_test.common.paths import ARTIFACTS_DIR, PLATFORM_DB_PATH, PROJECT_ROOT, TASKS_DB_PATH
from auto_test.platform.artifact_storage import MinioArtifactStorage, create_artifact_storage
from auto_test.platform.mysql_store import MySQLPlatformStore, MySQLTaskStore
from auto_test.platform.persistence import _settings


TASK_TABLES = ("runs", "run_events", "run_metrics")
PLATFORM_TABLES = (
    "model_profiles", "report_templates", "report_template_versions", "endpoint_specs",
    "report_jobs", "stress_jobs", "stress_samples",
)


def _sqlite_rows(path: Path, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
        return columns, rows
    finally:
        connection.close()


def _mysql_count(store, table: str) -> int:
    with store._connection() as connection:
        exists = connection.execute(
            """SELECT COUNT(*) AS total FROM information_schema.tables
               WHERE table_schema=? AND table_name=?""",
            (store.settings["database"], table),
        ).fetchone()
        if not exists or not int(exists["total"]):
            return 0
        row = connection.execute(f"SELECT COUNT(*) AS total FROM `{table}`").fetchone()
    return int(row["total"])


def backup_sqlite_databases(
    destination: str | Path, *, tasks_path: str | Path = TASKS_DB_PATH,
    platform_path: str | Path = PLATFORM_DB_PATH,
) -> dict[str, str]:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, source in (("tasks", Path(tasks_path)), ("platform", Path(platform_path))):
        if not source.is_file():
            raise FileNotFoundError(source)
        target = destination / f"{name}.db"
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        result[name] = str(target)
    manifest = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": result,
        "sha256": {name: _sha256(Path(path)) for name, path in result.items()},
    }
    manifest_path = destination / "migration_backup_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    result["manifest"] = str(manifest_path)
    return result


def restore_sqlite_backup(
    backup_directory: str | Path,
    *,
    tasks_path: str | Path = TASKS_DB_PATH,
    platform_path: str | Path = PLATFORM_DB_PATH,
    overwrite: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Restore a verified pre-migration SQLite snapshot for rollback."""
    backup_directory = Path(backup_directory).resolve()
    manifest_path = backup_directory / "migration_backup_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    targets = {"tasks": Path(tasks_path), "platform": Path(platform_path)}
    actions = {}
    for name, target in targets.items():
        source = backup_directory / f"{name}.db"
        expected = str((manifest.get("sha256") or {}).get(name) or "")
        actual = _sha256(source)
        if not expected or actual != expected:
            raise RuntimeError(f"{name}.db备份校验失败，拒绝回滚")
        if target.exists() and not overwrite:
            raise FileExistsError(f"目标数据库已存在，回滚需显式指定overwrite：{target}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".restore")
            shutil.copy2(source, temporary)
            temporary.replace(target)
        actions[name] = {"source": str(source), "target": str(target), "sha256": actual}
    return {"dry_run": dry_run, "verified": True, "actions": actions}


def migrate_sqlite_to_mysql(
    task_store: MySQLTaskStore,
    platform_store: MySQLPlatformStore,
    *,
    tasks_path: str | Path = TASKS_DB_PATH,
    platform_path: str | Path = PLATFORM_DB_PATH,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Upsert SQLite records to MySQL in dependency order and verify counts."""
    sources = ((Path(tasks_path), task_store, TASK_TABLES), (Path(platform_path), platform_store, PLATFORM_TABLES))
    report: dict[str, Any] = {"dry_run": dry_run, "tables": {}}
    for source, store, tables in sources:
        if not source.is_file():
            raise FileNotFoundError(source)
        for table in tables:
            columns, rows = _sqlite_rows(source, table)
            before = _mysql_count(store, table)
            if not dry_run and rows:
                quoted = ",".join(f"`{column}`" for column in columns)
                placeholders = ",".join("?" for _ in columns)
                updates = ",".join(
                    f"`{column}`=VALUES(`{column}`)" for column in columns
                )
                sql = (
                    f"INSERT INTO `{table}`({quoted}) VALUES({placeholders}) "
                    f"ON DUPLICATE KEY UPDATE {updates}"
                )
                with store._connection() as connection:
                    for row in rows:
                        connection.execute(sql, tuple(row.get(column) for column in columns))
            after = _mysql_count(store, table)
            report["tables"][table] = {
                "sqlite": len(rows), "mysql_before": before, "mysql_after": after,
                "verified": dry_run or after >= len(rows),
            }
    report["verified"] = all(item["verified"] for item in report["tables"].values())
    return report


def migrate_local_to_minio(
    storage: MinioArtifactStorage,
    source: str | Path = ARTIFACTS_DIR,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    source = Path(source).resolve()
    source.relative_to(storage.root)
    files = [path for path in sorted(source.rglob("*")) if path.is_file() and not path.name.endswith(".part")]
    references = []
    if not dry_run:
        references = storage.publish_tree(source)
    return {
        "dry_run": dry_run,
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "uploaded": len(references),
    }


def rewrite_mysql_artifact_references(
    task_store: MySQLTaskStore,
    platform_store: MySQLPlatformStore,
    storage: MinioArtifactStorage,
) -> dict[str, int]:
    """Convert migrated absolute local paths to stable MinIO references."""
    changes = {"runs": 0, "report_jobs": 0, "stress_jobs": 0}

    def reference(value: Any) -> str:
        text = str(value or "").strip()
        if not text or text.startswith("minio://"):
            return text
        return storage.reference(text)

    with task_store._connection() as connection:
        rows = connection.execute("SELECT id, run_dir FROM runs WHERE run_dir IS NOT NULL AND run_dir<>''").fetchall()
        for row in rows:
            connection.execute("UPDATE runs SET run_dir=? WHERE id=?", (reference(row["run_dir"]), row["id"]))
            changes["runs"] += 1
    with platform_store._connection() as connection:
        rows = connection.execute(
            """SELECT id, artifact_dir, docx_path, pdf_path FROM report_jobs
               WHERE artifact_dir IS NOT NULL AND artifact_dir<>''"""
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE report_jobs SET artifact_dir=?, docx_path=?, pdf_path=? WHERE id=?",
                (reference(row["artifact_dir"]), reference(row["docx_path"]), reference(row["pdf_path"]), row["id"]),
            )
            changes["report_jobs"] += 1
        rows = connection.execute(
            """SELECT id, artifact_dir, report_path FROM stress_jobs
               WHERE artifact_dir IS NOT NULL AND artifact_dir<>''"""
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE stress_jobs SET artifact_dir=?, report_path=? WHERE id=?",
                (reference(row["artifact_dir"]), reference(row["report_path"]), row["id"]),
            )
            changes["stress_jobs"] += 1
    return changes


def restore_minio_to_local(
    storage: MinioArtifactStorage,
    destination: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    destination = Path(destination).resolve()
    prefix = storage.prefix + "/" if storage.prefix else ""
    objects = list(storage.client.list_objects(storage.bucket, prefix=prefix, recursive=True))
    restored = 0
    skipped = 0
    for item in objects:
        key = str(item.object_name)
        relative = key[len(prefix):] if prefix else key
        target = (destination / Path(*relative.split("/"))).resolve()
        target.relative_to(destination)
        if target.exists() and not overwrite:
            skipped += 1
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            storage.client.fget_object(storage.bucket, key, str(partial))
            partial.replace(target)
        restored += 1
    return {"dry_run": dry_run, "objects": len(objects), "restored": restored, "skipped": skipped}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="蓝鲨平台MySQL/MinIO迁移工具（默认只预检）")
    parser.add_argument(
        "action",
        choices=("backup", "restore-sqlite", "sqlite-to-mysql", "local-to-minio", "minio-to-local"),
    )
    parser.add_argument("--execute", action="store_true", help="执行写入；未指定时仅dry-run")
    parser.add_argument("--output", default="", help="备份或恢复目标目录")
    parser.add_argument("--overwrite", action="store_true", help="恢复对象时允许覆盖已有文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = _settings()
    if args.action == "backup":
        output = args.output or str(PROJECT_ROOT / "backups" / time.strftime("%Y%m%d_%H%M%S"))
        result = backup_sqlite_databases(output)
    elif args.action == "restore-sqlite":
        if not args.output:
            raise ValueError("restore-sqlite必须使用--output指定备份目录")
        result = restore_sqlite_backup(
            args.output, overwrite=args.overwrite, dry_run=not args.execute
        )
    elif args.action == "sqlite-to-mysql":
        mysql_settings = dict(settings)
        mysql_settings["backend"] = "mysql"
        backup = None
        if args.execute:
            backup_dir = PROJECT_ROOT / "backups" / time.strftime("before_mysql_%Y%m%d_%H%M%S")
            backup = backup_sqlite_databases(backup_dir)
        result = migrate_sqlite_to_mysql(
            MySQLTaskStore(mysql_settings, initialize_schema=args.execute),
            MySQLPlatformStore(
                mysql_settings,
                initialize_schema=args.execute,
                recover_jobs=False,
            ),
            dry_run=not args.execute,
        )
        if backup:
            result["sqlite_backup"] = backup
    else:
        storage = create_artifact_storage(
            PROJECT_ROOT,
            {"create_bucket": bool(args.execute)},
        )
        if not isinstance(storage, MinioArtifactStorage):
            raise RuntimeError("该操作要求 artifact_storage.backend=minio")
        if args.action == "local-to-minio":
            result = migrate_local_to_minio(storage, dry_run=not args.execute)
            if args.execute and settings.get("backend") == "mysql":
                result["database_references"] = rewrite_mysql_artifact_references(
                    MySQLTaskStore(settings, initialize_schema=False),
                    MySQLPlatformStore(
                        settings,
                        initialize_schema=False,
                        recover_jobs=False,
                    ),
                    storage,
                )
        else:
            output = args.output or str(PROJECT_ROOT / "artifacts_restored")
            result = restore_minio_to_local(
                storage, output, overwrite=args.overwrite, dry_run=not args.execute
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
