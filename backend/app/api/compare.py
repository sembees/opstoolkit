"""巡检结果对比 API。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth, models
from app.database import get_db

router = APIRouter()


@router.get("/results/compare")
async def compare_results(
    asset_id: str = Query(...),
    limit: int = Query(2, ge=2, le=10),
    _user=Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """对比某台设备最近 N 次巡检结果的指标变化。

    返回格式:
    {
        "asset_id": "...",
        "snapshots": [
            {"task_name": "...", "time": "...", "metrics": {"cpu": {...}, ...}},
            ...
        ],
        "delta": {"cpu": {"prev": 30, "latest": 45, "change": "+15%", "trend": "up"}, ...}
    }
    """
    # 查询最近 N 条结果
    stmt = (
        select(models.InspectionResult)
        .where(models.InspectionResult.asset_id == asset_id)
        .order_by(desc(models.InspectionResult.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    if len(rows) < 2:
        return {
            "asset_id": asset_id,
            "snapshots": [
                {
                    "task_name": f"result-{r.id[:8]}",
                    "time": r.created_at.isoformat() if r.created_at else None,
                    "metrics": r.metrics or {},
                }
                for r in rows
            ],
            "delta": {},
            "note": "需要至少 2 次巡检记录才能对比" if len(rows) < 2 else None,
        }

    # 按时间倒序，第一个是最新
    snapshots = [
        {
            "task_name": f"result-{r.id[:8]}",
            "time": r.created_at.isoformat() if r.created_at else None,
            "metrics": r.metrics or {},
        }
        for r in rows
    ]

    latest = snapshots[0]["metrics"]
    prev = snapshots[1]["metrics"]

    delta = {}
    for key, lv in latest.items():
        pv = prev.get(key)
        if not pv or not isinstance(lv, dict) or not isinstance(pv, dict):
            continue
        l_status = lv.get("status", "unknown")
        p_status = pv.get("status", "unknown")
        # 数值对比
        # ★ 2026-10-19 修：原来写的是 `lv.get("value") or lv` —— 0 是合法数值，
        #   但 0 是假值，于是会退回整个 dict，把 "0 → 45" 判成 "changed"（趋势与
        #   change 全丢）。运维看到的就是"指标变了但说不清变成多少"。
        #   这里改成"有 value 键就用它，哪怕它是 0"。
        l_val = lv["value"] if ("value" in lv) else lv
        p_val = pv["value"] if ("value" in pv) else pv
        if isinstance(l_val, dict) and isinstance(p_val, dict):
            # 嵌套对象，如 temperature/device/fan 等
            if l_val == p_val:
                delta[key] = {"trend": "same", "summary": lv.get("summary", "")}
            else:
                delta[key] = {"trend": "changed", "prev_summary": pv.get("summary", ""), "latest_summary": lv.get("summary", "")}
        elif isinstance(l_val, (int, float)) and isinstance(p_val, (int, float)):
            diff = l_val - p_val
            trend = "up" if diff > 0 else ("down" if diff < 0 else "same")
            delta[key] = {
                "prev": p_val,
                "latest": l_val,
                "change": f"{'+' if diff > 0 else ''}{round(diff, 1)}",
                "trend": trend,
                "prev_status": p_status,
                "latest_status": l_status,
            }
        else:
            delta[key] = {"trend": "same" if l_val == p_val else "changed"}

    return {
        "asset_id": asset_id,
        "snapshots": snapshots,
        "delta": delta,
    }
