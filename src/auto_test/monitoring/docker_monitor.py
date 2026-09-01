"""
Docker 容器日志监控 + 服务器性能采集（支持 APP + GPU 双机）。
"""

import re
import csv
import time
import queue
import threading
from pathlib import Path

from auto_test.common.logging import log
from auto_test.common.paths import RUNS_DIR
from auto_test.monitoring.cpu_temperature import CPU_TEMPERATURE_COMMAND, parse_cpu_temperature

_ERROR_PATTERN = re.compile(r"\b(ERROR|Exception|FATAL|Traceback|panic|fatal)\b", re.IGNORECASE)
_LOG_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
_GPU_EXTENDED_QUERY = (
    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit,clocks.sm,fan.speed,pstate "
    "--format=csv,noheader,nounits"
)
_GPU_BASE_QUERY = (
    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader,nounits"
)


class DockerMonitor:
    def __init__(self, ssh, gpu_ssh, containers, enable_log=True, enable_perf=False,
                 run_dir=None, perf_prefix="", sample_callback=None):
        self.ssh = ssh
        # 业务服务器与 GPU 服务器通常是同一台。调用方没有提供独立
        # GPU 连接时，必须复用业务服务器连接，不能启动一个空采集线程。
        self.gpu_ssh = gpu_ssh or ssh
        self.containers = [c.strip() for c in containers.split(",") if c.strip()] if containers else []
        self.enable_log = enable_log
        self.enable_perf = enable_perf
        self._running = False
        self._threads = []
        self._error_queue = queue.Queue()
        self._perf_app = []
        self._perf_gpu = []
        self._gpu_processes = {}
        self._gpu_separate = self.gpu_ssh is not self.ssh  # 是否两台不同机器
        self._run_dir = Path(run_dir) if run_dir else None
        self._host_time = ""
        self._perf_prefix = perf_prefix
        self._sample_callback = sample_callback

    def start(self):
        if not self.containers and not self.enable_perf:
            return
        self._running = True
        if self._run_dir is None:
            self._run_dir = RUNS_DIR / time.strftime("%Y%m%d_%H%M%S")

        # 记录主机时间，用于与容器日志时间对比（容器可能用 UTC）
        if self.enable_log:
            try:
                self._host_time = self.ssh.exec("date '+%Y-%m-%d %H:%M:%S %Z'")[0].strip()
            except Exception:
                self._host_time = ""
        self._run_dir.mkdir(parents=True, exist_ok=True)

        if self.enable_log:
            valid = []
            out, _ = self.ssh.exec("docker ps --format '{{.Names}}'")
            running_names = [name.strip() for name in out.splitlines() if name.strip()]
            for c in self.containers:
                matches = [name for name in running_names if name == c]
                if not matches:
                    matches = [name for name in running_names if name.endswith(f"-{c}")]
                if matches:
                    cname = sorted(matches, key=lambda name: (len(name), name))[0]
                    valid.append(cname)
                    if len(matches) > 1:
                        log.warning(f"容器配置 {c} 匹配到多个运行实例，使用 {cname}")
                else:
                    log.warning(f"容器 {c} 不存在或未运行，跳过日志监控")
            self.containers = valid
            if valid:
                t = threading.Thread(target=self._poll_logs, daemon=True)
                t.start()
                self._threads.append(t)
                log.info(f"日志监控已启动（轮询模式），监控容器：{valid}")

        if self.enable_perf:
            # APP 服务器：CPU/内存/温度/磁盘（单机时含 GPU）
            t = threading.Thread(target=self._collect_perf,
                                 args=(self.ssh, self._perf_app, not self._gpu_separate, "APP"),
                                 daemon=True)
            t.start()
            self._threads.append(t)
            if self._gpu_separate:
                # GPU 服务器：全部指标（含 GPU）
                t2 = threading.Thread(target=self._collect_perf,
                                      args=(self.gpu_ssh, self._perf_gpu, True, "GPU"),
                                      daemon=True)
                t2.start()
                self._threads.append(t2)
                log.info("性能采集已启动（APP + GPU 双机）")
            else:
                # 同一台：把 GPU 指标合到 APP 里
                self._perf_gpu = self._perf_app
                log.info("性能采集已启动（单机，含 GPU）")

    def get_gpu_processes(self):
        """返回最新的 GPU 进程信息"""
        return self._gpu_processes

    def get_perf_summary(self):
        """返回 {'app': {...}, 'gpu': {...}}"""
        result = {}
        if self._perf_app:
            result["app"] = self._summary_from(self._perf_app, has_gpu=not self._gpu_separate)
        if self._gpu_separate and self._perf_gpu:
            result["gpu"] = self._summary_from(self._perf_gpu, has_gpu=True)
        return result

    def _summary_from(self, samples, has_gpu):
        def avg(key):
            vals = []
            for s in samples:
                v = s.get(key, "")
                try:
                    vals.append(float(v))
                except (ValueError, TypeError):
                    pass
            return round(sum(vals) / len(vals), 1) if vals else 0

        def latest(key, default=""):
            for sample in reversed(samples):
                value = sample.get(key)
                if value not in (None, ""):
                    return value
            return default

        s = {
            "cpu_pct_avg": avg("cpu_pct"),
            "cpu_temp_avg": avg("cpu_temp_c"),
            "cpu_temp_source": latest("cpu_temp_source", "unavailable"),
            "cpu_temp_label": latest("cpu_temp_label", "未发现可信 CPU 温度传感器"),
            "cpu_temp_details": latest("cpu_temp_details"),
            "cpu_temp_readings": latest("cpu_temp_readings", []),
            "mem_used_gb_avg": avg("mem_used_gb"),
            "mem_total_gb": samples[-1].get("mem_total_gb", 0) if samples else 0,
            "mem_avail_gb": samples[-1].get("mem_avail_gb", 0) if samples else 0,
            "disk_util_avg": avg("disk_util_pct"),
        }
        if has_gpu:
            s["gpu_pct_avg"] = avg("gpu_pct")
            s["gpu_temp_avg"] = avg("gpu_temp_c")
            s["gpu_memory_pct_avg"] = avg("gpu_mem_pct")
            s["gpu_power_w_avg"] = avg("gpu_power_w")
            s["gpu_clock_mhz_avg"] = avg("gpu_sm_clock_mhz")
            s["gpu_fan_pct_avg"] = avg("gpu_fan_pct")
            s["gpu_sampling_status"] = latest("gpu_sampling_status", "unavailable")
            s["gpu_sampling_message"] = latest("gpu_sampling_message")
            gpu_indexes = sorted({
                int(match.group(1))
                for sample in samples
                for key in sample
                if (match := re.fullmatch(r"gpu(\d+)_pct", key))
            })
            for i in gpu_indexes:
                s[f"gpu{i}_name"] = latest(f"gpu{i}_name", f"GPU {i}")
                s[f"gpu{i}_pct_avg"] = avg(f"gpu{i}_pct")
                s[f"gpu{i}_temp_avg"] = avg(f"gpu{i}_temp_c")
                s[f"gpu{i}_memory_pct_avg"] = avg(f"gpu{i}_mem_pct")
                s[f"gpu{i}_power_w_avg"] = avg(f"gpu{i}_power_w")
                s[f"gpu{i}_clock_mhz_avg"] = avg(f"gpu{i}_sm_clock_mhz")
                s[f"gpu{i}_fan_pct_avg"] = avg(f"gpu{i}_fan_pct")
                s[f"gpu{i}_pstate"] = latest(f"gpu{i}_pstate")
        return s

    def stop_perf(self):
        self._running = False
        time.sleep(1)
        if self.enable_perf:
            pf = self._perf_prefix
            if self._perf_app:
                self._write_perf_csv(self._perf_app, f"{pf}perf_app.csv", has_gpu=not self._gpu_separate)
                self._write_perf_summary(self._perf_app, f"{pf}perf_app_summary.csv", has_gpu=not self._gpu_separate)
            if self._gpu_separate and self._perf_gpu:
                self._write_perf_csv(self._perf_gpu, f"{pf}perf_gpu.csv", has_gpu=True)
                self._write_perf_summary(self._perf_gpu, f"{pf}perf_gpu_summary.csv", has_gpu=True)

    def stop_logs(self):
        for t in self._threads:
            t.join(timeout=10)
        if self.enable_log:
            return self._write_error_logs()
        return []

    # ---- 日志轮询 ----

    def _poll_logs(self):
        seen = set()
        while self._running:
            for container in self.containers:
                cname = container
                try:
                    out, _ = self.ssh.exec(f"docker logs --since 10s {cname} 2>&1")
                    lines = out.split("\n")
                    i = 0
                    while i < len(lines):
                        line = lines[i]
                        if _ERROR_PATTERN.search(line):
                            block = [line]
                            j = i + 1
                            while j < len(lines) and not _LOG_TS_PATTERN.match(lines[j]):
                                block.append(lines[j])
                                j += 1
                            key = f"{container}:{line.strip()[-120:]}"
                            if key not in seen:
                                seen.add(key)
                                self._error_queue.put((container, "\n".join(block)))
                            i = j
                        else:
                            i += 1
                    if len(seen) > 5000:
                        seen.clear()
                except Exception:
                    pass
            time.sleep(5)

    # ---- 性能采集 ----

    def _parse_cpu(self, out):
        m = re.search(r"(\d+\.?\d*)\s*id", out)
        return round(100 - float(m.group(1)), 1) if m else ""

    def _parse_mem(self, out):
        """从 free -m 提取 used / total / available (GB)"""
        m = re.search(r"Mem:\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+(\d+)", out)
        if m:
            total = round(int(m.group(1)) / 1024, 1)
            used = round(int(m.group(2)) / 1024, 1)
            avail = round(int(m.group(3)) / 1024, 1)
            return (used, total, avail)
        return ("", "", "")

    @staticmethod
    def _gpu_number(value):
        text = str(value or "").strip()
        normalized = text.upper().replace("[", "").replace("]", "")
        if normalized in {"", "N/A", "NA", "NOT SUPPORTED", "NOT_AVAILABLE"}:
            return None
        for suffix in (" MIB", " MHZ", " W", " %"):
            if text.upper().endswith(suffix):
                text = text[:-len(suffix)].strip()
                break
        try:
            return round(float(text), 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _gpu_text(value):
        text = str(value or "").strip()
        normalized = text.upper().replace("[", "").replace("]", "")
        return None if normalized in {"", "N/A", "NA", "NOT SUPPORTED", "NOT_AVAILABLE"} else text

    def _parse_gpu(self, out):
        """从 nvidia-smi 输出提取总体与逐卡数据；不支持的扩展字段保留为 None。"""
        devices = []
        for position, line in enumerate(out.strip().split("\n")):
            parts = [item.strip() for item in line.split(",")]
            if len(parts) >= 11:
                index_text = parts[0]
                name = ",".join(parts[1:-9]).strip()
                values = parts[-9:]
            elif len(parts) >= 6:
                index_text = parts[0]
                name = ",".join(parts[1:-4]).strip()
                values = parts[-4:] + [None] * 5
            elif len(parts) >= 3:  # 兼容旧采样格式：使用率、显存已用、温度
                index_text = str(position)
                name = f"GPU {position}"
                values = [parts[0], parts[1], None, parts[2]] + [None] * 5
            else:
                continue
            try:
                index = int(index_text)
            except (TypeError, ValueError):
                index = position
            utilization, memory_used, memory_total, temperature, power, power_limit, clock, fan, pstate = values
            device = {
                "index": index,
                "name": self._gpu_text(name) or f"GPU {index}",
                "utilization": self._gpu_number(utilization),
                "memory_used": self._gpu_number(memory_used),
                "memory_total": self._gpu_number(memory_total),
                "temperature": self._gpu_number(temperature),
                "power": self._gpu_number(power),
                "power_limit": self._gpu_number(power_limit),
                "clock": self._gpu_number(clock),
                "fan": self._gpu_number(fan),
                "pstate": self._gpu_text(pstate),
            }
            if any(device[key] is not None for key in ("utilization", "memory_used", "temperature")):
                devices.append(device)
        if not devices:
            return None

        def numbers(key):
            return [device[key] for device in devices if device[key] is not None]

        utilizations = numbers("utilization")
        memory_used = numbers("memory_used")
        memory_total = numbers("memory_total")
        temperatures = numbers("temperature")
        powers = numbers("power")
        power_limits = numbers("power_limit")
        clocks = numbers("clock")
        fans = numbers("fan")
        total_used = round(sum(memory_used), 1) if memory_used else None
        total_capacity = round(sum(memory_total), 1) if memory_total else None
        optional_complete = all(
            device[key] is not None
            for device in devices
            for key in ("power", "power_limit", "clock", "fan", "pstate")
        )
        result = {
            "gpu_count": len(devices),
            "gpu_pct": round(sum(utilizations) / len(utilizations), 1) if utilizations else None,
            "gpu_mem_mb": total_used,
            "gpu_mem_total_mb": total_capacity,
            "gpu_mem_pct": round(total_used / total_capacity * 100, 1) if total_used is not None and total_capacity else None,
            "gpu_temp_c": round(sum(temperatures) / len(temperatures), 1) if temperatures else None,
            "gpu_power_w": round(sum(powers), 1) if powers else None,
            "gpu_power_limit_w": round(sum(power_limits), 1) if power_limits else None,
            "gpu_sm_clock_mhz": round(sum(clocks) / len(clocks), 1) if clocks else None,
            "gpu_fan_pct": round(sum(fans) / len(fans), 1) if fans else None,
            "gpu_sampling_status": "available" if optional_complete else "partial",
            "gpu_sampling_message": (
                "基础与扩展 GPU 指标采集正常"
                if optional_complete
                else "基础 GPU 指标正常，部分扩展字段不受当前 GPU 或驱动支持"
            ),
        }
        for device in devices:
            index = device["index"]
            prefix = f"gpu{index}_"
            result.update({
                prefix + "name": device["name"],
                prefix + "pct": device["utilization"],
                prefix + "mem_mb": device["memory_used"],
                prefix + "mem_total_mb": device["memory_total"],
                prefix + "mem_pct": (
                    round(device["memory_used"] / device["memory_total"] * 100, 1)
                    if device["memory_used"] is not None and device["memory_total"] else None
                ),
                prefix + "temp_c": device["temperature"],
                prefix + "power_w": device["power"],
                prefix + "power_limit_w": device["power_limit"],
                prefix + "sm_clock_mhz": device["clock"],
                prefix + "fan_pct": device["fan"],
                prefix + "pstate": device["pstate"],
            })
        return result

    def _parse_temp(self, out):
        value = parse_cpu_temperature(out).get("value")
        return value if value is not None else ""

    def _parse_disk(self, out):
        m = re.search(r"(\d+\.?\d*)\s*$", out.strip().split("\n")[0] if out else "")
        return m.group(1) if m else ""

    def _collect_perf(self, ssh, samples, include_gpu, label):
        """通用性能采集线程"""
        _logged = set()
        _reconnect_count = 0
        while self._running:
            try:
                s = {"time": time.strftime("%H:%M:%S")}

                out, err = ssh.exec("top -bn1 | grep '%Cpu'")
                s["cpu_pct"] = self._parse_cpu(out)
                if s["cpu_pct"] == "" and f"cpu_{label}" not in _logged:
                    log.warning(f"[{label}] CPU 使用率采集失败")
                    _logged.add(f"cpu_{label}")

                out, err = ssh.exec("free -m | grep Mem")
                s["mem_used_gb"], s["mem_total_gb"], s["mem_avail_gb"] = self._parse_mem(out)

                out, err = ssh.exec(CPU_TEMPERATURE_COMMAND)
                temperature = parse_cpu_temperature(out)
                s["cpu_temp_c"] = temperature["value"] if temperature["value"] is not None else ""
                s["cpu_temp_status"] = temperature["status"]
                s["cpu_temp_source"] = temperature["source"]
                s["cpu_temp_label"] = temperature["label"]
                s["cpu_temp_details"] = temperature["details"]
                s["cpu_temp_readings"] = temperature["readings"]
                if s["cpu_temp_c"] == "":
                    if f"temp_fail_{label}" not in _logged:
                        log.warning(f"[{label}] CPU 温度无法获取：{temperature['details']}")
                        _logged.add(f"temp_fail_{label}")

                out, err = ssh.exec("iostat -x 1 1 2>/dev/null | grep -v '^$' | tail -1")
                s["disk_util_pct"] = self._parse_disk(out)

                if include_gpu:
                    out, err = ssh.exec(_GPU_EXTENDED_QUERY)
                    gpu_data = self._parse_gpu(out)
                    if gpu_data is None:
                        fallback_out, fallback_err = ssh.exec(_GPU_BASE_QUERY)
                        gpu_data = self._parse_gpu(fallback_out)
                        if gpu_data is not None:
                            gpu_data["gpu_sampling_status"] = "partial"
                            gpu_data["gpu_sampling_message"] = "扩展字段查询不可用，已回退到基础 GPU 指标"
                        out, err = fallback_out, fallback_err or err
                    if gpu_data is None:
                        s["gpu_sampling_status"] = "unavailable"
                        s["gpu_sampling_message"] = "nvidia-smi 未返回可解析的 GPU 指标"
                        if f"gpu_{label}" not in _logged:
                            log.warning(f"[{label}] GPU 采集失败，nvidia-smi 返回：{out[:200] or err[:200]}")
                            _logged.add(f"gpu_{label}")
                    else:
                        s.update(gpu_data)

                    pout, _ = ssh.exec("nvidia-smi")
                    if pout.strip():
                        self._gpu_processes[label] = pout.strip()

                samples.append(s)
                if self._sample_callback:
                    try:
                        self._sample_callback(label, dict(s))
                    except Exception as callback_error:
                        log.warning(f"[{label}] 性能采样回调失败：{callback_error}")
                _reconnect_count = 0
            except Exception as e:
                _reconnect_count += 1
                if _reconnect_count > 3:
                    log.warning(f"[{label}] SSH 重连失败超过 3 次，停止性能采集")
                    return
                log.warning(f"[{label}] SSH 连接断开，尝试重连（{_reconnect_count}/3）...")
                try:
                    ssh.connect()
                    _logged.clear()
                except Exception as re:
                    log.warning(f"[{label}] 重连失败：{re}，{5 * _reconnect_count}s 后重试")
                    time.sleep(5 * _reconnect_count)
            time.sleep(5)

    # ---- 文件输出 ----

    def _write_error_logs(self):
        errors_by_container = {}
        while not self._error_queue.empty():
            try:
                c, line = self._error_queue.get_nowait()
                errors_by_container.setdefault(c, []).append(line)
            except queue.Empty:
                break
        err_dir = self._run_dir / "errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for c, lines in errors_by_container.items():
            fpath = err_dir / f"{c}.log"
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(f"容器 {c} 错误日志 — {len(lines)} 条\n")
                if self._host_time:
                    f.write(f"主机时间：{self._host_time}\n")
                    f.write("注意：容器内日志时间戳默认使用 UTC 时区（比北京时间晚 8 小时）\n")
                f.write("=" * 60 + "\n")
                f.writelines(line + "\n" for line in lines)
            log.info(f"错误日志已保存：{fpath}（{len(lines)} 条）")
            files.append(str(fpath))
        return files

    def _write_perf_summary(self, samples, filename, has_gpu):
        report_dir = self._run_dir / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        ps = self._summary_from(samples, has_gpu)
        rows = [["指标", "均值"],
                ["CPU使用率(%)", ps.get("cpu_pct_avg", "")],
                ["CPU温度(°C)", ps.get("cpu_temp_avg", "")],
                ["CPU温度来源", ps.get("cpu_temp_source", "unavailable")],
                ["CPU温度传感器", ps.get("cpu_temp_label", "")],
                ["内存已用(GB)", ps.get("mem_used_gb_avg", "")],
                ["内存总量(GB)", ps.get("mem_total_gb", "")]]
        if has_gpu:
            rows.append(["GPU使用率-均值(%)", ps.get("gpu_pct_avg", "")])
            rows.append(["GPU温度-均值(°C)", ps.get("gpu_temp_avg", "")])
            rows.append(["GPU显存占比-均值(%)", ps.get("gpu_memory_pct_avg", "")])
            rows.append(["GPU功耗-均值(W)", ps.get("gpu_power_w_avg", "")])
            rows.append(["GPU采集状态", ps.get("gpu_sampling_status", "unavailable")])
            rows.append(["GPU采集说明", ps.get("gpu_sampling_message", "")])
            gpu_indexes = sorted({
                int(match.group(1))
                for sample in samples
                for key in sample
                if (match := re.fullmatch(r"gpu(\d+)_pct", key))
            })
            for i in gpu_indexes:
                rows.append([f"GPU{i}-使用率(%)", ps.get(f"gpu{i}_pct_avg", "")])
                rows.append([f"GPU{i}-温度(°C)", ps.get(f"gpu{i}_temp_avg", "")])
                rows.append([f"GPU{i}-显存占比(%)", ps.get(f"gpu{i}_memory_pct_avg", "")])
                rows.append([f"GPU{i}-功耗(W)", ps.get(f"gpu{i}_power_w_avg", "")])
                rows.append([f"GPU{i}-SM频率(MHz)", ps.get(f"gpu{i}_clock_mhz_avg", "")])
                rows.append([f"GPU{i}-风扇(%)", ps.get(f"gpu{i}_fan_pct_avg", "")])
                rows.append([f"GPU{i}-P-State", ps.get(f"gpu{i}_pstate", "")])
        rows.append(["磁盘利用率(%)", ps.get("disk_util_avg", "")])
        fpath = report_dir / filename
        with open(fpath, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        log.info(f"性能均值已保存：{fpath}")

    def _write_perf_csv(self, samples, filename, has_gpu):
        report_dir = self._run_dir / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        gpu_indexes = sorted({
            int(match.group(1))
            for sample in samples
            for key in sample
            if (match := re.fullmatch(r"gpu(\d+)_pct", key))
        })

        cols = ["time", "cpu_pct(%)", "mem_used_gb", "mem_total_gb",
                "cpu_temp(°C)", "cpu_temp_source", "cpu_temp_sensor", "disk_util_pct(%)"]
        if has_gpu:
            cols += [
                "gpu_pct(%)", "gpu_mem_mb", "gpu_mem_total_mb", "gpu_mem_pct(%)", "gpu_temp(°C)",
                "gpu_power_w", "gpu_power_limit_w", "gpu_sm_clock_mhz", "gpu_fan_pct(%)",
                "gpu_sampling_status", "gpu_sampling_message",
            ]
            for i in gpu_indexes:
                cols += [
                    f"gpu{i}_name", f"gpu{i}_pct(%)", f"gpu{i}_mem_mb", f"gpu{i}_mem_total_mb",
                    f"gpu{i}_mem_pct(%)", f"gpu{i}_temp(°C)", f"gpu{i}_power_w",
                    f"gpu{i}_power_limit_w", f"gpu{i}_sm_clock_mhz", f"gpu{i}_fan_pct(%)", f"gpu{i}_pstate",
                ]

        fpath = report_dir / filename
        with open(fpath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            for s in samples:
                row = [s.get("time", ""), s.get("cpu_pct", ""),
                       s.get("mem_used_gb", ""), s.get("mem_total_gb", ""),
                       s.get("cpu_temp_c", ""), s.get("cpu_temp_source", ""),
                       s.get("cpu_temp_label", ""), s.get("disk_util_pct", "")]
                if has_gpu:
                    row += [
                        s.get("gpu_pct", ""), s.get("gpu_mem_mb", ""), s.get("gpu_mem_total_mb", ""),
                        s.get("gpu_mem_pct", ""), s.get("gpu_temp_c", ""), s.get("gpu_power_w", ""),
                        s.get("gpu_power_limit_w", ""), s.get("gpu_sm_clock_mhz", ""), s.get("gpu_fan_pct", ""),
                        s.get("gpu_sampling_status", ""), s.get("gpu_sampling_message", ""),
                    ]
                    for i in gpu_indexes:
                        row += [
                            s.get(f"gpu{i}_name", ""), s.get(f"gpu{i}_pct", ""), s.get(f"gpu{i}_mem_mb", ""),
                            s.get(f"gpu{i}_mem_total_mb", ""), s.get(f"gpu{i}_mem_pct", ""),
                            s.get(f"gpu{i}_temp_c", ""), s.get(f"gpu{i}_power_w", ""),
                            s.get(f"gpu{i}_power_limit_w", ""), s.get(f"gpu{i}_sm_clock_mhz", ""),
                            s.get(f"gpu{i}_fan_pct", ""), s.get(f"gpu{i}_pstate", ""),
                        ]
                writer.writerow(row)
        log.info(f"性能数据已保存：{fpath}")
