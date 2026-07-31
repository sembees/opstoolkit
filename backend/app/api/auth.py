"""认证接口。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crud
from app.core.auth import create_access_token, get_current_user, verify_password
from app.core.schemas import LoginIn, TokenOut
from app.database import get_db

router = APIRouter()


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = await crud.get_user_by_username(db, body.username)
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(user.username, {"role": user.role, "name": user.display_name})
    return TokenOut(access_token=token, display_name=user.display_name, role=user.role)


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user
