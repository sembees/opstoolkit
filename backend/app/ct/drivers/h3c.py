"""H3C Comware 驱动。优先级最高。"""
from app.ct.drivers.base import BaseDriver, MetricCommand


class H3CDriver(BaseDriver):
    vendor = "h3c"

    def disable_pager_commands(self) -> list:
        return ["screen-length disable"]

    def standard_metrics(self) -> list:
        return [
            MetricCommand("version", "设备版本/型号", "display version", "h3c_version.textfsm"),
            MetricCommand("cpu", "CPU 使用率", "display cpu-usage", "h3c_cpu.textfsm", "%"),
            MetricCommand("memory", "内存使用率", "display memory", "h3c_memory.textfsm", "%"),
            MetricCommand("device", "硬件状态", "display device", "h3c_device.textfsm"),
            MetricCommand("environment", "温度", "display environment", "h3c_environment.textfsm"),
            MetricCommand("power", "电源", "display power", "h3c_power.textfsm"),
            MetricCommand("fan", "风扇", "display fan", "h3c_fan.textfsm"),
            MetricCommand("interface", "接口概要", "display interface brief", "h3c_interface_brief.textfsm"),
            MetricCommand("alarm", "告警信息", "display alarm"),
            MetricCommand("logbuffer", "日志缓冲", "display logbuffer reverse"),
        ]
