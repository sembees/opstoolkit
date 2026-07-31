"""API 路由聚合。"""
from fastapi import APIRouter

from app.api import assets, auth, inspection, netconfig, pxe

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(assets.router, prefix="/assets", tags=["资产与凭据"])
api_router.include_router(inspection.router, prefix="/ct/inspection", tags=["CT 巡检"])
api_router.include_router(netconfig.router, prefix="/it/netconfig", tags=["IT 网络配置"])
api_router.include_router(pxe.router, prefix="/it/pxe", tags=["IT PXE 装机"])
