# -*- coding: utf-8 -*-
"""用**真 netplan** 校验 netplan 产物（不是字符串断言）。

## 为什么必须有这一层（2026-10-18 的血案）

我原先给 netplan 写的 IPv6 键是 `addresses6` —— **netplan 根本没有这个键**。
所有字符串断言、快照基线、甚至浏览器 E2E 都是绿的，因为"生成器输出 == 我以为是的样子"。
把产物丢进 `netplan generate` 立刻现原形：

    /etc/netplan/99-opstk.yaml:8:7: Error in network definition: unknown key 'addresses6'

也就是说，Ubuntu 装机后那份配置**加载不了**，机器直接裸网 —— 而单元测试全绿。

## 本用例做什么

对 `test_netconfig_golden.py` 里所有 netplan 用例：把产物写成
`<root>/etc/netplan/99-opstk.yaml`，跑 `netplan generate --root-dir <root>`。
退出码非 0 就失败，并把 netplan 的原始报错贴出来（它会指出行号与非法键）。

## 环境要求

需要 `netplan` 可执行文件（Ubuntu/Debian：`apt-get install netplan.io`）。
没有就 **skip**（本机 Windows、以及没装 netplan 的 runner）；
CI 里有一个专门的 `netplan` job 装了它，所以这条**在 CI 上是真跑**的。
用 `OPSTK_NETPLAN_STRICT=1` 可以把"没有 netplan"从 skip 变成失败。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import NetConfigRequest          # noqa: E402
from app.it.netconfig.generator import generate_netconfig   # noqa: E402

from test_netconfig_golden import CASES               # noqa: E402


def _netplan_bin():
    return shutil.which("netplan")


class RealNetplanTest(unittest.TestCase):
    """netplan 产物必须能被真 netplan 加载。"""

    def setUp(self):
        self.bin = _netplan_bin()
        if not self.bin:
            if os.environ.get("OPSTK_NETPLAN_STRICT") == "1":
                self.fail("找不到 netplan；严格模式下不允许跳过"
                          "（apt-get install netplan.io）")
            self.skipTest("本机没有 netplan（apt-get install netplan.io 后可跑）")

    def test_all_netplan_cases_generate_successfully(self):
        cases = [c for c in CASES if c["req"]["format"] == "netplan"]
        self.assertGreaterEqual(len(cases), 5, "netplan 用例太少，覆盖不足")
        failures = []
        for case in cases:
            script, _fn = generate_netconfig(NetConfigRequest(**case["req"]))
            with tempfile.TemporaryDirectory() as root:
                np_dir = os.path.join(root, "etc", "netplan")
                os.makedirs(np_dir, exist_ok=True)
                path = os.path.join(np_dir, "99-opstk.yaml")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(script)
                os.chmod(path, 0o600)      # 644 会触发 netplan 的权限警告（不是错误）
                r = subprocess.run([self.bin, "generate", "--root-dir", root],
                                   capture_output=True, text=True)
            if r.returncode != 0:
                failures.append("%s:\n%s" % (case["id"],
                                             (r.stderr or r.stdout).strip()[:800]))
        self.assertEqual(failures, [],
                         "以下 netplan 产物**真 netplan 加载不了**：\n" + "\n".join(failures))

    def test_addresses6_key_is_not_used(self):
        """负向对照：`addresses6` 是**不存在的键** —— 真 netplan 必须拒绝它。

        没有这条，"我在用某个键"就可能又是一个"我以为它存在"。
        """
        with tempfile.TemporaryDirectory() as root:
            np_dir = os.path.join(root, "etc", "netplan")
            os.makedirs(np_dir, exist_ok=True)
            path = os.path.join(np_dir, "99-bad.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("network:\n  version: 2\n  ethernets:\n    eth0:\n"
                        "      addresses6: [\"2001:db8::5/64\"]\n")
            os.chmod(path, 0o600)
            r = subprocess.run([self.bin, "generate", "--root-dir", root],
                               capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "netplan 竟然接受了 addresses6（那我们的修法就白改了）")
        self.assertIn("addresses6", (r.stderr or r.stdout))


if __name__ == "__main__":
    unittest.main()
