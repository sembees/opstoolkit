"""认证接口。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crud
from app.core.auth import create_access_token, get_current_user, hash_password, verify_password
from app.core.schemas import ChangePasswordIn, LoginIn, TokenOut
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


@router.post("/password")
async def change_password(body: ChangePasswordIn,
                          db: AsyncSession = Depends(get_db),
                          user: dict = Depends(get_current_user)):
    """修改当前登录用户的口令。

    为什么补这个接口：在此之前**根本没有改口令的路径**（只有 /login 与 /me），
    而前端帮助页却写着"首次登录后建议修改密码"—— 等于把默认口令永久留在线上。
    要真正轮换掉历史默认口令，必须有一个能改的地方。改口令要求提供原口令。

    ★ 安装引导埋点：初始管理员（用户名 == 配置的 admin_username）改密成功时，
      在 app_meta 里记下 admin_password_changed_at —— 向导用"有没有这个标记"判断
      "初始口令是否已更换"（方案 (a)，见 app/core/setup.py 的说明；不比对口令原文，
      因为初始口令只打印过一次，运行期根本拿不到）。接口对外语义不变：
      请求/响应/状态码与改造前完全一致，operator 改自己的口令不产生任何标记。
    """
    u = await crud.get_user_by_username(db, user.get("username", ""))
    if not u or not verify_password(body.old_password, u.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="原口令不正确")
    u.hashed_password = hash_password(body.new_password)
    await db.commit()
    from app.core import setup as setup_core

    await setup_core.record_admin_password_changed(db, user.get("username", ""))
    return {"ok": True}
