"""资产与凭据管理接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models
from app.core.auth import get_current_user
from app.core.schemas import AssetIn, AssetOut, CredentialIn, CredentialOut
from app.ct.drivers import infer_netmiko_device_type
from app.database import get_db

router = APIRouter()


# ---------- 凭据 ----------
def _cred_out(c: models.Credential) -> CredentialOut:
    return CredentialOut(
        id=c.id, name=c.name, username=c.username, device_type=c.device_type,
        port=c.port, remark=c.remark, created_at=c.created_at,
        has_password=bool(c.encrypted_password),
        has_ssh_key=bool(c.ssh_key_encrypted),
    )


@router.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.Credential).order_by(models.Credential.created_at.desc()))
    return [_cred_out(c) for c in res.scalars().all()]


@router.post("/credentials", response_model=CredentialOut)
async def create_credential(body: CredentialIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    c = models.Credential(
        name=body.name, username=body.username, device_type=body.device_type,
        port=body.port, remark=body.remark,
        encrypted_password=crypto.encrypt(body.password),
        ssh_key_encrypted=crypto.encrypt(body.ssh_key),
        enable_secret_encrypted=crypto.encrypt(body.enable_secret),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return _cred_out(c)


@router.put("/credentials/{cid}", response_model=CredentialOut)
async def update_credential(cid: str, body: CredentialIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    c = await db.get(models.Credential, cid)
    if not c:
        raise HTTPException(status_code=404, detail="凭据不存在")
    c.name = body.name
    c.username = body.username
    c.device_type = body.device_type
    c.port = body.port
    c.remark = body.remark
    if body.password is not None:
        c.encrypted_password = crypto.encrypt(body.password)
    if body.ssh_key is not None:
        c.ssh_key_encrypted = crypto.encrypt(body.ssh_key)
    if body.enable_secret is not None:
        c.enable_secret_encrypted = crypto.encrypt(body.enable_secret)
    await db.commit()
    await db.refresh(c)
    return _cred_out(c)


@router.delete("/credentials/{cid}")
async def delete_credential(cid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    c = await db.get(models.Credential, cid)
    if c:
        used = (await db.execute(
            select(models.Asset.id, models.Asset.name).where(models.Asset.credential_id == cid)
        )).all()
        if used:
            names = "、".join(name for _, name in used)
            raise HTTPException(status_code=409, detail="凭据正被资产 [" + names + "]，请先解除关联")
        await db.delete(c)
        await db.commit()
    return {"ok": True}


# ---------- 资产 ----------
def _asset_out(a: models.Asset) -> AssetOut:
    return AssetOut(
        id=a.id, name=a.name, category=a.category, vendor=a.vendor,
        device_role=a.device_role, host=a.host, port=a.port,
        device_type=a.device_type, serial=a.serial, mac=a.mac,
        location=a.location, tags=a.tags or {}, remark=a.remark,
        credential_id=a.credential_id, created_at=a.created_at,
    )


@router.get("", response_model=list[AssetOut])
# GET /api/assets — 资产列表，可按 category 筛选
async def list_assets(category: str | None = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    stmt = select(models.Asset).order_by(models.Asset.created_at.desc())
    if category:
        stmt = stmt.where(models.Asset.category == category)
    res = await db.execute(stmt)
    return [_asset_out(a) for a in res.scalars().all()]


@router.post("", response_model=AssetOut)
# POST /api/assets — 新建资产，自动关联凭据
async def create_asset(body: AssetIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    device_type = body.device_type or infer_netmiko_device_type(body.vendor, body.device_role)
    a = models.Asset(
        name=body.name, category=body.category, vendor=(body.vendor or "").strip().lower(),
        device_role=body.device_role, host=body.host, port=body.port,
        device_type=device_type, serial=body.serial, mac=body.mac,
        location=body.location, tags=body.tags, remark=body.remark,
        credential_id=body.credential_id,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return _asset_out(a)


@router.put("/{aid}", response_model=AssetOut)
# PUT /api/assets/{aid} — 更新资产信息
async def update_asset(aid: str, body: AssetIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    a = await db.get(models.Asset, aid)
    if not a:
        raise HTTPException(status_code=404, detail="资产不存在")
    a.name = body.name
    a.category = body.category
    a.vendor = (body.vendor or "").strip().lower()
    a.device_role = body.device_role
    a.host = body.host
    a.port = body.port
    a.device_type = body.device_type or infer_netmiko_device_type(body.vendor, body.device_role)
    a.serial = body.serial
    a.mac = body.mac
    a.location = body.location
    a.tags = body.tags
    a.remark = body.remark
    a.credential_id = body.credential_id
    await db.commit()
    await db.refresh(a)
    return _asset_out(a)


@router.delete("/{aid}")
async def delete_asset(aid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    a = await db.get(models.Asset, aid)
    if a:
        await db.delete(a)
        await db.commit()
    return {"ok": True}
