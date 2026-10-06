"""CT 巡检接口：默认巡检、自定义命令、WebSocket 实时回显、任务查询。"""
from __future__ import annotations

import asyncio
import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crud, models
from app.core.auth import decode_access_token, get_current_user
from app.core.schemas import InspectionCreate, InspectionResultOut, InspectionTaskOut, InspectionTemplateIn, InspectionTemplateOut
from app.ct.drivers import DRIVERS
# ★ 2026-10-05 修：这里原来只 import 了 run_task_in_background / schedule_background，
#   而下面 POST /ct/inspection/run 调用了 inspect_many ⇒ 该端点一跑就 NameError
#   （前端走 WebSocket 不碰它，所以一直没暴露；API 直连/脚本会踩）。补回 inspect_many。
from app.ct.inspection.service import inspect_many, run_task_in_background, schedule_background
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

            # ── 进度统计 + 事件转发 ────────────────────────────────────────────
            # ★ 这里必须用**同步**回调 + 队列，不能让服务层直接 await：
            #   服务层是同步 `emit(ev)` 调用的，以前直接把 `async def on_event` 传进去，
            #   每次 emit 只得到一个没人 await 的协程 ⇒ **一个事件都发不出去**，
            #   前端永远停在它自己写的那行"连接已建立"，看着就像界面卡死。
            #   而且 `_connect_and_run` 跑在 `asyncio.to_thread` 里，回调来自**工作线程**，
            #   所以要用 `call_soon_threadsafe` 把事件投进事件循环上的队列。
            total = len(asset_ids)
            counters = {"started": 0, "done": 0, "failed": 0}
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def on_event(ev: dict):
                t = ev.get("type")
                if t == "start":
                    counters["started"] += 1
                elif t == "done":
                    counters["done"] += 1
                elif t == "error":
                    counters["failed"] += 1
                loop.call_soon_threadsafe(queue.put_nowait, dict(ev))
                if t in ("start", "done", "error"):
                    loop.call_soon_threadsafe(queue.put_nowait, {
                        "type": "progress", "started": counters["started"],
                        "done": counters["done"], "failed": counters["failed"], "total": total,
                    })

            async def drain():
                """把队列里的事件发出去；客户端断开时静默丢弃（但巡检继续跑完并落库）。"""
                while True:
                    ev = await queue.get()
                    if ev is None:
                        return
                    try:
                        await websocket.send_json(ev)
                    except Exception:  # noqa: BLE001
                        pass

            # ★ 任务**先落库**：这样历史记录/仪表盘计数是真的，也才有"可下载"的数据源；
            #   客户端半路关掉页面也不影响结果落库（以前 WS 跑完什么都不存）。
            task = models.InspectionTask(
                name="巡检 %d 台设备" % total + (("（%s 模板）" % template) if template else ""),
                kind=kind, template=template,
                commands=list(commands) if commands else None,
                asset_ids=list(asset_ids), status="running", created_by=user.id,
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            drainer = asyncio.create_task(drain())
            try:
                await websocket.send_json({"type": "task", "task_id": task.id, "name": task.name})
                # 复用后台那条链路（含输出日志、告警检查、InspectionResult 落库），
                # 只多传一个同步回调用于直播 —— 不再各写一套。
                results = await run_task_in_background(task.id, on_event=on_event) or []
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)
                await drainer
            await websocket.send_json({"type": "complete", "task_id": task.id, "results": results})
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


def _render_task_export(task, results, fmt: str):
    """把一次巡检任务渲染成导出内容 → (bytes, media_type, 扩展名)。

    三种格式的取舍：
      · txt  —— 给人看的报告：每台设备一段，逐条命令的**原始输出**原样贴出；
      · json —— 给程序用：任务元信息 + 每台设备的指标与原始结果，结构完整；
      · csv  —— 给表格用：一台设备×一条命令一行（摘要级），长输出不塞进 CSV。
    """
    if fmt == "json":
        payload = {
            "task": {
                "id": task.id, "name": task.name, "kind": task.kind, "template": task.template,
                "status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            },
            "results": [{
                "asset_id": r.asset_id, "asset_name": r.asset_name, "status": r.status,
                "error": r.error or "", "metrics": r.metrics or {}, "raw": r.raw or [],
            } for r in results],
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json; charset=utf-8", "json")

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["设备", "设备ID", "设备状态", "命令", "指标标签", "解析状态", "摘要"])
        for r in results:
            for item in (r.raw or []):
                w.writerow([r.asset_name or "", r.asset_id, r.status, item.get("cmd", ""),
                            item.get("label", ""), item.get("status", ""), item.get("summary", "")])
        # ★ 前置 BOM：否则 Excel 打开中文 CSV 会乱码
        return (("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", "csv")

    ok = sum(1 for r in results if r.status == "success")
    lines = [
        "OpsToolkit CT 巡检报告",
        "任务：%s" % task.name,
        "任务 ID：%s" % task.id,
        "类型：%s%s" % (task.kind, ("    模板：%s" % task.template) if task.template else ""),
        "状态：%s    设备：成功 %d / 共 %d" % (task.status, ok, len(results)),
        "开始：%s    结束：%s" % (task.created_at, task.finished_at),
    ]
    for r in results:
        lines += ["", "=" * 78,
                  "设备：%s（%s）    状态：%s" % (r.asset_name or r.asset_id, r.asset_id, r.status)]
        if r.error:
            lines.append("错误：%s" % r.error)
        for item in (r.raw or []):
            lines += ["-" * 78, "$ %s" % item.get("cmd", "")]
            if item.get("summary"):
                lines.append("# 解析：%s（%s）" % (item.get("summary"), item.get("status", "")))
            lines.append(str(item.get("output") or "").rstrip())
    return (("\n".join(lines) + "\n").encode("utf-8"), "text/plain; charset=utf-8", "txt")


@router.post("/tasks/{tid}/export")
async def export_task(tid: str, body: dict = None, db: AsyncSession = Depends(get_db),
                      _user=Depends(get_current_user)):
    """导出一次巡检的结果（原始输出 + 指标）：fmt = txt / json / csv。

    为什么是 POST 而不是直链 GET：前端 token 存在 localStorage（不是 cookie），
    `<a href>` 直链带不上鉴权头；所以走 POST + blob，复用 `api/index.js` 的 downloadZip。
    """
    fmt = ((body or {}).get("fmt") or "txt").lower()
    task = await db.get(models.InspectionTask, tid)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    res = await db.execute(
        select(models.InspectionResult).where(models.InspectionResult.task_id == tid)
    )
    results = list(res.scalars().all())
    content, media, ext = _render_task_export(task, results, fmt)
    stamp = task.created_at.strftime("%Y%m%d-%H%M") if task.created_at else "report"
    name = "inspection-%s-%s.%s" % (task.id[:8], stamp, ext)
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})


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
@router.get('/templates/{tid}/export')
async def export_template(tid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """导出模板为 JSON，用于备份和迁移。"""
    t = await db.get(models.InspectionTemplate, tid)
    if not t:
        raise HTTPException(status_code=404, detail='模板不存在')
    return {
        "name": t.name,
        "vendor": t.vendor,
        "description": t.description,
        "items": t.items,
    }


@router.post('/templates/import')
async def import_template(body: dict, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """从 JSON 导入模板。"""
    name = (body.get("name") or "").strip()
    vendor = (body.get("vendor") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail='模板名称不能为空')
    items = body.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail='items 必须是数组')
    t = models.InspectionTemplate(
        name=name,
        vendor=vendor,
        is_system=False,
        items=items,
        description=body.get("description", ""),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t




@router.get("/tasks/{task_id}/replay")
async def replay_task(task_id: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """回放任务的实时输出日志。"""
    t = await db.get(models.InspectionTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": t.id,
        "name": t.name,
        "status": t.status,
        "asset_count": len(t.asset_ids or []),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "log": t.output_log or [],
    }



# ★ 缺陷修复（Help 审计发现，见 mimo/out/help-claims.md）：这个装饰器原先被错接在
#   `replay_task` 上面（两行装饰器叠在同一个函数上），导致
#     · 不存在的 `delete_template`（下面那个函数）**没有任何路由**、永远不可达；
#     · 而 `DELETE /templates/{tid}` 实际会去调 `replay_task(tid)` —— 拿模板 id 当任务 id 查，
#       于是"删除用户模板"在界面上永远失败（或返回错东西）。
#   这也是"Help 说用户模板可增删改"与实现矛盾的根因。此处归位，并由用例钉住路由归属。
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
