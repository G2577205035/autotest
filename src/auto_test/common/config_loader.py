"""Load environment settings from ``config/config.local.yml`` or the example."""

import yaml
from auto_test.common.paths import PROJECT_ROOT

# 优先加载本地配置
_cfg_path = PROJECT_ROOT / "config" / "config.local.yml"
if not _cfg_path.exists():
    _cfg_path = PROJECT_ROOT / "config" / "config.example.yml"

with open(_cfg_path, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}


def api_cfg():
    return CFG.get("api", {})


def monitor_cfg():
    return CFG.get("monitor", {})


def upload_cfg():
    return CFG.get("upload", {})


def stress_cfg():
    return CFG.get("stress", {})


def database_cfg():
    return CFG.get("database", {})


def artifact_storage_cfg():
    return CFG.get("artifact_storage", {})


def task_queue_cfg():
    return CFG.get("task_queue", {})


def export_cfg():
    return CFG.get("export", {})


def ai_checks_cfg():
    return CFG.get("ai_checks", {})


def default_translate_name() -> str:
    """Return the platform translation language used when a run omits one."""
    return str(CFG.get("defaults", {}).get("translate_name", "") or "").strip()
