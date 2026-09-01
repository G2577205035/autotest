"""Database-agnostic persistence mixin for model-evaluation assets and runs."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from auto_test.evaluation.contracts import assert_secret_free


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ModelEvaluationStoreMixin:
    """Methods shared by SQLite ``PlatformStore`` and ``MySQLPlatformStore``."""

    @staticmethod
    def _decode_model_eval_suite(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["project_id"] = str(item.get("project_id") or "")
        item["case_count"] = int(item.get("case_count") or 0)
        item["version_count"] = int(item.get("version_count") or 0)
        return item

    @staticmethod
    def _decode_model_eval_version(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target, fallback in (
            ("manifest_json", "manifest", {}),
            ("upstream_json", "upstream", {}),
        ):
            try:
                item[target] = json.loads(item.pop(source) or json.dumps(fallback))
            except (TypeError, ValueError):
                item[target] = fallback
        item["version"] = int(item.get("version") or 0)
        item["case_count"] = int(item.get("case_count") or 0)
        return item

    @staticmethod
    def _decode_model_eval_run(row, *, include_snapshot: bool = True) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target in (
            ("snapshot_json", "snapshot"),
            ("summary_json", "summary"),
        ):
            if source not in item:
                continue
            try:
                item[target] = json.loads(item.pop(source) or "{}")
            except (TypeError, ValueError):
                item[target] = {}
        if not include_snapshot:
            item.pop("snapshot", None)
        item["stop_requested"] = bool(item.get("stop_requested"))
        item["progress"] = max(0, min(int(item.get("progress") or 0), 100))
        return item

    def save_model_eval_suite(
        self,
        project_id: str | None,
        data: dict[str, Any],
        suite_id: str = "",
        *,
        created_by: str = "",
    ) -> dict[str, Any]:
        source = str(data.get("source") or "project_custom").strip()
        if source not in {"platform_builtin", "evalscope_standard", "project_custom"}:
            raise ValueError("评测测试集来源无效")
        normalized_project = str(project_id or "")
        if source == "project_custom" and not normalized_project:
            raise ValueError("项目自定义测试集必须归属项目")
        if source != "project_custom":
            normalized_project = ""
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("评测测试集名称不能为空")
        status = str(data.get("status") or "draft")
        if status not in {"draft", "published", "archived"}:
            raise ValueError("评测测试集状态无效")
        suite_id = str(suite_id or data.get("id") or uuid.uuid4().hex)
        now = time.time()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT id FROM model_eval_suites WHERE id=?", (suite_id,)
            ).fetchone()
            values = (
                normalized_project or None,
                source,
                name,
                str(data.get("category") or "general"),
                str(data.get("description") or ""),
                status,
                str(created_by or data.get("created_by") or ""),
                now,
            )
            if existing:
                connection.execute(
                    """UPDATE model_eval_suites SET project_id=?,source=?,name=?,category=?,
                       description=?,status=?,updated_at=? WHERE id=?""",
                    (*values[:6], now, suite_id),
                )
            else:
                connection.execute(
                    """INSERT INTO model_eval_suites(
                       id,project_id,source,name,category,description,status,created_by,
                       created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (suite_id, *values[:7], now, now),
                )
        return self.get_model_eval_suite(normalized_project, suite_id, include_global=True) or {}

    def get_model_eval_suite(
        self, project_id: str, suite_id: str, *, include_global: bool = True
    ) -> dict[str, Any] | None:
        scope = "(project_id=? OR project_id IS NULL)" if include_global else "project_id=?"
        with self._connection() as connection:
            row = connection.execute(
                f"""SELECT s.*,
                    (SELECT COUNT(*) FROM model_eval_suite_versions v WHERE v.suite_id=s.id) AS version_count,
                    (SELECT COUNT(*) FROM model_eval_cases c JOIN model_eval_suite_versions v2
                     ON v2.id=c.version_id WHERE v2.suite_id=s.id) AS case_count
                    FROM model_eval_suites s WHERE s.id=? AND {scope}""",
                (suite_id, str(project_id or "")),
            ).fetchone()
        return self._decode_model_eval_suite(row)

    def list_model_eval_suites(
        self, project_id: str, *, include_global: bool = True, limit: int = 200
    ) -> list[dict[str, Any]]:
        scope = "(s.project_id=? OR s.project_id IS NULL)" if include_global else "s.project_id=?"
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT s.*,
                    (SELECT COUNT(*) FROM model_eval_suite_versions v WHERE v.suite_id=s.id) AS version_count,
                    (SELECT COUNT(*) FROM model_eval_cases c JOIN model_eval_suite_versions v2
                     ON v2.id=c.version_id WHERE v2.suite_id=s.id) AS case_count
                    FROM model_eval_suites s WHERE {scope}
                    ORDER BY CASE WHEN s.project_id IS NULL THEN 0 ELSE 1 END,
                             s.updated_at DESC LIMIT ?""",
                (str(project_id or ""), max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._decode_model_eval_suite(row) or {} for row in rows]

    def publish_model_eval_suite_version(
        self,
        project_id: str,
        suite_id: str,
        manifest: dict[str, Any],
        cases: list[dict[str, Any]],
        *,
        upstream: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        suite = self.get_model_eval_suite(project_id, suite_id, include_global=True)
        if not suite:
            raise KeyError(suite_id)
        if suite.get("project_id") and str(suite.get("project_id")) != str(project_id):
            raise KeyError(suite_id)
        assert_secret_free(manifest, path="model_eval_suite.manifest")
        assert_secret_free(cases, path="model_eval_suite.cases")
        content = {"manifest": manifest, "cases": cases}
        content_sha256 = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
        version_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM model_eval_suite_versions WHERE suite_id=?",
                (suite_id,),
            ).fetchone()
            version = int(row["version"] or 0) + 1
            connection.execute(
                """INSERT INTO model_eval_suite_versions(
                   id,suite_id,version,manifest_json,content_sha256,upstream_json,published_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    version_id,
                    suite_id,
                    version,
                    _canonical_json(manifest),
                    content_sha256,
                    _canonical_json(upstream or {}),
                    now,
                ),
            )
            for index, case in enumerate(cases, start=1):
                payload = dict(case.get("payload") or case)
                connection.execute(
                    """INSERT INTO model_eval_cases(
                       id,version_id,case_key,category,tags_json,payload_json,weight,sort_order
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        version_id,
                        str(case.get("case_key") or case.get("id") or f"case-{index}"),
                        str(case.get("category") or suite.get("category") or "general"),
                        _canonical_json(case.get("tags") or []),
                        _canonical_json(payload),
                        max(0.0, float(case.get("weight") or 1.0)),
                        index,
                    ),
                )
            connection.execute(
                "UPDATE model_eval_suites SET status='published',updated_at=? WHERE id=?",
                (now, suite_id),
            )
        return self.get_model_eval_suite_version(project_id, version_id) or {}

    def get_model_eval_suite_version(
        self, project_id: str, version_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT v.*,s.project_id,s.name AS suite_name,
                          (SELECT COUNT(*) FROM model_eval_cases c WHERE c.version_id=v.id) AS case_count
                   FROM model_eval_suite_versions v JOIN model_eval_suites s ON s.id=v.suite_id
                   WHERE v.id=? AND (s.project_id=? OR s.project_id IS NULL)""",
                (version_id, str(project_id or "")),
            ).fetchone()
        return self._decode_model_eval_version(row)

    def create_model_eval_run(
        self,
        project_id: str,
        *,
        model_profile_id: str = "",
        suite_version_id: str = "",
        backend: str = "mock",
        backend_version: str = "",
        snapshot: dict[str, Any] | None = None,
        created_by: str = "",
    ) -> dict[str, Any]:
        snapshot = dict(snapshot or {})
        assert_secret_free(snapshot, path="model_eval_run.snapshot")
        run_id = uuid.uuid4().hex
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO model_eval_runs(
                   id,project_id,model_profile_id,suite_version_id,backend,backend_version,
                   status,phase,progress,message,error,snapshot_json,summary_json,artifact_ref,
                   created_by,stop_requested,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    str(project_id),
                    str(model_profile_id or ""),
                    str(suite_version_id or ""),
                    str(backend or "mock"),
                    str(backend_version or ""),
                    "queued",
                    "queued",
                    0,
                    "评测任务已排队",
                    "",
                    _canonical_json(snapshot),
                    "{}",
                    "",
                    str(created_by or ""),
                    0,
                    now,
                ),
            )
        self.add_model_eval_run_event(
            run_id, "queued", "评测任务已排队", phase="queued", progress=0
        )
        return self.get_model_eval_run(project_id, run_id) or {}

    def claim_model_eval_run(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT id,project_id FROM model_eval_runs
                   WHERE status='queued' ORDER BY created_at ASC LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE model_eval_runs SET status='preparing',phase='preparing',progress=1,
                   message='评测 Worker 已领取',started_at=?,stop_requested=0
                   WHERE id=? AND status='queued'""",
                (time.time(), row["id"]),
            ).rowcount
            if not updated:
                return None
            claimed = connection.execute(
                "SELECT * FROM model_eval_runs WHERE id=?", (row["id"],)
            ).fetchone()
        self.add_model_eval_run_event(
            str(row["id"]), "claimed", "评测 Worker 已领取", phase="preparing", progress=1
        )
        return self._decode_model_eval_run(claimed)

    def update_model_eval_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        progress: int | None = None,
        message: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("phase", phase),
            ("progress", max(0, min(int(progress), 100)) if progress is not None else None),
            ("message", message),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if not assignments:
            return
        with self._connection() as connection:
            connection.execute(
                f"UPDATE model_eval_runs SET {','.join(assignments)} WHERE id=?",
                (*values, run_id),
            )

    def add_model_eval_run_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        phase: str = "",
        progress: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(data or {})
        assert_secret_free(payload, path="model_eval_event.data")
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO model_eval_run_events(
                   run_id,event_type,message,phase,progress,data_json,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    run_id,
                    str(event_type or "progress")[:64],
                    str(message or "")[:2000],
                    str(phase or "")[:64],
                    max(0, min(int(progress), 100)) if progress is not None else None,
                    _canonical_json(payload),
                    time.time(),
                ),
            )

    def request_stop_model_eval_run(
        self, project_id: str, run_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM model_eval_runs WHERE id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "queued":
                connection.execute(
                    """UPDATE model_eval_runs SET status='stopped',phase='stopped',
                       message='排队中的评测已停止',stop_requested=1,finished_at=?
                       WHERE id=? AND project_id=?""",
                    (time.time(), run_id, project_id),
                )
            elif row["status"] not in {"completed", "failed", "stopped"}:
                connection.execute(
                    """UPDATE model_eval_runs SET stop_requested=1,
                       message='正在安全停止评测' WHERE id=? AND project_id=?""",
                    (run_id, project_id),
                )
        self.add_model_eval_run_event(
            run_id, "stop_requested", "用户请求停止评测", phase="stop_requested"
        )
        return self.get_model_eval_run(project_id, run_id)

    def is_model_eval_run_stop_requested(self, run_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT stop_requested FROM model_eval_runs WHERE id=?", (run_id,)
            ).fetchone()
        return bool(row and row["stop_requested"])

    def finish_model_eval_run(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        artifact_ref: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "stopped"}:
            raise ValueError("评测运行结束状态无效")
        summary = dict(summary or {})
        assert_secret_free(summary, path="model_eval_run.summary")
        with self._connection() as connection:
            updated = connection.execute(
                """UPDATE model_eval_runs SET status=?,phase=?,progress=?,message=?,error=?,
                   summary_json=?,artifact_ref=?,finished_at=? WHERE id=? AND project_id=?""",
                (
                    status,
                    status,
                    100 if status == "completed" else 0,
                    {"completed": "评测完成", "failed": "评测失败", "stopped": "评测已停止"}[status],
                    str(error or "")[:2000],
                    _canonical_json(summary),
                    str(artifact_ref or ""),
                    time.time(),
                    run_id,
                    project_id,
                ),
            ).rowcount
            if not updated:
                raise KeyError(run_id)
        self.add_model_eval_run_event(run_id, status, "评测运行进入终态", phase=status)
        return self.get_model_eval_run(project_id, run_id) or {}

    def get_model_eval_run(
        self, project_id: str, run_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_eval_runs WHERE id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()
        return self._decode_model_eval_run(row)

    def list_model_eval_runs(
        self, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id,project_id,model_profile_id,suite_version_id,backend,
                          backend_version,status,phase,progress,message,error,summary_json,
                          artifact_ref,created_by,stop_requested,created_at,started_at,finished_at
                   FROM model_eval_runs WHERE project_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (project_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            self._decode_model_eval_run(row, include_snapshot=False) or {} for row in rows
        ]

    def list_model_eval_run_events(
        self, project_id: str, run_id: str, after_id: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        if not self.get_model_eval_run(project_id, run_id):
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id,event_type,message,phase,progress,data_json,created_at
                   FROM model_eval_run_events WHERE run_id=? AND id>?
                   ORDER BY id ASC LIMIT ?""",
                (run_id, max(0, int(after_id)), max(1, min(int(limit), 5000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json") or "{}")
            except (TypeError, ValueError):
                item["data"] = {}
            result.append(item)
        return result
