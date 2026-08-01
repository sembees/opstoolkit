"""CT 巡检接口：默认巡检、自定义命令、WebSocket 实时回显、任务查询。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crud, models
from app.core.auth import decode_access_token, get_current_user
from app.core.schemas import InspectionCreate, InspectionResultOut, InspectionTaskOut, InspectionTemplateIn, InspectionTemplateOut
from app.ct.drivers import DRIVERS, get_driver
from app.ct.drivers.base import MetricCommand
from app.ct.inspection.service import inspect_many, inspect_one, run_task_in_background, schedule_background
from app.database import async_session, get_db

router = APIRouter()


@router.get("/templates")
# GET /api/ct/inspection/templates — 巡检模板列表
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
# POST /api/ct/inspection/run — 发起巡检任务（后台执行）
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
    token = websocket.query_params.get("token", "")
    try:
        payload = decode_access_token(token)
        username = payload.get("sub")
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401, reason="未授权")
        return
    try:
        raw = await websocket.receive_text()
        cfg = json.loads(raw)
        asset_ids = cfg.get("asset_ids", [])
        kind = cfg.get("kind", "default")
        template = cfg.get("template")
        commands = cfg.get("commands")

        async with async_session() as db:
            user = await crud.get_user_by_username(db, username) if username else None
            if user is None:
                await websocket.close(code=4401, reason="未授权")
                return

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
    """创建巡检任务，立即返回；后台异步执行并写入结果。"""
    task = models.InspectionTask(
        name=body.name, kind=body.kind, template=body.template,
        commands=body.commands, asset_ids=body.asset_ids,
        status="running", created_by=user.get("id"),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    schedule_background(run_task_in_background(task.id))
    return task


@router.get("/tasks", response_model=list[InspectionTaskOut])
async def list_tasks(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.InspectionTask).order_by(models.InspectionTask.created_at.desc()).limit(100))
    return list(res.scalars().all())


@router.get("/tasks/{task_id}/results", response_model=list[InspectionResultOut])
async def get_task_results(task_id: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.InspectionResult).where(models.InspectionResult.task_id == task_id))
    return list(res.scalars().all())


# ---------- 巡检模板管理 ----------
@router.get('/templates/list', response_model=list[InspectionTemplateOut])
async def list_templates_db(vendor: str = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    stmt = select(models.InspectionTemplate).order_by(models.InspectionTemplate.vendor, models.InspectionTemplate.created_at)
    if vendor:
        stmt = stmt.where(func.lower(models.InspectionTemplate.vendor) == vendor.strip().lower())
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.post('/templates', response_model=InspectionTemplateOut)
async def create_template(body: InspectionTemplateIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = models.InspectionTemplate(name=body.name, vendor=body.vendor, is_system=False,
        items=[it.model_dump() for it in body.items], description=body.description)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@router.put('/templates/{tid}', response_model=InspectionTemplateOut)
async def update_template(tid: str, body: InspectionTemplateIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = await db.get(models.InspectionTemplate, tid)
    if not t:
        raise HTTPException(status_code=404, detail='模板不存在')
    if t.is_system:
        raise HTTPException(status_code=403, detail='系统内置模板不可编辑，请先克隆')
    t.name = body.name
    t.vendor = body.vendor
    t.items = [it.model_dump() for it in body.items]
    t.description = body.description
    await db.commit()
    await db.refresh(t)
    return t


@router.post('/templates/{tid}/clone', response_model=InspectionTemplateOut)
async def clone_template(tid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    src = await db.get(models.InspectionTemplate, tid)
    if not src:
        raise HTTPException(status_code=404, detail='模板不存在')
    t = models.InspectionTemplate(name=src.name + ' (副本)', vendor=src.vendor, is_system=False,
        items=list(src.items), description=src.description)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@router.delete('/templates/{tid}')
async def delete_template(tid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = await db.get(models.InspectionTemplate, tid)
    if not t:
        return {'ok': True}
    if t.is_system:
        raise HTTPException(status_code=403, detail='系统内置模板不可删除')
    await db.delete(t)
    await db.commit()
    return {'ok': True}
