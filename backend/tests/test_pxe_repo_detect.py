# -*- coding: utf-8 -*-
"""`_detect_rhel_media` 的仓库探测回归（2026-10-08 真机发现的缺陷）。

缺陷：同级目录扫描没有区分"同一张 DVD 内的兄弟仓库"与"别的发布介质树"。
后果（真机实测）：单仓库 DVD（openEuler 24.03：`Packages/`+`repodata/` 直接躺在树根）
会被塞进一条 `repo --name="centos-7"`，iPXE 内核行也多了
`inst.addrepo=centos-7,<url>` —— **openEuler 24.03 因此根本不取 kickstart**
（安装器退回交互式主菜单；手工去掉那条 addrepo 后立刻开始抓 ks，见 RUNBOOK §5.83.44/45）。

本单元钉两条：
  · 单仓库树（mirror 本身就是仓库根）→ extra 必须为空；
  · 多仓库 DVD（mirror 是树根、主仓库是其子目录）→ 仍要认出 AppStream（这条是既有行为，不能回归掉）。
"""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.api import pxe as pxe_api  # noqa: E402


def _touch(p: pathlib.Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


class DetectRhelMediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        # 让 _local_served_path 把 /pxe/serve/<token>/<rel> 映射到本临时目录
        self.patch = mock.patch.object(pxe_api, "WEB_ROOT", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _url(self, rel):
        # 必须用**真实的 serve 前缀**（带真 token）：_local_served_path 会先剥 token 再映射回
        # WEB_ROOT，写死一个假 token 会让它映射到错误的目录（第一版测试就因此把 stage2 判成空）。
        base = pxe_api.serve_token.serve_base("192.168.199.1").rstrip("/")
        return base + "/" + rel.strip("/") + "/"

    def test_single_repo_root_gets_no_extra_repos(self):
        """单仓库 DVD（openEuler 那种）：树根就是仓库，同级是别的介质树 ⇒ 不得当额外仓库。"""
        # 我们的发布目录：repo/openeuler-24.03（仓库根）+ 两个"别的介质树"
        _touch(self.root / "repo/openeuler-24.03/repodata/repomd.xml")
        (self.root / "repo/openeuler-24.03/images").mkdir(parents=True, exist_ok=True)
        _touch(self.root / "repo/centos-7/repodata/repomd.xml")
        _touch(self.root / "repo/rocky-9.4/BaseOS/repodata/repomd.xml")

        repo, stage2, extra = pxe_api._detect_rhel_media(
            self._url("repo/openeuler-24.03"), "192.168.199.1")
        self.assertEqual(extra, [], "单仓库树不该产生额外仓库（会把 centos-7 塞进 ks/iPXE）")
        self.assertIn("repo/openeuler-24.03/", repo)
        self.assertIn("repo/openeuler-24.03/", stage2, "stage2 应指向含 images/ 的那层（即树根）")

    def test_multi_repo_dvd_still_finds_appstream(self):
        """多仓库 DVD（RHEL/Rocky）：mirror 是树根、主仓库是 BaseOS ⇒ 必须仍认出 AppStream。"""
        _touch(self.root / "repo/rocky-9.4/BaseOS/repodata/repomd.xml")
        _touch(self.root / "repo/rocky-9.4/AppStream/repodata/repomd.xml")
        (self.root / "repo/rocky-9.4/images").mkdir(parents=True, exist_ok=True)

        repo, stage2, extra = pxe_api._detect_rhel_media(
            self._url("repo/rocky-9.4"), "192.168.199.1")
        names = [e["name"] for e in extra]
        # 生产实际（Rocky 9.4 的落盘 ks）就是：url --url=…/AppStream/  +  repo --name="BaseOS"
        # —— 下探取的是**字母序第一个**含 repodata 的子目录（AppStream 在 BaseOS 之前）。
        self.assertEqual(names, ["BaseOS"], "同级仓库必须被认出来（少了它会在认证步骤崩）")
        self.assertIn("AppStream/", repo, "主仓库落在字母序第一个子目录上（与生产一致）")
        self.assertIn("repo/rocky-9.4/", stage2)

    def test_remote_mirror_untouched(self):
        """远端/官方镜像（不带 /pxe/serve/）不猜：原样返回、无额外仓库、无 stage2。"""
        repo, stage2, extra = pxe_api._detect_rhel_media(
            "http://mirror.example/rocky/9/BaseOS/x86_64/os/", "192.168.199.1")
        self.assertEqual(extra, [])
        self.assertEqual(stage2, "")
        self.assertEqual(repo, "http://mirror.example/rocky/9/BaseOS/x86_64/os/")


if __name__ == "__main__":
    unittest.main()
