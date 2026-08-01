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
    return pw.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_truncate(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
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