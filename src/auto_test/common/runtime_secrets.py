"""Runtime master-key bootstrap shared by Web and external workers."""

from __future__ import annotations

import json
import secrets

from auto_test.common.env import get_env, has_env, set_env_default
from auto_test.common.paths import LOCAL_WEB_SECRETS_PATH


def ensure_runtime_master_key() -> None:
    if str(get_env("ENV", "")).lower() == "production":
        if not has_env("MASTER_KEY"):
            raise RuntimeError("生产环境必须设置 LIEMA_MASTER_KEY")
        return

    secret_file = LOCAL_WEB_SECRETS_PATH
    values = {}
    if secret_file.is_file():
        try:
            values = json.loads(secret_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
    values.setdefault("master_key", secrets.token_urlsafe(48))
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(
        json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    set_env_default("MASTER_KEY", values["master_key"])
