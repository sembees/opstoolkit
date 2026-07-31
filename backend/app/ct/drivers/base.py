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
    "hp_comware": "h3c",
    "huawei": "huawei",
    "huawei_vrpv8": "huawei",
    "cisco_ios": "cisco",
    "cisco_xe": "cisco",
    "cisco_asa": "cisco",
    "cisco_nxos": "cisco",
    "cisco_xr": "cisco",
}


def vendor_from_device_type(device_type: str) -> str:
    return _TYPE_TO_VENDOR.get((device_type or "").lower(), "generic")
