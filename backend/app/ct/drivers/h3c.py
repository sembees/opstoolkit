"""H3C Comware 驱动。优先级最高。

候选命令（alt_commands）：同一指标在不同 CMW7 机型上的命令名可能不同 ——
真机基线（mimo/out/captures/h3c_switch_capture.json）实测 S10508X 不认
`display alarm urgent`（报 % Too many parameters），只有 `display alarm`。
主命令被设备拒绝时由 service 层自动依次尝试 alt_commands（见 MetricCommand 注释）。
"""
from app.ct.drivers.base import BaseDriver, MetricCommand


class H3CDriver(BaseDriver):
    vendor = "h3c"

    def disable_pager_commands(self) -> list:
        return ["screen-length disable"]

    def standard_metrics(self) -> list:
        return [
            MetricCommand("version", "设备版本/型号", "display version", "h3c_version.textfsm"),
            # ★ unit 必须用关键字传参：此前按位置传会把 "%" 落进 regex 字段、unit 丢失
            MetricCommand("cpu", "CPU 使用率", "display cpu-usage", "h3c_cpu.textfsm", unit="%"),
            MetricCommand("memory", "内存使用率", "display memory", "h3c_memory.textfsm", unit="%"),
            MetricCommand("device", "硬件状态", "display device", "h3c_device.textfsm"),
            MetricCommand("environment", "温度", "display environment", "h3c_environment.textfsm"),
            MetricCommand("power", "电源", "display power", "h3c_power.textfsm"),
            MetricCommand("fan", "风扇", "display fan", "h3c_fan.textfsm"),
            MetricCommand("interface", "接口概要", "display interface brief", "h3c_interface_brief.textfsm"),
            # vFW/vSR 真机：`display alarm` 本身也不被识别 ⇒ 两个都试完 → unsupported（该机型无告警特性）
            MetricCommand("alarm", "告警信息", "display alarm urgent", alt_commands=("display alarm",)),
            MetricCommand("logbuffer", "日志缓冲", "display logbuffer reverse"),
        ]
