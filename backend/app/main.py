"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

# 部署时把前端构建产物挂到根路径（可选）
try:
    from pathlib import Path

    _dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if _dist.exists():
        app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
except Exception:  # noqa: BLE001
    pass
