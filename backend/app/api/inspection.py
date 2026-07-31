"""CT 巡检接口：默认巡检、自定义命令、WebSocket 实时回显、任务查询。"""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crud, models
from app.core.auth import get_current_user
from app.core.schemas import InspectionCreate, InspectionResultOut, InspectionTaskOut
from app.ct.drivers import DRIVERS, get_driver
from app.ct.drivers.base import MetricCommand
from app.ct.inspection.service import inspect_many, inspect_one
from app.database import async_session, get_db

router = APIRouter()


@router.get("/templates")
async def list_templates(_user=Depends(get_current_user)):
    """列出各厂商可用的默认巡检模板与指标项。"""
    out = {}
    for vendor, cls in DRIVERS.items():
        drv = cls()
        templates = drv.templates()
        out[vendor] = {
            name: [
                {"key": mc.key, "label": mc.label, "command": mc.command}
                for mc in cmds
            ]
            for name, cmds in templates.items()
        }
    return out


@router.post("/run")
async def run_inspection(
    body: InspectionCreate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """同步执行巡检并返回全部结果（无实时流，适合小批量）。"""
    results = await inspect_many(
        db, body.asset_ids, body.kind, body.template, body.commands
    )
    return {"kind": body.kind, "results": results}


@router.websocket("/ws")
async def inspection_ws(websocket: WebSocket):
    """WebSocket 实时巡检：客户端发送配置，服务端流式返回每条命令输出。"""
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        cfg = json.loads(raw)
        asset_ids = cfg.get("asset_ids", [])
        kind = cfg.get("kind", "default")
        template = cfg.get("template")
        commands = cfg.get("commands")

        async with async_session() as db:

            async def on_event(ev: dict):
                try:
                    await websocket.send_json(ev)
                except Exception:  # noqa: BLE001
                    pass

            results = await inspect_many(db, asset_ids, kind, template, commands, on_event)
            await websocket.send_json({"type": "complete", "results": results})
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "fatal", "error": str(e)})
        except Exception:  # noqa: BLE001
            pass


@router.post("/tasks", response_model=InspectionTaskOut)
async def create_task(
    body: InspectionCreate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """创建巡检任务记录并立即执行，结果入库。"""
    task = models.InspectionTask(
        name=body.name, kind=body.kind, template=body.template,
        commands=body.commands, asset_ids=body.asset_ids,
        status="running", created_by=user.get("id"),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    results = await inspect_many(db, body.asset_ids, body.kind, body.template, body.commands)
    for r in results:
        db.add(models.InspectionResult(
            task_id=task.id, asset_id=r["asset_id"], asset_name=r["asset_name"],
            status=r["status"], error=r.get("error", ""),
            metrics=r.get("metrics", {}), raw=r.get("raw", []),
        ))
    task.status = "done"
    task.finished_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/tasks", response_model=list[InspectionTaskOut])
async def list_tasks(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.InspectionTask).order_by(models.InspectionTask.created_at.desc()).limit(100))
    return list(res.scalars().all())


@router.get("/tasks/{task_id}/results", response_model=list[InspectionResultOut])
async def get_task_results(task_id: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.InspectionResult).where(models.InspectionResult.task_id == task_id))
    return list(res.scalars().all())
