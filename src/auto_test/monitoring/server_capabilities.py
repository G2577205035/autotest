"""Read-only capability discovery for Linux test targets.

The probe deliberately does not install packages, change drivers, or write to the
target.  Its output is used to decide whether a stress mode can run as-is, needs
an explicit fallback, or must stop with a prerequisite list.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any, Iterable

from auto_test.integrations.ssh import SSHClient


DEFAULT_GPU_BURN_IMAGE = "xiaoyi/gpu-burn:cuda11.8-universal"
BLACKWELL_GPU_BURN_IMAGE = "xiaoyi/gpu-burn:cuda12.8-blackwell"


_PROBE_SCRIPT = r"""
emit() {
    key="$1"
    shift
    value=$(printf '%s' "$*" | tr '\r\n\t' '   ' | sed 's/  */ /g; s/^ //; s/ $//')
    printf '%s\t%s\n' "$key" "$value"
}
has() { command -v "$1" >/dev/null 2>&1; }

if [ -r /etc/os-release ]; then
    . /etc/os-release
fi
emit probe_version 5
emit hostname "$(hostname 2>/dev/null || uname -n 2>/dev/null)"
emit os_id "${ID:-unknown}"
emit os_name "${NAME:-unknown}"
emit os_version "${VERSION_ID:-unknown}"
emit os_pretty_name "${PRETTY_NAME:-unknown}"
emit kernel "$(uname -r 2>/dev/null)"
emit architecture "$(uname -m 2>/dev/null)"
emit user "$(id -un 2>/dev/null)"
emit uid "$(id -u 2>/dev/null)"
emit cpu_logical "$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null)"
emit memory_kb "$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null)"
emit tmp_available_kb "$(df -Pk /tmp 2>/dev/null | awk 'NR==2 {print $4; exit}')"
if [ -d /tmp ] && [ -w /tmp ]; then emit tmp_writable yes; else emit tmp_writable no; fi
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then emit cgroup_version v2; elif [ -d /sys/fs/cgroup ]; then emit cgroup_version v1; else emit cgroup_version none; fi
emit glibc_version "$(getconf GNU_LIBC_VERSION 2>/dev/null)"
emit open_files_limit "$(ulimit -n 2>/dev/null)"

package_manager=none
for item in dnf yum apt-get zypper apk; do
    if has "$item"; then package_manager="$item"; break; fi
done
emit package_manager "$package_manager"

container_runtime=none
container_version=
for item in docker podman nerdctl; do
    if has "$item"; then
        container_runtime="$item"
        container_version=$($item --version 2>/dev/null | head -1)
        break
    fi
done
emit container_runtime "$container_runtime"
emit container_version "$container_version"

container_runtimes=
nvidia_container_runtime=no
gpu_burn_container_image="${GPU_BURN_IMAGE:-xiaoyi/gpu-burn:cuda11.8-universal}"
gpu_burn_blackwell_image="${GPU_BURN_BLACKWELL_IMAGE:-xiaoyi/gpu-burn:cuda12.8-blackwell}"
gpu_burn_container_image_ready=no
gpu_burn_blackwell_image_ready=no
if [ "$container_runtime" = docker ]; then
    container_runtimes=$(docker info --format '{{json .Runtimes}}' 2>/dev/null)
    case "$container_runtimes" in *nvidia*) nvidia_container_runtime=yes ;; esac
    if docker image inspect "$gpu_burn_container_image" >/dev/null 2>&1; then
        gpu_burn_container_image_ready=yes
    fi
    if docker image inspect "$gpu_burn_blackwell_image" >/dev/null 2>&1; then
        gpu_burn_blackwell_image_ready=yes
    fi
fi
emit container_runtimes "$container_runtimes"
emit nvidia_container_runtime "$nvidia_container_runtime"
emit gpu_burn_container_image "$gpu_burn_container_image"
emit gpu_burn_container_image_ready "$gpu_burn_container_image_ready"
emit gpu_burn_blackwell_image "$gpu_burn_blackwell_image"
emit gpu_burn_blackwell_image_ready "$gpu_burn_blackwell_image_ready"

for item in python3 stress-ng top free iostat sensors nvidia-smi nvcc make gcc g++ unzip tar nohup setsid dcgmi dcgm-exporter gpu_burn all_reduce_perf; do
    key=$(printf '%s' "$item" | tr '-' '_')
    if has "$item"; then emit "tool_$key" "$(command -v "$item")"; else emit "tool_$key" ""; fi
done

python_version=
stress_ng_version=
if has python3; then python_version=$(python3 --version 2>&1 | head -1); fi
if has stress-ng; then stress_ng_version=$(stress-ng --version 2>&1 | head -1); fi
emit python_version "$python_version"
emit stress_ng_version "$stress_ng_version"

if has sensors && sensors >/dev/null 2>&1; then emit sensors_readable yes; else emit sensors_readable no; fi
if dmesg >/dev/null 2>&1; then emit dmesg_readable yes; else emit dmesg_readable no; fi

gpu_burn_path=$(command -v gpu_burn 2>/dev/null)
if [ -z "$gpu_burn_path" ] && [ -x /tmp/liema_gpu_burn/gpu-burn-master/gpu_burn ]; then
    gpu_burn_path=/tmp/liema_gpu_burn/gpu-burn-master/gpu_burn
fi
emit gpu_burn_path "$gpu_burn_path"
if [ -f /tmp/liema_gpu_burn/gpu-burn-master/Makefile ]; then emit gpu_burn_source_ready yes; else emit gpu_burn_source_ready no; fi

nccl_path=$(command -v all_reduce_perf 2>/dev/null)
if [ -z "$nccl_path" ] && [ -x /opt/nccl-tests/build/all_reduce_perf ]; then nccl_path=/opt/nccl-tests/build/all_reduce_perf; fi
emit nccl_tests_path "$nccl_path"

if has nvidia-smi; then
    gpu_count=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | awk 'NF {count++} END {print count+0}')
    gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    driver_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    cuda_runtime=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([^ |]*\).*/\1/p' | head -1)
    emit nvidia_smi_healthy "$(if [ "${gpu_count:-0}" -gt 0 ] 2>/dev/null; then echo yes; else echo no; fi)"
    emit gpu_count "$gpu_count"
    emit gpu_name "$gpu_name"
    emit driver_version "$driver_version"
    emit cuda_runtime_version "$cuda_runtime"
    gpu_compute_capabilities=$(nvidia-smi --query-gpu=index,compute_cap --format=csv,noheader,nounits 2>/dev/null | awk -F, '{ gsub(/ /, "", $1); gsub(/ /, "", $2); if ($1 != "" && $2 != "") printf "%s%s:%s", separator, $1, $2; separator="," }')
    emit gpu_compute_capabilities "$gpu_compute_capabilities"
    gpu_devices=$(nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,compute_cap --format=csv,noheader,nounits 2>/dev/null | awk -F, '{ for (i=1; i<=7; i++) { gsub(/^[ ]+|[ ]+$/, "", $i) } printf "%s%s|%s|%s|%s|%s|%s|%s", separator, $1, $2, $3, $4, $5, $6, $7; separator=";" }')
    emit gpu_devices "$gpu_devices"
    gpu_busy_count=$(nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | awk -F, '{ used=$2+0; total=$3+0; util=$4+0; memory_limit=total*0.10; if (memory_limit < 1024) memory_limit=1024; if (used >= memory_limit || util >= 10) busy++ } END { print busy+0 }')
    gpu_busy_indexes=$(nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | awk -F, '{ used=$2+0; total=$3+0; util=$4+0; memory_limit=total*0.10; if (memory_limit < 1024) memory_limit=1024; if (used >= memory_limit || util >= 10) { gsub(/ /, "", $1); printf "%s%s", separator, $1; separator="," } }')
    emit gpu_busy_count "$gpu_busy_count"
    emit gpu_busy_indexes "$gpu_busy_indexes"
else
    emit nvidia_smi_healthy no
    emit gpu_count 0
    emit gpu_name ""
    emit driver_version ""
    emit cuda_runtime_version ""
    emit gpu_compute_capabilities ""
    emit gpu_devices ""
    emit gpu_busy_count 0
    emit gpu_busy_indexes ""
fi

nvcc_version=$(nvcc --version 2>/dev/null | sed -n 's/.*release \([^,]*\).*/\1/p' | tail -1)
emit nvcc_version "$nvcc_version"
dcgm_version=$(dcgmi --version 2>/dev/null | head -1)
emit dcgm_version "$dcgm_version"
""".strip()


_TOOL_NAMES = (
    "python3",
    "stress_ng",
    "top",
    "free",
    "iostat",
    "sensors",
    "nvidia_smi",
    "nvcc",
    "make",
    "gcc",
    "g++",
    "unzip",
    "tar",
    "nohup",
    "setsid",
    "dcgmi",
    "dcgm_exporter",
    "gpu_burn",
    "all_reduce_perf",
)

_PRIMARY_OS_RELEASES = {
    "ubuntu": ("20.04", "22.04", "24.04"),
    "openeuler": ("22.03", "24.03"),
}


def _yes(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


class ServerCapabilityProbe:
    """Collect and classify target-server capabilities without changing it."""

    probe_version = 5

    def collect(
        self,
        ssh: SSHClient,
        requested_modes: Iterable[str] | None = None,
        *,
        gpu_burn_source: str | Path | None = None,
        gpu_burn_image: str = DEFAULT_GPU_BURN_IMAGE,
        gpu_burn_blackwell_image: str = BLACKWELL_GPU_BURN_IMAGE,
        gpu_devices: str = "",
    ) -> dict[str, Any]:
        modes = sorted({str(item).strip() for item in (requested_modes or ["monitor"]) if str(item).strip()})
        container_image = str(gpu_burn_image or DEFAULT_GPU_BURN_IMAGE).strip()
        blackwell_image = str(gpu_burn_blackwell_image or BLACKWELL_GPU_BURN_IMAGE).strip()
        selected_gpu_indexes = self.parse_gpu_devices(gpu_devices)
        command = (
            f"GPU_BURN_IMAGE={shlex.quote(container_image)} "
            f"GPU_BURN_BLACKWELL_IMAGE={shlex.quote(blackwell_image)} sh -lc "
            + shlex.quote(_PROBE_SCRIPT)
        )
        stdout, stderr = ssh.exec(command, timeout=60)
        raw = self._parse(stdout)
        if raw.get("probe_version") != str(self.probe_version):
            detail = (stderr or stdout or "目标机没有返回探测结果").strip()[-1000:]
            raise RuntimeError(f"服务器能力探测失败：{detail}")
        return self._build_report(
            raw,
            modes,
            bool(gpu_burn_source and Path(gpu_burn_source).expanduser().is_file()),
            container_image,
            blackwell_image,
            selected_gpu_indexes,
        )

    @staticmethod
    def parse_gpu_devices(value: str | None) -> list[int]:
        text = str(value or "").strip()
        if not text:
            return []
        indexes = []
        for item in text.split(","):
            item = item.strip()
            if not item.isdigit():
                raise ValueError("GPU 卡号格式应为 0 或 0,1")
            indexes.append(int(item))
        return sorted(set(indexes))

    @staticmethod
    def _parse(output: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in (output or "").splitlines():
            key, separator, value = line.partition("\t")
            if separator and key:
                result[key.strip()] = value.strip()
        return result

    def _build_report(
        self,
        raw: dict[str, str],
        modes: list[str],
        local_gpu_burn_source: bool,
        container_image: str,
        blackwell_image: str,
        selected_gpu_indexes: list[int],
    ) -> dict[str, Any]:
        os_id = raw.get("os_id", "unknown").lower()
        os_version = raw.get("os_version", "")
        architecture = raw.get("architecture", "")
        architecture_supported = architecture in {"x86_64", "amd64", "aarch64", "arm64"}
        tmp_writable = _yes(raw.get("tmp_writable"))
        tmp_available_kb = _integer(raw.get("tmp_available_kb"))
        background_available = tools_background = bool(
            raw.get("tool_nohup") or raw.get("tool_setsid")
        )
        tools = {name: bool(raw.get(f"tool_{name}")) for name in _TOOL_NAMES}
        tools["gpu_burn"] = bool(raw.get("gpu_burn_path"))
        tools["nccl_tests"] = bool(raw.get("nccl_tests_path"))

        primary_releases = _PRIMARY_OS_RELEASES.get(os_id, ())
        release_verified = any(os_version.startswith(item) for item in primary_releases)
        if primary_releases and release_verified:
            support_tier = "primary"
            support_label = "主支持"
        elif primary_releases:
            support_tier = "compatible"
            support_label = "版本待验证"
        elif os_id == "centos":
            support_tier = "legacy"
            support_label = "兼容维护"
        elif os_id in {"debian", "rhel", "rocky", "almalinux", "anolis", "kylin"}:
            support_tier = "compatible"
            support_label = "兼容支持"
        else:
            support_tier = "unverified"
            support_label = "待验证"

        warnings: list[str] = []
        if not architecture_supported:
            warnings.append(f"CPU 架构 {architecture or 'unknown'} 尚未纳入支持矩阵")
        if support_tier == "legacy":
            warnings.append("CentOS 仅做存量兼容维护，新增部署优先使用 openEuler 或 Ubuntu LTS")
        elif support_tier == "unverified":
            warnings.append(f"操作系统 {raw.get('os_pretty_name') or os_id} 需要专项验证")
        if primary_releases and not release_verified:
            warnings.append(
                f"{raw.get('os_name') or os_id} {os_version or 'unknown'} 尚未纳入平台主支持版本，"
                f"当前已验证版本：{', '.join(primary_releases)}"
            )
        if not _yes(raw.get("sensors_readable")):
            warnings.append("CPU 温度不可读取；压测仍可执行，但温度保护和报告将缺少该指标")
        if not _yes(raw.get("dmesg_readable")):
            warnings.append("当前账号不能读取 dmesg；硬件/内核错误检查将降级")
        if not tmp_writable:
            warnings.append("/tmp 不可写；CPU/GPU 压测无法创建临时脚本、日志或构建目录")
        elif tmp_available_kb and tmp_available_kb < 512 * 1024:
            warnings.append(f"/tmp 可用空间仅 {tmp_available_kb // 1024} MiB，GPU 源码构建可能失败")
        if not background_available:
            warnings.append("缺少 nohup/setsid，压测进程无法可靠脱离 SSH 会话")

        monitor_missing = [name for name in ("top", "free") if not tools[name]]
        if monitor_missing:
            monitoring = self._decision(
                "blocked",
                "缺少基础性能采集命令",
                [f"预装命令：{', '.join(monitor_missing)}"],
            )
        else:
            optional = [name for name in ("iostat", "sensors") if not tools[name]]
            monitoring = self._decision(
                "degraded" if optional else "ready",
                "基础指标可采集" if not optional else f"基础指标可采集，缺少可选指标：{', '.join(optional)}",
                [f"如需完整磁盘/温度指标，请预装：{', '.join(optional)}"] if optional else [],
            )

        if tools["stress_ng"] and tmp_writable and tools["nohup"]:
            cpu_stress = self._decision("ready", "使用目标机已有 stress-ng", [])
            cpu_engine = "stress-ng"
        elif tools["python3"] and tmp_writable and background_available:
            cpu_stress = self._decision(
                "degraded",
                "stress-ng 不可用，将使用平台内置 Python CPU 降级引擎",
                ["如需标准化 bogo-ops 指标，请由运维预装 stress-ng"],
            )
            cpu_engine = "python"
        else:
            prerequisites = []
            if not tmp_writable:
                prerequisites.append("提供可写的 /tmp 目录")
            if not background_available:
                prerequisites.append("预装 nohup 或 setsid")
            if tools["stress_ng"] and not tools["nohup"]:
                prerequisites.append("使用 stress-ng 时需预装 nohup")
            if not tools["stress_ng"] and not tools["python3"]:
                prerequisites.append("预装 stress-ng，或至少提供 Python 3")
            cpu_stress = self._decision(
                "blocked",
                "没有可用的 CPU 压测执行器",
                prerequisites,
            )
            cpu_engine = "none"

        nvidia_healthy = _yes(raw.get("nvidia_smi_healthy"))
        all_busy_gpu_indexes = self.parse_gpu_devices(raw.get("gpu_busy_indexes"))
        gpu_count = _integer(raw.get("gpu_count"))
        compute_capabilities = self._parse_compute_capabilities(
            raw.get("gpu_compute_capabilities", "")
        )
        target_gpu_indexes = selected_gpu_indexes or sorted(compute_capabilities)
        target_compute_capabilities = {
            index: compute_capabilities[index]
            for index in target_gpu_indexes
            if index in compute_capabilities
        }
        blackwell_target = any(value >= 10.0 for value in target_compute_capabilities.values())
        invalid_gpu_indexes = [index for index in selected_gpu_indexes if index >= gpu_count]
        busy_gpu_indexes = (
            [index for index in all_busy_gpu_indexes if index in selected_gpu_indexes]
            if selected_gpu_indexes
            else all_busy_gpu_indexes
        )
        gpu_busy_count = len(busy_gpu_indexes)
        remote_gpu_burn = tools["gpu_burn"]
        remote_source = _yes(raw.get("gpu_burn_source_ready"))
        nvidia_container_runtime = (
            raw.get("container_runtime") == "docker"
            and _yes(raw.get("nvidia_container_runtime"))
        )
        universal_image_ready = _yes(raw.get("gpu_burn_container_image_ready"))
        blackwell_image_ready = _yes(raw.get("gpu_burn_blackwell_image_ready"))
        if blackwell_target and blackwell_image_ready:
            selected_container_image = blackwell_image
            container_image_ready = True
            container_compatibility_mode = "native-blackwell"
        elif blackwell_target and universal_image_ready:
            selected_container_image = container_image
            container_image_ready = True
            container_compatibility_mode = "ptx-fallback"
        else:
            selected_container_image = blackwell_image if blackwell_target else container_image
            container_image_ready = blackwell_image_ready if blackwell_target else universal_image_ready
            container_compatibility_mode = "native-multiarch" if container_image_ready else "unavailable"
        can_unpack_source = tools["unzip"] or tools["python3"]
        enough_build_space = not tmp_available_kb or tmp_available_kb >= 512 * 1024
        can_build = tools["nvcc"] and tools["make"] and tools["g++"] and enough_build_space and (
            remote_source or (local_gpu_burn_source and can_unpack_source)
        )
        if not nvidia_healthy:
            gpu_stress = self._decision(
                "blocked",
                "未发现可用的 NVIDIA GPU 或 nvidia-smi 无法正常查询",
                ["由运维确认 NVIDIA GPU、驱动和 nvidia-smi 已正确安装"],
            )
            gpu_engine = "none"
        elif not tmp_writable or not tools["nohup"]:
            needs = []
            if not tmp_writable:
                needs.append("提供可写的 /tmp 目录")
            if not tools["nohup"]:
                needs.append("预装 nohup")
            gpu_stress = self._decision("blocked", "GPU 可监控，但远程压测运行环境不完整", needs)
            gpu_engine = "none"
        elif remote_gpu_burn:
            gpu_stress = self._decision("ready", "使用目标机已有 gpu-burn 二进制", [])
            gpu_engine = "gpu-burn"
        elif nvidia_container_runtime and container_image_ready:
            ptx_fallback = container_compatibility_mode == "ptx-fallback"
            gpu_stress = self._decision(
                "degraded" if ptx_fallback else "ready",
                (
                    f"Blackwell 原生镜像未安装，将使用 {selected_container_image} 的 PTX 前向兼容路径"
                    if ptx_fallback
                    else f"使用与所选 GPU 架构匹配的容器镜像 {selected_container_image}，无需改动宿主机 CUDA"
                ),
                ([f"建议预装 Blackwell 原生镜像：{blackwell_image}"] if ptx_fallback else []),
            )
            gpu_engine = "gpu-burn-docker"
        elif can_build:
            gpu_stress = self._decision(
                "degraded",
                "具备 GPU 压测条件，任务开始时需要构建 gpu-burn",
                ["建议将验证过的 gpu-burn 二进制纳入平台离线工具仓库，避免现场编译"],
            )
            gpu_engine = "gpu-burn-build"
        else:
            needs = []
            if raw.get("container_runtime") == "docker" and not nvidia_container_runtime:
                needs.append("配置 NVIDIA Container Toolkit/runtime（不修改现有 NVIDIA 驱动）")
            if nvidia_container_runtime and not container_image_ready:
                needs.append(f"预装平台 GPU 压测镜像：{selected_container_image}")
            if not tools["nvcc"] and not nvidia_container_runtime:
                needs.append("CUDA devel/nvcc、容器执行器或与目标架构匹配的 gpu-burn 二进制")
            if not tools["make"] and not remote_gpu_burn:
                needs.append("make（仅源码构建需要）")
            if not tools["g++"] and not remote_gpu_burn:
                needs.append("g++（仅源码构建需要）")
            if not enough_build_space and not remote_gpu_burn:
                needs.append("/tmp 至少保留 512 MiB 可用空间")
            if not local_gpu_burn_source and not remote_source:
                needs.append("gpu-burn 源码包或预编译二进制")
            elif local_gpu_burn_source and not remote_source and not can_unpack_source:
                needs.append("unzip 或 Python 3（用于解压 gpu-burn 源码包）")
            gpu_stress = self._decision("blocked", "GPU 可监控，但缺少 GPU 压测执行器", needs)
            gpu_engine = "none"

        if invalid_gpu_indexes and nvidia_healthy:
            gpu_stress = self._decision(
                "blocked",
                f"指定的 GPU 卡号不存在：{','.join(map(str, invalid_gpu_indexes))}",
                [f"当前服务器可用卡号范围为 0-{max(0, gpu_count - 1)}"],
            )
        elif gpu_busy_count and gpu_stress["status"] != "blocked":
            target_description = "指定的 GPU" if selected_gpu_indexes else "检测到的 GPU"
            gpu_stress = self._decision(
                "blocked",
                f"{target_description}（{','.join(map(str, busy_gpu_indexes))}）正在承载业务，满载压测暂不可执行；在线体检仍可使用",
                ["可直接执行在线体检；如需满载压测，请在维护窗口释放目标 GPU 后重新检查"],
            )
            warnings.append("平台已根据当前占用推荐安全方案，不会自动停止业务进程或容器")

        gpu_devices = self._build_gpu_device_reports(
            raw.get("gpu_devices", ""),
            selected_gpu_indexes=selected_gpu_indexes,
            busy_gpu_indexes=all_busy_gpu_indexes,
            nvidia_container_runtime=nvidia_container_runtime,
            remote_gpu_burn=remote_gpu_burn,
            can_build=can_build,
            universal_image=container_image,
            universal_image_ready=universal_image_ready,
            blackwell_image=blackwell_image,
            blackwell_image_ready=blackwell_image_ready,
        )
        stress_ready_devices = [item for item in gpu_devices if item["stress_selectable"]]
        if stress_ready_devices:
            recommended_preset = "quick"
            strategy_message = f"发现 {len(stress_ready_devices)} 张空闲 GPU，建议先做 60 秒快速验证"
        elif gpu_devices:
            recommended_preset = "health"
            strategy_message = "GPU 均有业务占用，已推荐不烧卡的在线体检"
        else:
            recommended_preset = "health"
            strategy_message = "未发现可识别 GPU，仅执行服务器基础体检"
        test_strategy = {
            "recommended_preset": recommended_preset,
            "message": strategy_message,
            "detected_count": len(gpu_devices),
            "full_stress_count": len(stress_ready_devices),
            "online_health_count": len(gpu_devices),
            "maintenance_required": bool(gpu_devices) and not stress_ready_devices,
            "automatic_container_stop": False,
        }

        gpu_monitor = self._decision(
            "ready" if nvidia_healthy else "blocked",
            "nvidia-smi 可采集 GPU 指标" if nvidia_healthy else "GPU 指标不可采集",
            [] if nvidia_healthy else ["修复 NVIDIA 驱动或 nvidia-smi"],
        )
        dcgm = {
            "installed": tools["dcgmi"],
            "exporter_installed": tools["dcgm_exporter"],
            "version": raw.get("dcgm_version", "") if tools["dcgmi"] else "",
            "role": "optional_diagnostics",
            "required_for_stress": False,
            "message": "DCGM 仅用于可选健康诊断/指标增强，不作为兼容性或 GPU 压测前置条件",
        }
        nccl = {
            "installed": tools["nccl_tests"],
            "path": raw.get("nccl_tests_path", ""),
            "role": "optional_multi_gpu_communication_test",
            "required_for_stress": False,
        }

        decisions = {
            "monitor": monitoring,
            "cpu": cpu_stress,
            "gpu": gpu_stress,
        }
        requested = {mode: decisions[mode] for mode in modes if mode in decisions}
        overall = "ready"
        if any(item["status"] == "blocked" for item in requested.values()):
            overall = "blocked"
        elif any(item["status"] == "degraded" for item in requested.values()):
            overall = "degraded"

        return {
            "schema_version": self.probe_version,
            "read_only": True,
            "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host": {
                "hostname": raw.get("hostname", ""),
                "os_id": os_id,
                "os_name": raw.get("os_name", ""),
                "os_version": raw.get("os_version", ""),
                "os_pretty_name": raw.get("os_pretty_name", ""),
                "kernel": raw.get("kernel", ""),
                "architecture": architecture,
                "user": raw.get("user", ""),
                "uid": _integer(raw.get("uid"), -1),
                "is_root": _integer(raw.get("uid"), -1) == 0,
                "cpu_logical": _integer(raw.get("cpu_logical")),
                "memory_kb": _integer(raw.get("memory_kb")),
                "tmp_available_kb": tmp_available_kb,
                "tmp_writable": tmp_writable,
                "cgroup_version": raw.get("cgroup_version", "none"),
                "open_files_limit": _integer(raw.get("open_files_limit")),
                "package_manager": raw.get("package_manager", "none"),
                "container_runtime": raw.get("container_runtime", "none"),
                "container_version": raw.get("container_version", ""),
                "container_runtimes": raw.get("container_runtimes", ""),
                "nvidia_container_runtime": nvidia_container_runtime,
            },
            "support": {
                "tier": support_tier,
                "label": support_label,
                "release_verified": release_verified,
                "verified_releases": list(primary_releases),
                "primary_systems": ["openEuler", "Ubuntu LTS"],
                "legacy_systems": ["CentOS"],
            },
            "tools": tools,
            "versions": {
                "python": raw.get("python_version", "") if tools["python3"] else "",
                "stress_ng": raw.get("stress_ng_version", "") if tools["stress_ng"] else "",
                "nvidia_driver": raw.get("driver_version", ""),
                "cuda_runtime": raw.get("cuda_runtime_version", ""),
                "cuda_compiler": raw.get("nvcc_version", "") if tools["nvcc"] else "",
                "glibc": raw.get("glibc_version", ""),
            },
            "gpu": {
                "available": nvidia_healthy,
                "count": gpu_count,
                "busy_count": gpu_busy_count,
                "busy_indexes": busy_gpu_indexes,
                "selected_indexes": selected_gpu_indexes,
                "compute_capabilities": compute_capabilities,
                "selected_compute_capabilities": target_compute_capabilities,
                "devices": gpu_devices,
                "name": raw.get("gpu_name", ""),
                "monitoring": gpu_monitor,
                "stress_engine": gpu_engine,
                "gpu_burn_path": raw.get("gpu_burn_path", ""),
                "container_image": selected_container_image,
                "container_image_ready": container_image_ready,
                "universal_image": container_image,
                "universal_image_ready": universal_image_ready,
                "blackwell_image": blackwell_image,
                "blackwell_image_ready": blackwell_image_ready,
                "container_compatibility_mode": container_compatibility_mode,
                "test_strategy": test_strategy,
                "local_source_available": local_gpu_burn_source,
                "dcgm": dcgm,
                "nccl_tests": nccl,
            },
            "cpu": {"stress_engine": cpu_engine},
            "runtime": {
                "architecture_supported": architecture_supported,
                "temporary_directory_writable": tmp_writable,
                "temporary_directory_available_kb": tmp_available_kb,
                "background_execution_available": tools_background,
            },
            "capabilities": decisions,
            "requested_modes": modes,
            "overall": overall,
            "warnings": warnings,
            "policy": {
                "automatic_driver_install": False,
                "automatic_cuda_install": False,
                "automatic_package_install": False,
                "ownership": "驱动、CUDA、容器运行时和内核由运维预装；平台只检测、选择执行器并输出缺失清单",
            },
        }

    @staticmethod
    def _decision(status: str, message: str, prerequisites: list[str]) -> dict[str, Any]:
        return {
            "status": status,
            "runnable": status != "blocked",
            "message": message,
            "prerequisites": prerequisites,
        }

    @staticmethod
    def _parse_compute_capabilities(value: str) -> dict[int, float]:
        result: dict[int, float] = {}
        for item in str(value or "").split(","):
            index, separator, capability = item.partition(":")
            if not separator:
                continue
            try:
                result[int(index.strip())] = float(capability.strip())
            except ValueError:
                continue
        return result

    @staticmethod
    def _build_gpu_device_reports(
        value: str,
        *,
        selected_gpu_indexes: list[int],
        busy_gpu_indexes: list[int],
        nvidia_container_runtime: bool,
        remote_gpu_burn: bool,
        can_build: bool,
        universal_image: str,
        universal_image_ready: bool,
        blackwell_image: str,
        blackwell_image_ready: bool,
    ) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        for row in str(value or "").split(";"):
            fields = [item.strip() for item in row.split("|")]
            if len(fields) != 7:
                continue
            try:
                index = int(fields[0])
                memory_total = int(float(fields[2]))
                memory_used = int(float(fields[3]))
                utilization = int(float(fields[4]))
                temperature = int(float(fields[5]))
                compute_capability = float(fields[6])
            except ValueError:
                continue
            memory_limit = max(1024, int(memory_total * 0.10))
            memory_percent = round(memory_used / memory_total * 100, 1) if memory_total else 0.0
            busy_reasons = []
            if memory_used >= memory_limit:
                busy_reasons.append(f"显存已使用 {memory_used} MiB")
            if utilization >= 10:
                busy_reasons.append(f"GPU 利用率 {utilization}%")
            if index in busy_gpu_indexes and not busy_reasons:
                busy_reasons.append("检测到业务占用")
            blackwell = compute_capability >= 10.0
            if remote_gpu_burn:
                compatibility = "ready"
                mode = "host-binary"
                image = ""
                reason = "使用服务器已有 gpu-burn"
            elif nvidia_container_runtime and blackwell and blackwell_image_ready:
                compatibility = "ready"
                mode = "native-blackwell"
                image = blackwell_image
                reason = "Blackwell 原生镜像已就绪"
            elif nvidia_container_runtime and blackwell and universal_image_ready:
                compatibility = "degraded"
                mode = "ptx-fallback"
                image = universal_image
                reason = "可使用 PTX 兼容路径，建议部署 Blackwell 原生镜像"
            elif nvidia_container_runtime and not blackwell and universal_image_ready:
                compatibility = "ready"
                mode = "native-multiarch"
                image = universal_image
                reason = "原生架构镜像已就绪"
            elif can_build:
                compatibility = "degraded"
                mode = "host-build"
                image = ""
                reason = "需要在任务开始时编译 gpu-burn"
            else:
                compatibility = "blocked"
                mode = "unavailable"
                image = blackwell_image if blackwell else universal_image
                reason = "缺少兼容执行镜像或运行环境"
            busy = bool(busy_reasons)
            if temperature >= 85 or utilization >= 80 or memory_percent >= 80:
                occupancy_level = "high"
                occupancy_label = "高负载"
            elif busy:
                occupancy_level = "active"
                occupancy_label = "业务占用"
            else:
                occupancy_level = "idle"
                occupancy_label = "空闲"
            stress_selectable = compatibility != "blocked" and occupancy_level == "idle"
            safe_test_modes = ["health"]
            if stress_selectable:
                safe_test_modes.extend(["quick", "standard", "stability"])
            status = "busy" if busy else compatibility
            devices.append({
                "index": index,
                "name": fields[1],
                "memory_total_mib": memory_total,
                "memory_used_mib": memory_used,
                "memory_used_percent": memory_percent,
                "utilization_percent": utilization,
                "temperature_c": temperature,
                "compute_capability": compute_capability,
                "architecture_family": "Blackwell" if blackwell else ("Ada" if compute_capability >= 8.9 else "Ampere" if compute_capability >= 8.0 else "Turing" if compute_capability >= 7.5 else "Legacy"),
                "busy": busy,
                "busy_reasons": busy_reasons,
                "occupancy_level": occupancy_level,
                "occupancy_label": occupancy_label,
                "safe_test_modes": safe_test_modes,
                "compatibility": compatibility,
                "status": status,
                "selectable": stress_selectable,
                "stress_selectable": stress_selectable,
                "health_selectable": True,
                "selected": index in selected_gpu_indexes,
                "execution_mode": mode,
                "container_image": image,
                "reason": (
                    "；".join(busy_reasons) + "；建议先执行在线体检"
                    if busy_reasons
                    else reason
                ),
            })
        return devices
