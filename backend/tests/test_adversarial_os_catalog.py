# -*- coding: utf-8 -*-
"""对抗性测试：PXE 系统目录（os_catalog.py）—— 审计方独立复核。

只测作者自测（test_pxe_os_catalog.py）没覆盖的边界：

  1. **确定性**：同一输入两次 pick_iso、输入顺序打乱 ⇒ 结果必须一致
     （同族多镜像时，机器重启/列表顺序不能改变挑选）。
  2. **kylin 与优麒麟（UbuntuKylin）互相冲突**：os_type=kylin 生成 kickstart，
     pick_iso(kylin) 却可能命中 Ubuntu 家族镜像（目录 note 承认冲突但写明
     "未验证此冲突的实际影响"——本用例把"实际影响"验证出来）。
  3. **同版本"live 精简镜像"与" dvd 安装镜像"并存时的择优**：live 没有
     anaconda/autoinstall 安装器，pick 中 live 装机必败 —— 排序兜底只保证
     "确定"，没保证"装得了"。

全部纯函数，零 I/O。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.pxe import os_catalog  # noqa: E402
from app.it.pxe.generator import pick_iso  # noqa: E402


def test_adversarial_pick_iso_deterministic_under_reordering():
    """同族多镜像 + 输入顺序不同 ⇒ 两次结果必须一致（契约：结果确定、可复现）。"""
    names = ["Rocky-9.4-x86_64-dvd.iso", "rocky-9.4-live-server.iso",
             "Rocky-9.4-x86_64-minimal.iso", "rocky-9.5-x86_64-dvd.iso"]
    base = pick_iso(names, "rocky", "9.4")
    for order in (sorted(names), list(reversed(names)),
                  sorted(names, key=lambda n: n[::-1])):
        assert pick_iso(list(names), "rocky", "9.4") == base
        assert pick_iso(order, "rocky", "9.4") == base, (
            "输入顺序改变了 pick 结果：%r → %r" % (order, pick_iso(order, "rocky", "9.4")))
    assert pick_iso(names, "rocky", "9.4") == pick_iso(names, "rocky", "9.4")


def test_adversarial_kylin_pick_must_not_hit_ubuntu_family_iso():
    """优麒麟(UbuntuKylin) 是 Ubuntu/casper 家族镜像；os_type=kylin 走 kickstart。

    os_catalog 的 kylin note 自己承认："pick_iso(kylin) 仍可能命中优麒麟镜像
    （未验证此冲突的实际影响）"。本用例验证影响：
      库里只有优麒麟 ISO（没装真麒麟镜像）时，pick_iso("kylin", "24.04")
      会返回 UbuntuKylin ISO ⇒ 用 kickstart/inst.* 参数去装一个 casper 镜像，
      装机必然失败。
    目录语义的正确行为：kylin 的关键字集合不该命中 ubuntu 家族文件名——
    命中不了（返回 ""）才能走"识别不出来"的人工路径。
    """
    picked = pick_iso(["UbuntuKylin-24.04.iso"], "kylin", "24.04")
    assert picked == "", (
        "os_type=kylin（kickstart 家族）命中了 Ubuntu 家族镜像 %r："
        "生成的 kickstart 会指向一个无 anaconda 的 casper 镜像，装机必败" % picked)


def test_adversarial_same_version_dvd_edition_beats_live_edition():
    """同族同版本并存"dvd（含安装器）"与"live（仅 live 环境，无安装器）"。

    pick_iso 的兜底是"文件名排序取最大"：'openeuler-24.03-live…' 与
    'openEuler-24.03-LTS-SP4…' 同分（版本段数相同、都无 live-server），
    ASCII 里小写 'l' > 大写 'L' ⇒ live 胜出。live 镜像没有安装器，
    生成 kickstart/autoinstall 后装机必然失败。
    装机目录化语义：应当优先含安装器的 dvd（live-server 优先只适用于 ubuntu）。
    """
    names = ["openEuler-24.03-LTS-SP4-x86_64-dvd.iso", "openeuler-24.03-live-x86_64.iso"]
    picked = pick_iso(names, "openeuler", "24.03")
    assert "dvd" in picked.lower(), (
        "同版本 dvd/live 并存时挑了 %r —— live 无安装器，按它生成的装机配置必然失败；"
        "结果虽确定但语义错（确定性≠正确性）" % picked)
