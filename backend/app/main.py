"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.crypto import ensure_secret_key

    ensure_secret_key()
    await init_db()
    from app.ct.inspection.service import recover_interrupted_tasks

    await recover_interrupted_tasks()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, debug=settings.debug)

# 允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import api_router  # noqa: E402

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


# PXE/ZTP HTTP 文件服务 (本机部署后生效，必须在根路径前注册)
# 如果本机已部署 PXE/ZTP ，挂载静态文件服务
try:
    from pathlib import Path

    _pxe_web = Path("/srv/opstk/pxe-web")
    if _pxe_web.is_dir():
        app.mount("/pxe/serve", StaticFiles(directory=str(_pxe_web)), name="pxe-serve")
except Exception:  # noqa: BLE001
    pass

try:
    from pathlib import Path

    _ztp_web = Path("/srv/opstk/ztp-web")
    if _ztp_web.is_dir():
        app.mount("/ztp", StaticFiles(directory=str(_ztp_web)), name="ztp-serve")
except Exception:  # noqa: BLE001
    pass

# 装机镜像（ISO）单独挂一个静态路径。
# 现有的 ISO 上传/列表/删除都作用于 /srv/opstk/iso，所以挂这一个目录就实现
# "上传完即可用"，且支持任意多个不同系统的镜像。
# casper 的 url=<iso> 会把这个文件取到内存并 loop 挂载，再从里面找 casper/*.squashfs——
# 这是 live 介质能被找到的前提（只给一个裸 squashfs 是不行的）。
class _IsoOnlyStatic(StaticFiles):
    """只对外提供 *.iso。

    /srv/opstk/iso 除了镜像，还可能混进运维产物（例如 download-iso.sh、
    download-complete.flag）。装机网段通常是无认证的，这些文件不该被任何人 HTTP 取走，
    所以这里只放行 .iso，其余一律 404（目录列表本来就没开）。
    """

    async def get_response(self, path, scope):
        if not str(path).lower().endswith(".iso"):
            return Response(status_code=404)
        return await super().get_response(path, scope)


try:
    from pathlib import Path

    _pxe_iso = Path("/srv/opstk/iso")
    if _pxe_iso.is_dir():
        app.mount("/pxe/iso", _IsoOnlyStatic(directory=str(_pxe_iso)), name="pxe-iso")
except Exception:  # noqa: BLE001
    pass

# 部署时把前端构建产物挂到根路径（可选）
# 前端 SPA 静态页（若已构建），使用 SPA fallback 路由
try:
    from pathlib import Path

    _dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if _dist.exists():
        # 挂载 assets 为静态文件（JS/CSS/图片）
        app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="frontend-assets")

        # SPA fallback：所有非 API 路由返回 index.html（Vue Router history mode）
        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            # 未匹配的 /api/* 必须返回 404 JSON，不能被 SPA 兜底吞成 200+HTML
            if full_path.startswith(settings.api_prefix.strip("/")):
                raise HTTPException(status_code=404, detail="Not Found")
            index = _dist / "index.html"
            if index.exists():
                return FileResponse(index)
            return {"detail": "Not Found"}
except Exception:  # noqa: BLE001
    pass
