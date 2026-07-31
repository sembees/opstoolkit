"""通用 zip 打包工具。"""
import io
import zipfile

from fastapi.responses import StreamingResponse


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
        headers={"Content-Disposition": "attachment; filename=" + chr(34) + filename + chr(34)},
    )
