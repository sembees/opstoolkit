"""异步数据库会话与初始化。"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def init_db() -> None:
    """建表并写入默认管理员。在应用启动时调用。"""
    from app.core import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.core.auth import hash_password
    from app.core.crud import get_user_by_username
    from app.ct.seeding import seed_default_templates

    await seed_default_templates()

    async with async_session() as session:
        if not await get_user_by_username(session, settings.admin_username):
            session.add(models.User(
                username=settings.admin_username,
                hashed_password=hash_password(settings.admin_password),
                display_name="管理员",
                role="admin",
            ))
            await session.commit()
