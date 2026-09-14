"""Run imported code only in a bounded, credential-free Docker container."""

import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import time

from auto_test.platform.secrets import decrypt_secret, encrypt_secret
from auto_test.ui_automation.environment import browser_proxy, options, validate_image, validate_network


def settings():
    value = options()
    image, network = value['runner_image'], value['network']
    validate_image(image, 'UI Runner 镜像')
    validate_network(network)
    return {'image': image, 'network': network, 'enabled': bool(image), 'proxy': browser_proxy()}


def container_name(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('UI 运行编号无效')
    return 'liema-ui-' + run_id


def browser_cpu_arguments():
    # CPU quota alone does not reduce Chromium's visible host CPU count. On
    # large Linux hosts its thread pools can exhaust the container PID limit.
    # The local Docker engine shares this kernel's affinity numbering.
    affinity = getattr(os, 'sched_getaffinity', None)
    if affinity:
        cpus = sorted(affinity(0))[:2]
        if cpus:
            return ['--cpuset-cpus', ','.join(map(str, cpus))]
    return []


def prepare_input(snapshot, destination, proxy=''):
    destination = Path(destination)
    for file in snapshot['files']:
        (destination / file['name']).write_text(file['content'], encoding='utf-8')
    (destination / 'parameters.json').write_text(json.dumps(snapshot['parameters'], ensure_ascii=False), encoding='utf-8')
    config = {
        'testDir': '/tmp/liema-tests' if snapshot.get('recorded') else '/work/tests', 'testMatch': ['**/*.spec.js', '**/*.spec.ts', '**/*.test.js', '**/*.test.ts', '**/*.spec.cjs', '**/*.test.cjs'],
        'outputDir': '/results/test-results', 'workers': 1, 'retries': 0, 'forbidOnly': True,
        'timeout': min(60_000, snapshot['timeout_seconds'] * 1000),
        'globalTimeout': snapshot['timeout_seconds'] * 1000,
        'reporter': [['json', {'outputFile': '/results/playwright.json'}]],
        'use': {'browserName': 'chromium', 'headless': True,
                'screenshot': 'on', 'video': 'on', 'trace': 'on', 'viewport': {'width': 1440, 'height': 900}},
    }
    if snapshot['base_url']:
        config['use']['baseURL'] = snapshot['base_url']
    if snapshot.get('recorded'):
        config['use']['viewport'] = {'width': 1024, 'height': 768}
    if proxy:
        config['use']['proxy'] = {'server': proxy}
    (destination / 'playwright.config.cjs').write_text('module.exports = ' + json.dumps(config) + ';\n', encoding='utf-8')


def extract_artifacts(archive, destination, encrypted=False):
    destination = Path(destination).resolve()
    artifacts, total = [], 0
    with tarfile.open(fileobj=archive, mode='r:') as tar:
        for index, member in enumerate(tar):
            if index > 2000:
                raise ValueError('UI 产物文件过多')
            parts = PurePosixPath(member.name).parts
            if member.isdir():
                continue
            if not member.isfile() or member.name.startswith('/') or '\\' in member.name or any(part in {'..', ''} or ':' in part for part in parts):
                raise ValueError('UI 产物包含非法路径或链接')
            if Path(member.name).suffix.lower() not in {'.json', '.png', '.webm', '.zip', '.txt'}:
                continue
            total += member.size
            if member.size > 30_000_000 or total > 60_000_000:
                raise ValueError('UI 产物超过单文件 30 MB / 总计 60 MB 限制')
            original_name = PurePosixPath(*parts).as_posix()
            safe_name = ('playwright.json' if original_name == 'playwright.json' else f'artifact-{index}' + Path(member.name).suffix.lower()) if encrypted else original_name
            target = destination.joinpath(safe_name + '.enc' if encrypted else safe_name).resolve()
            target.relative_to(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if encrypted:
                target.write_text(encrypt_secret(base64.b64encode(source.read()).decode('ascii')), encoding='ascii')
                artifacts.append({'name': safe_name, 'storage_name': target.name, 'encrypted': True, 'size': member.size})
            else:
                with target.open('wb') as output:
                    while chunk := source.read(64 * 1024):
                        output.write(chunk)
                artifacts.append({'name': target.relative_to(destination).as_posix(), 'size': member.size})
    return artifacts


def decrypt_artifact(path):
    if path.stat().st_size > 55_000_000:
        raise ValueError('加密产物超过大小限制')
    return base64.b64decode(decrypt_secret(path.read_text(encoding='ascii')), validate=True)


def summarize_result(path, exit_code, encrypted=False):
    result = {'passed': 0, 'failed': 0, 'skipped': 0, 'total': 0, 'cases': []}
    if not path.is_file() or path.stat().st_size > (9_000_000 if encrypted else 5_000_000):
        return 'failed', {**result, 'message': '执行未产生有效测试结果'}
    raw = json.loads(decrypt_artifact(path) if encrypted else path.read_text(encoding='utf-8'))
    def visit(suites, depth=0):
        if depth > 30:
            raise ValueError('测试结果嵌套过深')
        for suite in suites:
            visit(suite.get('suites') or [], depth + 1)
            for spec in suite.get('specs') or []:
                for test in spec.get('tests') or []:
                    if len(result['cases']) >= 1000:
                        raise ValueError('首版每次运行最多 1000 个测试结果')
                    outcomes = test.get('results') or []
                    last = outcomes[-1] if outcomes else {}
                    status = last.get('status', 'missing')
                    # Expected-to-fail and flaky tests do not masquerade as passed.
                    key = 'passed' if status == 'passed' and test.get('expectedStatus', 'passed') == 'passed' else 'skipped' if status == 'skipped' else 'failed'
                    result[key] += 1
                    duration = last.get('duration')
                    duration = duration if type(duration) in (int, float) and 0 <= duration < 1e10 else None
                    result['cases'].append({'name': f'录制用例 {len(result["cases"]) + 1}' if encrypted else str(spec.get('title') or '')[:500], 'status': key, 'duration_ms': duration})
    visit(raw.get('suites') or [])
    result['total'] = sum(result[key] for key in ('passed', 'failed', 'skipped'))
    success = exit_code == 0 and result['passed'] > 0 and not result['failed'] and not raw.get('errors')
    result['message'] = '测试执行完成' if success else '测试失败、未执行断言或运行异常；详见原始结果'
    return ('succeeded' if success else 'failed'), result


class DockerUiRunner:
    def __init__(self, config=None):
        self.config = config or settings()

    @staticmethod
    def command(arguments, **kwargs):
        # The imported script receives none of this host environment; Docker only
        # gets explicitly selected launcher variables, never platform credentials.
        env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'WINDIR', 'HOME', 'USERPROFILE', 'TEMP', 'TMP') if key in os.environ}
        return subprocess.run(['docker', *arguments], env=env, **({} if 'input' in kwargs else {'stdin': subprocess.DEVNULL}),
                              stderr=subprocess.DEVNULL, timeout=kwargs.pop('timeout', 20), check=kwargs.pop('check', True), **kwargs)

    def preflight(self):
        if not self.config['enabled']:
            raise RuntimeError('UI Runner 尚未配置')
        if self.config['network'] != 'none':
            result = self.command(['network', 'inspect', self.config['network'], '--format', '{{.Internal}}'], capture_output=False, stdout=subprocess.PIPE)
            if result.stdout.strip() != b'true':
                raise RuntimeError('UI Runner 网络必须为 Docker Internal 测试网络')
        self.command(['image', 'inspect', self.config['image']], stdout=subprocess.DEVNULL)

    def create_arguments(self, name, input_dir, recorded=False):
        return ['create', '--pull=never', '--name', name, '--label', 'liema.role=ui-runner', '--init', '--read-only',
                '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '384', '--memory', '1g', '--cpus', '1', *browser_cpu_arguments(), '--shm-size', '256m',
                '--network', self.config['network'], '--log-driver', 'none',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777',
                '--tmpfs', '/results:rw,nosuid,nodev,size=64m,mode=1777',
                '--tmpfs', '/work/tests:rw,nosuid,nodev,size=8m,mode=1777',
                '--workdir', '/work', '--env', 'HOME=/tmp', '--env', 'LIEMA_UI_PARAMETERS=/work/tests/parameters.json',
                *(['--env', 'LIEMA_UI_RECORDED=1'] if recorded else []),
                '--entrypoint', 'node', self.config['image'], '/opt/liema-ui-launcher.cjs']

    def remove(self, run_id):
        self.command(['rm', '-f', container_name(run_id)], check=False, stdout=subprocess.DEVNULL)

    def run(self, run, destination, should_stop):
        self.preflight()
        name = container_name(run['id'])
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        created = False
        status, result = 'failed', {'message': 'UI 执行异常'}
        with tempfile.TemporaryDirectory(prefix='liema-ui-input-') as temp:
            prepare_input(run['snapshot'], temp, self.config.get('proxy', ''))
            recorded = bool(run['snapshot'].get('recorded'))
            try:
                self.command(self.create_arguments(name, temp, recorded), stdout=subprocess.DEVNULL)
                created = True
                self.command(['start', name], stdout=subprocess.DEVNULL)
                # Host bind paths do not refer to the same filesystem when the
                # Worker itself runs in Docker. Inject the bounded input through
                # stdin, then release the launcher only after all files exist.
                files = {file.name: file.read_text(encoding='utf-8') for file in Path(temp).iterdir() if file.is_file()}
                self.command(['exec', '-i', name, 'node', '-e',
                              "const fs=require('fs');let b='';process.stdin.setEncoding('utf8');"
                              "process.stdin.on('data',c=>{b+=c;if(Buffer.byteLength(b)>3000000)process.exit(2)});"
                              "process.stdin.on('end',()=>{const files=JSON.parse(b);for(const [name,text] of Object.entries(files)){"
                              "if(!/^[a-zA-Z0-9_.-]+$/.test(name)||name.startsWith('.'))throw Error('Invalid input');"
                              "fs.writeFileSync('/work/tests/'+name,text,{mode:0o600});}"
                              "fs.writeFileSync('/work/tests/.ready','1');});"],
                             input=json.dumps(files, ensure_ascii=False).encode('utf-8'), stdout=subprocess.DEVNULL)
                if recorded:
                    source = decrypt_secret(run['snapshot']['recording_source_enc']).encode('utf-8')
                    if not 1 <= len(source) <= 500_000:
                        raise ValueError('录制脚本大小无效')
                    self.command(['exec', '-i', name, 'node', '-e',
                                  "const fs=require('fs');let b='';process.stdin.setEncoding('utf8');"
                                  "process.stdin.on('data',c=>{b+=c;if(Buffer.byteLength(b)>500000)process.exit(2)});"
                                  "process.stdin.on('end',()=>{fs.mkdirSync('/tmp/liema-tests',{recursive:true});"
                                  "fs.symlinkSync('/work/node_modules','/tmp/liema-tests/node_modules');"
                                  "fs.writeFileSync('/tmp/liema-tests/recorded.spec.js',b,{mode:0o600});"
                                  "fs.writeFileSync('/tmp/liema-tests/.ready','1');});"], input=source, stdout=subprocess.DEVNULL)
                deadline = time.monotonic() + run['snapshot']['timeout_seconds'] + 15
                code = None
                while time.monotonic() < deadline and not should_stop():
                    check = self.command(['exec', name, 'cat', '/results/.exit-code'], check=False, stdout=subprocess.PIPE, timeout=10)
                    if check.returncode == 0:
                        code = int(check.stdout.strip())
                        break
                    alive = self.command(['inspect', name, '--format', '{{.State.Running}}'], stdout=subprocess.PIPE)
                    if alive.stdout.strip() != b'true':
                        break
                    time.sleep(.5)
                interrupted = should_stop()
                # Stop the test process group before exporting, while the
                # separate launcher keeps the results tmpfs mounted.
                self.command(['exec', name, 'node', '-e',
                              "const fs=require('fs');try{const p=Number(fs.readFileSync('/results/.test-pid','utf8'));"
                              "if(Number.isInteger(p)&&p>1)process.kill(-p,'SIGKILL');}catch(e){}"],
                             check=False, stdout=subprocess.DEVNULL, timeout=10)
                with (io.BytesIO() if recorded else tempfile.TemporaryFile()) as archive:
                    # Docker cp cannot export tmpfs. Use tar inside this live
                    # container without following links; validate every entry.
                    copied = self.command(['exec', name, 'tar', '-C', '/results', '-cf', '-', '.'],
                                          check=False, stdout=subprocess.PIPE if recorded else archive, timeout=30)
                    if copied.returncode == 0:
                        if recorded:
                            if len(copied.stdout) > 80_000_000:
                                raise ValueError('录制产物归档过大')
                            archive.write(copied.stdout)
                        archive.seek(0)
                        artifacts = extract_artifacts(archive, destination, recorded)
                    else:
                        artifacts = []
                if interrupted:
                    status, result = 'stopped', {'message': '用户停止或 Worker 所有权丢失'}
                elif code is None:
                    status, result = 'failed', {'message': '超过运行时限、容器异常退出或未生成结束标记'}
                else:
                    status, result = summarize_result(destination / ('playwright.json.enc' if recorded else 'playwright.json'), code, recorded)
                result['artifacts'] = artifacts
                result['runner_image'] = self.config['image']
                return status, result
            finally:
                if created:
                    self.remove(run['id'])
