"""Optional cross-process wake-up signals for persistent task workers.

MySQL/SQLite remain the source of truth for task state.  Redis only carries
small wake-up signals, so a Redis outage can delay a claim until the fallback
poll without losing or duplicating the persisted job.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

from auto_test.common.config_loader import task_queue_cfg
from auto_test.common.env import get_env


class TaskSignalQueue:
    backend = "local"

    def notify(self, topic: str) -> bool:
        return True

    def wait(self, topic: str, timeout: float) -> bool:
        time.sleep(max(0.0, float(timeout)))
        return False

    def status(self) -> dict[str, Any]:
        return {"backend": self.backend, "available": True, "worker_available": None}

    def heartbeat(self, worker_name: str = "primary", ttl_seconds: int = 20) -> bool:
        return True

    def clear_heartbeat(self, worker_name: str = "primary") -> None:
        return None

    def close(self) -> None:
        return None


class LocalTaskSignalQueue(TaskSignalQueue):
    """In-process signal queue used by the default PyCharm deployment."""

    backend = "local"

    def __init__(self):
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _event(self, topic: str) -> threading.Event:
        normalized = str(topic or "default")
        with self._lock:
            return self._events.setdefault(normalized, threading.Event())

    def notify(self, topic: str) -> bool:
        self._event(topic).set()
        return True

    def wait(self, topic: str, timeout: float) -> bool:
        event = self._event(topic)
        signaled = event.wait(max(0.0, float(timeout)))
        event.clear()
        return signaled


class RedisTaskSignalQueue(TaskSignalQueue):
    """Redis list-backed signals with database-polling degradation."""

    backend = "redis"

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "liema",
        connect_timeout: float = 1.0,
        username: str | None = None,
        password: str | None = None,
        client=None,
    ):
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("task_queue.redis_url 必须是有效的 redis:// 或 rediss:// 地址")
        self.namespace = str(namespace or "liema").strip(":") or "liema"
        self._last_error_at = 0.0
        if client is not None:
            self.client = client
            return
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("启用 Redis 任务队列需要安装 redis 依赖") from exc
        connection_options: dict[str, Any] = {}
        if username:
            connection_options["username"] = username
        if password:
            connection_options["password"] = password
        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=max(0.1, float(connect_timeout)),
            socket_timeout=max(1.0, float(connect_timeout) + 1.0),
            health_check_interval=30,
            **connection_options,
        )

    def _key(self, topic: str) -> str:
        normalized = str(topic or "default").replace(":", "-")[:80]
        return f"{self.namespace}:wake:{normalized}"

    def notify(self, topic: str) -> bool:
        try:
            pipe = self.client.pipeline(transaction=False)
            key = self._key(topic)
            pipe.lpush(key, str(time.time_ns()))
            pipe.ltrim(key, 0, 99)
            pipe.expire(key, 86400)
            pipe.execute()
            return True
        except Exception:
            self._last_error_at = time.time()
            return False

    def wait(self, topic: str, timeout: float) -> bool:
        try:
            result = self.client.brpop(
                self._key(topic), timeout=max(1, int(math.ceil(float(timeout))))
            )
            return bool(result)
        except Exception:
            self._last_error_at = time.time()
            time.sleep(min(max(float(timeout), 0.05), 1.0))
            return False

    def status(self) -> dict[str, Any]:
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.ping()
            pipe.get(f"{self.namespace}:worker:primary")
            available, last_seen = pipe.execute()
            available = bool(available)
        except Exception:
            available = False
            last_seen = None
            self._last_error_at = time.time()
        try:
            worker_age = max(0.0, time.time() - float(last_seen)) if last_seen else None
        except (TypeError, ValueError):
            worker_age = None
        return {
            "backend": self.backend,
            "available": available,
            "worker_available": bool(worker_age is not None and worker_age <= 20),
            "worker_age_seconds": round(worker_age, 1) if worker_age is not None else None,
            "last_error_at": self._last_error_at or None,
        }

    def heartbeat(self, worker_name: str = "primary", ttl_seconds: int = 20) -> bool:
        try:
            self.client.set(
                f"{self.namespace}:worker:{str(worker_name or 'primary')[:80]}",
                str(time.time()),
                ex=max(5, int(ttl_seconds)),
            )
            return True
        except Exception:
            self._last_error_at = time.time()
            return False

    def clear_heartbeat(self, worker_name: str = "primary") -> None:
        try:
            self.client.delete(
                f"{self.namespace}:worker:{str(worker_name or 'primary')[:80]}"
            )
        except Exception:
            self._last_error_at = time.time()

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def task_execution_mode(overrides: dict[str, Any] | None = None) -> str:
    values = dict(task_queue_cfg())
    values.update(overrides or {})
    mode = str(
        get_env("TASK_EXECUTION_MODE", values.get("execution_mode", "embedded"))
        or "embedded"
    ).strip().lower()
    if mode not in {"embedded", "external"}:
        raise ValueError("task_queue.execution_mode 只支持 embedded 或 external")
    return mode


def redis_connection_settings(
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve Redis connection fields without embedding passwords in URLs.

    Deployment platforms often provide ``SERVICE_REDIS_IP/PORT/PASSWORD``
    separately.  ``LIEMA_REDIS_*`` remains the application-specific override,
    while ``password_env`` lets a local YAML file name a secret environment
    variable without ever containing the secret itself.
    """

    values = dict(task_queue_cfg())
    values.update(overrides or {})
    explicit_url = str(get_env("REDIS_URL", "") or "").strip()
    host = str(
        get_env("REDIS_HOST", "")
        or os.environ.get("SERVICE_REDIS_IP", "")
        or values.get("redis_host", "")
        or ""
    ).strip()
    if explicit_url:
        url = explicit_url
    elif host:
        raw_port = (
            get_env("REDIS_PORT", "")
            or os.environ.get("SERVICE_REDIS_PORT", "")
            or values.get("redis_port", 6379)
        )
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("Redis 端口必须是 1～65535 的整数") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Redis 端口必须是 1～65535 的整数")
        scheme = str(values.get("redis_scheme") or "redis").strip().lower()
        if scheme not in {"redis", "rediss"}:
            raise ValueError("task_queue.redis_scheme 只支持 redis 或 rediss")
        db = max(0, int(values.get("redis_db") or 0))
        normalized_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        url = f"{scheme}://{normalized_host}:{port}/{db}"
    else:
        url = str(values.get("redis_url") or "redis://127.0.0.1:6379/0")

    password = get_env("REDIS_PASSWORD")
    if not password:
        password_env = str(
            values.get("password_env") or "SERVICE_REDIS_PASSWORD"
        ).strip()
        password = os.environ.get(password_env) if password_env else None
    username = str(
        get_env("REDIS_USERNAME", "")
        or os.environ.get("SERVICE_REDIS_USERNAME", "")
        or values.get("redis_username", "")
        or ""
    ).strip()
    return {
        "url": url,
        "username": (username or None) if password not in {None, ""} else None,
        "password": str(password) if password not in {None, ""} else None,
    }


def create_task_signal_queue(
    overrides: dict[str, Any] | None = None,
) -> TaskSignalQueue:
    values = dict(task_queue_cfg())
    values.update(overrides or {})
    backend = str(
        get_env("TASK_QUEUE_BACKEND", values.get("backend", "local")) or "local"
    ).strip().lower()
    if backend == "local":
        return LocalTaskSignalQueue()
    if backend != "redis":
        raise ValueError(f"不支持的任务队列后端：{backend}")
    connection = redis_connection_settings(values)
    return RedisTaskSignalQueue(
        connection["url"],
        namespace=str(
            get_env("REDIS_NAMESPACE", values.get("namespace", "liema")) or "liema"
        ),
        connect_timeout=float(values.get("connect_timeout") or 1.0),
        username=connection["username"],
        password=connection["password"],
    )
