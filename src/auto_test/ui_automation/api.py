"""Project-scoped UI suite management, run submission and artifact downloads."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from auto_test.platform.worker_rpc import WorkerMailbox
from auto_test.ui_automation.runner import decrypt_artifact, settings
from auto_test.ui_automation.store import UiStore


class ScriptFile(BaseModel):
    name: str = Field(max_length=100)
    content: str = Field(max_length=500_000)


class UiSuiteInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    files: list[ScriptFile] = Field(min_length=1, max_length=20)
    base_url: str = Field(default='', max_length=2000)
    parameters: dict = Field(default_factory=dict)
    timeout_seconds: int = Field(default=120, ge=10, le=900)


class UiRunInput(BaseModel):
    suite_id: str = Field(min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1)


def register_ui_api(store, artifacts, router=None):
    router = router if router is not None else APIRouter()
    records = UiStore(store)
    from auto_test.ui_automation.recorder_api import register_recording_api
    register_recording_api(router, store)
    def project(request):
        return request.state.identity['current_project']['id']
    def call(function, *args):
        try:
            return function(*args)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, 'UI 套件或版本不存在') from exc
    def get_run(request, identifier):
        item = records.run(project(request), identifier)
        if not item:
            raise HTTPException(404, 'UI 运行不存在')
        return item

    @router.get('/ui-automation')
    def workspace(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        try:
            enabled = settings()['enabled']
        except ValueError:
            enabled = False
        return {'suites': records.suites(project(request)), 'history': records.runs(project(request), page, page_size), 'runner': {'configured': enabled, 'available': enabled and WorkerMailbox(store, 'ui').status()['available']}}

    @router.post('/ui-suites', status_code=201)
    def create_suite(payload: UiSuiteInput, request: Request):
        return records.public(call(records.save, project(request), request.state.identity['user']['id'], payload.model_dump()))

    @router.put('/ui-suites/{identifier}')
    def update_suite(identifier: str, payload: UiSuiteInput, request: Request):
        return records.public(call(records.save, project(request), request.state.identity['user']['id'], payload.model_dump(), identifier))

    @router.get('/ui-suites/{identifier}')
    def read_suite(identifier: str, request: Request, version: int | None = Query(None, ge=1)):
        item = records.suite(project(request), identifier, version)
        if not item:
            raise HTTPException(404, 'UI 套件或版本不存在')
        return records.public(item)

    @router.delete('/ui-suites/{identifier}')
    def delete_suite(identifier: str, request: Request):
        if not call(records.delete_suite, project(request), identifier):
            raise HTTPException(404, 'UI 套件不存在')
        return {'deleted': True}

    @router.post('/ui-runs', status_code=202)
    def create_run(payload: UiRunInput, request: Request):
        try:
            available = settings()['enabled'] and WorkerMailbox(store, 'ui').status()['available']
        except ValueError:
            available = False
        if not available:
            raise HTTPException(503, 'UI Runner 未在线。套件可以先保存，启动独立 UI Worker 后再运行。')
        return records.public(call(records.enqueue, project(request), request.state.identity['user']['id'], payload.suite_id, payload.version))

    @router.get('/ui-runs/{identifier}')
    def read_run(identifier: str, request: Request):
        item = get_run(request, identifier)
        return records.public(item)

    @router.post('/ui-runs/{identifier}/stop')
    def stop_run(identifier: str, request: Request):
        get_run(request, identifier)
        return {'requested': records.stop(project(request), identifier)}

    @router.get('/ui-runs/{identifier}/download')
    def download(identifier: str, request: Request, name: str = Query(min_length=1, max_length=500)):
        item = get_run(request, identifier)
        manifest = item.get('result', {}).get('artifacts') or []
        entry = next((entry for entry in manifest if entry['name'] == name), None)
        if not entry or not item['artifact_ref']:
            raise HTTPException(404, '产物不存在或尚未完成归档')
        try:
            container = artifacts.resolve(item['artifact_ref'])
            path = artifacts.resolve_file(artifacts.reference(container / entry.get('storage_name', name)), container=item['artifact_ref'])
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, '产物不可用') from exc
        if entry.get('encrypted'):
            from urllib.parse import quote
            try:
                content = decrypt_artifact(path)
            except Exception as exc:
                raise HTTPException(503, '加密产物暂不可用，请检查存储和主密钥') from exc
            return Response(content, media_type='application/octet-stream', headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name.rsplit('/', 1)[-1]), 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})
        return FileResponse(path, media_type='application/octet-stream', filename=path.name, headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})
    return router
