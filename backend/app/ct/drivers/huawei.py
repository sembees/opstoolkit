"""华为 VRP 驱动。优先级最高。

候选命令（alt_commands）：不同 VRP 版本对同一指标的命令名不同 ——
部分老版本只认 `display cpu` / `display memory`（不认 -usage 后缀），
部分机型 `display alarm urgent` 不存在只有 `display alarm`。
主命令被设备拒绝时由 service 层自动依次尝试 alt_commands（见 MetricCommand 注释）。
"""
from app.ct.drivers.base import BaseDriver, MetricCommand


class HuaweiDriver(BaseDriver):
    vendor = "huawei"

    def disable_pager_commands(self) -> list:
        return ["screen-length 0 temporary"]

    def standard_metrics(self) -> list:
        return [
            MetricCommand("version", "设备版本/型号", "display version", "huawei_version.textfsm"),
            # ★ unit 必须用关键字传参：此前按位置传会把 "%" 落进 regex 字段、unit 丢失
            MetricCommand("cpu", "CPU 使用率", "display cpu-usage", "huawei_cpu.textfsm",
                          unit="%", alt_commands=("display cpu",)),
            MetricCommand("memory", "内存使用率", "display memory-usage", "huawei_memory.textfsm",
                          unit="%", alt_commands=("display memory",)),
            MetricCommand("device", "硬件状态", "display device", "huawei_device.textfsm"),
            # temperature/power/fan：各版本命令名差异较大、未真机验证，暂不猜测候选，
            # 若主命令被拒会得到明确的 unsupported 标注（"该机型不支持此指标"）。
            MetricCommand("temperature", "温度", "display temperature", "huawei_temperature.textfsm"),
            MetricCommand("power", "电源", "display power", "huawei_power.textfsm"),
            MetricCommand("fan", "风扇", "display fan", "huawei_fan.textfsm"),
            MetricCommand("interface", "接口概要", "display interface brief", "huawei_interface_brief.textfsm"),
            MetricCommand("alarm", "告警信息", "display alarm urgent", alt_commands=("display alarm",)),
            MetricCommand("logbuffer", "日志缓冲", "display logbuffer"),
        ]
