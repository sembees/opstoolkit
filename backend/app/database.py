"""异步数据库会话与初始化。"""
import logging
import secrets
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# 异步 SQLAlchemy 引擎，默认 SQLite (aiosqlite)
engine = create_async_engine(settings.database_url, echo=settings.debug, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def init_db() -> None:
    """应用启动时自动调用。

    1. 创建所有表（若不存在）
    2. 写入各厂商默认巡检模板
    3. 创建默认管理员账号（若不存在）
    """
    from app.core import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.core.auth import hash_password
    from app.core.crud import get_user_by_username
    from app.ct.seeding import seed_default_templates

    await seed_default_templates()

    async with async_session() as session:
        if not await get_user_by_username(session, settings.admin_username):
            pw = settings.admin_password
            generated = False
            if not pw:
                # 不再代填默认口令：写死的默认口令在公开仓库里等于公开凭据。
                # 首次初始化生成一次性随机口令，且**只在这里打印一次**，
                # 运维从服务日志取（docker logs opstoolkit）；想固定就设 ADMIN_PASSWORD。
                pw = secrets.token_urlsafe(18)
                generated = True
            session.add(models.User(
                username=settings.admin_username,
                hashed_password=hash_password(pw),
                display_name="管理员",
                role="admin",
            ))
            await session.commit()
            if generated:
                logging.getLogger(__name__).warning(
                    "已创建管理员 %s；初始随机口令（仅本次打印，登录后请立即修改）: %s",
                    settings.admin_username, pw,
                )
