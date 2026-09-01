"""
SSH 连接抽象层。
远程模式用 paramiko，本地模式走 subprocess，上层调用无需区分。
"""

import socket
import subprocess
import paramiko

from auto_test.common.logging import log


class SSHClient:
    def __init__(self, host, user, password, port=22):
        self.host = host or ""
        self.user = user or "root"
        self.password = password or ""
        self.port = max(1, min(int(port or 22), 65535))
        self._client = None
        self.last_error = ""

    @property
    def is_local(self):
        """判断是否本地模式：host 为空、localhost 或 127.0.0.1"""
        return not self.host or self.host in ("localhost", "127.0.0.1", "::1")

    @property
    def is_connected(self):
        if self.is_local:
            return True
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active())

    def connect(self):
        self.last_error = ""
        if self.is_local:
            log.info("SSH 模式：本地（无需远程连接）")
            return True
        if self.is_connected:
            return True
        self.disconnect()
        log.info(f"SSH 连接：{self.user}@{self.host}")
        try:
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10,
            )
            self._client.get_transport().set_keepalive(30)
            log.info(f"SSH 连接成功：{self.host}（keepalive 30s）")
            return True
        except paramiko.AuthenticationException:
            self.last_error = "SSH 认证失败，请检查用户名、密码或服务器认证策略"
            log.error(f"SSH 连接失败：{self.host}:{self.port} — 认证失败")
            return False
        except (socket.timeout, TimeoutError):
            self.last_error = "SSH 连接超时，请检查地址、端口、防火墙和网络路由"
            log.error(f"SSH 连接失败：{self.host}:{self.port} — 连接超时")
            return False
        except socket.gaierror:
            self.last_error = "服务器地址无法解析"
            log.error(f"SSH 连接失败：{self.host}:{self.port} — 地址无法解析")
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.error(f"SSH 连接失败：{self.host}:{self.port} — {e}")
            return False

    def disconnect(self):
        if self._client:
            self._client.close()
            self._client = None
            log.info("SSH 已断开")

    def exec(self, cmd, timeout=30):
        """执行一次性命令，返回 (stdout, stderr)"""
        if self.is_local:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return p.stdout, p.stderr
        _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        return stdout.read().decode("utf-8", errors="replace"), stderr.read().decode("utf-8", errors="replace")

    def stream(self, cmd):
        """
        流式执行命令，返回一个生成器，逐行 yield 输出。
        调用方应在独立线程中消费。
        """
        if self.is_local:
            p = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in p.stdout:
                yield line.rstrip("\n")
            p.wait()
        else:
            _, stdout, _ = self._client.exec_command(cmd, timeout=None)
            for line in stdout:
                yield line.rstrip("\n")

    def sftp_put_dir(self, local_dir, remote_dir):
        """递归上传整个目录到远程，返回 (成功文件数, 失败文件列表)"""
        import os
        from pathlib import Path

        sftp = self._client.open_sftp()
        success = 0
        failed = []
        local_dir = Path(local_dir)

        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            self.exec(f"mkdir -p '{remote_dir}'")

        for root, dirs, files in os.walk(local_dir):
            rel_root = Path(root).relative_to(local_dir)
            for dirname in dirs:
                remote_sub = f"{remote_dir}/{rel_root / dirname}".replace("\\", "/")
                try:
                    sftp.stat(remote_sub)
                except FileNotFoundError:
                    self.exec(f"mkdir -p '{remote_sub}'")

            for filename in files:
                local_path = Path(root) / filename
                remote_path = f"{remote_dir}/{rel_root / filename}".replace("\\", "/") if str(rel_root) != "." else f"{remote_dir}/{filename}"
                try:
                    sftp.put(str(local_path), remote_path)
                    # 校验大小
                    remote_size = sftp.stat(remote_path).st_size
                    local_size = local_path.stat().st_size
                    if remote_size == local_size:
                        success += 1
                    else:
                        failed.append(f"{filename}（大小不一致：本地{local_size} 远程{remote_size}）")
                except Exception as e:
                    failed.append(f"{filename}（{e}）")

        sftp.close()
        return success, failed

    def sftp_put_file(self, local_path, remote_path):
        """上传单个文件到远程路径，返回是否成功。"""
        from pathlib import Path

        if self.is_local:
            return False

        local_path = Path(local_path)
        remote_dir = str(remote_path).replace("\\", "/").rsplit("/", 1)[0]
        self.exec(f"mkdir -p '{remote_dir}'")
        sftp = self._client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
            return sftp.stat(remote_path).st_size == local_path.stat().st_size
        finally:
            sftp.close()
