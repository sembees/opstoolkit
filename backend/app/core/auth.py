"""认证：密码哈希、JWT 生成与校验。

直接使用 bcrypt 库做哈希,避免 passlib 与 bcrypt>=4.0 的版本探测不兼容
(passlib 1.7.4 仍用 bcrypt.__about__.__version__,该属性在新版已被移除)。
bcrypt 的密码上限为 72 字节,这里统一截断。
"""
from __future__ import annotations

import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.crud import get_user_by_username
from app.database import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_prefix}/auth/login")

ALGORITHM = "HS256"


def _truncate(pw: str) -> bytes:
    """截断密码至 72 字节。bcrypt 只处理前 72 字节，超出部分被忽略。"""
    return pw.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_truncate(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    """生成 JWT 令牌，设置过期时间并附带可选的额外字段。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {"sub": subject, "exp": expire}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析并验证 JWT，返回 payload；失败抛出 JWTError。"""
    payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    if payload.get("sub") is None:
        raise JWTError("missing sub")
    return payload


# 解析 Authorization: Bearer <token> 并验证用户存在性
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        username: str | None = payload.get("sub")
    except JWTError:
        raise cred_exc
    user = await get_user_by_username(db, username)
    if user is None:
        raise cred_exc
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}


def require_role(*roles: str):
    """依赖工厂：只有指定角色能调这个接口（默认 admin）。

    为什么需要（外部审查 U4-F1，我复核确认）：`get_current_user` 把 `role` 取出来了，
    但**全项目没有任何地方用它** —— 于是任意登录用户都能
    `POST /it/pxe/server/service {"action":"stop"}` 把 dnsmasq 停掉：它是 IT PXE 与
    CT ZTP **共用**的 DHCP/TFTP（红线），停掉等于整个装机网段同时失去 DHCP/TFTP。
    同理还能删 ISO、一键部署、改别人的模板。

    用法：`_user=Depends(require_role("admin"))`（放在原 `get_current_user` 的位置）。
    """
    allowed = {str(r).strip().lower() for r in roles if str(r).strip()} or {"admin"}

    async def _require_role(user: dict = Depends(get_current_user)) -> dict:
        role = str((user or {}).get("role") or "").strip().lower()
        if role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该操作需要 %s 权限（当前角色：%s）"
                       % ("/".join(sorted(allowed)), role or "未知"),
            )
        return user

    return _require_role