"""IT 网络配置生成接口。"""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.core.auth import get_current_user
from app.core.schemas import NetConfigRequest
from app.it.netconfig.generator import BOND_MODE_NAMES, generate_netconfig

router = APIRouter()


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
            {"id": "netplan", "name": "netplan (仅 Ubuntu)"},
        ],
        "netplan_renderers": [
            {"id": "networkd", "name": "networkd (服务器静态IP推荐)"},
            {"id": "NetworkManager", "name": "NetworkManager (无线/动态认证)"},
        ],
    }


@router.post("/generate")
async def generate(body: NetConfigRequest, _user=Depends(get_current_user)):
    """生成网络配置脚本，返回 JSON（含脚本内容与文件名）。"""
    script, filename = generate_netconfig(body)
    return {"script": script, "format": body.format, "filename": filename}


@router.post("/download", response_class=PlainTextResponse)
async def download(body: NetConfigRequest, _user=Depends(get_current_user)):
    """直接下载生成的脚本文件。"""
    script, _ = generate_netconfig(body)
    return PlainTextResponse(script, media_type="text/x-shellscript")
