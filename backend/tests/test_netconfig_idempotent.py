# -*- coding: utf-8 -*-
"""nmcli 脚本必须**可重入**（2026-10-18 内网审阅提出）。

问题原样：脚本用 `nmcli connection add ...`，第二次执行时连接已存在 ⇒ `add` 报错，
而脚本是 `set -e` ⇒ 每次都停在"改到一半"的状态（一半连接是新配置、一半是旧的）。
运维真实场景里重跑脚本很常见（改错一个地址再跑一次），所以这不是理论问题。

修法：`nmcli connection show <name> >/dev/null 2>&1 || nmcli connection add ...`
—— 已存在就跳过 add，后面的 `modify` 覆盖设置；刻意不用 `delete`（会先拆连接断线）。
"""
import sys
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import NetConfigRequest          # noqa: E402
from app.it.netconfig.generator import generate_netconfig   # noqa: E402


def gen(**kw):
    out = generate_netconfig(NetConfigRequest(**kw))
    return out[0] if isinstance(out, tuple) else out[0]


class NmcliIdempotentTest(unittest.TestCase):
    def _adds(self, script):
        return [l for l in script.splitlines() if "connection add" in l]

    def _assert_all_guarded(self, script):
        adds = self._adds(script)
        self.assertTrue(adds, "没有 add 行，用例本身失效")
        for line in adds:
            self.assertIn("nmcli connection show ", line,
                          "add 前没有探测连接是否存在（不可重入）：" + line)
            self.assertIn("|| nmcli connection add", line,
                          "探测之后没有接 add：不可重入写法")

    def test_iface_is_idempotent(self):
        p = gen(os="rhel", format="nmcli",
                interfaces=[{"name": "eth0", "mode": "static", "ip": "10.0.0.5", "cidr": 24}])
        self._assert_all_guarded(p)
        self.assertIn("nmcli connection show eth0 >/dev/null 2>&1 ||", p)

    def test_bond_with_slaves_is_idempotent(self):
        p = gen(os="rhel", format="nmcli",
                interfaces=[{"name": "eth1", "mode": "dhcp"}, {"name": "eth2", "mode": "dhcp"}],
                bonds=[{"name": "bond0", "mode": 1, "interfaces": ["eth1", "eth2"],
                        "ip": "10.0.0.9", "cidr": 24}])
        self._assert_all_guarded(p)
        # bond 本体 + 两个从口都要有守卫
        for name in ("bond0", "bond0-slave-eth1", "bond0-slave-eth2"):
            self.assertIn("nmcli connection show " + name + " >/dev/null 2>&1 ||", p)

    def test_vlan_is_idempotent(self):
        p = gen(os="rhel", format="nmcli",
                interfaces=[{"name": "eth0", "mode": "dhcp"}],
                vlans=[{"parent": "eth0", "vlan_id": 100, "ip": "10.1.100.5", "cidr": 24}])
        self._assert_all_guarded(p)
        self.assertIn("nmcli connection show eth0.100 >/dev/null 2>&1 ||", p)

    def test_bridge_is_idempotent(self):
        p = gen(os="rhel", format="nmcli",
                interfaces=[{"name": "eth0", "mode": "dhcp"}],
                bridges=[{"name": "br0", "interfaces": ["eth0"], "mode": "static",
                          "ip": "10.2.0.5", "cidr": 24}])
        self._assert_all_guarded(p)

    def test_script_still_has_set_e_and_no_delete(self):
        """守卫不能改变别的语义：仍然 set -e，且**没有** connection delete（避免断线）。"""
        p = gen(os="rhel", format="nmcli",
                interfaces=[{"name": "eth0", "mode": "static", "ip": "10.0.0.5", "cidr": 24}])
        self.assertIn("set -e", p)
        self.assertNotIn("connection delete", p)


if __name__ == "__main__":
    unittest.main()
