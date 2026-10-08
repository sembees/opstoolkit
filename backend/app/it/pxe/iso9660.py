# -*- coding: utf-8 -*-
"""最小 ISO9660 只读解析器 —— PXE「提取引导介质」在 loop 挂载失败时的兜底。

背景（生产实测，应用机容器内对 openEuler-24.03-LTS-SP4.iso 提取失败）：
  server.extract_from_iso 走 `mount -o loop,ro` 挂 ISO，util-linux 的 mount
  解析 `-o loop` 时**不再扫描现存的空闲 loop 节点**，而是向 /dev/loop-control
  现取一个空闲设备号（比如 2），随后打开 /dev/loop2 —— 而容器 /dev 里往往只有
  loop0/loop1 两个节点（且常被宿主侧的 overlay/snap 长期占用），节点不存在
  就整体失败：`mount: ...: failed to setup loop device for ...iso`。
  在离线、无特权/无 loop 的主机上"必须挂载才能提取"是脆弱设计，所以提供
  不依赖 loop 的兜底：纯标准库解析 ISO9660，直接把引导文件读出来写盘。

设计边界（刻意写死，防止这里长成通用 ISO 库 —— 调用方只提取内核/initrd/squashfs）：
  · 只认 Primary Volume Descriptor（PVD，扇区 16，字节偏移 0x8000，标识 "CD001"）
    + 目录记录的逐层查找与文件读出；不实现路径表查找、多卷集、HFS；
  · 名字匹配用 ISO9660 **主名字集**：大小写不敏感、忽略 ";1" 版本后缀、
    目录名末尾的 "."（ISO9660 的现实形态：目录记录形如 `IMAGES.`，
    文件形如 `VMLINUZ.;1` / `INITRD.IMG;1`）。**不解析 Joliet(UCS-2) /
    Rock Ridge(NM)** —— 按家族惯例，需要提取的引导文件名（vmlinuz /
    initrd.img / casper/initrd 等）都在 8.3 内，主名字集足以命中；万一真有
    镜像改写了主名字集，结果是如实报「没找到」，绝不会错拷别的文件；
  · 支持多 extent 文件（ECMA-119 目录标志 bit 7）：大 initrd 被拆成多个
    extent 时按序拼接读出（规范要求同名记录连续，见 §6.8.1.1）；
  · 文件按 1MB 块流式写出：4.6GB 的 ISO 全程只占常数内存，绝不整文件读入。

字段速查（实现依据 ECMA-119，全部小端在先、大端重复在后）：
  · PVD：0=类型(1)，1..5="CD001"，6=版本；80..87=卷空间大小(总扇区)；
    128..131=逻辑块大小(u16 双端)；156..189=根目录记录(34 字节)；
  · 目录记录：0=记录长度；2..9=extent LBA；10..17=数据长度；25=文件标志
    （bit1=目录，bit7=还有后续 extent）；28..31=卷序号（双端）；
    32=文件名长度；33=文件名（其后补齐到偶数字节）；
    记录不跨扇区（§6.8.1.2），扇区尾部 0 填充。
"""
from __future__ import annotations

import os

SECTOR = 2048           # 寻址粒度；真实逻辑块大小以 PVD 偏移 128 字段为准
PVD_OFFSET = 16 * SECTOR  # 0x8000：Primary Volume Descriptor 固定在扇区 16
MAGIC = b"CD001"
READ_CHUNK = 1 << 20      # 文件读出按 1MB 流式写，控制内存


class IsoError(ValueError):
    """ISO9660 结构不符合预期（坏 PVD / 目录越界 / extent 截断）。"""


def _u16(raw, off):
    return raw[off] | (raw[off + 1] << 8)


def _u32(raw, off):
    return (raw[off] | (raw[off + 1] << 8) | (raw[off + 2] << 16)
            | (raw[off + 3] << 24))


def _norm_name(name_raw: bytes) -> str:
    """目录记录文件名 → 匹配键：去 ";版本"、去目录名末尾 "."、转大写。

    这是 ISO9660 名字匹配的全部坑所在：
      · `vmlinuz` 记作 `VMLINUZ.;1`（无扩展名也要补点做版本分隔）；
      · `initrd.img` 记作 `INITRD.IMG;1`；
      · 目录 `images` 记作 `IMAGES.`（有的镜像带 ";1"）；
      · 也有镜像不写版本后缀、甚至写小写（genisoimage -J -l 风格）。
    统一归一后比较，两边都走同一个函数，谁也不迁就谁。
    """
    s = name_raw.decode("ascii", "replace")
    if ";" in s:
        s = s.split(";", 1)[0]
    return s.rstrip(".").upper()


def _parse_record(raw: bytes):
    """解析一条目录记录；长度字段为 0 表示目录扇区尾部填充（返回 None）。"""
    ln = raw[0] if raw else 0
    if ln == 0:
        return None
    if ln < 34:
        raise IsoError("目录记录长度异常: %d" % ln)
    if len(raw) < ln:
        raise IsoError("目录记录被截断（应有 %d 字节，只有 %d）" % (ln, len(raw)))
    name_len = raw[32]
    if 33 + name_len > ln:
        raise IsoError("目录记录文件名超出记录长度")
    return {
        "extent": _u32(raw, 2),
        "size": _u32(raw, 10),
        "flags": raw[25],
        "name_raw": bytes(raw[33:33 + name_len]),
    }


class Iso9660:
    """只读打开一个 ISO9660 镜像：解析 PVD，按路径逐层查目录、读文件。"""

    def __init__(self, path):
        self.fh = open(path, "rb")
        try:
            self.file_size = os.fstat(self.fh.fileno()).st_size
            self.fh.seek(PVD_OFFSET)
            pvd = self.fh.read(SECTOR)
            if len(pvd) < SECTOR or pvd[0] != 1 or bytes(pvd[1:6]) != MAGIC:
                raise IsoError("不是 ISO9660 镜像（0x8000 处没有 CD001 主卷描述符）")
            self.block = _u16(pvd, 128) or SECTOR
            if not (512 <= self.block <= 32768):
                raise IsoError("ISO9660 逻辑块大小异常: %d" % self.block)
            root = _parse_record(pvd[156:190])
            if root is None or not (root["flags"] & 2):
                raise IsoError("PVD 根目录记录无效")
            self.root = root
        except Exception:
            self.fh.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()
        return False

    # ── 目录解析 ──

    def _dir_records(self, extent, size):
        """按序产出目录 extent 里的每条记录（扇区尾部 0 填充自动跳过）。"""
        start = extent * self.block
        if start + size > self.file_size:
            raise IsoError("目录 extent 越界: 扇区 %d" % extent)
        self.fh.seek(start)
        buf = self.fh.read(size)
        if len(buf) != size:
            raise IsoError("目录 extent 读取不完整: 扇区 %d" % extent)
        off = 0
        while off < len(buf):
            ln = buf[off]
            if ln == 0:
                # 目录记录不跨扇区：剩下的 0 填充直接跳到下一个扇区边界
                off = (off // self.block + 1) * self.block
                continue
            rec = _parse_record(buf[off:off + ln])
            off += ln
            if rec is not None:
                yield rec

    def _dir_entries(self, extent, size):
        """目录条目（合并多 extent 文件：同名连续记录按 ECMA-119 拼接）。"""
        entries = []
        for rec in self._dir_records(extent, size):
            key = _norm_name(rec["name_raw"])
            if (entries and entries[-1]["key"] == key
                    and entries[-1]["flags"] & 0x80):
                # 前一条声明"还有后续 extent"，本条就是同一文件的下一块
                entries[-1]["extents"].append((rec["extent"], rec["size"]))
                entries[-1]["flags"] = rec["flags"]
                continue
            entries.append({
                "key": key,
                "name_raw": rec["name_raw"],
                "flags": rec["flags"],
                # extent/size = 首个 extent 的定位（多 extent 文件的完整链在
                # "extents" 里；目录只有一条 extent，供 lookup 逐层下钻用）
                "extent": rec["extent"],
                "size": rec["size"],
                "extents": [(rec["extent"], rec["size"])],
            })
        return entries

    def lookup(self, rel_path):
        """按 'images/pxeboot/vmlinuz' 这类相对路径查文件；找不到返回 None。

        中间层必须是目录、最后一段必须是文件 —— 候选路径写错成目录名时
        返回 None（如实说"没找到"），不会把目录当文件拷出去。
        """
        parts = [p for p in str(rel_path).split("/") if p]
        if not parts:
            return None
        cur = self.root
        for i, part in enumerate(parts):
            want = _norm_name(part.encode("ascii", "replace"))
            found = None
            for ent in self._dir_entries(cur["extent"], cur["size"]):
                if ent["key"] == want:
                    found = ent
                    break
            if found is None:
                return None
            is_dir = bool(found["flags"] & 2)
            if is_dir != (i < len(parts) - 1):
                return None
            cur = found
        return cur

    # ── 文件读出 ──

    def _check_extent(self, extent, size):
        start = extent * self.block
        if start + size > self.file_size:
            raise IsoError("文件 extent 越界: 扇区 %d（长度 %d）" % (extent, size))

    def write_file(self, entry, out_path) -> int:
        """把文件条目按 extent 链流式写出；返回写入字节数。"""
        written = 0
        with open(out_path, "wb") as out:
            for extent, size in entry["extents"]:
                self._check_extent(extent, size)
                self.fh.seek(extent * self.block)
                remaining = size
                while remaining > 0:
                    chunk = self.fh.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        raise IsoError("extent 数据不完整: 扇区 %d" % extent)
                    out.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        return written


def extract(iso_path, wanted, dest_dir, log) -> list:
    """按候选相对路径从 ISO 提取文件，写到 dest_dir/<落盘名>。

    wanted: [(落盘名, [候选相对路径…])] —— 候选顺序由调用方按 os_catalog
    的 kernel_dirs × kernel_names 拼出（与挂载路径逐个尝试的顺序一致），
    本函数只负责"逐个试、命中即用、每一步都写日志"。
    返回成功提取的落盘名列表（顺序与 wanted 一致）。
    """
    extracted = []
    with Iso9660(iso_path) as iso:
        for dest_name, candidates in wanted:
            hit = None
            for rel in candidates:
                entry = iso.lookup(rel)
                if entry is not None:
                    hit = (rel, entry)
                    break
            if hit is None:
                log.append("ISO9660: " + dest_name + " 未找到（试过候选路径: "
                           + ", ".join(candidates) + "）")
                continue
            rel, entry = hit
            n = iso.write_file(entry, os.path.join(dest_dir, dest_name))
            extracted.append(dest_name)
            log.append("ISO9660: " + rel + " -> " + dest_name
                       + "（" + str(round(n / 1048576, 1)) + "MB）")
    return extracted


def extract_largest(iso_path, dir_relpath, suffix, dest_path, log):
    """在 dir_relpath 目录里找后缀匹配的最大文件并写盘（squashfs 兜底用）。

    与挂载路径同口径：同目录多个匹配时取**体积最大**的那个。
    命中返回镜像里的原始名字（主名字集、去 ";1"，小写展示）；
    目录不存在或没有匹配文件返回 None，绝不抛错打断提取。
    """
    parts = [p for p in str(dir_relpath).split("/") if p]
    if not parts:
        return None
    with Iso9660(iso_path) as iso:
        # 逐层走到目标目录（lookup 只返回文件，这里手动逐层）
        cur = iso.root
        for part in parts:
            want = _norm_name(part.encode("ascii", "replace"))
            found = None
            for ent in iso._dir_entries(cur["extent"], cur["size"]):
                if ent["key"] == want and ent["flags"] & 2:
                    found = ent
                    break
            if found is None:
                log.append("ISO9660: 目录 " + dir_relpath + " 不存在（找 "
                           + suffix + " 跳过）")
                return None
            cur = found
        want_suffix = suffix.upper()
        cands = [(sum(size for _e, size in e["extents"]), e)
                 for e in iso._dir_entries(cur["extent"], cur["size"])
                 if not (e["flags"] & 2) and e["key"].endswith(want_suffix)]
        if not cands:
            return None
        cands.sort(key=lambda t: t[0], reverse=True)
        size, entry = cands[0]
        orig = _norm_name(entry["name_raw"]).lower()
        iso.write_file(entry, dest_path)
        log.append("ISO9660: " + dir_relpath + "/" + orig
                   + " -> " + os.path.basename(dest_path)
                   + "（" + str(round(size / 1048576, 1)) + "MB）")
        return orig
