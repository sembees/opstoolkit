"""IT 网络配置生成接口。"""
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import models
from app.core.auth import get_current_user
from app.core.schemas import NetConfigRequest
from app.database import get_db
from app.it.netconfig import conflicts
from app.it.netconfig.generator import BOND_MODE_NAMES, generate_netconfig

router = APIRouter()


async def _dhcp_pool_warnings(body: NetConfigRequest, db: AsyncSession) -> list:
    """H：把「静态 IP 落在 DHCP 池内」的冲突算出来（只警告，不拦）。

    池有两处来源：PXE 模板（池在 net_config 里）与 ZTP 模板（dhcp_start/dhcp_end）。
    任何一步出错都不该让生成接口失败 —— 这只是一条提示。
    """
    try:
        pxes = (await db.execute(select(models.PxeProfile))).scalars().all()
        ztps = (await db.execute(select(models.ZtpTemplate))).scalars().all()
        pools = (conflicts.pools_from_pxe_profiles(pxes)
                 + conflicts.pools_from_ztp_templates(ztps))
        return conflicts.pool_conflicts(conflicts.collect_static_addresses(body), pools)
    except Exception:            # noqa: BLE001 —— 提示性检查绝不能把生成接口带崩
        return []


@router.get("/meta")
async def get_meta(_user=Depends(get_current_user)):
    """返回聚合模式等元数据，供前端构建表单下拉。"""
    return {
        "bond_modes": [{"id": k, "name": v} for k, v in BOND_MODE_NAMES.items()],
        "os_options": [
            {"id": "ubuntu", "name": "Ubuntu 22.04+"},
            {"id": "rhel", "name": "RHEL / Rocky / Alma 8+"},
        ],
        "formats": [
            {"id": "nmcli", "name": "nmcli 脚本 (通用)"},
            {"id": "netplan", "name": "netplan (Ubuntu)"},
            {"id": "ifcfg", "name": "ifcfg (RHEL, 无需 NetworkManager)"},
        ],
        "netplan_renderers": [
            {"id": "networkd", "name": "networkd (服务器静态IP推荐)"},
            {"id": "NetworkManager", "name": "NetworkManager (无线/动态认证)"},
        ],
    }


@router.post("/generate")
async def generate(body: NetConfigRequest, db: AsyncSession = Depends(get_db),
                   _user=Depends(get_current_user)):
    """生成网络配置脚本，返回 JSON（含脚本内容与文件名）。

    `warnings`：跨模块一致性提示（H）—— 静态 IP 若落在 PXE/ZTP 的 DHCP 池内，
    可能和正在装机的机器撞成同一个地址。只是提示，不影响生成结果。
    """
    try:
        script, filename = generate_netconfig(body)
    except ValueError as e:
        # ★ 外部审查 U4-F9：生成期校验失败（os×format 组合、掩码/cidr 非法、
        #   注入字符被拦…）必须与 pxe 侧一致地报 422 + 可读中文，
        #   而不是 500 "Internal Server Error"（前端无法区分"请求有问题"和"后端坏了"）。
        raise HTTPException(status_code=422, detail=str(e))
    return {"script": script, "format": body.format, "filename": filename,
            "warnings": await _dhcp_pool_warnings(body, db)}


@router.post("/download", response_class=PlainTextResponse)
async def download(body: NetConfigRequest, _user=Depends(get_current_user)):
    """直接下载生成的脚本文件。"""
    try:
        script, _ = generate_netconfig(body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    from fastapi.responses import Response
    # ★ 外部审查 U4-F7：`filename="..."` 里直接拼 hostname —— 非 ASCII（中文主机名）
    #   会让 Starlette 的 latin-1 编码抛 UnicodeEncodeError（500），`"`/CR/LF 还能破坏
    #   响应头结构。这里把文件名收窄到安全字符集，空则退回固定名。
    host = re.sub(r"[^A-Za-z0-9._-]", "_", (body.hostname or ""))[:64].strip("._-")
    filename = (host + "-" if host else "") + (
        "99-opstk.yaml" if body.format == "netplan"
        else ("ifcfg-files.txt" if body.format == "ifcfg" else "apply-network.sh"))
    return Response(script, media_type="text/plain",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % filename})
