"""Cisco IOS/XE/ASA/NXOS 驱动。"""
from app.ct.drivers.base import BaseDriver, MetricCommand


class CiscoDriver(BaseDriver):
    vendor = "cisco"

    def disable_pager_commands(self) -> list:
        return ["terminal length 0"]

    def standard_metrics(self) -> list:
        return [
            MetricCommand("version", "设备版本/型号", "show version", "cisco_version.textfsm"),
            MetricCommand("cpu", "CPU 使用率", "show processes cpu sorted", "cisco_cpu.textfsm", "%"),
            MetricCommand("memory", "内存使用率", "show memory statistics", "cisco_memory.textfsm", "%"),
            MetricCommand("environment", "温度/电源/风扇", "show environment all", "cisco_environment.textfsm"),
            MetricCommand("power", "电源", "show environment power", "cisco_power.textfsm"),
            MetricCommand("interface", "接口概要", "show ip interface brief", "cisco_interface_brief.textfsm"),
            MetricCommand("inventory", "硬件清单", "show inventory", "cisco_inventory.textfsm"),
            MetricCommand("alarm", "告警信息", "show alarms"),
            MetricCommand("logging", "日志", "show logging"),
        ]
