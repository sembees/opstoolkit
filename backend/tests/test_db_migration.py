# -*- coding: utf-8 -*-
"""**存量库迁移**用例：补列 / 补唯一索引（审计里"迁移测试"那块空白）。

为什么要测：这两段逻辑是"**升级已有生产库**"的唯一保障 ——
  · `create_all` 只建不存在的表，**不会**给已存在的表加列/加索引；
  · 于是模型上新增的列对存量库是"隐形的"，直到某次查询报 no such column；
  · 补索引更危险：库里早就有重复数据时，直接建唯一索引会让**应用起不来**
    （`_ensure_additive_indexes` 的设计就是"打印冲突行、先不建索引"）。

这组用例把三件事钉住：补得对、跑两次没事、有脏数据时不把应用弄挂。
"""
import asyncio
import pathlib
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import database as db  # noqa: E402
from app.core import models  # noqa: E402

ADDED = db._ADDITIVE_COLUMNS
UNIQ = db._ADDITIVE_UNIQUE_INDEXES


def _engine(tmp_path):
    return create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "mig.db").as_posix(),
                               poolclass=NullPool)


def _run(engine, fn):
    async def _go():
        async with engine.begin() as conn:
            await fn(conn)

    return asyncio.run(_go())


def _columns(engine, table):
    async def _q():
        async with engine.begin() as conn:
            return [r[1] for r in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()]

    return asyncio.run(_q())


def _indexes(engine):
    async def _q():
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index'"))).all()
            return {r[0] for r in rows}

    return asyncio.run(_q())


def _make_old_tables(engine):
    """手工造"老库"：表存在、但缺新列，且没有唯一索引。"""

    async def _go():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE notifications (id VARCHAR PRIMARY KEY, text VARCHAR)"))
            await conn.execute(text(
                "INSERT INTO notifications (id, text) VALUES ('n1', 'hi')"))
            await conn.execute(text(
                "CREATE TABLE pxe_installs (id VARCHAR PRIMARY KEY, profile_id VARCHAR, "
                "mac VARCHAR, status VARCHAR)"))

    asyncio.run(_go())


def test_additive_columns_are_added_with_defaults(tmp_path):
    """老表缺列 → 补上；存量行取默认值（不能被写成 NULL 让后续查询炸）。"""
    e = _engine(tmp_path)
    _make_old_tables(e)
    before = set(_columns(e, "notifications"))
    _run(e, db._ensure_additive_columns)
    after = set(_columns(e, "notifications"))

    need = {c for t, c, _ in ADDED if t == "notifications"}
    assert need, "这个用例假设 notifications 有增量列"
    assert not (need & before), "前置：老表本来就不该有这些列"
    assert need <= after, "补列失败: 缺 %s" % (need - after,)

    async def _val():
        async with e.begin() as conn:
            return (await conn.execute(text(
                "SELECT oncall_lookup_failed, merged_count FROM notifications "
                "WHERE id='n1'"))).first()

    got = asyncio.run(_val())
    assert tuple(got) == (0, 0), "存量行应取默认值 0/0，实际 %r" % (got,)


def test_additive_columns_is_idempotent(tmp_path):
    """跑两次不报错（第二次全部跳过）—— 生产上每次启动都会跑一次。"""
    e = _engine(tmp_path)
    _make_old_tables(e)
    _run(e, db._ensure_additive_columns)
    cols1 = _columns(e, "notifications")
    _run(e, db._ensure_additive_columns)          # 第二次
    cols2 = _columns(e, "notifications")
    assert cols1 == cols2, "第二次补列不应改变结构"
    assert len(cols2) == len(set(cols2)), "不应出现重复列"


def test_missing_tables_are_skipped(tmp_path):
    """表都不存在时（全新库/别的库）跳过，不能抛错。"""
    e = _engine(tmp_path)
    _run(e, db._ensure_additive_columns)          # 不该抛
    _run(e, db._ensure_additive_indexes)


def test_unique_index_skipped_when_duplicates_exist(tmp_path):
    """库里有重复 MAC 时**不建**唯一索引、也不抛错（否则平台起不来）。

    这是设计上的"宁可晚一步，也不要把运维平台弄挂"。
    """
    e = _engine(tmp_path)
    _make_old_tables(e)

    async def _dup():
        async with e.begin() as conn:
            for i in ("a", "b"):
                await conn.execute(text(
                    "INSERT INTO pxe_installs (id, profile_id, mac, status) "
                    "VALUES (:i, 'p1', 'AA:BB', 'pending')"), {"i": i})

    asyncio.run(_dup())
    _run(e, db._ensure_additive_indexes)          # 不该抛
    name = next(n for n, t, _ in UNIQ if t == "pxe_installs")
    assert name not in _indexes(e), "有重复数据时不该建唯一索引"


def test_unique_index_created_when_clean(tmp_path):
    """没有重复数据 → 索引建起来（之后重复插入会被数据库拒绝）。"""
    e = _engine(tmp_path)
    _make_old_tables(e)

    async def _one():
        async with e.begin() as conn:
            await conn.execute(text(
                "INSERT INTO pxe_installs (id, profile_id, mac, status) "
                "VALUES ('a', 'p1', 'AA:BB', 'pending')"))

    asyncio.run(_one())
    _run(e, db._ensure_additive_indexes)
    name = next(n for n, t, _ in UNIQ if t == "pxe_installs")
    assert name in _indexes(e), "干净库应能建出唯一索引"

    async def _dup_insert():
        async with e.begin() as conn:
            await conn.execute(text(
                "INSERT INTO pxe_installs (id, profile_id, mac, status) "
                "VALUES ('b', 'p1', 'AA:BB', 'pending')"))

    try:
        asyncio.run(_dup_insert())
    except Exception:
        pass                                       # 被唯一索引拒绝，正是期望
    else:
        raise AssertionError("唯一索引没生效：重复 MAC 竟然插进去了")


def test_fresh_db_create_all_then_migrate_is_noop(tmp_path):
    """全新库：create_all 建全表后，两个迁移函数应是 no-op（且不报错）。"""
    e = _engine(tmp_path)

    async def _create():
        async with e.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(_create())
    _run(e, db._ensure_additive_columns)
    _run(e, db._ensure_additive_indexes)
    # 新库的列本来就是全的
    cols = set(_columns(e, "notifications"))
    assert {c for t, c, _ in ADDED if t == "notifications"} <= cols
