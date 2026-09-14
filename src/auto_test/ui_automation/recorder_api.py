"""Recording API and same-origin, session-authenticated noVNC HTTP/WS gateway."""

import asyncio
import os
import re
import secrets
import shutil
import time

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from auto_test.platform.identity import SESSION_COOKIE
from auto_test.platform.secrets import decrypt_secret
from auto_test.platform.worker_rpc import WorkerMailbox
from auto_test.ui_automation.recordings import IDLE_TIMEOUT, RecordingStore, public_recording
from auto_test.ui_automation.recorder_runtime import endpoint, recorder_settings
from auto_test.ui_automation.environment import ENVIRONMENT_KEYS, options, save_options, validate_image, validate_network


class RecordingInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=2000)
    suite_id: str = Field(default='', max_length=64)


class EnvironmentInput(BaseModel):
    runner_image: str = Field(default='', max_length=500)
    recorder_image: str = Field(default='', max_length=500)
    network: str = Field(default='none', min_length=1, max_length=101)


def gateway_dependencies():
    """Load optional recording transports only when a desktop is needed.

    Existing installations can start the Web app before installing the new
    recording dependencies. Keep missing/broken imports out of route setup.
    """
    try:
        import httpx
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise RuntimeError('当前 Python 环境缺少录制组件依赖；请按录制说明安装 httpx 和 websockets 后重启') from exc
    return httpx, connect


def require_gateway_dependencies():
    try:
        return gateway_dependencies()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def availability(store):
    try:
        configured = recorder_settings()['enabled']
        if configured:
            try:
                gateway_dependencies()
            except RuntimeError as exc:
                return {'configured': True, 'available': False, 'message': str(exc)}
        online = WorkerMailbox(store, 'ui').status()['available']
        return {'configured': configured, 'available': configured and online,
                'message': '录制环境已就绪' if configured and online else '录制服务未启动，请打开“录制环境设置”查看启动步骤' if configured else '录制环境未就绪：请打开“录制环境设置”完成首次配置'}
    except ValueError as exc:
        return {'configured': False, 'available': False, 'message': str(exc)}


def environment_status(store, editable):
    checks, config_error = [], ''
    try:
        value = options()
    except ValueError as exc:
        value, config_error = {'runner_image': '', 'recorder_image': '', 'network': 'none'}, str(exc)
    def add(key, label, status, detail):
        checks.append({'key': key, 'label': label, 'status': status, 'detail': detail})
    try:
        gateway_dependencies()
        add('dependencies', 'Web 录制组件', 'ready', '当前 Web 的 Python 环境已安装录制连接组件')
    except RuntimeError:
        add('dependencies', 'Web 录制组件', 'missing', '需在启动 Web 的 Python 环境安装 httpx 和 websockets')
    worker = WorkerMailbox(store, 'ui').status()
    docker_found = bool(shutil.which('docker'))
    add('docker', '浏览器容器工具', 'ready' if worker['available'] else 'configured' if docker_found else 'missing',
        'UI Worker 在线，已通过启动检查' if worker['available'] else '已找到 Docker 命令；启动服务后验证 Docker 引擎' if docker_found else 'Web 进程未找到 Docker 命令；请在运行 UI Worker 的主机安装并启动 Docker')
    for key, label in [('runner_image', '回放镜像'), ('recorder_image', '录制镜像')]:
        try:
            validate_image(value[key], label)
            valid = bool(value[key])
            detail = '已填写固定镜像 ID；启动 UI Worker 后校验本机镜像' if valid else '在下方填写本机已有镜像的 SHA-256 ID'
        except ValueError as exc:
            valid, detail = False, str(exc)
        add(key, label, 'ready' if valid and worker['available'] else 'configured' if valid else 'missing',
            'UI Worker 在线，已通过镜像启动检查' if valid and worker['available'] else detail)
    try:
        validate_network(value['network'])
        valid_network = value['network'] != 'none'
    except ValueError:
        valid_network = False
    add('network', '测试网络', 'ready' if valid_network and worker['available'] else 'configured' if valid_network else 'missing',
        'UI Worker 在线，已通过 Internal 网络启动检查' if valid_network and worker['available'] else '已填写网络名称；启动 UI Worker 后检查是否为 Internal 网络' if valid_network else '录制需要专用 Internal 网络，不能使用 none 或默认 bridge')
    add('worker', '录制服务', 'ready' if worker['available'] else 'missing',
        '独立 UI Worker 在线' if worker['available'] else '保存配置后，在项目目录的独立终端启动 UI Worker')
    state = availability(store)
    return {**state, 'checks': checks, 'can_edit': editable, 'worker_online': worker['available'],
            'configuration': value if editable else None, 'configuration_error': config_error,
            'managed_fields': [key for key, env in ENVIRONMENT_KEYS.items() if env in os.environ] if editable else [],
            'deployment_mode': 'container' if os.environ.get('LIEMA_UI_RECORDER_CONNECT_MODE') == 'network' else 'local',
            'scope': '当前安装实例的设置，所有项目共用；测试网站地址在录制表单中填写。'}


def register_recording_api(router, store):
    records = RecordingStore(store)

    def owned(request, identifier):
        context = request.state.identity
        if not context['user'].get('is_superuser') and 'interface:manage' not in context['permissions']:
            raise HTTPException(403, '录制需要套件管理权限')
        item = records.get(identifier)
        if not item or item['project_id'] != context['current_project']['id'] or item['created_by'] != context['user']['id']:
            raise HTTPException(404, '录制会话不存在')
        return item

    @router.get('/ui-recordings')
    def workspace(request: Request):
        context = request.state.identity
        items = [public_recording(item) for item in records.active()
                 if item['project_id'] == context['current_project']['id'] and item['created_by'] == context['user']['id']]
        return {**availability(store), 'items': items}

    @router.get('/ui-recordings/environment')
    def environment(request: Request):
        return environment_status(store, bool(request.state.identity['user'].get('is_superuser')))

    @router.put('/ui-recordings/environment')
    def save_environment(payload: EnvironmentInput, request: Request):
        if not request.state.identity['user'].get('is_superuser'):
            raise HTTPException(403, '运行环境由平台管理员统一配置，请联系平台管理员')
        try:
            save_options(store, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, '配置无法写入，请检查 Web 对 config 目录的写入权限') from exc
        return {**environment_status(store, True), 'saved': True}

    @router.post('/ui-recordings', status_code=202)
    def create(payload: RecordingInput, request: Request):
        state = availability(store)
        if not state['available']:
            raise HTTPException(503, state['message'])
        try:
            context = request.state.identity
            item = records.create(context['current_project']['id'], context['user']['id'], payload.name, payload.target_url, payload.suite_id)
            return public_recording(item)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, '套件不存在') from exc

    @router.get('/ui-recordings/{identifier}')
    def read(identifier: str, request: Request):
        return public_recording(owned(request, identifier))

    @router.post('/ui-recordings/{identifier}/heartbeat')
    def heartbeat(identifier: str, request: Request):
        owned(request, identifier)
        records.touch(identifier)
        return public_recording(records.get(identifier))

    @router.post('/ui-recordings/{identifier}/save')
    def save(identifier: str, request: Request):
        item = owned(request, identifier)
        if item['status'] not in {'recording', 'saving', 'saved'}:
            raise HTTPException(409, '当前录制尚未就绪或已经结束')
        return public_recording(records.request_save(identifier))

    @router.post('/ui-recordings/{identifier}/cancel')
    def cancel(identifier: str, request: Request):
        owned(request, identifier)
        records.close(identifier, 'cancelled', '录制已取消，未保存为新版本')
        return public_recording(records.get(identifier))


def register_recorder_gateway(app, store, identity):
    records = RecordingStore(store)
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'}

    def checked(connection, identifier):
        authenticated = identity.authenticate_token(connection.cookies.get(SESSION_COOKIE, ''))
        if not authenticated:
            raise HTTPException(401, '登录已失效')
        item = records.get(identifier)
        if not item or item['created_by'] != authenticated.session['user_id']:
            raise HTTPException(404, '录制会话不存在')
        try:
            context = identity.context(authenticated, item['project_id'])
        except PermissionError as exc:
            raise HTTPException(403, '项目权限已撤销') from exc
        if not identity.has_permission(context, 'interface:manage'):
            raise HTTPException(403, '套件管理权限已撤销')
        origin = str(connection.url).split('/ui-recorder/', 1)[0].replace('ws://', 'http://', 1).replace('wss://', 'https://', 1)
        if connection.headers.get('origin', origin) != origin or connection.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '只允许从平台页面连接录制窗口')
        worker = WorkerMailbox(store, 'ui').status()
        if (item['status'] not in {'recording', 'saving'} or item['expires_at'] <= time.time()
                or item['heartbeat_at'] <= time.time() - IDLE_TIMEOUT or not worker['available'] or worker['owner'] != item['owner']):
            raise HTTPException(410, '录制已结束、到期或服务离线')
        require_gateway_dependencies()
        return item

    @app.get('/ui-recorder/{identifier}/desktop')
    def desktop(identifier: str, request: Request):
        checked(request, identifier)
        nonce = secrets.token_urlsafe(24)
        # No bearer token or upstream port is ever sent to the browser.
        html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Playwright 录制窗口</title>
<style nonce="NONCE">html,body,#desktop{height:100%;width:100%;margin:0;overflow:hidden;background:#20242d;color:#fff;font:14px sans-serif}#message{position:absolute;top:10px;left:10px;background:#20242d;padding:8px;z-index:2}</style>
<div id="message" role="status">正在连接浏览器…</div><div id="desktop"></div>
<script type="module" nonce="NONCE">
const base = location.pathname.slice(0, -8);
try {
  const {default:RFB} = await import(base + '/assets/core/rfb.js');
  const rfb = new RFB(document.getElementById('desktop'), (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + base + '/socket');
  rfb.scaleViewport = true; rfb.resizeSession = false; rfb.showDotCursor = true;
  window.addEventListener('message', event => {
    if (event.source !== parent || event.origin !== location.origin || event.data?.type !== 'liema-recorder-scale') return;
    if (!['native', 'fit'].includes(event.data.mode)) return;
    rfb.scaleViewport = event.data.mode === 'fit';
    rfb.clipViewport = false;
  });
  parent.postMessage({type: 'liema-recorder-ready'}, location.origin);
  const message = document.getElementById('message');
  rfb.addEventListener('connect', () => {message.hidden = true;});
  rfb.addEventListener('disconnect', () => {message.hidden = false; message.textContent = '连接已断开；可点击页面上的“重连窗口”';});
  window.addEventListener('pagehide', () => rfb.disconnect(), {once:true});
} catch (_) { document.getElementById('message').textContent = '窗口连接失败；请重连或检查录制服务'; }
</script></html>'''.replace('NONCE', nonce)
        return HTMLResponse(html, headers={**headers, 'Content-Security-Policy': "default-src 'none'; script-src 'self' 'nonce-" + nonce + "'; style-src 'nonce-" + nonce + "'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'", 'X-Frame-Options': 'SAMEORIGIN'})

    @app.get('/ui-recorder/{identifier}/assets/{asset:path}')
    async def asset(identifier: str, asset: str, request: Request):
        item = await run_in_threadpool(checked, request, identifier)
        if not re.fullmatch(r'[A-Za-z0-9_./-]+\.(js|css)', asset) or '..' in asset.split('/'):
            raise HTTPException(404, '资源不存在')
        httpx, _ = require_gateway_dependencies()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                async with client.stream('GET', endpoint(item) + '/assets/' + asset,
                                         headers={'Authorization': 'Bearer ' + decrypt_secret(item['gateway_secret_enc'])}) as upstream:
                    if upstream.status_code != 200:
                        raise HTTPException(404, '资源不可用')
                    payload = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 2_000_000:
                            raise HTTPException(502, '录制资源过大')
            return Response(bytes(payload), media_type='text/javascript' if asset.endswith('.js') else 'text/css', headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(502, '录制窗口暂时不可连接') from exc

    @app.websocket('/ui-recorder/{identifier}/socket')
    async def socket(identifier: str, websocket: WebSocket):
        tasks = []
        accepted = False
        try:
            if not websocket.headers.get('origin'):
                raise HTTPException(403, '缺少来源')
            item = await run_in_threadpool(checked, websocket, identifier)
            _, connect = require_gateway_dependencies()
            async with connect(endpoint(item).replace('http:', 'ws:', 1) + '/socket',
                               additional_headers={'Authorization': 'Bearer ' + decrypt_secret(item['gateway_secret_enc'])},
                               subprotocols=['binary'], proxy=None, open_timeout=5, close_timeout=2,
                               max_size=4_000_000, max_queue=8, compression=None) as upstream:
                await websocket.accept(subprotocol='binary' if 'binary' in websocket.headers.get('sec-websocket-protocol', '').split(', ') else None)
                accepted = True
                async def to_desktop():
                    while True:
                        payload = await websocket.receive_bytes()
                        if len(payload) > 1_000_000:
                            raise ValueError('输入帧过大')
                        await upstream.send(payload)
                async def to_browser():
                    async for payload in upstream:
                        if not isinstance(payload, bytes):
                            raise ValueError('不支持文本帧')
                        await websocket.send_bytes(payload)
                async def revalidate():
                    while True:
                        await asyncio.sleep(3)
                        await run_in_threadpool(checked, websocket, identifier)
                tasks = [asyncio.create_task(fn()) for fn in (to_desktop, to_browser, revalidate)]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await websocket.close(code=1000 if accepted else 1008)
            except RuntimeError:
                pass
