# -*- coding: utf-8 -*-
"""`core/ziputil.py` 的用例（审计 P1：这个模块此前**零测试引用**）。

为什么值得测：它是"把生成物打包给运维下载"的唯一出口（PXE 生成配置的 ZIP、ZTP 下载等），
一旦文件名/编码/内容出问题，运维拿到的是**打不开或内容错**的包 —— 而且现场很难第一时间
判断是生成错了还是打包错了。这里锁住四件事：
  1. 内容与文件名逐字节往返（含 bytes、str、子目录、空包）；
  2. 压缩方式确实是 DEFLATE（体积敏感：ISO 相关的文本包）；
  3. Content-Disposition 里的文件名；
  4. 中文文件名不能把响应头搞崩（HTTP 头是 latin-1，这一点最容易踩）。
"""
import asyncio
import io
import sys
import urllib.parse
import zipfile

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.core.ziputil import files_to_zip_response  # noqa: E402


def _body(resp) -> bytes:
    """把 StreamingResponse 的异步迭代器读成 bytes。"""
    async def _go():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
        return b"".join(chunks)

    return asyncio.run(_go())


def _read_zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def test_roundtrip_str_and_bytes():
    files = {
        "ifcfg-eth0": "DEVICE=eth0\nBOOTPROTO=static\n",
        "route6-eth0": "2001:db8:98::/64 via 2001:db8:80::1\n",
        "raw.bin": b"\x00\x01\x02\xff",
    }
    zf = _read_zip(_body(files_to_zip_response(files)))
    assert sorted(zf.namelist()) == sorted(files)
    for name, content in files.items():
        want = content if isinstance(content, bytes) else content.encode()
        assert zf.read(name) == want, name


def test_subdirectory_names_are_preserved():
    """生成物里有 `profiles/<id>/ks.cfg` 这种嵌套路径，打包后目录结构不能丢。"""
    files = {"profiles/p1/ks.cfg": "text", "profiles/p1/user-data": "#cloud-config\n"}
    zf = _read_zip(_body(files_to_zip_response(files)))
    assert "profiles/p1/ks.cfg" in zf.namelist()


def test_empty_dict_makes_valid_zip():
    """没有任何文件时也必须是**合法 zip**（前端会直接保存，坏了就是"下载了 0 字节"）。"""
    data = _body(files_to_zip_response({}))
    zf = _read_zip(data)
    assert zf.namelist() == []
    assert len(data) > 0        # 至少要有 zip 的中央目录


def test_compression_method_is_deflate():
    """大段重复文本必须被压缩（近 1MB 的重复内容压完应远小于原文）。"""
    text = "x" * 400_000
    data = _body(files_to_zip_response({"big.txt": text}))
    zf = _read_zip(data)
    info = zf.getinfo("big.txt")
    assert info.compress_type == zipfile.ZIP_DEFLATED
    assert len(data) < len(text) // 10, (len(data), len(text))


def test_content_disposition_default_filename():
    resp = files_to_zip_response({})
    assert resp.headers["content-disposition"] == 'attachment; filename="deploy.zip"'
    assert resp.media_type == "application/zip"


def test_content_disposition_custom_filename():
    resp = files_to_zip_response({}, filename="ops-netconfig.zip")
    assert resp.headers["content-disposition"] == 'attachment; filename="ops-netconfig.zip"'


def test_non_ascii_filename_is_safe_and_rfc5987():
    """中文文件名：不能让构造响应抛错（HTTP 头是 latin-1），且要给出可读的原名。

    修复前（2026-10-19 实测）：`files_to_zip_response({}, filename="网络配置.zip")` 在
    Starlette 里抛 UnicodeEncodeError ⇒ 任何用中文名下载的接口都是 500。
    现在的契约：`filename=` 只用 ASCII 安全名，另加 `filename*=UTF-8''<percent>`。
    """
    resp = files_to_zip_response({}, filename="网络配置.zip")
    cd = resp.headers["content-disposition"]
    assert cd.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in cd
    assert "\u7f51" not in cd            # 不能把中文原样塞进头
    # percent-encoded 的中文能解回原名
    encoded = cd.split("filename*=UTF-8''", 1)[1]
    assert urllib.parse.unquote(encoded) == "网络配置.zip"
    # 头整体必须是 latin-1 可编码的（这正是原来的崩溃点）
    cd.encode("latin-1")


def test_header_injection_is_neutralised():
    """文件名里的引号与 CR/LF 不能破坏响应头（否则可被用来注入任意头）。"""
    resp = files_to_zip_response({}, filename='a"b\r\nX-Evil: 1.zip')
    cd = resp.headers["content-disposition"]
    assert cd.count('"') == 2, cd       # 只有包裹文件名的那一对引号
    assert "\r" not in cd and "\n" not in cd
    assert "X-Evil" not in cd or "filename*=" in cd


def test_ascii_filename_output_is_unchanged():
    """纯 ASCII 文件名保持历史输出（不额外带 filename*，避免影响既有客户端）。"""
    resp = files_to_zip_response({}, filename="pxe-deploy.zip")
    assert resp.headers["content-disposition"] == 'attachment; filename="pxe-deploy.zip"'
