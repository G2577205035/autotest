"""Long-lived SSH sessions for server inventory and live monitoring."""

from __future__ import annotations

import copy
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from auto_test.common.config_loader import monitor_cfg, stress_cfg
from auto_test.common.logging import log
from auto_test.integrations.ssh import SSHClient
from auto_test.monitoring.docker_monitor import DockerMonitor
from auto_test.monitoring.server_capabilities import ServerCapabilityProbe
from auto_test.platform.contracts import PlatformRepository
from auto_test.platform.secrets import decrypt_secret, encrypt_secret


class ServerSessionManager:
    """Own server profiles and process-local live SSH monitoring sessions."""

    def __init__(
        self,
        store: PlatformRepository,
        *,
        idle_timeout: float | None = None,
        reap_interval: float = 5.0,
        capability_probe: ServerCapabilityProbe | None = None,
        monitor_factory=DockerMonitor,
        ssh_factory=SSHClient,
    ):
        configured_timeout = idle_timeout
        if configured_timeout is None:
            configured_timeout = float(
                stress_cfg().get("session_idle_timeout_seconds") or 300
            )
        self.store = store
        self.idle_timeout = max(60.0, min(float(configured_timeout), 3600.0))
        self.reap_interval = max(0.1, float(reap_interval))
        self.capability_probe = capability_probe or ServerCapabilityProbe()
        self.monitor_factory = monitor_factory
        self.ssh_factory = ssh_factory
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def start(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return
        self._stop.clear()
        self._reaper = threading.Thread(
            target=self._reap_loop,
            name="liema-server-session-reaper",
            daemon=True,
        )
        self._reaper.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._reaper:
            self._reaper.join(timeout=max(0.0, timeout))
        with self._lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            try:
                self.close(session_id, force=True, reason="平台服务停止")
            except Exception as exc:
                log.warning(f"关闭服务器会话失败：{session_id} - {exc}")

    def list_profiles(self) -> list[dict[str, Any]]:
        return self.store.list_server_profiles()

    def save_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = dict(data)
        password = str(payload.pop("password", "") or "")
        save_password = bool(payload.pop("save_password", False))
        existing = None
        if payload.get("id"):
            existing = self.store.get_server_profile(
                str(payload["id"]), include_secret=True
            )
        if save_password:
            if password:
                credential_enc: str | None = encrypt_secret(password)
            elif existing and existing.get("credential_enc"):
                credential_enc = None
            else:
                raise RuntimeError("选择保存密码时，请先填写 SSH 密码")
        else:
            credential_enc = ""
        if not str(payload.get("host") or "").strip():
            raise RuntimeError("请填写服务器 IP / 主机名")
        if not str(payload.get("user") or "").strip():
            raise RuntimeError("请填写 SSH 用户")
        return self.store.save_server_profile(payload, credential_enc)

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            active = any(
                item.get("profile_id") == profile_id
                for item in self._sessions.values()
            )
        if active:
            raise RuntimeError("该服务器仍有活动会话，请先关闭连接")
        return self.store.delete_server_profile(profile_id)

    def connect(self, options: dict[str, Any]) -> dict[str, Any]:
        data = dict(options)
        profile_id = str(data.get("profile_id") or "").strip()
        profile = None
        if profile_id:
            profile = self.store.get_server_profile(profile_id, include_secret=True)
            if not profile:
                raise KeyError(profile_id)
            for key in ("name", "host", "port", "user"):
                if data.get(key) in (None, ""):
                    data[key] = profile.get(key)

        host = str(data.get("host") or "").strip()
        user = str(data.get("user") or "").strip()
        port = max(1, min(int(data.get("port") or 22), 65535))
        password = str(data.get("password") or "")
        if not password and profile and profile.get("credential_enc"):
            password = decrypt_secret(str(profile["credential_enc"]))
        if not host:
            raise RuntimeError("请填写服务器 IP / 主机名")
        if not user:
            raise RuntimeError("请填写 SSH 用户")
        if host not in {"localhost", "127.0.0.1", "::1"} and not password:
            raise RuntimeError("该服务器未保存凭据，请输入 SSH 密码")

        with self._lock:
            for existing in self._sessions.values():
                same_project = str(existing.get("project_id") or "") == str(
                    data.get("_project_id") or ""
                )
                same_profile = profile_id and existing.get("profile_id") == profile_id
                same_target = (
                    not profile_id
                    and existing.get("host") == host
                    and existing.get("port") == port
                    and existing.get("user") == user
                )
                if same_project and (same_profile or same_target):
                    existing["last_active"] = time.time()
                    return self._public(existing, include_capability=True)

        ssh = self.ssh_factory(host, user, password, port)
        if not ssh.connect():
            raise RuntimeError(ssh.last_error or "SSH 连接失败")

        try:
            capability = self.capability_probe.collect(
                ssh,
                ["monitor", "cpu", "gpu"],
                gpu_burn_source=str(data.get("gpu_burn_source") or ""),
                gpu_burn_image=str(data.get("gpu_burn_image") or ""),
                gpu_burn_blackwell_image=str(
                    data.get("gpu_burn_blackwell_image") or ""
                ),
                gpu_devices="",
            )
        except Exception as exc:
            message = f"SSH 已连接，但服务器能力探测未完整完成：{exc}"
            log.warning(message)
            capability = {
                "overall": "degraded",
                "requested_modes": ["monitor", "cpu", "gpu"],
                "host": {"hostname": host, "user": user},
                "gpu": {
                    "count": 0,
                    "devices": [],
                    "test_strategy": {
                        "recommended_preset": "health",
                        "detected_count": 0,
                        "full_stress_count": 0,
                        "online_health_count": 0,
                        "maintenance_required": False,
                        "message": "连接已保留；环境探测不完整，压测暂不可用",
                    },
                },
                "capabilities": {
                    "monitor": {"status": "degraded", "message": message},
                    "cpu": {"status": "blocked", "message": "环境探测未完成"},
                    "gpu": {"status": "blocked", "message": "环境探测未完成"},
                },
                "warnings": [message],
            }

        session_id = uuid.uuid4().hex
        now = time.time()
        temp_dir = Path(tempfile.mkdtemp(prefix=f"liema-session-{session_id[:8]}-"))
        session = {
            "id": session_id,
            "profile_id": profile_id,
            "project_id": str(data.get("_project_id") or ""),
            "created_by_user_id": str(data.get("_created_by_user_id") or ""),
            "name": str(
                data.get("server_name")
                or data.get("name")
                or (profile or {}).get("name")
                or host
            ),
            "host": host,
            "port": port,
            "user": user,
            "status": "connected",
            "message": "SSH 已连接，实时指标采集中",
            "created_at": now,
            "last_active": now,
            "last_sample_at": None,
            "busy_count": 0,
            "capability": capability,
            "ssh": ssh,
            "monitor": None,
            "samples": deque(maxlen=600),
            "sample_sequence": 0,
            "temp_dir": temp_dir,
        }
        with self._lock:
            self._sessions[session_id] = session
        monitor = self.monitor_factory(
            ssh,
            ssh,
            ",".join(monitor_cfg().get("containers", [])),
            enable_log=False,
            enable_perf=True,
            run_dir=str(temp_dir),
            perf_prefix="session_",
            sample_callback=lambda label, sample: self._record_sample(
                session_id, label, sample
            ),
        )
        try:
            session["monitor"] = monitor
            monitor.start()
            if profile_id:
                self.store.touch_server_profile(profile_id)
            self.start()
            return self._public(session, include_capability=True)
        except Exception:
            self.close(session_id, force=True, reason="监控启动失败")
            raise

    def _record_sample(
        self, session_id: str, source: str, sample: dict[str, Any]
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            session["sample_sequence"] += 1
            created_at = time.time()
            session["samples"].append(
                {
                    "id": session["sample_sequence"],
                    "source": source,
                    "data": dict(sample),
                    "created_at": created_at,
                }
            )
            session["last_sample_at"] = created_at

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = sorted(
                self._sessions.values(),
                key=lambda item: item["last_active"],
                reverse=True,
            )
            return [self._public(item) for item in sessions]

    def get(self, session_id: str, *, touch: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError(session_id)
            if touch:
                session["last_active"] = time.time()
            return self._public(session, include_capability=True)

    def heartbeat(self, session_id: str) -> dict[str, Any]:
        return self.get(session_id, touch=True)

    def metrics(
        self, session_id: str, after_id: int = 0, limit: int = 5000
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError(session_id)
            rows = [row for row in session["samples"] if row["id"] > after_id]
            rows = rows[: max(1, min(int(limit), 20000))]
            last_id = rows[-1]["id"] if rows else after_id
        return {"metrics": copy.deepcopy(rows), "last_id": last_id}

    def capability(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError(session_id)
            return copy.deepcopy(session["capability"])

    def acquire(self, session_id: str) -> SSHClient:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError(session_id)
            session["busy_count"] += 1
            session["last_active"] = time.time()
            return session["ssh"]

    def release(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            session["busy_count"] = max(0, int(session["busy_count"]) - 1)
            session["last_active"] = time.time()

    def close(
        self, session_id: str, *, force: bool = False, reason: str = "用户关闭连接"
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError(session_id)
            if session["busy_count"] and not force:
                raise RuntimeError("该会话仍在执行压测，请先停止压测任务")
            self._sessions.pop(session_id, None)
            public = self._public(session, include_capability=True)
        monitor = session.get("monitor")
        if monitor:
            try:
                monitor.stop_perf()
                monitor.stop_logs()
            except Exception as exc:
                log.warning(f"停止服务器会话监控失败：{session_id} - {exc}")
        try:
            session["ssh"].disconnect()
        finally:
            shutil.rmtree(session["temp_dir"], ignore_errors=True)
        public.update({"status": "closed", "message": reason})
        return public

    def _reap_loop(self) -> None:
        while not self._stop.wait(self.reap_interval):
            now = time.time()
            with self._lock:
                expired = [
                    session_id
                    for session_id, item in self._sessions.items()
                    if not item["busy_count"]
                    and now - float(item["last_active"]) >= self.idle_timeout
                ]
            for session_id in expired:
                try:
                    self.close(session_id, force=True, reason="会话空闲超时，已自动断开")
                    log.info(f"服务器会话空闲回收：{session_id}")
                except KeyError:
                    pass

    def _public(
        self, session: dict[str, Any], *, include_capability: bool = False
    ) -> dict[str, Any]:
        now = time.time()
        result = {
            key: session.get(key)
            for key in (
                "id",
                "profile_id",
                "project_id",
                "name",
                "host",
                "port",
                "user",
                "status",
                "message",
                "created_at",
                "last_active",
                "last_sample_at",
                "busy_count",
            )
        }
        result["sample_count"] = len(session.get("samples") or [])
        result["idle_timeout_seconds"] = int(self.idle_timeout)
        result["idle_seconds_remaining"] = max(
            0, int(self.idle_timeout - (now - float(session["last_active"])))
        )
        if include_capability:
            result["capability"] = copy.deepcopy(session.get("capability") or {})
        return result
