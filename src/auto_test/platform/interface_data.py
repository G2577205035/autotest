"""Dataset validation and transactional scheduling for interface scenarios."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
import uuid
from typing import Any


SENSITIVE_PARAMETER = re.compile(
    r"(?:pass(?:word)?|secret|token|cookie|authorization|api[-_.]?key|credential)", re.I
)


def normalize_parameters(parameters: dict) -> dict[str, str]:
    if not isinstance(parameters, dict) or len(parameters) > 100:
        raise ValueError("用例参数必须是最多 100 项的 JSON 对象")
    result = {}
    for key, value in parameters.items():
        key = str(key or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", key):
            raise ValueError("用例参数名不合法")
        if SENSITIVE_PARAMETER.search(key):
            raise ValueError("敏感参数请改用环境与变量中的加密密钥")
        def inspect_nested(item):
            if isinstance(item, dict):
                if any(SENSITIVE_PARAMETER.search(str(nested_key)) for nested_key in item):
                    raise ValueError("敏感参数请改用环境与变量中的加密密钥")
                for nested_value in item.values():
                    inspect_nested(nested_value)
            elif isinstance(item, list):
                for nested_value in item:
                    inspect_nested(nested_value)
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("用例参数不支持非有限数值")
        inspect_nested(value)
        value = json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else "" if value is None else str(value)
        if len(value) > 20_000:
            raise ValueError("单个用例参数超过 20000 字符")
        result[key] = value
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 100_000:
        raise ValueError("用例参数总大小不能超过 100 KB")
    return result


def parse_dataset(content: str, format_name: str) -> list[dict[str, str]]:
    if len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("数据集不能超过 1 MB")
    if format_name == "csv":
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
        fields = reader.fieldnames or []
        if not fields or len(set(fields)) != len(fields):
            raise ValueError("CSV 需要不重复的参数名表头")
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise ValueError("CSV 每行列数必须与表头一致")
    elif format_name == "json":
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("JSON 包含重复参数名")
                result[key] = value
            return result
        try:
            rows = json.loads(content, object_pairs_hook=unique)
        except (ValueError, RecursionError) as exc:
            raise ValueError("数据集 JSON 格式无效或参数名重复") from exc
    else:
        raise ValueError("数据集格式必须为 json 或 csv")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("数据集必须包含 1～100 行参数对象")
    return [normalize_parameters(row) for row in rows]


class InterfaceDataStoreMixin:
    def initialize_interface_data(self, connection) -> None:
        for sql in (
            """CREATE TABLE IF NOT EXISTS platform_dispatch_locks (
               name VARCHAR(64) PRIMARY KEY, touched_at DOUBLE NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS interface_datasets (
               id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
               scenario_id VARCHAR(64) NOT NULL, name VARCHAR(120) NOT NULL,
               rows_json LONGTEXT NOT NULL, created_by VARCHAR(64) NOT NULL,
               updated_at DOUBLE NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS interface_schedules (
               id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
               scenario_id VARCHAR(64) NOT NULL, dataset_id VARCHAR(64) NOT NULL,
               interval_seconds INTEGER NOT NULL, concurrency INTEGER NOT NULL,
               enabled INTEGER NOT NULL, next_run_at DOUBLE NOT NULL,
               last_batch_id VARCHAR(64) NOT NULL, last_error VARCHAR(200) NOT NULL,
               created_by VARCHAR(64) NOT NULL, updated_at DOUBLE NOT NULL)""",
        ):
            connection.execute(sql)
        connection.execute("INSERT OR IGNORE INTO platform_dispatch_locks(name,touched_at) VALUES('interface',0)")

    @staticmethod
    def lock_interface_dispatch(connection) -> None:
        # A real database row lock serializes claim/count/insert across processes.
        connection.execute("UPDATE platform_dispatch_locks SET touched_at=? WHERE name='interface'", (time.time(),))

    def list_interface_datasets(self, project_id: str, scenario_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM interface_datasets WHERE project_id=? AND scenario_id=? ORDER BY updated_at DESC", (project_id, scenario_id)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["rows"] = json.loads(item.pop("rows_json"))
            item["row_count"] = len(item["rows"])
            items.append(item)
        return items

    def save_interface_dataset(self, project_id: str, scenario_id: str, name: str, content: str, format_name: str, actor: str) -> dict:
        rows = parse_dataset(content, format_name)
        name = str(name).strip()
        if not name or len(name) > 120:
            raise ValueError("数据集名称需为 1～120 个字符")
        identifier = uuid.uuid4().hex
        with self._connection() as connection:
            self.lock_interface_dispatch(connection)
            if not connection.execute("SELECT id FROM interface_scenarios WHERE project_id=? AND id=?", (project_id, scenario_id)).fetchone():
                raise KeyError(scenario_id)
            count = connection.execute("SELECT COUNT(*) AS n FROM interface_datasets WHERE project_id=? AND scenario_id=?", (project_id, scenario_id)).fetchone()["n"]
            if count >= 20:
                raise ValueError("每个场景最多保留 20 个数据集，请清理旧数据集")
            connection.execute("INSERT INTO interface_datasets(id,project_id,scenario_id,name,rows_json,created_by,updated_at) VALUES(?,?,?,?,?,?,?)", (identifier, project_id, scenario_id, name, json.dumps(rows, ensure_ascii=False), actor, time.time()))
        return {"id": identifier, "name": name, "rows": rows, "row_count": len(rows)}

    def delete_interface_dataset(self, project_id: str, dataset_id: str) -> bool:
        with self._connection() as connection:
            self.lock_interface_dispatch(connection)
            if connection.execute("SELECT id FROM interface_schedules WHERE project_id=? AND dataset_id=? LIMIT 1", (project_id, dataset_id)).fetchone():
                raise ValueError("该数据集仍被定时计划引用，请先删除计划")
            return bool(connection.execute("DELETE FROM interface_datasets WHERE project_id=? AND id=?", (project_id, dataset_id)).rowcount)

    def _data_batch(self, connection, project_id: str, scenario_id: str, dataset_id: str, actor: str, concurrency: int) -> dict:
        row = connection.execute(self._interface_scenario_query() + " WHERE s.project_id=? AND s.id=?", (project_id, scenario_id)).fetchone()
        scenario = self._decode_interface_scenario(row)
        if not scenario:
            raise KeyError(scenario_id)
        parameters = [{}]
        if dataset_id:
            dataset = connection.execute("SELECT rows_json FROM interface_datasets WHERE project_id=? AND scenario_id=? AND id=?", (project_id, scenario_id, dataset_id)).fetchone()
            if not dataset:
                raise KeyError(dataset_id)
            parameters = [normalize_parameters(item) for item in json.loads(dataset["rows_json"])]
        batch_id = uuid.uuid4().hex
        run_ids = []
        now = time.time()
        for index, values in enumerate(parameters, 1):
            run_id = uuid.uuid4().hex
            snapshot = dict(scenario)
            if dataset_id:
                snapshot["name"] = f"{scenario['name']} · 数据行 {index}"
            summary = {"total_steps": scenario["step_count"], "passed_steps": 0, "failed_steps": 0, "skipped_steps": 0}
            connection.execute("""INSERT INTO interface_scenario_runs(
                id,project_id,scenario_id,scenario_name,environment_id,batch_id,concurrency_limit,status,
                scenario_json,parameters_json,summary_json,result_json,artifact_dir,docx_path,pdf_path,
                created_by,stop_requested,created_at,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, project_id, scenario_id, snapshot["name"], scenario.get("environment_id") or "", batch_id, concurrency, "queued",
                 json.dumps(snapshot, ensure_ascii=False), json.dumps(values, ensure_ascii=False), json.dumps(summary), "{}", "", "", "", actor, 0, now, None))
            run_ids.append(run_id)
        return {"batch_id": batch_id, "run_ids": run_ids, "queued": len(run_ids)}

    def enqueue_interface_dataset(self, project_id: str, scenario_id: str, dataset_id: str, actor: str, concurrency: int = 3) -> dict:
        if not 1 <= concurrency <= 5 or not dataset_id:
            raise ValueError("请选择数据集，并将并发设为 1～5")
        with self._connection() as connection:
            self.lock_interface_dispatch(connection)
            return self._data_batch(connection, project_id, scenario_id, dataset_id, actor, concurrency)

    def list_interface_schedules(self, project_id: str, scenario_id: str) -> list[dict]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM interface_schedules WHERE project_id=? AND scenario_id=? ORDER BY updated_at DESC", (project_id, scenario_id)).fetchall()]

    def save_interface_schedule(self, project_id: str, scenario_id: str, dataset_id: str, interval_seconds: int, concurrency: int, actor: str) -> dict:
        if not 60 <= interval_seconds <= 2_592_000 or not 1 <= concurrency <= 5:
            raise ValueError("执行间隔需为 60 秒～30 天，并发需为 1～5")
        now = time.time()
        item = dict(id=uuid.uuid4().hex, project_id=project_id, scenario_id=scenario_id, dataset_id=dataset_id, interval_seconds=interval_seconds, concurrency=concurrency, enabled=1, next_run_at=now + interval_seconds, last_batch_id="", last_error="", created_by=actor, updated_at=now)
        with self._connection() as connection:
            self.lock_interface_dispatch(connection)
            if not connection.execute("SELECT id FROM interface_scenarios WHERE id=? AND project_id=?", (scenario_id, project_id)).fetchone():
                raise KeyError(scenario_id)
            if dataset_id and not connection.execute("SELECT id FROM interface_datasets WHERE id=? AND project_id=? AND scenario_id=?", (dataset_id, project_id, scenario_id)).fetchone():
                raise KeyError(dataset_id)
            if connection.execute("SELECT COUNT(*) AS n FROM interface_schedules WHERE project_id=? AND scenario_id=?", (project_id, scenario_id)).fetchone()["n"] >= 10:
                raise ValueError("每个场景最多保留 10 个定时计划")
            connection.execute("INSERT INTO interface_schedules(" + ",".join(item) + ") VALUES(" + ",".join("?" for _ in item) + ")", tuple(item.values()))
        return item

    def change_interface_schedule(self, project_id: str, identifier: str, enabled: bool | None, actor: str) -> bool:
        with self._connection() as connection:
            self.lock_interface_dispatch(connection)
            row = connection.execute("SELECT interval_seconds FROM interface_schedules WHERE project_id=? AND id=?", (project_id, identifier)).fetchone()
            if not row:
                return False
            if enabled is None:
                connection.execute("DELETE FROM interface_schedules WHERE project_id=? AND id=?", (project_id, identifier))
            else:
                now = time.time()
                connection.execute("UPDATE interface_schedules SET enabled=?,next_run_at=?,created_by=?,last_error='',updated_at=? WHERE project_id=? AND id=?", (int(enabled), now + row["interval_seconds"], actor, now, project_id, identifier))
            return True

    def dispatch_interface_schedule(self, *, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self._connection() as connection:
            self.lock_interface_dispatch(connection)
            row = connection.execute("SELECT * FROM interface_schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at,id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            item = dict(row)
            actor = connection.execute("""SELECT u.is_active,u.is_superuser,p.is_active AS project_active,m.role
                FROM users u JOIN projects p ON p.id=? LEFT JOIN project_memberships m ON m.user_id=u.id AND m.project_id=p.id
                WHERE u.id=?""", (item["project_id"], item["created_by"])).fetchone()
            permitted = actor and actor["is_active"] and actor["project_active"] and (actor["is_superuser"] or actor["role"] in {"tester", "project_admin"})
            if not permitted:
                connection.execute("UPDATE interface_schedules SET enabled=0,last_error=?,updated_at=? WHERE id=?", ("创建者已停用或失去执行权限，计划已暂停", now, item["id"]))
                return {"paused": item["id"]}
            active = connection.execute("SELECT id FROM interface_scenario_runs WHERE project_id=? AND batch_id=? AND status IN ('queued','running') LIMIT 1", (item["project_id"], item["last_batch_id"])).fetchone() if item["last_batch_id"] else None
            result = {"skipped": item["id"]}
            if not active:
                try:
                    result = self._data_batch(connection, item["project_id"], item["scenario_id"], item["dataset_id"], item["created_by"], item["concurrency"])
                except KeyError:
                    connection.execute("UPDATE interface_schedules SET enabled=0,last_error=?,updated_at=? WHERE id=?", ("场景或数据集已不存在，计划已暂停", now, item["id"]))
                    return {"paused": item["id"]}
            # Skip missed intervals after downtime; never burst-replay old runs.
            next_run = item["next_run_at"] + (math.floor((now - item["next_run_at"]) / item["interval_seconds"]) + 1) * item["interval_seconds"]
            connection.execute("UPDATE interface_schedules SET next_run_at=?,last_batch_id=?,last_error=?,updated_at=? WHERE id=?", (next_run, result.get("batch_id", item["last_batch_id"]), "上轮未结束，本次跳过" if active else "", now, item["id"]))
            return result

    def interface_scenario_trend(self, project_id: str, scenario_id: str, limit: int = 50) -> dict:
        with self._connection() as connection:
            rows = connection.execute("SELECT id,scenario_name,status,summary_json,created_at FROM interface_scenario_runs WHERE project_id=? AND scenario_id=? AND status IN ('succeeded','failed','interrupted') ORDER BY created_at DESC,id DESC LIMIT ?", (project_id, scenario_id, max(1, min(limit, 200)))).fetchall()
        points = []
        for row in reversed(rows):
            item = dict(row)
            summary = json.loads(item.pop("summary_json") or "{}")
            elapsed = summary.get("elapsed_ms")
            item["elapsed_ms"] = elapsed if isinstance(elapsed, (float, int)) and math.isfinite(elapsed) and elapsed >= 0 else None
            points.append(item)
        succeeded = sum(item["status"] == "succeeded" for item in points)
        durations = sorted(item["elapsed_ms"] for item in points if item["elapsed_ms"] is not None)
        return {"points": points, "total": len(points), "succeeded": succeeded, "success_rate": round(succeeded / len(points) * 100, 2) if points else None, "p95_ms": durations[math.ceil(len(durations) * .95) - 1] if durations else None}
