"""告警规则 CRUD + 告警记录查询。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth, models
from app.database import get_db

router = APIRouter()


@router.get("/rules")
async def list_rules(
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出所有告警规则。"""
    stmt = select(models.AlertRule).order_by(models.AlertRule.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id, "name": r.name, "metric_key": r.metric_key,
            "operator": r.operator, "threshold": r.threshold,
            "enabled": r.enabled, "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/rules")
async def create_rule(
    body: dict,
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建告警规则。"""
    r = models.AlertRule(
        name=body.get("name", "").strip(),
        metric_key=body.get("metric_key", "").strip(),
        operator=body.get("operator", "gt"),
        threshold=float(body.get("threshold", 0)),
        enabled=body.get("enabled", True),
    )
    if not r.name or not r.metric_key:
        raise HTTPException(status_code=400, detail="名称和指标 key 不能为空")
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return {"id": r.id, "name": r.name, "metric_key": r.metric_key,
            "operator": r.operator, "threshold": r.threshold, "enabled": r.enabled}


@router.put("/rules/{rid}")
async def update_rule(
    rid: str, body: dict,
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新告警规则。"""
    r = await db.get(models.AlertRule, rid)
    if not r:
        raise HTTPException(status_code=404, detail="规则不存在")
    for key in ("name", "metric_key", "operator"):
        if key in body:
            setattr(r, key, body[key])
    if "threshold" in body:
        r.threshold = float(body["threshold"])
    if "enabled" in body:
        r.enabled = bool(body["enabled"])
    await db.commit()
    return {"ok": True}


@router.delete("/rules/{rid}")
async def delete_rule(
    rid: str,
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除告警规则。"""
    r = await db.get(models.AlertRule, rid)
    if r:
        await db.delete(r)
        await db.commit()
    return {"ok": True}


@router.get("/history")
async def alert_history(
    limit: int = Query(20, le=100),
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询最近告警记录。告警信息存在巡检结果中带 error 的记录。"""
    stmt = (
        select(models.InspectionResult)
        .where(models.InspectionResult.status == "failed")
        .order_by(desc(models.InspectionResult.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id, "asset_id": r.asset_id, "asset_name": r.asset_name,
            "error": r.error, "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
