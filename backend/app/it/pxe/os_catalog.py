# -*- coding: utf-8 -*-
"""PXE 支持的系统目录（catalog）—— **唯一**定义点。

为什么要有这个模块（背景：用户实测反馈）：
  用户把 `openEuler-24.03-LTS-SP4.iso` 放进 /srv/opstk/iso 后，
  「提取」界面选不到 openEuler、装机模板的「系统」下拉也只有 Ubuntu/RHEL 两项 ——
  因为旧实现把系统种类散在多处硬编码：
    · generator._ISO_TYPE_KEYS（pick_iso / 卷标识别）
    · generator.RHEL_FAMILY（走 anaconda 分支）
    · schemas._OS_TYPE_ALLOWED（保存模板时的白名单）
    · server._ALLOWED_OS_TYPES（提取时的白名单）
    · 前端 Pxe.vue 三处下拉各写一份 <el-option>
  加一个发行版要改 N 处，漏一处就是"加了镜像却选不到"。本模块把
  **每个系统一条记录**集中在这里，其余所有地方都从它派生：
    · generator：_ISO_TYPE_KEYS / RHEL_FAMILY / is_rhel_family / 分支判断
    · schemas：_OS_TYPE_ALLOWED（ids + aliases）
    · server：_ALLOWED_OS_TYPES（ids + aliases）与提取候选路径
    · api：GET /api/it/pxe/os-catalog（前端三个下拉与识别的唯一来源）
    · 前端：不再维护第二份清单（接口失败时的降级从界面已有数据收集，无硬编码）

每条记录的字段语义：
  key                os_type 的存储值（模板/介质目录名），小写。
  display            界面显示名。
  installer          安装器家族：kickstart(anaconda) / preseed(debian-installer) /
                     autoyast(YaST)。**生成逻辑按家族复用，不按发行版复制**。
  auto_install       本工具是否实现了该家族的自动装机生成。
                     目前 kickstart(RHEL 系) 与 ubuntu(autoinstall) 为 True；
                     debian(presseed)/opensuse(autoyast) 为 False —— 只支持
                     识别 ISO + 提取引导介质，生成配置时会给出明确拒绝提示。
  filename_keywords  ISO **文件名**关键字（pick_iso 与前端"从文件名识别"共用）。
  label_keywords     ISO **卷标**关键字。ISO9660 卷标不含 '.'，匹配前先把
                     '-'/'_' 归一化成 '.'（归一化实现在 transfers.detect_os_from_label）。
  aliases            os_type 别名（历史数据兼容），如 alma = almalinux。
  kernel_dirs        ISO 内内核/initrd 的候选目录（按序尝试，命中即用）。
  kernel_names       内核文件名候选。
  initrd_names       initrd 文件名候选。
  dest_initrd        提取落盘时的 initrd 文件名（kickstart 系是 initrd.img，
                     ubuntu/debian 沿用 initrd —— 与 _default_media/media_list 一致）。
  dest_squashfs      casper squashfs 的落盘名（只有 ubuntu 有）。
  version_hints      提取界面"版本"下拉的常见版本候选（提示值，不承诺全部存在）。
  verified           引导路径/卷标规则是否经过验证。**False = 未验证**：
                     按 RHEL 系惯例填写，绝不编造；提取按候选路径逐个尝试，
                     找不到会如实写日志、不会误拷别的文件。

verified 的口径（诚实声明，报告里同样适用）：
  · verified=True  —— 既有代码与真机串口实证覆盖过：ubuntu(casper/)、
    rhel/centos/rocky/almalinux（images/pxeboot/，kickstart 链路有真机验收）。
  · 其余一律 verified=False（含 openeuler/kylin/uos/anolis/fedora/oraclelinux/
    debian/opensuse 的引导路径与卷标）—— 它们按家族惯例给出候选，**未经本环境实测**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 安装器家族常量（api 载荷里原样返回，前端据此分组/提示）
KICKSTART = "kickstart"
PRESEED = "preseed"
AUTOYAST = "autoyast"

# os_version 目录名白名单（提取时用）：字母数字点下划线中划线，与 server._VERSION_RE 同源
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 文件名/卷标里的版本抽取规则：与 generator._ISO_VER_RE 同一个正则（数字段边界），
# 这里独立写一份以避免 os_catalog 反向依赖 generator（generator 才是依赖本模块的一方）。
VER_RE = re.compile(r"(?<![0-9])(\d+(?:\.\d+)*)(?![0-9])")


@dataclass(frozen=True)
class OsCatalogEntry:
    key: str
    display: str
    installer: str
    auto_install: bool
    filename_keywords: tuple
    label_keywords: tuple
    aliases: tuple = ()
    kernel_dirs: tuple = ()
    kernel_names: tuple = ()
    initrd_names: tuple = ()
    dest_initrd: str = "initrd.img"
    dest_squashfs: str = ""
    version_hints: tuple = ()
    verified: bool = False
    # pick_keywords：pick_iso 用的关键字集；留空 = 用 filename_keywords。
    # 只有 rhel 需要：它是整个 RHEL 家族的"伞"，旧实现（及其测试）让
    # pick_iso("rhel", …) 在整个家族里挑镜像 —— 保持该语义不变。
    pick_keywords: tuple = ()
    # pick_exclude_keywords：pick_iso 的排除关键字 —— 文件名命中任一关键字的
    # 同时还含排除词的，直接跳过（审计 D9：跨家族镜像装机必败，宁可不挑、
    # 走"识别不出来"的人工路径）。只有 kylin 需要：优麒麟(UbuntuKylin) 文件名
    # 同时含 "ubuntu" 与 "kylin"，但它是 ubuntu/casper 家族镜像、没有 anaconda，
    # 用 kylin 的 kickstart/inst.* 去装必然失败。不误伤正例：银河麒麟官方 ISO
    # （Kylin-Server-/Kylin-Desktop-…）文件名从不带 ubuntu；优麒麟 ISO 文件名
    # 必带 ubuntukylin。ubuntu 自己的 pick 仍能挑中优麒麟镜像（与"最长关键字
    # 识别判给 ubuntu"的既有语义一致）。
    pick_exclude_keywords: tuple = ()
    # live_is_installer：官方安装介质本身就是 live 形态（casper+subiquity 的
    # live-server），目前只有 ubuntu 是。对它 pick 保持既有"live-server 优先"；
    # anaconda 系的 live/desktop/workstation 是"无安装器"的减分形态（审计 D10，
    # 分层实现见 generator.pick_iso）。
    live_is_installer: bool = False
    note: str = ""


_OS = (
    # ── 已验证的两族（既有行为，字节级 golden 锁住生成物）───────────────
    OsCatalogEntry(
        key="ubuntu", display="Ubuntu", installer=PRESEED, auto_install=True,
        filename_keywords=("ubuntu",), label_keywords=("ubuntu",),
        kernel_dirs=("casper",), kernel_names=("vmlinuz",), initrd_names=("initrd",),
        dest_initrd="initrd", dest_squashfs="installer.squashfs",
        live_is_installer=True,
        version_hints=("22.04", "24.04", "20.04"), verified=True,
        note=("Ubuntu 22.04+ 实际生成的是 autoinstall(subiquity) user-data（本工具既有实现）；"
              "installer=preseed 只表示其引导/安装器家族。"),
    ),
    OsCatalogEntry(
        key="rhel", display="Red Hat Enterprise Linux", installer=KICKSTART, auto_install=True,
        filename_keywords=("rhel", "redhat"), label_keywords=("rhel", "redhat"),
        aliases=("redhat",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("8.10", "9.4", "9.5", "9.6"), verified=True,
        pick_keywords=("rhel", "redhat", "centos", "rocky", "almalinux", "oraclelinux"),
        note=("rhel 是整个 RHEL 家族的伞：pick_iso(rhel) 在家族内按版本挑选（既有语义，"
              "有测试锁住）；新增的 openeuler/kylin 等不并入这把伞，避免版本串族。"),
    ),
    OsCatalogEntry(
        key="rocky", display="Rocky Linux", installer=KICKSTART, auto_install=True,
        filename_keywords=("rocky",), label_keywords=("rocky",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("8.10", "9.4", "9.5", "9.6"), verified=True,
    ),
    OsCatalogEntry(
        key="almalinux", display="AlmaLinux", installer=KICKSTART, auto_install=True,
        filename_keywords=("almalinux", "alma"), label_keywords=("almalinux", "alma"),
        aliases=("alma",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("8.10", "9.4", "9.5", "9.6"), verified=True,
    ),
    OsCatalogEntry(
        key="centos", display="CentOS / CentOS Stream", installer=KICKSTART, auto_install=True,
        filename_keywords=("centos",), label_keywords=("centos",),
        aliases=("centos-stream",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("7.9", "9", "10"), verified=True,
        note="CentOS 7/8 与 CentOS Stream 同属一族；os_type=centos-stream 归入 centos。",
    ),
    # ── 新增 RHEL 系（kickstart 家族，引导路径按惯例填写，未验证）────────
    OsCatalogEntry(
        key="oraclelinux", display="Oracle Linux", installer=KICKSTART, auto_install=True,
        filename_keywords=("oraclelinux",), label_keywords=("oraclelinux",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("8.10", "9.4", "9.5"), verified=False,
        note=("未验证：引导路径按 RHEL 系惯例填 images/pxeboot；卷标识别只认含 oraclelinux "
              "的卷标（Oracle 官方卷标形如 OL-9-x 的说法未验证，未纳入匹配）。"),
    ),
    OsCatalogEntry(
        key="openeuler", display="openEuler", installer=KICKSTART, auto_install=True,
        filename_keywords=("openeuler",), label_keywords=("openeuler",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("20.03", "22.03", "24.03", "25.03"), verified=False,
        note=("未验证：openEuler 为 RHEL 系（anaconda + kickstart），引导文件按惯例在 "
              "images/pxeboot/；本环境无 openEuler 真机/ISO，未实测，提取按候选路径尝试。"),
    ),
    OsCatalogEntry(
        key="kylin", display="银河麒麟 Kylin", installer=KICKSTART, auto_install=True,
        filename_keywords=("kylin",), label_keywords=("kylin",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("10",), verified=False,
        pick_exclude_keywords=("ubuntu",),
        note=("未验证：麒麟服务器版 V10 按 RHEL 系惯例填 images/pxeboot。注意：优麒麟"
              "（UbuntuKylin）文件名也含 kylin —— 按最长关键字识别会判给 ubuntu；"
              "pick_iso(kylin) 则排除含 ubuntu 的文件名（优麒麟是 casper 家族，"
              "kickstart/inst.* 装不了它），库里只有优麒麟 ISO 时返回空串、走"
              "\"识别不出来\"的人工路径。version_hints 校准为文件名里的真实版本"
              " token \"10\"（V10 的数字段；旧值 v10/v10-sp3 在 pick_iso 的数字段"
              "边界匹配下永远命中不了，只会在下拉里给出选不中的死值）。"),
    ),
    OsCatalogEntry(
        key="uos", display="统信 UOS", installer=KICKSTART, auto_install=True,
        filename_keywords=("uos",), label_keywords=("uos",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("20", "1060", "1070"), verified=False,
        note=("未验证：UOS 服务器版的安装器与 anaconda 的兼容性未实测（关键字 uos 是短"
              "子串，理论上存在误命中其他文件名的可能）；按任务要求归入 kickstart 家族"
              "复用 ks 生成，装机失败请以手工安装兜底。"),
    ),
    OsCatalogEntry(
        key="anolis", display="龙蜥 Anolis OS", installer=KICKSTART, auto_install=True,
        filename_keywords=("anolis",), label_keywords=("anolis",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("8.9", "8.10", "23"), verified=False,
        note="未验证：龙蜥为 RHEL 系（anaconda），引导文件按惯例在 images/pxeboot（未实测）。",
    ),
    OsCatalogEntry(
        key="fedora", display="Fedora Server", installer=KICKSTART, auto_install=True,
        filename_keywords=("fedora",), label_keywords=("fedora",),
        kernel_dirs=("images/pxeboot",), kernel_names=("vmlinuz",), initrd_names=("initrd.img",),
        version_hints=("40", "41", "42"), verified=False,
        note=("未验证：Fedora 为 RHEL 上游（anaconda），引导文件按惯例在 images/pxeboot；"
              "版本迭代快，kickstart 语法偶有变化，装机前请先小范围验证。"),
    ),
    # ── 只识别 + 提取，暂不支持自动安装（生成配置时明确拒绝）────────────
    OsCatalogEntry(
        key="debian", display="Debian", installer=PRESEED, auto_install=True,  # ★ 2026-10-09：preseed 家族已实现（generator._debian_preseed）；verified 待真机装机通过后再翻
        filename_keywords=("debian",), label_keywords=("debian",),
        kernel_dirs=("install.amd", "install", "install.386"),
        kernel_names=("vmlinuz", "linux"), initrd_names=("initrd.gz", "initrd.img", "initrd"),
        dest_initrd="initrd", dest_squashfs="",
        version_hints=("11", "12", "13"), verified=False,
        note=("暂不支持自动安装：debian-installer 的 preseed 未实现（与 Ubuntu subiquity "
              "的 autoinstall 不是同一套语法，不能硬凑）。目前支持按 debian 识别 ISO 与"
              "提取引导介质（引导路径候选 install.amd|install|install.386，未验证 —— "
              "未在本环境实测，提取按候选路径逐个尝试）。"),
    ),
    OsCatalogEntry(
        key="opensuse", display="openSUSE", installer=AUTOYAST, auto_install=False,
        filename_keywords=("opensuse", "suse"), label_keywords=("opensuse", "suse"),
        kernel_dirs=("boot/x86_64/loader",),
        kernel_names=("vmlinuz",), initrd_names=("initrd",),
        version_hints=("15.5", "15.6"), verified=False,
        note=("暂不支持自动安装：AutoYaST 未实现。目前支持按 opensuse 识别 ISO 与提取"
              "引导介质（引导路径候选 boot/x86_64/loader/，未验证 —— 未在本环境实测）。"),
    ),
)

# ── 派生量（其余模块一律从这里取，不再各写一份）────────────────────────

_INDEX = {e.key: e for e in _OS}
for _e in _OS:
    for _a in _e.aliases:
        _INDEX[_a] = _e

_OS_ORDER = tuple(e.key for e in _OS)


def entries() -> tuple:
    """按显示顺序返回全部条目。"""
    return _OS


def entry(os_type) -> OsCatalogEntry | None:
    """os_type（含别名，大小写/空白不敏感）→ 目录条目；未知返回 None。"""
    return _INDEX.get((os_type or "").strip().lower()) or None


def all_os_types() -> tuple:
    """全部可作 os_type 存储的值（ids + 别名），供 schemas/server 白名单派生。"""
    return tuple(_INDEX.keys())


def kickstart_family() -> tuple:
    """kickstart(anaconda) 家族的主 id 列表（generator.RHEL_FAMILY 由此派生）。"""
    return tuple(e.key for e in _OS if e.installer == KICKSTART)


def is_kickstart(os_type) -> bool:
    """是否 kickstart(anaconda) 家族（含别名）。"""
    e = entry(os_type)
    return e is not None and e.installer == KICKSTART


def pick_keywords_for(os_type) -> tuple:
    """pick_iso 用的文件名关键字（rhel 伞 = 家族联合，其余 = 自己的关键字）。"""
    e = entry(os_type)
    if e is None:
        return ((os_type or "").strip().lower(),)
    return e.pick_keywords or e.filename_keywords


def detect_os_from_name(name) -> dict:
    """ISO 文件名 → (os_type, os_version)。

    与 transfers.detect_os_from_label 同一套路：
      · 关键字按"最长命中"挑（ubuntukylin 同时命中 ubuntu/kylin 时判给 ubuntu）；
      · 版本按数字段边界抽取，取第一个候选（版本段在架构段之前）；
      · 任一为空 ⇒ 两者都空，不抛错。
    """
    low = str(name or "").strip().lower()
    norm = low.replace("-", ".").replace("_", ".")
    os_type = ""
    for e in _OS:
        for k in e.filename_keywords + e.aliases:
            if k and k in norm and len(k) > len(os_type):
                os_type = k
    vers = VER_RE.findall(low)
    os_version = vers[0] if vers else ""
    if os_type and os_type in _INDEX and os_type not in _OS_ORDER:
        # 别名归一到主 id（alma → almalinux），与前端识别语义一致
        os_type = _INDEX[os_type].key
    if not os_type or not os_version:
        return {"os_type": "", "os_version": ""}
    return {"os_type": os_type, "os_version": os_version}


def extract_allowed(os_type) -> bool:
    """提取接口是否放行该 os_type（含别名）。"""
    return entry(os_type) is not None


def api_payload() -> dict:
    """GET /api/it/pxe/os-catalog 的载荷：前端三个下拉与识别的唯一来源。

    字段名与前端 Pxe.vue 的消费一一对应；aliases 让前端把历史模板里的
    alma/redhat/centos-stream 也映射到主 id。
    """
    return {
        "items": [
            {
                "id": e.key,
                "name": e.display,
                "family": e.installer,
                "auto_install": e.auto_install,
                "aliases": list(e.aliases),
                "filename_keywords": list(e.filename_keywords),
                "label_keywords": list(e.label_keywords),
                "kernel_dirs": list(e.kernel_dirs),
                "kernel_names": list(e.kernel_names),
                "initrd_names": list(e.initrd_names),
                "dest_initrd": e.dest_initrd,
                "dest_squashfs": e.dest_squashfs,
                "version_hints": list(e.version_hints),
                "verified": e.verified,
                "note": e.note,
            }
            for e in _OS
        ],
    }
