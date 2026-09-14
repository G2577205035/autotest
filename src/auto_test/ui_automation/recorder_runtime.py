"""Lease-owned Docker desktops. Only authenticated loopback gateways are exposed."""

import json
import re
import subprocess
import time

import requests

from auto_test.platform.secrets import decrypt_secret
from auto_test.ui_automation.environment import browser_proxy, connection_mode, options, validate_image
from auto_test.ui_automation.recordings import ACTIVE, IDLE_TIMEOUT, RecordingStore, authorized, gateway_secret
from auto_test.ui_automation.runner import DockerUiRunner, browser_cpu_arguments, settings


def recorder_settings():
    image = options()['recorder_image']
    validate_image(image, '录制镜像')
    network = settings()['network']
    if image and network == 'none':
        raise ValueError('录制需要专用 Internal 测试网络；none 无法连接桌面窗口')
    return {'image': image, 'network': network, 'enabled': bool(image),
            'connection_mode': connection_mode(), 'proxy': browser_proxy()}


def recording_container(identifier):
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise ValueError('录制编号无效')
    return 'liema-recorder-' + identifier


def endpoint(item):
    port = item['gateway_port']
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('录制窗口尚未连接')
    if connection_mode() == 'network':
        if port != 6080:
            raise ValueError('容器网络录制端口不匹配，请重新录制')
        return 'http://' + recording_container(item['id']) + ':6080'
    return 'http://127.0.0.1:' + str(port)


class DockerRecorder(DockerUiRunner):
    def __init__(self, config=None):
        super().__init__(config or recorder_settings())

    def create_arguments(self, name):
        return ['create', '--pull=never', '--name', name, '--label', 'liema.role=ui-recorder', '--init',
                '--read-only', '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '512', '--memory', '1536m', '--cpus', '1.5', *browser_cpu_arguments(), '--shm-size', '256m',
                '--network', self.config['network'],
                *(['--publish', '127.0.0.1::6080'] if self.config.get('connection_mode', 'loopback') == 'loopback' else []),
                '--log-driver', 'none',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777', '--env', 'HOME=/tmp',
                '--workdir', '/work', '--entrypoint', 'node', self.config['image'], '/opt/liema-recorder.cjs']

    def start(self, item, should_stop):
        self.preflight()
        name, secret = recording_container(item['id']), gateway_secret()
        try:
            self.command(self.create_arguments(name), stdout=subprocess.DEVNULL)
            self.command(['start', name], stdout=subprocess.DEVNULL)
            # Never pass a secret or target URL through argv, docker env or host files.
            payload = json.dumps({'secret': secret, 'url': decrypt_secret(item['target_enc']), 'proxy': self.config.get('proxy', '')}).encode()
            self.command(['exec', '-i', name, 'node', '-e',
                          "let b='';process.stdin.on('data',c=>{b+=c;if(b.length>12000)process.exit(2)});"
                          "process.stdin.on('end',()=>require('fs').writeFileSync('/tmp/recorder-init.json',b,{mode:0o600}));"],
                         input=payload, stdout=subprocess.DEVNULL)
            port = 6080
            if self.config.get('connection_mode', 'loopback') == 'loopback':
                output = self.command(['inspect', name, '--format', '{{json .NetworkSettings.Ports}}'], stdout=subprocess.PIPE)
                ports = json.loads(output.stdout)['6080/tcp']
                if len(ports) != 1 or ports[0]['HostIp'] != '127.0.0.1':
                    raise RuntimeError('录制窗口必须仅绑定本机回环地址')
                port = int(ports[0]['HostPort'])
            running = {**item, 'gateway_port': port, 'gateway_secret_enc': ''}
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not should_stop():
                try:
                    if self.request(running, 'GET', '/health', secret=secret).get('ready'):
                        return running['gateway_port'], secret
                except (requests.RequestException, ValueError):
                    pass
                time.sleep(.5)
            raise RuntimeError('录制环境启动超时或已取消')
        except Exception:
            self.remove(item['id'])
            raise

    @staticmethod
    def request(item, method, path, secret=None):
        with requests.Session() as client:
            client.trust_env = False
            with client.request(method, endpoint(item) + path,
                                headers={'Authorization': 'Bearer ' + (secret or decrypt_secret(item['gateway_secret_enc']))},
                                timeout=(2, 8), allow_redirects=False, stream=True) as response:
                response.raise_for_status()
                payload = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > 3_100_000:
                        raise ValueError('录制响应过大')
                return json.loads(payload)

    def capture(self, item):
        return self.request(item, 'POST', '/capture')

    def resume(self, item):
        self.request(item, 'POST', '/resume')

    def healthy(self, item):
        return self.request(item, 'GET', '/health').get('ready') is True

    def remove(self, identifier):
        self.command(['rm', '-f', recording_container(identifier)], check=False, stdout=subprocess.DEVNULL)

    def recover(self, identifiers):
        names = {recording_container(identifier) for identifier in identifiers}
        output = self.command(['ps', '-a', '--format', '{{.Names}}', '--filter', 'label=liema.role=ui-recorder'], stdout=subprocess.PIPE)
        for name in output.stdout.decode().split():
            if name in names:
                self.command(['rm', '-f', name], check=False, stdout=subprocess.DEVNULL)


class RecorderManager:
    def __init__(self, store, owner, stopping, runtime=None):
        self.store, self.owner, self.stopping = store, owner, stopping
        self.records = RecordingStore(store)
        self.runtime = runtime or DockerRecorder()
        self.containers = set()

    def recover(self):
        with self.store._connection() as connection:
            known = connection.execute('SELECT id FROM ui_recordings').fetchall()
        pending = self.records.active()
        self.runtime.recover([row['id'] for row in known])
        for item in pending:
            # Startup follows explicit lease recovery, so the old process is gone.
            self.records.close(item['id'], 'failed', '录制服务已重启，请重新录制')

    def tick(self):
        with self.store._connection() as connection:
            if not self.records.lease(connection, self.owner):
                self.stopping.set()
                return
        for identifier in tuple(self.containers):
            item = self.records.get(identifier)
            if not item or item['status'] not in ACTIVE:
                self.runtime.remove(identifier)
                self.containers.discard(identifier)
        for item in self.records.active():
            identifier = item['id']
            try:
                if not authorized(self.store, item):
                    self.records.close(identifier, 'cancelled', '项目权限已撤销', self.owner)
                elif item['expires_at'] <= time.time() or item['heartbeat_at'] < time.time() - IDLE_TIMEOUT:
                    self.records.close(identifier, 'expired', '录制已到期或页面长时间未连接，请重新录制', self.owner)
                elif item['status'] == 'queued' and self.records.claim(identifier, self.owner):
                    self.containers.add(identifier)
                    def stopped():
                        current = self.records.get(identifier)
                        return self.stopping.is_set() or not current or current['status'] != 'starting' or not authorized(self.store, current)
                    port, secret = self.runtime.start(item, stopped)
                    if not self.records.ready(identifier, self.owner, port, secret):
                        self.runtime.remove(identifier)
                elif item['status'] == 'saving' and item['owner'] == self.owner:
                    capture = self.runtime.capture(item)
                    if capture.get('checks', 0) < 1:
                        self.runtime.resume(item)
                        self.records.retry(identifier, self.owner, '上次未保存：缺少预期结果验证。点击“如何添加验证”，添加后再次保存；当前录制仍可继续。')
                        continue
                    saved = self.records.save(identifier, self.owner, capture.get('source'), capture.get('checks'))
                    if saved:
                        self.runtime.remove(identifier)
                        self.containers.discard(identifier)
                        self.store.add_audit_event(actor_user_id=item['created_by'], project_id=item['project_id'],
                            action='ui.recording.save', target_type='ui_suite', target_id=saved['suite_id'],
                            detail={'version': saved['suite_version'], 'checks': saved['checks']})
                    else:
                        self.records.close(identifier, 'expired', '录制到期或执行所有权已变更', self.owner)
                elif item['status'] == 'recording' and item['owner'] == self.owner and not self.runtime.healthy(item):
                    self.records.close(identifier, 'failed', '录制浏览器已退出；请重新录制，若重复发生请检查目标网络和浏览器资源', self.owner)
                    self.runtime.remove(identifier)
                    self.containers.discard(identifier)
            except Exception:
                # No generated source, URLs, browser output or exception text in logs.
                self.records.close(identifier, 'failed', '录制环境异常；请检查独立 UI Worker 和镜像配置', self.owner)
                self.runtime.remove(identifier)
                self.containers.discard(identifier)

    def run(self):
        try:
            while not self.stopping.is_set():
                self.tick()
                self.stopping.wait(1)
        except Exception:
            # Losing the DB or Docker connection must stop accepting recordings.
            self.stopping.set()
        finally:
            for identifier in tuple(self.containers):
                try:
                    self.runtime.remove(identifier)
                    self.records.close(identifier, 'failed', '录制服务已停止', self.owner)
                except Exception:
                    pass  # The container also has an independent hard lifetime.
