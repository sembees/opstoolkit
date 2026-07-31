"""设备驱动：按厂商抽象命令集与解析。"""
from app.ct.drivers.base import BaseDriver
from app.ct.drivers.cisco import CiscoDriver
from app.ct.drivers.h3c import H3CDriver
from app.ct.drivers.huawei import HuaweiDriver

DRIVERS = {
    "h3c": H3CDriver,
    "huawei": HuaweiDriver,
    "cisco": CiscoDriver,
}


def get_driver(vendor: str) -> BaseDriver:
    """按厂商返回驱动实例。未知厂商回落到通用基类。"""
    cls = DRIVERS.get((vendor or "").lower())
    if cls is None:
        return BaseDriver()
    return cls()


def infer_netmiko_device_type(vendor: str, role: str = "") -> str:
    """根据厂商+角色推断 netmiko device_type。H3C/华为优先。"""
    v = (vendor or "").lower()
    r = (role or "").lower()
    if v in ("h3c", "hp", "comware"):
        return "hp_comware"
    if v == "huawei":
        return "huawei"
    if v == "cisco":
        if "firewall" in r or "fw" in r or "asa" in r:
            return "cisco_asa"
        if "xr" in r:
            return "cisco_xr"
        if "nx" in r or "nexus" in r:
            return "cisco_nxos"
        return "cisco_ios"
    return ""
