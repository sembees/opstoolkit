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


# 存量库的增量唯一索引。`create_all` 只建**不存在的表**，不会给已存在的表补索引，
# 所以模型上加的 `Index(..., unique=True)` 对已经跑起来的库是无效的 —— 必须显式补一次。
# 外部审查 U7-F9（同一模板/模板下同一 MAC 只能有一条装机记录/设备清单条目）。
_ADDITIVE_UNIQUE_INDEXES = (
    ("uq_pxe_install_profile_mac", "pxe_installs", "profile_id, mac"),
    ("uq_ztp_dev_template_mac", "ztp_devices", "template_id, mac"),
)

# 存量库的增量补列。同上：`create_all`（SQLite 亦然）**不会给已存在的表加列**，
# 模型上新加的 Notification.oncall_lookup_failed / merged_count 对存量生产库无效，
# 必须在启动时幂等补一次（先 PRAGMA 查列，缺了才 ALTER TABLE ADD COLUMN）。
# 列定义与 models.Notification 上的声明对齐：NOT NULL + 默认值（ALTER 加列带 NOT NULL
# 在 SQLite 里必须给非 NULL 默认值，存量行会直接取该默认值，不需要回填）。
_ADDITIVE_COLUMNS = (
    ("notifications", "oncall_lookup_failed", "BOOLEAN NOT NULL DEFAULT 0"),
    ("notifications", "merged_count", "INTEGER NOT NULL DEFAULT 0"),
)


async def _ensure_additive_columns(conn) -> None:
    """幂等地给存量表补列（create_all 不做这件事）。

    · 表不存在 ⇒ 跳过（首次启动由 create_all 直接建出带全列的表）；
    · 列已存在 ⇒ 跳过（幂等：重启多少次都只补缺的那几列）；
    · 列缺失 ⇒ ALTER TABLE ADD COLUMN（SQLite 支持加列，存量行取 DEFAULT）。
    """
    from sqlalchemy import text

    log = logging.getLogger(__name__)
    for table, column, ddl in _ADDITIVE_COLUMNS:
        exists = (await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )).first()
        if not exists:
            continue    # 首次启动时由 create_all 建表并带上全部列
        # PRAGMA table_info 的第 2 列（下标 1）是列名
        cols = {row[1] for row in (await conn.execute(
            text(f"PRAGMA table_info({table})"))).all()}
        if column in cols:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        log.info("存量表 %s 已补列 %s（%s）", table, column, ddl)


async def _ensure_additive_indexes(conn) -> None:
    """幂等地补索引；库里有历史重复数据时**不**让应用起不来。

    生产库里可能早就存着重复 MAC（改前没有任何约束），直接建唯一索引会让启动失败 ——
    那等于用一个"顺手加的索引"把整个运维平台弄挂。所以先把冲突的行打出来（带主键，
    运维照着删/改即可），索引留到清理干净后的下一次启动再建。
    """
    from sqlalchemy import text

    log = logging.getLogger(__name__)
    for name, table, cols in _ADDITIVE_UNIQUE_INDEXES:
        exists = (await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )).first()
        if not exists:
            continue    # 首次启动时由 create_all 建表并带上索引
        dups = (await conn.execute(text(
            f"SELECT {cols}, COUNT(*) AS n FROM {table} WHERE mac <> '' "
            f"GROUP BY {cols} HAVING COUNT(*) > 1"
        ))).all()
        if dups:
            log.warning(
                "表 %s 存在 %d 组重复 MAC，本次跳过唯一索引 %s；"
                "请先清理这些记录（否则装机/下发配置时同 MAC 的多条会互相覆盖）：%s",
                table, len(dups), name, [tuple(r) for r in dups[:5]],
            )
            continue
        await conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols}) WHERE mac <> ''"
        ))


async def _check_credential_key() -> None:
    """启动期凭证密钥体检（外部审查 U4-F6）。

    不在这里"直接启动失败"：PXE/ZTP 的投递本身并不需要这把密钥，为一个凭据密钥把整个
    运维平台打死更糟。所以是**大声报错 + 说明怎么办**，让运维在还能进界面的时候修。
    """
    from sqlalchemy import func, select

    from app.core import crypto, models

    log = logging.getLogger(__name__)
    warn = crypto.credential_key_health()
    if warn:
        log.error(warn)
        return
    if (settings.credential_key or "").strip():
        return
    # 密钥为空：首次启动属正常（第一次用到时才生成）；但库里已经有密文就说明密钥**丢了** ——
    # 这时再生成一把新的会让那些密文永久打不开，必须喊出来。
    async with async_session() as session:
        creds = (await session.execute(
            select(func.count()).select_from(models.Credential)
            .where(models.Credential.encrypted_password.isnot(None))
        )).scalar() or 0
        pxe_tpl = (await session.execute(
            select(func.count()).select_from(models.PxeProfile)
            .where(models.PxeProfile.admin_password_enc.isnot(None))
        )).scalar() or 0
        ztp_tpl = (await session.execute(
            select(func.count()).select_from(models.ZtpTemplate)
            .where(models.ZtpTemplate.admin_password_enc.isnot(None))
        )).scalar() or 0
    if creds or pxe_tpl or ztp_tpl:
        log.error(
            "credential_key 为空，但库里已有 %d 条凭据 / %d 个 PXE 模板 / %d 个 ZTP 模板存着加密口令："
            "说明这把密钥丢了。请先恢复原来那把密钥再启动，否则本次自动生成的密钥会让这些密文"
            "永久无法解密（表现：巡检认证失败、装机报「口令不能为空」）。",
            creds, pxe_tpl, ztp_tpl,
        )


async def init_db() -> None:
    """应用启动时自动调用。

    1. 创建所有表（若不存在）
    2. 给存量库补增量唯一索引（见 _ensure_additive_indexes）
       并给存量表补增量列（见 _ensure_additive_columns —— create_all 不给已存在的表加列）
    3. 凭证密钥体检（外部审查 U4-F6）
    4. 写入各厂商默认巡检模板
    5. 创建默认管理员账号（若不存在）
    """
    from app.core import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_additive_indexes(conn)
        await _ensure_additive_columns(conn)

    await _check_credential_key()

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
