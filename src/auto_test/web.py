"""FastAPI entry point backed by persistent automation tasks.

The current local console opens directly without the retired page API key.
User login, roles and project permissions protect the console and its APIs.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from auto_test.common.config_loader import default_translate_name
from auto_test.common.runtime_secrets import ensure_runtime_master_key
from auto_test.common.paths import (
    PROJECT_ROOT,
    RUNS_DIR,
    UPLOADS_DIR,
    prepare_runtime_layout,
)
from auto_test.core.task_manager import TaskManager
from auto_test.monitoring.translation_speed import summarize_translation_speed
from auto_test.platform.task_store import TaskNotFoundError
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.identity import create_identity_api, install_identity_guard
from auto_test.platform.secrets import SecretEncryptionError, decrypt_secret, encrypt_secret
from auto_test.platform.upload_ownership import UPLOAD_ID_PATTERN, authorize_upload
from auto_test.core.task_queue import create_task_signal_queue, task_execution_mode
from auto_test.monitoring.remote_server import server_execution_mode
from auto_test.platform.worker_rpc import WorkerMailbox


BASE_DIR = PROJECT_ROOT
prepare_runtime_layout(BASE_DIR)
ensure_runtime_master_key()
task_signal_queue = create_task_signal_queue()
execution_mode = task_execution_mode()
manager = TaskManager.default(BASE_DIR, signal_queue=task_signal_queue)
store = manager.store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if execution_mode == "embedded":
        manager.start()
        report_manager.start()
        interface_scenario_manager.start()
        model_evaluation_manager.start()
    stress_manager.start()
    try:
        yield
    finally:
        stress_manager.stop()
        if execution_mode == "embedded":
            model_evaluation_manager.stop()
            interface_scenario_manager.stop()
            report_manager.stop()
            manager.stop()
        else:
            close_store = getattr(manager.store, "close", None)
            if callable(close_store):
                close_store()
        task_signal_queue.close()


app = FastAPI(title="烈马自动化测试平台", version="2.0", lifespan=lifespan)


api = APIRouter(prefix="/api")


class RunRequest(BaseModel):
    username: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=200)
    administrator_username: str = Field(default="", max_length=100)
    administrator_password: str = Field(default="", max_length=200)
    upload_id: str = Field(default="", max_length=100)
    upload_path: str = Field(default="", max_length=1000)
    server_profile_id: str = Field(default="", max_length=64)
    host: str = Field(default="", max_length=300)
    ssh_user: str = Field(default="", max_length=100)
    ssh_password: str = Field(default="", max_length=300)
    gpu_server_profile_id: str = Field(default="", max_length=64)
    gpu_host: str = Field(default="", max_length=300)
    gpu_ssh_user: str = Field(default="", max_length=100)
    gpu_ssh_password: str = Field(default="", max_length=300)
    case_name: str = Field(default="", max_length=200)
    translate_name: str = Field(default="", max_length=100)
    analysis: list[str] | str = Field(default_factory=list)
    monitor_modules: list[str] | str = Field(default_factory=list)
    monitor_enable_perf: bool | None = None
    monitor_enable_log: bool | None = None
    upload_mode: str = Field(default="", max_length=20)
    upload_size_limit_mb: int | None = Field(default=None, ge=1, le=1024 * 1024)
    export_enable: bool | None = None
    export_types: list[str] | str = Field(default_factory=list)
    ai_checks_enable: bool | None = None
    ai_simple_translation_enable: bool | None = None
    ai_xiaoyi_translation_enable: bool | None = None
    ai_xiaoyi_summary_enable: bool | None = None
    ai_sample_path: str = Field(default="", max_length=1000)
    ai_max_file_ids: int | None = Field(default=None, ge=1, le=1000)
    stress_enable: bool | None = None
    stress_workers: int | None = Field(default=None, ge=0, le=4096)
    stress_cpu_load: int | None = Field(default=None, ge=1, le=100)
    stress_duration: int | None = Field(default=None, ge=10, le=86400)


from auto_test.platform.api import create_platform_api
platform_repository = create_platform_repository(
    BASE_DIR, recover_jobs=execution_mode == "embedded"
)
platform_api, report_manager, platform_store, stress_manager = create_platform_api(
    store,
    platform_store=platform_repository,
    signal_queue=task_signal_queue,
)
interface_scenario_manager = report_manager.interface_scenario_manager
model_evaluation_manager = report_manager.model_evaluation_manager
identity_api, identity_service = create_identity_api(platform_store)
install_identity_guard(app, identity_service)
from auto_test.ui_automation.recorder_api import register_recorder_gateway
register_recorder_gateway(app, platform_store, identity_service)


def _identity(request: Request | None) -> dict:
    if request is None:
        return {}
    return getattr(request.state, "identity", {}) or {}


def _legacy_visible(request: Request) -> bool:
    context = _identity(request)
    if not bool((context.get("user") or {}).get("is_superuser")):
        return False
    current_project_id = str((context.get("current_project") or {}).get("id") or "")
    legacy_project_id = str(context.get("legacy_project_id") or "")
    return bool(current_project_id and (not legacy_project_id or current_project_id == legacy_project_id))


def _run_visible(run: dict, request: Request) -> bool:
    context = _identity(request)
    current_project_id = str((context.get("current_project") or {}).get("id") or "")
    run_project_id = str((run.get("metadata") or {}).get("project_id") or "")
    if run_project_id:
        return bool(current_project_id and run_project_id == current_project_id)
    return _legacy_visible(request)


def _latest_visible_run(request: Request):
    """Newest run visible to the caller, re-fetched with full fields.

    ``list_runs`` intentionally omits heavy columns (run_dir/error) so the
    polling list stays light; status endpoints that need those fields fetch
    the complete record here.
    """
    for run in store.list_runs(500):
        if _run_visible(run, request):
            return store.get_run(run["id"])
    return None


def _get_run_or_404(run_id: str, request: Request | None = None):
    try:
        run = store.get_run(run_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    if request is not None and not _run_visible(run, request):
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _authorize_run_artifact_identifier(identifier: str, request: Request) -> None:
    """Authorize project runs and superuser-only unscoped legacy artifacts."""

    if re.fullmatch(r"\d{8}_\d{6}", identifier):
        if not _legacy_visible(request):
            raise HTTPException(status_code=404, detail="run not found")
        return
    _get_run_or_404(identifier, request)


def _resolve_run_dir(identifier: str) -> Path:
    """Resolve a task id, with a safe timestamp fallback for legacy reports."""
    try:
        run = store.get_run(identifier)
        if not run.get("run_dir"):
            raise HTTPException(status_code=409, detail="run has no report directory yet")
        path = manager.artifact_storage.materialize_tree(run["run_dir"])
    except TaskNotFoundError:
        if not re.fullmatch(r"\d{8}_\d{6}", identifier):
            raise HTTPException(status_code=404, detail="run not found")
        path = (RUNS_DIR / identifier).resolve()

    try:
        path.relative_to(manager.artifact_storage.resolve("runs"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid run directory") from exc
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="run directory not found")
    return path


def _report_file(run_dir: Path) -> Path | None:
    preferred = run_dir / "translate" / "report" / "run_report.txt"
    if preferred.is_file():
        return preferred
    return next(iter(sorted(run_dir.glob("**/report/run_report.txt"))), None)


@app.get("/health")
async def health():
    queue_status = await run_in_threadpool(task_signal_queue.status)
    if execution_mode == "embedded":
        queue_status["worker_available"] = True
    return {
        "status": "ok",
        "active_run_id": manager.active_run_id or None,
        "database_backend": getattr(store, "backend", "unknown"),
        "artifact_backend": getattr(manager.artifact_storage, "backend", "unknown"),
        "task_queue_backend": task_signal_queue.backend,
        "task_execution_mode": execution_mode,
        "task_queue": queue_status,
        "server_execution_mode": server_execution_mode(),
        "server_worker": {"available": True} if server_execution_mode() == "embedded" else {"available": (await run_in_threadpool(WorkerMailbox(platform_store).status))["available"]},
    }


@api.post("/run", status_code=202)
async def run_test(payload: RunRequest | None = None, request: Request = None):
    # The whole submission pipeline is synchronous (config, secrets, profile
    # lookups and the persisted queue insert), so it runs in the threadpool
    # to keep the event loop responsive for live polling endpoints.
    return await run_in_threadpool(_submit_run, payload or RunRequest(), request)


def _submit_run(payload: RunRequest, request: Request | None) -> dict:
    options = payload.model_dump(exclude_none=True)
    secret_options = {
        key: str(options.pop(key, "") or "")
        for key in (
            "password",
            "ssh_password",
            "gpu_ssh_password",
            "administrator_username",
            "administrator_password",
        )
    }
    if not str(options.get("username") or "").strip():
        raise HTTPException(status_code=400, detail="请填写业务系统登录用户名")
    if not secret_options["password"]:
        raise HTTPException(status_code=400, detail="请填写业务系统登录密码")
    if bool(secret_options["administrator_username"]) != bool(
        secret_options["administrator_password"]
    ):
        raise HTTPException(status_code=400, detail="业务管理员账号和密码必须同时填写")

    def apply_server_profile(
        profile_key: str,
        host_key: str,
        user_key: str,
        password_key: str,
    ) -> None:
        profile_id = str(options.get(profile_key) or "").strip()
        if not profile_id:
            return
        profile = platform_store.get_server_profile(profile_id, include_secret=True)
        if not profile:
            raise HTTPException(status_code=404, detail="选择的服务器资产不存在")
        options[host_key] = str(profile.get("host") or "").strip()
        options[user_key] = str(profile.get("user") or "").strip()
        if not secret_options[password_key] and profile.get("credential_enc"):
            try:
                secret_options[password_key] = decrypt_secret(
                    str(profile["credential_enc"])
                )
            except SecretEncryptionError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

    apply_server_profile("server_profile_id", "host", "ssh_user", "ssh_password")
    apply_server_profile(
        "gpu_server_profile_id",
        "gpu_host",
        "gpu_ssh_user",
        "gpu_ssh_password",
    )
    if options.get("gpu_host") and not options.get("gpu_ssh_user"):
        options["gpu_ssh_user"] = options.get("ssh_user", "")
    if not options.get("gpu_host") or options.get("gpu_host") == options.get("host"):
        options.pop("gpu_server_profile_id", None)
        options["gpu_host"] = ""
        options["gpu_ssh_user"] = ""
        secret_options["gpu_ssh_password"] = ""
    elif not secret_options["gpu_ssh_password"]:
        secret_options["gpu_ssh_password"] = secret_options["ssh_password"]

    options["translate_name"] = str(
        options.get("translate_name") or default_translate_name()
    ).strip()
    if not options["translate_name"]:
        raise HTTPException(
            status_code=400,
            detail="请填写翻译语种，或在 config.local.yml 的 defaults.translate_name 中配置默认值",
        )
    upload_id = options.pop("upload_id", "")
    if upload_id:
        if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise HTTPException(status_code=400, detail="invalid upload id")
        context = _identity(request)
        try:
            authorize_upload(
                UPLOADS_DIR,
                upload_id,
                project_id=str((context.get("current_project") or {}).get("id") or ""),
                is_superuser=_legacy_visible(request),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail="uploaded files not found") from exc
        upload_path = (UPLOADS_DIR / upload_id).resolve()
        try:
            upload_path.relative_to(UPLOADS_DIR)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid upload path") from exc
        if not upload_path.is_dir():
            raise HTTPException(status_code=404, detail="uploaded files not found")
        options["upload_path"] = str(upload_path)
        options["upload_id"] = upload_id
    elif str(options.get("upload_path") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="测试数据不能引用平台服务器本地路径，请从客户电脑选择测试文件夹上传",
        )
    elif options.get("upload_mode") == "server":
        raise HTTPException(
            status_code=400,
            detail="服务器直传模式需要先从客户电脑选择测试文件夹并完成上传",
        )
    options = {key: value for key, value in options.items() if value not in ("", [], None)}
    try:
        secret_enc = encrypt_secret(
            json.dumps(
                {key: value for key, value in secret_options.items() if value},
                ensure_ascii=False,
            )
        )
    except SecretEncryptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    context = _identity(request)
    task = manager.submit(
        {
            "source": "web",
            "project_id": (context.get("current_project") or {}).get("id", ""),
            "created_by_user_id": (context.get("user") or {}).get("id", ""),
            "options": options,
        },
        secret_enc=secret_enc,
    )
    return {"status": task["status"], "run_id": task["id"], "task": task}


@api.get("/runs")
async def list_runs(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    runs = await run_in_threadpool(_list_visible_runs, request, limit)
    active_run_id = manager.active_run_id or None
    if active_run_id and not any(run["id"] == active_run_id for run in runs):
        active_run_id = None
    return {"runs": runs, "active_run_id": active_run_id}


def _list_visible_runs(request: Request, limit: int) -> list[dict]:
    return [run for run in store.list_runs(500) if _run_visible(run, request)][:limit]


@api.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    run = await run_in_threadpool(_get_run_or_404, run_id, request)
    events = await run_in_threadpool(store.get_translation_progress_events, run_id)
    run["translation_speed"] = summarize_translation_speed(events)
    return run


@api.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    await run_in_threadpool(_get_run_or_404, run_id, request)
    return await run_in_threadpool(manager.stop_run, run_id)


@api.post("/runs/{run_id}/restart", status_code=202)
async def restart_run(run_id: str, request: Request):
    await run_in_threadpool(_get_run_or_404, run_id, request)
    raise HTTPException(
        status_code=409,
        detail="任务凭据不会写入历史，请在页面重新配置后提交",
    )


@api.post("/runs/{run_id}/execute", status_code=202)
async def execute_run(run_id: str, request: Request):
    run = await run_in_threadpool(_get_run_or_404, run_id, request)
    if run.get("status") != "queued":
        raise HTTPException(
            status_code=409,
            detail="历史任务不能沿用旧凭据执行，请在页面重新配置后提交",
        )
    return await run_in_threadpool(manager.execute_run, run_id)


@api.get("/runs/{run_id}/logs")
async def get_run_logs(
    run_id: str,
    request: Request,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    run = await run_in_threadpool(_get_run_or_404, run_id, request)
    events = await run_in_threadpool(store.get_events, run_id, after_id, limit)
    return {
        "events": events,
        "last_id": events[-1]["id"] if events else after_id,
        "status": run["status"],
        "stage": run["stage"],
        "progress": run["progress"],
    }


@api.get("/status")
async def get_status(request: Request, run_id: str | None = None):
    run = (
        await run_in_threadpool(_get_run_or_404, run_id, request)
        if run_id
        else await run_in_threadpool(_latest_visible_run, request)
    )
    if not run:
        return {"running": False, "done": False, "run": None}
    return {
        "running": run["status"] == "running",
        "done": run["status"] in {"succeeded", "failed", "interrupted"},
        "run_dir": run["run_dir"],
        "run_id": run["id"],
        "status": run["status"],
        "stage": run["stage"],
        "progress": run["progress"],
        "message": run["message"],
        "error": run["error"],
    }


@api.get("/logs")
async def get_logs(
    request: Request,
    since: int = Query(default=0, ge=0),
    run_id: str | None = None,
):
    run = (
        await run_in_threadpool(_get_run_or_404, run_id, request)
        if run_id
        else await run_in_threadpool(_latest_visible_run, request)
    )
    if not run:
        return {"lines": [], "count": since, "running": False, "done": False}
    events = await run_in_threadpool(store.get_events, run["id"], since)
    lines = [
        {
            "id": event["id"], "time": event["created_at"],
            "level": event["level"], "msg": event["message"],
            "stage": event["stage"], "progress": event["progress"],
        }
        for event in events
    ]
    return {
        "lines": lines,
        "count": events[-1]["id"] if events else since,
        "running": run["status"] == "running",
        "done": run["status"] in {"succeeded", "failed", "interrupted"},
        "run_id": run["id"],
    }


@api.get("/report/{identifier}")
async def get_report(identifier: str, request: Request):
    await run_in_threadpool(_authorize_run_artifact_identifier, identifier, request)
    report = _report_file(await run_in_threadpool(_resolve_run_dir, identifier))
    return {
        "content": report.read_text(encoding="utf-8") if report else "",
        "run_id": identifier,
    }


@api.get("/charts/{identifier}/{folder}")
async def list_charts(identifier: str, folder: str, request: Request):
    await run_in_threadpool(_authorize_run_artifact_identifier, identifier, request)
    if folder not in {"stress", "translate"}:
        raise HTTPException(status_code=400, detail="invalid chart folder")
    chart_dir = await run_in_threadpool(_resolve_run_dir, identifier)
    chart_dir = chart_dir / folder / "charts"
    if not chart_dir.is_dir():
        return {"charts": []}
    charts = [
        f"/api/chart/{identifier}/{folder}/{item.name}"
        for item in sorted(chart_dir.iterdir())
        if item.is_file() and item.suffix.lower() == ".png"
    ]
    return {"charts": charts}


@api.get("/chart/{identifier}/{folder}/{name}")
async def get_chart(identifier: str, folder: str, name: str, request: Request):
    await run_in_threadpool(_authorize_run_artifact_identifier, identifier, request)
    if folder not in {"stress", "translate"} or Path(name).name != name:
        raise HTTPException(status_code=400, detail="invalid chart path")
    path = (await run_in_threadpool(_resolve_run_dir, identifier)) / folder / "charts" / name
    if not path.is_file() or path.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="chart not found")
    return FileResponse(str(path), media_type="image/png")


@api.get("/dirs")
async def list_dirs(request: Request):
    if not _legacy_visible(request):
        return []

    def _scan_dirs() -> list[dict]:
        result = []
        if RUNS_DIR.is_dir():
            for directory in sorted(RUNS_DIR.iterdir(), reverse=True):
                if directory.is_dir() and re.fullmatch(r"\d{8}_\d{6}", directory.name):
                    result.append({
                        "ts": directory.name,
                        "stress": (directory / "stress" / "report").exists(),
                        "translate": (directory / "translate" / "report").exists(),
                    })
        return result

    return await run_in_threadpool(_scan_dirs)


app.include_router(api)
app.include_router(platform_api)
app.include_router(identity_api)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(str(index_file), headers={"Cache-Control": "no-store"})
    return HTMLResponse(
        "<h1>烈马自动化测试平台 API</h1>"
        "<p>任务服务已启动。访问 <a href='/docs'>/docs</a> 查看接口文档。</p>"
    )


def main():
    """Start the local single-worker Web server."""
    import uvicorn

    print("=" * 72)
    print("烈马自动化测试平台：http://127.0.0.1:8080")
    print("首次打开需创建平台管理员；后续使用账号密码登录。")
    print("访问权限由平台管理员、项目管理员、测试执行者和只读成员控制。")
    print("=" * 72)
    uvicorn.run(app, host="127.0.0.1", port=8080, workers=1)


if __name__ == "__main__":
    main()
