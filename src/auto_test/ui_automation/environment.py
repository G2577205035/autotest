"""Local UI runtime configuration shared by Web and UI Worker on one host."""

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from auto_test.common.paths import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / 'config' / 'ui-runtime.local.json'
ENVIRONMENT_KEYS = {'runner_image': 'LIEMA_UI_RUNNER_IMAGE', 'recorder_image': 'LIEMA_UI_RECORDER_IMAGE', 'network': 'LIEMA_UI_RUNNER_NETWORK'}
IMAGE_PATTERN = r'(?:[a-zA-Z0-9._/:\-]+@)?sha256:[0-9a-f]{64}'


def connection_mode():
    mode = os.environ.get('LIEMA_UI_RECORDER_CONNECT_MODE', 'loopback').strip()
    if mode not in {'loopback', 'network'}:
        raise ValueError('录制连接方式只允许 loopback 或 network')
    return mode


def browser_proxy():
    value = os.environ.get('LIEMA_UI_BROWSER_PROXY', '').strip()
    if not value:
        return ''
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or not url.hostname or not url.port
                or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment
                or any(character.isspace() for character in value)):
            raise ValueError
    except ValueError as exc:
        raise ValueError('浏览器代理须为不含账号密码的 http(s)://主机:端口') from exc
    return value.rstrip('/')


def options():
    saved = {}
    if CONFIG_PATH.exists():
        try:
            if CONFIG_PATH.stat().st_size > 8000:
                raise ValueError('配置文件过大')
            saved = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or any(key not in ENVIRONMENT_KEYS for key in saved):
                raise ValueError('配置格式无效')
        except (OSError, ValueError) as exc:
            raise ValueError('本机录制配置无法读取，请检查 config/ui-runtime.local.json') from exc
    result = {}
    for key, env in ENVIRONMENT_KEYS.items():
        value = os.environ.get(env, saved.get(key, 'none' if key == 'network' else ''))
        if not isinstance(value, str):
            raise ValueError('本机录制配置字段必须为文本')
        result[key] = value.strip()
    return result


def validate_image(image, label):
    if image and not re.fullmatch(IMAGE_PATTERN, image):
        raise ValueError(label + '必须填写固定的 sha256 镜像 ID，不能使用 latest 等标签')


def validate_network(network):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', network) or network in {'host', 'bridge', 'default'}:
        raise ValueError('测试网络仅允许 none 或专用 Internal 网络名称')


def save_options(store, data):
    """Save non-secret settings only while the UI Worker is fully stopped."""
    value = {key: str(data.get(key) or '').strip() for key in ENVIRONMENT_KEYS}
    validate_image(value['runner_image'], '回放镜像')
    validate_image(value['recorder_image'], '录制镜像')
    validate_network(value['network'])
    if value['recorder_image'] and (not value['runner_image'] or value['network'] == 'none'):
        raise ValueError('启用录制时，请同时填写回放镜像和专用 Internal 测试网络')
    for key, env in ENVIRONMENT_KEYS.items():
        if env in os.environ and value[key] != os.environ[env].strip():
            raise ValueError(env + '已由启动环境变量指定；请先移除该变量，再在页面修改')
    with store._connection() as connection:
        store.lock_interface_dispatch(connection)
        # Serialize with Worker acquisition. A stale owner also needs explicit
        # recovery, since expiry does not prove the old process has exited.
        connection.execute("UPDATE platform_worker_leases SET owner=owner WHERE name='ui'")
        lease = connection.execute("SELECT owner FROM platform_worker_leases WHERE name='ui'").fetchone()
        if lease and lease['owner']:
            raise ValueError('请先在运行 UI Worker 的终端按 Ctrl+C 停止录制服务，再保存配置并重新启动')
        if connection.execute("SELECT 1 FROM ui_runs WHERE status IN ('queued','running') LIMIT 1").fetchone() or connection.execute("SELECT 1 FROM ui_recordings WHERE status IN ('queued','starting','recording','saving') LIMIT 1").fetchone():
            raise ValueError('还有未结束的 UI 任务或录制，请先停止或取消后再修改运行配置')
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CONFIG_PATH.parent, prefix='.ui-runtime-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, CONFIG_PATH)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    return value
