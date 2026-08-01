"""Dashboard 汇总 API。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import models, auth
from app.database import get_db

router = APIRouter()


@router.get("/dashboard")
async def dashboard_stats(
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """返回仪表盘汇总数据。"""

    # 资产统计
    stmt = select(
        models.Asset.category,
        func.count(models.Asset.id).label("cnt"),
    ).group_by(models.Asset.category)
    rows = (await db.execute(stmt)).all()
    asset_counts = {r.category: r.cnt for r in rows}

    # 最近巡检任务 (最新 5 条)
    stmt = (
        select(models.InspectionTask)
        .order_by(models.InspectionTask.created_at.desc())
        .limit(5)
    )
    recent_tasks = (await db.execute(stmt)).scalars().all()
    tasks_out = []
    for t in recent_tasks:
        # 统计任务下的结果条数
        res_stmt = select(func.count(models.InspectionResult.id)).where(
            models.InspectionResult.task_id == t.id
        )
        result_count = (await db.execute(res_stmt)).scalar_one()
        tasks_out.append({
            "id": t.id,
            "name": t.name,
            "kind": t.kind,
            "status": t.status,
            "asset_count": len(t.asset_ids or []),
            "result_count": result_count,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })

    # 最近 PXE 安装记录 (最新 5 条)
    stmt = (
        select(models.PxeInstall)
        .order_by(models.PxeInstall.created_at.desc())
        .limit(5)
    )
    recent_installs = (await db.execute(stmt)).scalars().all()
    installs_out = [{
        "id": i.id,
        "hostname": i.hostname,
        "mac": i.mac,
        "ip": i.ip,
        "status": i.status,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    } for i in recent_installs]

    # 模板统计
    tpl_stmt = select(func.count()).select_from(models.InspectionTemplate)
    tpl_count = (await db.execute(tpl_stmt)).scalar_one()

    return {
        "assets": asset_counts,
        "recent_tasks": tasks_out,
        "recent_installs": installs_out,
        "template_count": tpl_count,
    }
