# -*- coding: utf-8 -*-
"""首次访问引导向导的接口（全部需要登录态；改设置的只有 admin）。

★ 安全铁律（外部任务书）：不新增任何**匿名可写**接口 ——
  · /setup/status            登录即可（operator 也要能看到"去完成引导"的横幅）；
  · /setup/checks            admin 只读体检；
  · /setup/network           admin 只读网卡探测（复用 pxe_server.detect_network）；
  · /system/storage          admin 只读目录盘点；
  · /setup/complete          admin 写 app_meta（唯一的写动作，且有双重前置守卫）。
向导完成后 checks / network / complete 一律 **403**（防后门）；status 永远可用，
因为顶栏横幅要靠它判断"还欠不欠引导"。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import setup as setup_core
from app.core.auth import get_current_user, require_role
from app.database import get_db

router = APIRouter()


async def _setup_done(db: AsyncSession) -> bool:
    """向导是否已完成（只看完成标记；改没改口令不影响"关闭后门"的判定）。"""
    return await setup_core.get_meta(db, setup_core.KEY_SETUP_COMPLETED_AT) is not None


@router.get("/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db),
                       _user: dict = Depends(get_current_user)):
    """向导状态（登录即可）。

    needs_setup = (setup_completed_at 不存在) AND (库看起来全新)——判据与理由见
    app/core/setup.py 的 db_looks_fresh 注释块（2026-10-08 误判修正：只看 app_meta
    标记会把"人工设口令、没用过改密接口"的在用实例误判成未安装）。
    返回字段名与旧版逐字相同（needs_setup / steps_done / 两个时间戳），契约不漂移。
    """
    return await setup_core.setup_state(db)


@router.get("/setup/checks")
async def setup_checks(db: AsyncSession = Depends(get_db),
                       _user: dict = Depends(require_role("admin"))):
    """体检清单（admin，只读）。向导完成后 403：不让向导变成常开的配置后门。"""
    if await _setup_done(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="安装引导已完成，体检接口已关闭（如需排查请用宿主机的 packaging/doctor.sh）")
    return setup_core.checks()


@router.get("/setup/network")
async def setup_network(db: AsyncSession = Depends(get_db),
                        _user: dict = Depends(require_role("admin"))):
    """网络准备（admin，只读）：复用 PXE 模块既有的网卡探测，向导第 4 步展示。

    向导完成后 403（与 checks 同口径）。返回即 pxe_server.detect_network() 的原样结构：
    只包含取到的键（interface / server_ip / gateway / dhcp_start / dhcp_end / warnings），
    非 Linux 或取不到时可能是空 dict —— 前端按"未探测到"如实展示。
    """
    if await _setup_done(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="安装引导已完成，网络探测接口已关闭")
    from app.it.pxe import server as pxe_server

    return pxe_server.detect_network()


@router.get("/system/storage")
async def system_storage(_user: dict = Depends(require_role("admin"))):
    """目录盘点（admin，只读）：向导固定路径的存在性/可写性/可用空间。"""
    return {"dirs": setup_core.storage_dirs()}


@router.post("/setup/complete")
async def setup_complete(db: AsyncSession = Depends(get_db),
                         _user: dict = Depends(require_role("admin"))):
    """完成向导（admin）。守卫：① 已改初始口令；② 体检无 error 级项。

    为什么用 400 而不是 403：403 保留给"向导已关闭"（完成后复访），
    "还没资格完成"属于可修复的业务前置不满足，与既有"业务校验失败 = 400"的口径一致。
    """
    if await _setup_done(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="安装引导已完成，无需重复提交（接口已关闭）")
    blockers = await setup_core.completion_blockers(db)
    if blockers:
        titles = "；".join(b["title"] for b in blockers)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="还不能完成安装引导：" + titles)
    completed_at = await setup_core.mark_completed(db)
    return {"ok": True, "setup_completed_at": completed_at}
