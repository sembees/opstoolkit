"""启动时把驱动默认巡检模板写入数据库（标记 is_system 只读）。"""
from sqlalchemy import select

from app.core.models import InspectionTemplate
from app.ct.drivers import DRIVERS
from app.database import async_session


async def seed_default_templates() -> None:
    """仅当表中无系统模板时执行种子写入，已有则跳过。"""
    async with async_session() as db:
        exists = await db.execute(
            select(InspectionTemplate).where(InspectionTemplate.is_system == True).limit(1)
        )
        if exists.scalar_one_or_none() is not None:
            return

        for vendor, cls in DRIVERS.items():
            drv = cls()
            for tmpl_name, metric_cmds in drv.templates().items():
                items = [
                    {"key": mc.key, "label": mc.label, "command": mc.command,
                     "textfsm": mc.textfsm, "unit": mc.unit}
                    for mc in metric_cmds
                ]
                db.add(InspectionTemplate(
                    name=tmpl_name,
                    vendor=vendor,
                    is_system=True,
                    items=items,
                    description=f"{vendor} 默认巡检模板（系统内置，只读）",
                ))
        await db.commit()