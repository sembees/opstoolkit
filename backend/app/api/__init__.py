"""API 路由聚合。"""
from fastapi import APIRouter

from app.api import alerts, assets, auth, compare, dashboard, inspection, netconfig, pxe, ztp

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(assets.router, prefix="/assets", tags=["资产与凭据"])
api_router.include_router(inspection.router, prefix="/ct/inspection", tags=["CT 巡检"])
api_router.include_router(compare.router, prefix="/ct/inspection", tags=["CT 巡检对比"])
api_router.include_router(netconfig.router, prefix="/it/netconfig", tags=["IT 网络配置"])
api_router.include_router(pxe.router, prefix="/it/pxe", tags=["IT PXE 装机"])

api_router.include_router(ztp.router, prefix="/ct/ztp", tags=["CT ZTP 开局"])

api_router.include_router(dashboard.router, tags=["仪表盘"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["告警规则"])