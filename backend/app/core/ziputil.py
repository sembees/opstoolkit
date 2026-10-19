"""通用 zip 打包工具。"""
import io
import re
import urllib.parse
import zipfile

from fastapi.responses import StreamingResponse

# 文件名里只允许这些字符；其余（中文、引号、CR/LF、路径分隔符）一律折叠成 _。
# 为什么必须收窄：HTTP 头只能是 latin-1，且 `"`/CR/LF 能破坏响应头结构 —— 直传
# 中文文件名会让 Starlette 在构造响应时就抛 UnicodeEncodeError（= 下载变 500）。
# 这条口径与 `api/netconfig.py` 的 U4-F7 修复保持一致（同一课不交两遍学费）。
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _content_disposition(filename: str) -> str:
    """生成 Content-Disposition：ASCII 安全名为主，非 ASCII 时补 RFC 5987 的 filename*。

    · 纯 ASCII 文件名：输出与历史**逐字节相同**（`attachment; filename="x.zip"`），
      不改变任何既有下载行为；
    · 含中文/引号等：`filename=` 用折叠后的安全名，另给 `filename*=UTF-8''<percent>`
      —— 现代浏览器会用后者显示原名，同时响应头一定合法。
    """
    raw = (filename or "").strip()
    safe = _SAFE_NAME.sub("_", raw).strip("_")
    if not safe or safe in (".", ".."):
        safe = "download.zip"
    elif safe.startswith("."):
        safe = "file" + safe
    out = 'attachment; filename="%s"' % safe
    if safe != raw:
        out += "; filename*=UTF-8''" + urllib.parse.quote(raw, safe="")
    return out


def files_to_zip_response(files, filename="deploy.zip"):
    """把 {路径: 内容} 字典打包成 zip 并返回下载响应。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(filename)},
    )
