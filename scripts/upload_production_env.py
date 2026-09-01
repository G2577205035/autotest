"""Upload a production Compose environment without writing secrets locally.

The script reads the ignored local YAML and development master-key file,
builds the deployment environment in memory, and creates a new mode-0600
``.env.production`` over SFTP.  It refuses to overwrite an existing file.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import posixpath
from pathlib import Path
from urllib.parse import urlparse

import paramiko
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _quoted(value: object) -> str:
    text = str(value if value is not None else "")
    if any(character in text for character in ("\x00", "\r", "\n")):
        raise ValueError("环境变量值不能包含空字符或换行")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _required(values: dict, key: str, section: str) -> str:
    value = str(values.get(key) or "").strip()
    if not value:
        raise ValueError(f"本机 {section}.{key} 未配置")
    return value


def _redis_host_and_port(queue: dict) -> tuple[str, int]:
    host = str(queue.get("redis_host") or "").strip()
    port = int(queue.get("redis_port") or 6379)
    if not host:
        parsed = urlparse(str(queue.get("redis_url") or ""))
        host = str(parsed.hostname or "")
        port = int(parsed.port or port)
    if not host:
        raise ValueError("本机 task_queue 未配置 Redis 地址")
    return host, port


def _build_environment(args: argparse.Namespace) -> str:
    config_path = Path(args.config).resolve()
    secret_path = Path(args.master_key_file).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    secrets = json.loads(secret_path.read_text(encoding="utf-8"))
    database = dict(config.get("database") or {})
    artifact = dict(config.get("artifact_storage") or {})
    queue = dict(config.get("task_queue") or {})

    if str(database.get("backend") or "").lower() != "mysql":
        raise ValueError("本机配置未启用 MySQL")
    if str(artifact.get("backend") or "").lower() != "minio":
        raise ValueError("本机配置未启用 MinIO")
    master_key = _required(secrets, "master_key", "local_web_secrets")
    redis_host, redis_port = _redis_host_and_port(queue)
    password_env = str(queue.get("password_env") or "SERVICE_REDIS_PASSWORD").strip()
    redis_password = os.environ.get(password_env, "") if password_env else ""
    if not redis_password and not args.allow_anonymous_redis:
        raise ValueError("Redis 未配置密码；临时免密部署必须显式添加 --allow-anonymous-redis")

    environment = {
        "COMPOSE_PROJECT_NAME": args.project_name,
        "LIEMA_IMAGE": args.image,
        "LIEMA_WEB_BIND": args.web_bind,
        "LIEMA_WEB_PORT": args.web_port,
        "LIEMA_MASTER_KEY": master_key,
        "LIEMA_MYSQL_HOST": _required(database, "host", "database"),
        "LIEMA_MYSQL_PORT": int(database.get("port") or 3306),
        "LIEMA_MYSQL_DATABASE": _required(database, "database", "database"),
        "LIEMA_MYSQL_USER": _required(database, "user", "database"),
        "LIEMA_MYSQL_PASSWORD": _required(database, "password", "database"),
        "LIEMA_MINIO_ENDPOINT": _required(artifact, "endpoint", "artifact_storage"),
        "LIEMA_MINIO_ACCESS_KEY": _required(artifact, "access_key", "artifact_storage"),
        "LIEMA_MINIO_SECRET_KEY": _required(artifact, "secret_key", "artifact_storage"),
        "LIEMA_MINIO_BUCKET": _required(artifact, "bucket", "artifact_storage"),
        "LIEMA_MINIO_SECURE": str(bool(artifact.get("secure"))).lower(),
        "LIEMA_REDIS_HOST": args.redis_host or redis_host,
        "LIEMA_REDIS_PORT": args.redis_port or redis_port,
        "LIEMA_REDIS_NAMESPACE": args.redis_namespace,
        "LIEMA_REDIS_USERNAME": str(queue.get("redis_username") or ""),
        "LIEMA_REDIS_PASSWORD": redis_password,
    }
    return "\n".join(f"{key}={_quoted(value)}" for key, value in environment.items()) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="安全上传烈马平台生产环境变量")
    parser.add_argument("host")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--target-dir", default="/home/liema")
    parser.add_argument("--web-bind", required=True)
    parser.add_argument("--web-port", type=int, required=True)
    parser.add_argument("--redis-host", default="")
    parser.add_argument("--redis-port", type=int, default=0)
    parser.add_argument("--redis-namespace", default="liema-auto-production")
    parser.add_argument("--image", default="liema-auto:2026.08.22")
    parser.add_argument("--project-name", default="liema")
    parser.add_argument("--allow-anonymous-redis", action="store_true")
    parser.add_argument(
        "--config", default=str(PROJECT_ROOT / "config" / "config.local.yml")
    )
    parser.add_argument(
        "--master-key-file",
        default=str(PROJECT_ROOT / "instance" / "local_web_secrets.json"),
    )
    args = parser.parse_args()
    if not 1 <= args.web_port <= 65535:
        raise ValueError("Web 端口必须是 1～65535")
    content = _build_environment(args)
    password = getpass.getpass(f"{args.ssh_user}@{args.host} SSH password: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host,
        port=args.ssh_port,
        username=args.ssh_user,
        password=password,
        timeout=10,
        banner_timeout=10,
        auth_timeout=10,
    )
    try:
        sftp = client.open_sftp()
        try:
            sftp.stat(args.target_dir)
            target = posixpath.join(args.target_dir.rstrip("/"), ".env.production")
            try:
                sftp.stat(target)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"拒绝覆盖已存在的环境文件：{target}")
            with sftp.open(target, "w") as stream:
                stream.write(content)
            sftp.chmod(target, 0o600)
        finally:
            sftp.close()
    finally:
        client.close()
    print(f"created {target} with mode 0600; secret values were not printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
