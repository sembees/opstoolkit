"""数据库访问函数。"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models


# ---------- 用户 ----------
async def get_user_by_username(db: AsyncSession, username: str) -> Optional[models.User]:
    res = await db.execute(select(models.User).where(models.User.username == username))
    return res.scalar_one_or_none()


# ---------- 凭据 ----------
async def list_credentials(db: AsyncSession) -> list[models.Credential]:
    res = await db.execute(select(models.Credential).order_by(models.Credential.created_at.desc()))
    return list(res.scalars().all())


async def get_credential(db: AsyncSession, cid: str) -> Optional[models.Credential]:
    return await db.get(models.Credential, cid)


async def delete_credential(db: AsyncSession, cid: str) -> None:
    obj = await db.get(models.Credential, cid)
    if obj:
        await db.delete(obj)
        await db.commit()


# ---------- 资产 ----------
async def list_assets(db: AsyncSession, category: Optional[str] = None) -> list[models.Asset]:
    stmt = select(models.Asset).order_by(models.Asset.created_at.desc())
    if category:
        stmt = stmt.where(models.Asset.category == category)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_asset(db: AsyncSession, aid: str) -> Optional[models.Asset]:
    return await db.get(models.Asset, aid)


async def delete_asset(db: AsyncSession, aid: str) -> None:
    obj = await db.get(models.Asset, aid)
    if obj:
        await db.delete(obj)
        await db.commit()


async def get_credential_for_asset(db: AsyncSession, asset: models.Asset) -> Optional[models.Credential]:
    """取资产关联凭据；若没有则不返回。"""
    if asset.credential_id:
        return await db.get(models.Credential, asset.credential_id)
    return None


async def decrypt_credential(cred: models.Credential) -> dict:
    """把凭据解密成明文字典，供 netmiko 使用。"""
    return {
        "username": cred.username,
        "password": crypto.decrypt(cred.encrypted_password),
        "enable_secret": crypto.decrypt(cred.enable_secret_encrypted),
        "ssh_key": crypto.decrypt(cred.ssh_key_encrypted),
        "port": cred.port,
    }
