"""CPU temperature collection and parsing helpers.

Only current readings from identifiable CPU sensors are returned.  Alert
thresholds such as ``high``/``crit`` and anonymous thermal zones must never be
treated as live CPU temperature samples.
"""

from __future__ import annotations

import re
from typing import Any


SENSORS_MARKER = "__LIEMA_SENSORS__"
SYSFS_ZONE_MARKER = "__LIEMA_SYSFS_ZONE__"

CPU_TEMPERATURE_COMMAND = (
    "sensor_command=''; "
    "if command -v sensors >/dev/null 2>&1; then sensor_command=$(command -v sensors); "
    "elif [ -x /usr/bin/sensors ]; then sensor_command=/usr/bin/sensors; "
    "elif [ -x /usr/sbin/sensors ]; then sensor_command=/usr/sbin/sensors; fi; "
    "if [ -n \"$sensor_command\" ]; then "
    f"printf '{SENSORS_MARKER}\\n'; \"$sensor_command\" 2>/dev/null; "
    "fi; "
    "for zone in /sys/class/thermal/thermal_zone*; do "
    "[ -d \"$zone\" ] || continue; "
    "[ -r \"$zone/type\" ] && [ -r \"$zone/temp\" ] || continue; "
    "zone_name=${zone##*/}; "
    f"printf '{SYSFS_ZONE_MARKER}\\t%s\\t%s\\t%s\\n' "
    "\"$zone_name\" \"$(cat \"$zone/type\" 2>/dev/null)\" \"$(cat \"$zone/temp\" 2>/dev/null)\"; "
    "done"
)

_SENSOR_RE = re.compile(
    r"^\s*(Package id\s+\d+|Tctl|Tdie|Core\s+\d+|CPU Temp(?:erature)?)\s*:"
    r"\s*([+-]?\d+(?:\.\d+)?)\s*°C",
    re.IGNORECASE,
)


def _empty_result(message: str) -> dict[str, Any]:
    return {
        "value": None,
        "status": "unavailable",
        "source": "unavailable",
        "label": "未发现可信 CPU 温度传感器",
        "details": message,
        "readings": [],
    }


def _format_readings(records: list[dict[str, Any]]) -> str:
    return "；".join(f"{record['label']} {record['value']:.1f}°C" for record in records)


def _parse_sensors(output: str) -> dict[str, Any] | None:
    records: list[dict[str, Any]] = []
    current_chip = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line == SENSORS_MARKER:
            continue
        match = _SENSOR_RE.match(raw_line)
        if match:
            label = re.sub(r"\s+", " ", match.group(1)).strip()
            records.append({"label": label, "value": float(match.group(2)), "chip": current_chip})
            continue
        if not raw_line[0].isspace() and ":" not in line and not line.startswith("__LIEMA_"):
            current_chip = line

    if not records:
        return None

    packages = [record for record in records if record["label"].lower().startswith("package id")]
    tctl = [record for record in records if record["label"].lower() == "tctl"]
    tdie = [record for record in records if record["label"].lower() == "tdie"]
    cores = [record for record in records if record["label"].lower().startswith("core ")]
    cpu_labels = [record for record in records if record["label"].lower().startswith("cpu temp")]
    selected = packages or tctl or tdie or cores or cpu_labels
    hottest = max(selected, key=lambda record: record["value"])
    sensor_label = " · ".join(part for part in (hottest["chip"], hottest["label"]) if part)
    readings = [
        {
            "id": f"{record['chip']}|{record['label']}",
            "label": record["label"],
            "chip": record["chip"],
            "value": round(record["value"], 1),
        }
        for record in selected
    ]
    return {
        "value": round(hottest["value"], 1),
        "status": "available",
        "source": "lm-sensors",
        "label": sensor_label or hottest["label"],
        "details": _format_readings(selected),
        "readings": readings,
    }


def _is_cpu_zone(zone_type: str) -> bool:
    normalized = re.sub(r"\s+", " ", zone_type.strip().lower())
    if any(blocked in normalized for blocked in ("gpu", "pch", "acpi", "i350", "nvme", "wifi")):
        return False
    trusted_names = {
        "x86_pkg_temp",
        "cpu-thermal",
        "cpu_thermal",
        "cpu thermal",
        "coretemp",
        "k10temp",
        "zenpower",
        "tctl",
        "tdie",
    }
    return normalized in trusted_names or normalized.startswith("x86_pkg_temp")


def _parse_sysfs(output: str) -> dict[str, Any] | None:
    records: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        if not raw_line.startswith(SYSFS_ZONE_MARKER + "\t"):
            continue
        parts = raw_line.split("\t", 3)
        if len(parts) != 4:
            continue
        _, zone_name, zone_type, raw_value = parts
        if not _is_cpu_zone(zone_type):
            continue
        try:
            value = float(raw_value.strip()) / 1000.0
        except (TypeError, ValueError):
            continue
        if not -40.0 <= value <= 150.0:
            continue
        records.append({"zone": zone_name.strip(), "label": zone_type.strip(), "value": value})
    if not records:
        return None
    hottest = max(records, key=lambda record: record["value"])
    readings = [
        {
            "id": f"{record['zone']}|{record['label']}",
            "label": record["label"],
            "chip": record["zone"],
            "value": round(record["value"], 1),
        }
        for record in records
    ]
    return {
        "value": round(hottest["value"], 1),
        "status": "available",
        "source": "sysfs",
        "label": f"{hottest['zone']} · {hottest['label']}",
        "details": _format_readings(records),
        "readings": readings,
    }


def parse_cpu_temperature(output: str) -> dict[str, Any]:
    """Return one trustworthy current CPU temperature and its provenance."""
    text = output or ""
    sensors_section = text.split(SYSFS_ZONE_MARKER, 1)[0]
    sensor_result = _parse_sensors(sensors_section)
    if sensor_result is not None:
        return sensor_result
    sysfs_result = _parse_sysfs(text)
    if sysfs_result is not None:
        return sysfs_result
    if SENSORS_MARKER in text:
        message = "lm-sensors 未返回 Package/Tctl/Tdie/Core 当前值，sysfs 也没有可识别的 CPU 热区"
    else:
        message = "未安装 lm-sensors，且 sysfs 中没有可识别的 CPU 热区"
    return _empty_result(message)
