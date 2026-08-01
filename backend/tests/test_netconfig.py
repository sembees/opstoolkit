"""网络配置生成器单元测试。"""
import unittest

from app.core.schemas import NetBondIn, NetBridgeIn, NetConfigRequest, NetInterfaceIn, NetVlanIn
from app.it.netconfig.generator import generate_netconfig


class NetConfigTest(unittest.TestCase):
    def test_netplan_full(self):
        req = NetConfigRequest(
            os="ubuntu",
            hostname="web01",
            format="netplan",
            netplan_renderer="networkd",
            interfaces=[
                NetInterfaceIn(name="ens33", mode="dhcp"),
                NetInterfaceIn(name="ens34", mode="static", ip="10.0.0.5", cidr=24, gateway="10.0.0.1", dns=["10.0.0.1"]),
            ],
            bonds=[NetBondIn(name="bond0", mode=4, interfaces=["ens35", "ens36"], ip="10.1.1.2", cidr=24, gateway="10.1.1.1")],
            vlans=[NetVlanIn(parent="bond0", vlan_id=10, mode="static", ip="10.1.10.2", cidr=24)],
            bridges=[NetBridgeIn(name="br0", interfaces=["ens37"], ip="10.2.2.2", cidr=24)],
        )
        script, filename = generate_netconfig(req)
        self.assertEqual(filename, "99-opstk.yaml")
        self.assertIn("renderer: networkd", script)
        self.assertIn("  ethernets:", script)
        self.assertIn("  bonds:", script)
        self.assertIn("  vlans:", script)
        self.assertIn("  bridges:", script)
        self.assertIn("bond0.10:", script)
        self.assertIn("mode: 802.3ad", script)
        self.assertIn("addresses: [10.1.10.2/24]", script)

    def test_nmcli_rhel(self):
        req = NetConfigRequest(
            os="rhel",
            hostname="db01",
            format="nmcli",
            interfaces=[NetInterfaceIn(name="ens160", mode="static", ip="192.168.1.10", cidr=24, gateway="192.168.1.1")],
        )
        script, filename = generate_netconfig(req)
        self.assertEqual(filename, "apply-network.sh")
        self.assertIn("nmcli connection add type ethernet ifname ens160", script)
        self.assertIn("ipv4.method manual", script)
        self.assertIn("hostnamectl set-hostname db01", script)


if __name__ == "__main__":
    unittest.main()
