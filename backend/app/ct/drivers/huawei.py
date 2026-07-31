"""华为 VRP 驱动。优先级最高。"""
from app.ct.drivers.base import BaseDriver, MetricCommand


class HuaweiDriver(BaseDriver):
    vendor = "huawei"

    def disable_pager_commands(self) -> list:
        return ["screen-length 0 temporary"]

    def standard_metrics(self) -> list:
        return [
            MetricCommand("version", "设备版本/型号", "display version", "huawei_version.textfsm"),
            MetricCommand("cpu", "CPU 使用率", "display cpu-usage", "huawei_cpu.textfsm", "%"),
            MetricCommand("memory", "内存使用率", "display memory-usage", "huawei_memory.textfsm", "%"),
            MetricCommand("device", "硬件状态", "display device", "huawei_device.textfsm"),
            MetricCommand("temperature", "温度", "display temperature", "huawei_temperature.textfsm"),
            MetricCommand("power", "电源", "display power", "huawei_power.textfsm"),
            MetricCommand("fan", "风扇", "display fan", "huawei_fan.textfsm"),
            MetricCommand("interface", "接口概要", "display interface brief", "huawei_interface_brief.textfsm"),
            MetricCommand("alarm", "告警信息", "display alarm urgent"),
            MetricCommand("logbuffer", "日志缓冲", "display logbuffer"),
        ]
