import unittest

from tests import bootstrap  # noqa: F401

from auto_test.monitoring.cpu_temperature import (
    CPU_TEMPERATURE_COMMAND,
    SENSORS_MARKER,
    SYSFS_ZONE_MARKER,
    parse_cpu_temperature,
)


DUAL_SOCKET_SENSORS_OUTPUT = f"""{SENSORS_MARKER}
coretemp-isa-0000
Adapter: ISA adapter
Package id 0:  +36.0°C  (high = +88.0°C, crit = +98.0°C)
Core 0:        +36.0°C  (high = +88.0°C, crit = +98.0°C)

i350bb-pci-0100
Adapter: PCI adapter
loc1:         +48.0°C  (high = +120.0°C, crit = +110.0°C)

coretemp-isa-0001
Adapter: ISA adapter
Package id 1:  +33.0°C  (high = +88.0°C, crit = +98.0°C)
Core 0:        +31.0°C  (high = +88.0°C, crit = +98.0°C)

pch_lewisburg-virtual-0
Adapter: Virtual device
temp1:        +29.0°C
"""


class CpuTemperatureTests(unittest.TestCase):
    def test_dual_socket_parser_uses_current_package_value_not_high_or_crit(self):
        result = parse_cpu_temperature(DUAL_SOCKET_SENSORS_OUTPUT)

        self.assertEqual(result["value"], 36.0)
        self.assertEqual(result["source"], "lm-sensors")
        self.assertEqual(result["label"], "coretemp-isa-0000 · Package id 0")
        self.assertIn("Package id 0 36.0°C", result["details"])
        self.assertIn("Package id 1 33.0°C", result["details"])
        self.assertNotIn("98.0", result["details"])
        self.assertNotIn("48.0", result["details"])
        self.assertEqual(result["readings"], [
            {"id": "coretemp-isa-0000|Package id 0", "label": "Package id 0", "chip": "coretemp-isa-0000", "value": 36.0},
            {"id": "coretemp-isa-0001|Package id 1", "label": "Package id 1", "chip": "coretemp-isa-0001", "value": 33.0},
        ])

    def test_sysfs_fallback_accepts_named_cpu_zone_and_rejects_hotter_non_cpu_zones(self):
        output = "\n".join((
            f"{SYSFS_ZONE_MARKER}\tthermal_zone0\tx86_pkg_temp\t41000",
            f"{SYSFS_ZONE_MARKER}\tthermal_zone1\tpch_lewisburg\t48000",
            f"{SYSFS_ZONE_MARKER}\tthermal_zone2\tacpitz\t44000",
        ))

        result = parse_cpu_temperature(output)

        self.assertEqual(result["value"], 41.0)
        self.assertEqual(result["source"], "sysfs")
        self.assertEqual(result["label"], "thermal_zone0 · x86_pkg_temp")
        self.assertEqual(result["readings"][0]["chip"], "thermal_zone0")

    def test_anonymous_or_non_cpu_sysfs_temperatures_are_not_reported_as_cpu(self):
        output = "\n".join((
            f"{SYSFS_ZONE_MARKER}\tthermal_zone0\tacpitz\t41000",
            f"{SYSFS_ZONE_MARKER}\tthermal_zone1\tpch_lewisburg\t44000",
        ))

        result = parse_cpu_temperature(output)

        self.assertIsNone(result["value"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("未安装 lm-sensors", result["details"])
        self.assertEqual(result["readings"], [])

    def test_core_reading_is_used_only_when_package_level_reading_is_absent(self):
        output = f"""{SENSORS_MARKER}
coretemp-isa-0000
Adapter: ISA adapter
Core 0: +34.0°C (high = +88.0°C, crit = +98.0°C)
Core 1: +37.0°C (high = +88.0°C, crit = +98.0°C)
"""

        result = parse_cpu_temperature(output)

        self.assertEqual(result["value"], 37.0)
        self.assertTrue(result["label"].endswith("Core 1"))

    def test_remote_command_collects_sensor_names_and_sysfs_types(self):
        self.assertIn(SENSORS_MARKER, CPU_TEMPERATURE_COMMAND)
        self.assertIn(SYSFS_ZONE_MARKER, CPU_TEMPERATURE_COMMAND)
        self.assertIn('$zone/type', CPU_TEMPERATURE_COMMAND)
        self.assertIn("/usr/bin/sensors", CPU_TEMPERATURE_COMMAND)
        self.assertIn("/usr/sbin/sensors", CPU_TEMPERATURE_COMMAND)
        self.assertNotIn("head -3", CPU_TEMPERATURE_COMMAND)


if __name__ == "__main__":
    unittest.main()
