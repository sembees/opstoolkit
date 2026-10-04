"""驱动基类：定义各厂商命令集、分屏关闭与提示符等通用行为。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MetricCommand:
    key: str
    label: str
    command: str
    textfsm: str = ""
    regex: list = field(default_factory=list)
    unit: str = ""
    # ★ 候选命令回退（2026-10-05）：同一指标在不同机型/版本上的命令名不一致
    #   （如 H3C 部分交换机不认 `display alarm urgent` 只认 `display alarm`）。
    #   按优先级排列的替代命令；主命令被设备拒绝时依次尝试（见 inspection/service.py）。
    #   全部候选都被拒绝 ⇒ 该指标标记为 unsupported（"该机型不支持此指标"）。
    alt_commands: tuple = ()


class BaseDriver:
    vendor: str = "generic"
    default_template: str = "standard"

    def templates(self) -> dict:
        return {"standard": self.standard_metrics()}

    def standard_metrics(self) -> list:
        return []

    def disable_pager_commands(self) -> list:
        return []

    def enter_enable_commands(self) -> list:
        return []


_TYPE_TO_VENDOR = {
    "hp": "h3c",
    "comware": "h3c",
    "h3c": "h3c",
    "hp_comware": "h3c",
    "huawei": "huawei",
    "huawei_vrpv8": "huawei",
    "cisco": "cisco",
    "cisco_ios": "cisco",
    "ios": "cisco",
    "ios_xe": "cisco",
    "cisco_xe": "cisco",
    "cisco_asa": "cisco",
    "asa": "cisco",
    "cisco_nxos": "cisco",
    "nxos": "cisco",
    "cisco_xr": "cisco",
    "ios_xr": "cisco",
}


def vendor_from_device_type(device_type: str) -> str:
    normalized = (device_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _TYPE_TO_VENDOR.get(normalized, "generic")
