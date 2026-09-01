"""
CPU 压测模块 — 通过 SSH 在远程服务器上安装/运行 stress-ng，自动收集结果。
支持同步模式（阻塞等待）和后台模式（与业务并行）。
压测结束后自动检测：温度阈值、热降频、硬件错误、stress-ng 报错。
"""

import csv
import re
import time
from pathlib import Path

from auto_test.common.logging import log
from auto_test.monitoring.cpu_temperature import CPU_TEMPERATURE_COMMAND, parse_cpu_temperature

_OUTPUT_PATH = "/tmp/stress_ng_output.txt"

# 默认安全阈值
TEMP_WARN_C = 85      # CPU 温度超过此值警告
TEMP_CRIT_C = 95      # CPU 温度超过此值危险


def _parse_bogo(output):
    """从 stress-ng --metrics-brief 输出中提取 bogo ops"""
    for line in output.split("\n"):
        m = re.match(r"cpu\s+[\d,.]+\s+([\d.]+)\s+([\d.]+)", line.strip())
        if m:
            return {"bogo_ops_real": float(m.group(1)), "bogo_ops_usr": float(m.group(2))}
    return {"bogo_ops_real": 0, "bogo_ops_usr": 0}


class CpuStressRunner:
    """在远程服务器上运行 stress-ng 并管理其生命周期"""

    def __init__(self, ssh):
        self.ssh = ssh
        self._installed = None
        self._install_error = ""
        self._start_time = None
        self._end_time = None
        self._output = ""
        self._run_dir = None
        self._duration = 0
        self._workers = 0
        self._cpu_load = 100
        self._health = {}

    def _stress_cfg(self):
        try:
            from auto_test.common.config_loader import stress_cfg

            cfg = stress_cfg()
            return cfg if isinstance(cfg, dict) else {}
        except Exception as exc:
            log.warning(f"读取 stress 配置失败，使用默认安装策略：{exc}")
            return {}

    def _remote_env_prefix(self):
        cfg = self._stress_cfg()
        return str(cfg.get("remote_env") or cfg.get("install_env") or "").strip()

    def _wrap_remote_cmd(self, cmd):
        env_prefix = self._remote_env_prefix()
        if not env_prefix:
            return cmd
        return f"{env_prefix}; {cmd}"

    # ---- 安装 ----

    def install(self, allow_install=None):
        """检测 stress-ng；仅在显式允许时尝试安装。

        驱动、运行时和系统软件属于运维基线。默认只读探测可以避免测试
        平台在未知服务器上修改软件源或安装包。存量环境如确有需要，可在
        ``stress.auto_install`` 中显式开启旧的自动安装行为。
        """
        self._install_error = ""
        # 1. 已安装的 stress-ng 允许普通账号直接使用。
        out, _ = self.ssh.exec(
            self._wrap_remote_cmd("command -v stress-ng 2>/dev/null || which stress-ng 2>/dev/null || echo NOT_FOUND")
        )
        if "NOT_FOUND" not in out:
            log.info(f"stress-ng 已安装：{out.strip()}")
            self._installed = True
            return True

        cfg = self._stress_cfg()
        if allow_install is None:
            allow_install = bool(cfg.get("auto_install", False))
        if not allow_install:
            self._install_error = "未找到 stress-ng；平台按只读策略不会自动安装系统软件"
            log.warning(self._install_error)
            self._installed = False
            return False

        # 2. 只有显式开启安装时才检查 root 权限。
        out, _ = self.ssh.exec("id -u 2>/dev/null")
        try:
            uid = int(out.strip())
            if uid != 0:
                log.error("安装 stress-ng 需要 root 权限；也可以由运维提前安装后使用普通账号执行")
                self._installed = False
                return False
        except Exception:
            log.warning("无法获取用户 UID，跳过 root 校验，继续尝试安装")

        log.info("stress-ng 未安装，开始自动适配系统安装...")
        custom_install = str(cfg.get("install_command") or "").strip()
        if custom_install:
            log.info("检测到 stress.install_command，优先按配置执行 stress-ng 安装")
            install_out, install_err = self.ssh.exec(self._wrap_remote_cmd(custom_install), timeout=600)
            self._install_error = (install_out + "\n" + install_err).strip()
            check_out, _ = self.ssh.exec(
                self._wrap_remote_cmd("command -v stress-ng 2>/dev/null || which stress-ng 2>/dev/null || echo NOT_FOUND")
            )
            if "NOT_FOUND" not in check_out:
                log.info(f"stress-ng 自定义安装成功：{check_out.strip()}")
                self._installed = True
                return True
            log.warning(f"stress.install_command 执行后仍未找到 stress-ng：{self._install_error[:1000]}")

        # 3. 识别操作系统发行版
        os_out, _ = self.ssh.exec(". /etc/os-release 2>/dev/null; echo ${ID:-UNKNOWN}")
        os_id = os_out.strip().lower()

        # YUM系列：CentOS/RHEL/Rocky/AlmaLinux/Amazon Linux
        if os_id in ["centos", "rhel", "rocky", "almalinux", "amzn"]:
            log.info("识别YUM系系统，先安装EPEL源再安装stress-ng")
            self.ssh.exec(self._wrap_remote_cmd("yum install -y epel-release"), timeout=300)
            install_out, install_err = self.ssh.exec(self._wrap_remote_cmd("yum install -y stress-ng"), timeout=600)
            self._install_error = (install_out + "\n" + install_err).strip()
            log.info(f"yum安装输出：\n{install_out}")

        # openEuler 专用 dnf 安装，完全避开 yum
        elif os_id == "openeuler":
            log.info("识别 openEuler 系统，使用 dnf 安装 stress-ng，跳过yum")
            check_out, check_err = self.ssh.exec(self._wrap_remote_cmd("dnf --version"), timeout=60)
            dnf_check = (check_out + "\n" + check_err).strip()
            if "GLIBCXX_" in dnf_check:
                self._install_error = dnf_check
                log.error(f"dnf/yum 运行环境异常，无法自动安装 stress-ng：{self._install_error}")
                self._installed = False
                return False
            # 先清理缓存重建源
            self.ssh.exec(self._wrap_remote_cmd("dnf clean all"), timeout=300)
            self.ssh.exec(self._wrap_remote_cmd("dnf makecache"), timeout=600)
            install_out, install_err = self.ssh.exec(self._wrap_remote_cmd("dnf install -y stress-ng"), timeout=600)
            self._install_error = (install_out + "\n" + install_err).strip()
            log.info(f"dnf安装输出：\n{install_out}")

        # APT系列：Ubuntu/Debian
        elif os_id in ["ubuntu", "debian"]:
            log.info("识别APT系系统，更新软件源后安装stress-ng")
            self.ssh.exec(self._wrap_remote_cmd("apt-get update -qq"), timeout=600)
            install_out, install_err = self.ssh.exec(self._wrap_remote_cmd("apt-get install -y -qq stress-ng"), timeout=600)
            self._install_error = (install_out + "\n" + install_err).strip()
            log.info(f"apt安装输出：\n{install_out}")

        else:
            self._install_error = f"暂不支持自动安装当前系统(ID:{os_id})"
            log.error(f"{self._install_error}，请手动安装stress-ng")
            self._installed = False
            return False

        # 4. 安装完成后二次校验是否可用
        check_out, _ = self.ssh.exec(
            self._wrap_remote_cmd("command -v stress-ng 2>/dev/null || which stress-ng 2>/dev/null || echo NOT_FOUND")
        )
        if "NOT_FOUND" not in check_out:
            log.info(f"stress-ng 安装成功：{check_out.strip()}")
            self._installed = True
            return True

        # 走到这里=安装失败
        log.error(f"stress-ng 自动安装失败，请检查：1.服务器外网/软件源 2.系统权限。输出：{self._install_error[:1000]}")
        self._installed = False
        return False

    def install_error(self):
        return self._install_error

    def install_diagnosis(self):
        err = self._install_error or ""
        recommendations = []
        if "GLIBCXX_" in err or "libstdc++" in err:
            recommendations.append(
                "包管理器 dnf/yum 在当前 SSH 非交互环境中被 libstdc++/GLIBCXX 版本冲突拦截，需先修复目标机 libstdc++，或在 stress.remote_env 中加载你手动安装时使用的环境。"
            )
        if "Could not resolve" in err or "Name or service not known" in err:
            recommendations.append("目标机软件源或 DNS 不可用，需检查网络、DNS 或配置离线源。")
        if "No match for argument" in err or "Unable to locate package" in err:
            recommendations.append("当前软件源未提供 stress-ng，需要启用 EPEL/对应仓库，或配置 stress.install_command 使用离线包安装。")
        if "Permission denied" in err or "not root" in err:
            recommendations.append("自动安装需要 root 权限；请使用 root 账号或提前手动安装 stress-ng。")
        if not recommendations:
            recommendations.append("请检查目标机包管理器、软件源、网络和权限；也可以把手动成功的安装命令配置到 stress.install_command。")
        return {
            "error": err[-4000:],
            "recommendations": recommendations,
        }

    # ---- 执行 ----

    def run_sync(self, workers=0, cpu_load=100, duration=300, run_dir=None):
        """同步执行压测，实时监控温度，超温自动终止"""
        if not self._installed:
            log.error("stress-ng 未安装，无法启动压测")
            return False

        self._run_dir = Path(run_dir) if run_dir else None
        self._duration = duration
        self._workers = workers
        self._cpu_load = cpu_load
        self._start_time = time.time()

        # 后台启动 stress-ng，记录主进程 PID
        cmd = (
            f"nohup stress-ng --cpu {workers} --cpu-load {cpu_load} "
            f"--timeout {duration}s --metrics-brief > {_OUTPUT_PATH} 2>&1 & echo $!"
        )
        log.info(f"开始 CPU 压测：workers={workers} cores load={cpu_load}% duration={duration}s")
        out, _ = self.ssh.exec(self._wrap_remote_cmd(cmd))
        time.sleep(1)

        pid = out.strip().split("\n")[-1].strip()
        if not pid or not pid.isdigit():
            log.error("stress-ng 启动失败")
            return False
        self._stress_pid = pid
        log.info(f"stress-ng 已启动，PID：{pid}，实时温度监控中...")

        # 轮询：检查主进程存活 + 温度
        stopped_by_temp = False
        interval = 5
        while True:
            # 用 kill -0 精确检查主进程
            out, _ = self.ssh.exec(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD")
            if "DEAD" in out:
                break

            # 超时兜底
            if time.time() - self._start_time > duration + 30:
                log.warning("压测超时，强制结束")
                self.ssh.exec(f"kill -9 {pid} 2>/dev/null")
                break

            # 读温度
            temp = self._read_cpu_temp()
            if temp is not None and temp >= TEMP_CRIT_C:
                log.warning(f"CPU 温度 {temp}°C 超过临界值 {TEMP_CRIT_C}°C，立即终止压测！")
                self.ssh.exec(f"kill -9 {pid} 2>/dev/null")
                stopped_by_temp = True
                break

            time.sleep(interval)

        self._end_time = time.time()
        self._output, _ = self.ssh.exec(f"cat {_OUTPUT_PATH} 2>/dev/null")
        if stopped_by_temp:
            log.warning(f"压测因温度过高提前终止，实际运行 {self.elapsed():.0f}s")
        else:
            log.info(f"CPU 压测完成，耗时 {self.elapsed():.0f}s")
        return True

    def _read_cpu_temp(self):
        """通过 SSH 读取远程 CPU 温度，失败返回 None"""
        out, _ = self.ssh.exec(CPU_TEMPERATURE_COMMAND)
        return parse_cpu_temperature(out).get("value")

    def start_background(self, workers=0, cpu_load=100, duration=300, run_dir=None):
        """在远程后台启动 stress-ng，立即返回"""
        if not self._installed:
            log.error("stress-ng 未安装，无法启动压测")
            return False

        self._run_dir = Path(run_dir) if run_dir else None
        self._duration = duration
        self._workers = workers
        self._cpu_load = cpu_load
        self._start_time = time.time()

        cmd = (
            f"nohup stress-ng --cpu {workers} --cpu-load {cpu_load} "
            f"--timeout {duration}s --metrics-brief > {_OUTPUT_PATH} 2>&1 & echo $!"
        )
        log.info(f"后台启动 CPU 压测：workers={workers} cores load={cpu_load}% duration={duration}s")
        out, _ = self.ssh.exec(self._wrap_remote_cmd(cmd))
        time.sleep(0.5)

        pid = out.strip().split("\n")[-1].strip()
        if not pid or not pid.isdigit():
            log.error("stress-ng 启动失败")
            return False
        self._stress_pid = pid
        log.info(f"stress-ng 已在后台运行，PID：{pid}")
        return True

    def is_running(self):
        pid = getattr(self, "_stress_pid", None)
        if not pid:
            return False
        out, _ = self.ssh.exec(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD")
        return "ALIVE" in out

    def stop(self):
        """Best-effort termination of the exact stress-ng process started by this runner."""
        pid = getattr(self, "_stress_pid", None)
        if not pid:
            return
        self.ssh.exec(f"kill -TERM {pid} 2>/dev/null || true")

    def wait_and_collect(self, stop_callback=None):
        """等待后台压测完成，收集结果"""
        if self._start_time is None:
            return

        elapsed = time.time() - self._start_time
        remain = self._duration - elapsed
        if remain > 0:
            log.info(f"等待压测完成（剩余约 {remain:.0f}s）...")
            while self.is_running():
                if stop_callback:
                    try:
                        stop_callback()
                    except InterruptedError:
                        pid = getattr(self, "_stress_pid", None)
                        if pid:
                            self.ssh.exec(f"kill -TERM {pid} 2>/dev/null || true")
                        raise
                time.sleep(5)
                if time.time() - self._start_time > self._duration + 30:
                    log.warning("压测超时，强制结束")
                    pid = getattr(self, "_stress_pid", None)
                    if pid:
                        self.ssh.exec(f"kill -9 {pid} 2>/dev/null")
                    break

        self._end_time = time.time()
        self._output, _ = self.ssh.exec(f"cat {_OUTPUT_PATH} 2>/dev/null")
        log.info("CPU 压测完成")

    # ---- 健康检查 ----

    def check_health(self, perf_csv=None):
        """
        压测后健康检查，返回 dict：
          temp_max, temp_avg, temp_status: ok/warn/crit
          throttled: bool, throttle_detail: str
          ng_errors: list[str], ng_failed: bool
          dmesg_errors: list[str], dmesg_failed: bool
          overall: ok/warn/crit
        """
        h = {
            "temp_max": 0, "temp_avg": 0, "temp_status": "ok",
            "throttled": False, "throttle_detail": "",
            "ng_errors": [], "ng_failed": False,
            "dmesg_errors": [], "dmesg_failed": False,
            "overall": "ok",
        }

        # 1. 温度分析（从 perf CSV）
        if perf_csv:
            h.update(self._check_temp(perf_csv))

        # 2. 热降频检测
        h.update(self._check_throttle())

        # 3. stress-ng 输出错误
        h.update(self._check_ng_errors())

        # 4. 系统硬件错误
        h.update(self._check_dmesg())

        # 综合判定
        statuses = [h["temp_status"]]
        if h["throttled"]:
            statuses.append("warn")
        if h["ng_failed"]:
            statuses.append("crit")
        if h["dmesg_failed"]:
            statuses.append("crit")
        if "crit" in statuses:
            h["overall"] = "crit"
        elif "warn" in statuses:
            h["overall"] = "warn"
        else:
            h["overall"] = "ok"

        self._health = h
        return h

    def _check_temp(self, perf_csv):
        """从 perf CSV 读取 CPU 温度列，返回 {temp_max, temp_avg, temp_status}"""
        temps = []
        try:
            with open(perf_csv, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    v = row.get("cpu_temp(°C)", row.get("cpu_temp_c", ""))
                    if v:
                        try:
                            temps.append(float(v))
                        except ValueError:
                            pass
        except Exception as e:
            log.warning(f"读取温度数据失败：{e}")
            return {"temp_max": 0, "temp_avg": 0, "temp_status": "ok"}

        if not temps:
            return {"temp_max": 0, "temp_avg": 0, "temp_status": "ok"}

        tmax = round(max(temps), 1)
        tavg = round(sum(temps) / len(temps), 1)
        if tmax >= TEMP_CRIT_C:
            status = "crit"
            log.warning(f"CPU 温度达到临界值：最高 {tmax}°C（阈值 {TEMP_CRIT_C}°C）")
        elif tmax >= TEMP_WARN_C:
            status = "warn"
            log.warning(f"CPU 温度偏高：最高 {tmax}°C（警告阈值 {TEMP_WARN_C}°C）")
        else:
            status = "ok"
        return {"temp_max": tmax, "temp_avg": tavg, "temp_status": status}

    def _check_throttle(self):
        """检测是否发生过热降频"""
        result = {"throttled": False, "throttle_detail": ""}
        details = []

        # 检查 sysfs thermal_throttle
        out, _ = self.ssh.exec(
            "cat /sys/devices/system/cpu/cpu*/thermal_throttle/* 2>/dev/null || echo NO_SYSFS"
        )
        if "NO_SYSFS" not in out and out.strip():
            for line in out.strip().split("\n"):
                if line.strip() not in ("0", ""):
                    details.append(f"sysfs thermal_throttle: {line.strip()}")
                    result["throttled"] = True

        # 检查 dmesg 降频相关
        out, _ = self.ssh.exec(
            "dmesg 2>/dev/null | grep -iE 'throttl|thermal|temperature|cpu clock' | tail -20 || echo NO_DMESG"
        )
        if "NO_DMESG" not in out and out.strip():
            for line in out.strip().split("\n"):
                line = line.strip()
                if line and any(kw in line.lower() for kw in
                               ("throttl", "temperature above", "package temperature",
                                "thermal event", "cpu clock throttled")):
                    details.append(f"dmesg: {line}")
                    result["throttled"] = True

        if result["throttled"]:
            log.warning(f"检测到 CPU 热降频：{len(details)} 条记录")
        result["throttle_detail"] = "\n".join(details) if details else "无"
        return result

    def _check_ng_errors(self):
        """检查 stress-ng 输出是否有 FAILED"""
        result = {"ng_errors": [], "ng_failed": False}
        if not self._output:
            result["ng_errors"] = ["(stress-ng 无输出)"]
            result["ng_failed"] = True
            return result

        errors = []
        for line in self._output.split("\n"):
            if "FAILED" in line or "FAIL" in line:
                errors.append(line.strip())
                result["ng_failed"] = True
        if result["ng_failed"]:
            log.warning(f"stress-ng 检测到错误：{len(errors)} 条")
        result["ng_errors"] = errors if errors else []
        return result

    def _check_dmesg(self):
        """检查系统日志中的硬件错误"""
        result = {"dmesg_errors": [], "dmesg_failed": False}
        out, _ = self.ssh.exec(
            "dmesg 2>/dev/null | grep -iE 'hardware error|mce|machine.check|panic|oops|segfault' "
            "| grep -v 'MCE: In-kernel' | tail -20 || echo NO_DMESG"
        )
        if "NO_DMESG" in out or not out.strip():
            return result

        errors = []
        for line in out.strip().split("\n"):
            line = line.strip()
            if line:
                errors.append(line)
                result["dmesg_failed"] = True
        if result["dmesg_failed"]:
            log.warning(f"dmesg 检测到硬件/内核错误：{len(errors)} 条")
        result["dmesg_errors"] = errors
        return result

    # ---- 输出 ----

    def save_results(self):
        """保存所有压测结果到文件（run_sync / wait_and_collect 之后调用）"""
        if not self._run_dir:
            return
        report_dir = self._run_dir / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        # 原始输出
        fpath = report_dir / "stress_ng_output.txt"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"stress-ng 输出 — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"耗时：{self.elapsed():.0f}s\n")
            f.write("=" * 60 + "\n")
            f.write(self._output)
        log.info(f"压测原始输出已保存：{fpath}")

        # 汇总（含健康检查）
        s = self.summary()
        spath = report_dir / "stress_summary.txt"
        with open(spath, "w", encoding="utf-8") as f:
            f.write(f"CPU 压测汇总 — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 50 + "\n")
            f.write(f"压测参数\n")
            f.write(f"  工作进程数：{self._workers}\n")
            f.write(f"  目标负载：{self._cpu_load}%\n")
            f.write(f"  压测时长：{s['elapsed_s']}s\n")
            f.write(f"  bogo ops (real)：{s['bogo_ops_real']}\n")
            f.write(f"  bogo ops (usr)：{s['bogo_ops_usr']}\n")
            f.write("\n健康检查\n")
            f.write("-" * 50 + "\n")
            self._write_health_section(f)
        log.info(f"压测汇总已保存：{spath}")

    def _write_health_section(self, f):
        h = self._health
        if not h:
            f.write("  (未执行健康检查)\n")
            return

        # 综合判定
        label = {"ok": "✅ 正常", "warn": "⚠️ 警告", "crit": "❌ 危险"}
        f.write(f"  综合判定：{label.get(h['overall'], h['overall'])}\n\n")

        # 温度
        if h.get("temp_max"):
            f.write(f"  CPU 温度：最高 {h['temp_max']}°C / 均值 {h['temp_avg']}°C\n")
            f.write(f"  温度状态：{label.get(h['temp_status'], h['temp_status'])}"
                    f"（警告阈值 {TEMP_WARN_C}°C / 临界阈值 {TEMP_CRIT_C}°C）\n")
        else:
            f.write(f"  CPU 温度：无数据（可能为虚拟机或未安装 sensors）\n")

        # 降频
        f.write(f"\n  热降频：{'⚠️ 是' if h.get('throttled') else '✅ 否'}\n")
        if h.get("throttle_detail") and h["throttle_detail"] != "无":
            f.write(f"  降频记录：\n")
            for line in h["throttle_detail"].split("\n"):
                f.write(f"    {line}\n")

        # stress-ng 错误
        f.write(f"\n  stress-ng 错误：{'❌ 有' if h.get('ng_failed') else '✅ 无'}\n")
        if h.get("ng_errors"):
            for err in h["ng_errors"]:
                f.write(f"    {err}\n")

        # dmesg 错误
        f.write(f"\n  系统硬件错误：{'❌ 有' if h.get('dmesg_failed') else '✅ 无'}\n")
        if h.get("dmesg_errors"):
            for err in h["dmesg_errors"]:
                f.write(f"    {err}\n")

    # ---- 查询方法 ----

    def elapsed(self):
        if self._start_time is None:
            return 0
        end = self._end_time or time.time()
        return end - self._start_time

    def bogo_ops(self):
        return _parse_bogo(self._output)

    def summary(self):
        bogo = self.bogo_ops()
        return {
            "elapsed_s": round(self.elapsed(), 1),
            "bogo_ops_real": bogo.get("bogo_ops_real", 0),
            "bogo_ops_usr": bogo.get("bogo_ops_usr", 0),
        }
