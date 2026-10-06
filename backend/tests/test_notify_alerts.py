# -*- coding: utf-8 -*-
"""告警通知集成单元测试 —— **全部 mock，绝不真实联网、绝不真发飞书消息**。

被测对象：
  · app/core/notify/alerts.py  notify_alert（平台 ok → @当班人；非 ok → @all 降级 /
    去重合并 merged_count / 发送记录 / 绝不抛）
  · app/core/oncall.py         current_oncall（细分状态：ok / no_shift / unauthorized /
    team_not_found / bad_request / platform_error / not_configured / empty；30 秒缓存；
    超时读配置；绝不抛）
  · app/database.py            _ensure_additive_columns（存量库幂等补列）
  · app/ct/inspection/service.py  告警命中 → fire-and-forget 发通知的接入点

联网边界（读用例前先看这里）：
  · oncall 的 HTTP 收敛在 oncall._get_json，用例把它整个 mock 掉 ⇒ 不可能有真实请求；
  · send_text 在 alerts 模块内被直接 mock（返回 FeishuResult）⇒ 不可能有真实发送；
  · 巡检接入点用例 mock 掉 service.inspect_many ⇒ 不连任何设备；
  · DB 一律用临时目录里的 sqlite 文件（NullPool，连接即用即建），不碰生产库。
"""
import asyncio
import inspect
import json
import pathlib
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import httpx
from sqlalchemy import inspect as sa_inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import database as database_mod
from app.config import settings
from app.core import models
from app.core import oncall as oncall_mod
from app.core.notify import alerts as alerts_mod
from app.core.notify.feishu import FeishuResult
from app.core.timeutil import utcnow
from app.ct.inspection import service as inspection_service


def make_rule(**over):
    """假告警规则（不落库也能用 —— notify_alert 只按鸭子类型取属性）。"""
    kw = dict(id="r1", name="CPU 过高", metric_key="cpu",
              operator="gt", threshold=90.0, enabled=True)
    kw.update(over)
    return models.AlertRule(**kw)


def alert_kwargs(**over):
    """notify_alert 的一组默认实参（假数据）。"""
    kw = dict(rule=make_rule(), metric_key="cpu", value=95, asset_id="a1",
              asset_name="sw-01", asset_host="10.0.0.9")
    kw.update(over)
    return kw


def send_ok():
    return FeishuResult(ok=True, detail="sent", attempts=1)


class AlertsTestBase(unittest.TestCase):
    """公共脚手架：临时 sqlite 库 + settings 钉值 + oncall 缓存清零。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(cls._tmp.name) / "alerts-test.db"
        cls.engine = create_async_engine(
            "sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
        cls.SessionLocal = async_sessionmaker(
            cls.engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.engine.dispose())
        cls._tmp.cleanup()

    def setUp(self):
        async def _reset():
            async with self.engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
        asyncio.run(_reset())
        # alerts 模块用它自己的会话工厂（经模块属性访问 app.database.async_session）——
        # 这里把它指到临时库，任何用例都不会碰生产库。
        p = mock.patch.object(database_mod, "async_session", self.SessionLocal)
        p.start()
        self.addCleanup(p.stop)
        oncall_mod._reset_cache()
        self.addCleanup(oncall_mod._reset_cache)

    def configure(self, **over):
        """把通知相关设置钉成确定值（默认：通知启用 + 飞书全配 + 值班平台未配置）。"""
        values = {
            "notify_enabled": True,
            "feishu_app_id": "cli_unit_test_appid",
            "feishu_app_secret": "unit_test_secret",
            "feishu_receive_id": "oc_unit_test_chat",
            "feishu_receive_id_type": "chat_id",
            "feishu_at_open_ids": "",
            "feishu_at_all": False,
            "feishu_timeout": 8.0,
            "notify_dedup_window": 300,
            "oncall_base_url": "",
            "oncall_token": "",
            "oncall_team": "",
            "oncall_timeout": 2.0,
        }
        values.update(over)
        for k, v in values.items():
            p = mock.patch.object(settings, k, v)
            p.start()
            self.addCleanup(p.stop)

    def _notifications(self):
        async def _q():
            async with self.SessionLocal() as db:
                return (await db.execute(
                    # 按 created_at 排序：id 是随机 uuid，按它排每次都不同
                    select(models.Notification).order_by(models.Notification.created_at)
                )).scalars().all()
        return asyncio.run(_q())

    def _age_all_records(self, seconds):
        """把库里所有通知记录的 created_at 拨回到过去（模拟去重窗口过期）。"""
        async def _go():
            async with self.SessionLocal() as db:
                await db.execute(text("UPDATE notifications SET created_at = :t"),
                                 {"t": utcnow() - timedelta(seconds=seconds)})
                await db.commit()
        asyncio.run(_go())

    def _clear_records(self):
        async def _go():
            async with self.SessionLocal() as db:
                await db.execute(text("DELETE FROM notifications"))
                await db.commit()
        asyncio.run(_go())


class DisabledTest(AlertsTestBase):
    """notify_enabled=False ⇒ 零网络、零 @ 查询、零记录。"""

    def test_disabled_sends_nothing_and_records_nothing(self):
        self.configure(notify_enabled=False)
        send = mock.AsyncMock(return_value=send_ok())
        oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        with mock.patch.object(alerts_mod, "send_text", send), \
                mock.patch.object(oncall_mod, "current_oncall", oncall):
            r = asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        self.assertIsNone(r)
        self.assertEqual(send.await_count, 0)      # ★ 零发送（send_text 零调用）
        self.assertEqual(oncall.await_count, 0)    # ★ 零查询（current_oncall 零调用）
        self.assertEqual(self._notifications(), [])  # ★ 零记录


class AtTargetsTest(AlertsTestBase):
    """@ 目标路径：平台 ok → 平台结果；非 ok → 一律 @all 降级（配置覆盖保留不启用）。"""

    def setUp(self):
        super().setUp()
        self.configure()
        self.send = mock.AsyncMock(return_value=send_ok())
        p = mock.patch.object(alerts_mod, "send_text", self.send)
        p.start()
        self.addCleanup(p.stop)

    def _text(self):
        self.assertEqual(self.send.await_count, 1)
        return self.send.call_args.args[0]

    def test_path1_platform_targets_win(self):
        """① 平台查到当班人 ⇒ 用平台结果，不降级、不叠配置、不算查询失败。"""
        self.configure(feishu_at_open_ids="ou_cfg|配置兜底")  # 有配置也不该用
        oncall = mock.AsyncMock(return_value=(["ou_zhang|张三", "ou_li|李四"], "ok"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertIn('<at user_id="ou_zhang">张三</at>', text)
        self.assertIn('<at user_id="ou_li">李四</at>', text)
        self.assertNotIn("ou_cfg", text)
        self.assertNotIn("<at user_id=\"all\"", text)
        self.assertNotIn("已降级", text)             # ① 不算降级
        rec = self._notifications()[0]
        self.assertFalse(rec.degraded)
        self.assertFalse(rec.oncall_lookup_failed)   # ★ 平台 ok ⇒ 未失败
        self.assertEqual(json.loads(rec.at_targets), ["ou_zhang|张三", "ou_li|李四"])

    def test_platform_failure_skips_config_ids_and_goes_at_all(self):
        """② 非 ok ⇒ 一律 @all（配置固定 open_id 覆盖保留但本次不启用）。"""
        self.configure(feishu_at_open_ids="ou_cfg1|值班-配置, ou_cfg2",
                       feishu_at_all=True)
        oncall = mock.AsyncMock(return_value=([], "platform_error"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertIn('<at user_id="all">所有人</at>', text)   # ★ 走 @all
        self.assertNotIn("ou_cfg", text)                       # ★ 不再用配置 open_id
        self.assertIn("已降级", text)                           # 备注里说清降级原因
        rec = self._notifications()[0]
        self.assertTrue(rec.degraded)
        self.assertTrue(rec.oncall_lookup_failed)
        self.assertEqual(json.loads(rec.at_targets), [])

    def test_path3_only_at_all(self):
        """③ 配置里没有 open_id、但 feishu_at_all=True ⇒ @所有人。"""
        self.configure(feishu_at_all=True)
        oncall = mock.AsyncMock(return_value=([], "platform_error"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertIn('<at user_id="all">所有人</at>', text)
        self.assertNotIn("ou_cfg", text)
        rec = self._notifications()[0]
        self.assertTrue(rec.degraded)
        self.assertTrue(rec.oncall_lookup_failed)
        self.assertEqual(json.loads(rec.at_targets), [])

    def test_path4_nothing_to_at_but_message_still_sent(self):
        """④ 平台未配置且 @all 未开 ⇒ 不 @，但消息照发（告警事实不能丢）。"""
        oncall = mock.AsyncMock(return_value=([], "not_configured"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertNotIn("<at", text)
        self.assertIn("已降级", text)
        self.assertIn("未配置", text)                 # 备注里写明"未配置"而非"查询失败"
        self.assertIn("sw-01", text)                  # 正文照常
        rec = self._notifications()[0]
        self.assertTrue(rec.degraded)
        self.assertTrue(rec.oncall_lookup_failed)     # ★ 未配置也算"没 @ 到值班人"
        self.assertTrue(rec.ok)
        # ④ 时 settings.feishu_at_all=False ⇒ send_text 不得从配置里兜出 @ 来
        self.assertIs(self.send.call_args.kwargs.get("at_all"), False)

    def test_message_carries_rule_and_metric(self):
        """正文带规则名/指标名/实测值/阈值/设备/IP（平台路径下顺便验证）。"""
        oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        for frag in ("CPU 过高", "cpu", "95", "90", ">", "sw-01", "10.0.0.9",
                     "严重告警", "OpsToolkit 巡检"):
            self.assertIn(frag, text)


class DedupTest(AlertsTestBase):
    """去重：event_key = opstk:{metric_key}:{asset_host}；窗口内只发一条，
    重复触发合并进首条成功记录（merged_count 原子 +1）；失败记录不占窗口。"""

    def setUp(self):
        super().setUp()
        self.configure()
        self.oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        p = mock.patch.object(oncall_mod, "current_oncall", self.oncall)
        p.start()
        self.addCleanup(p.stop)

    def _run(self):
        asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))

    def test_second_send_within_window_is_suppressed_and_merged(self):
        """窗口内第 2 次触发：不发消息，首条成功记录 merged_count 原子 +1。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            self._run()                                # 同指标同设备 ⇒ 同 event_key
        self.assertEqual(send.await_count, 1)          # ★ 第二条没发
        recs = self._notifications()
        self.assertEqual(len(recs), 1)                 # ★ 也没有第二条记录
        self.assertEqual(recs[0].merged_count, 1)      # ★ 但合并计数 +1

    def test_second_and_third_triggers_merge_to_2(self):
        """同键 5 分钟内第 2、3 次都不发，第一条记录 merged_count 递增到 2。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            self._run()
            self._run()
        self.assertEqual(send.await_count, 1)
        recs = self._notifications()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].merged_count, 2)      # ★ 0 → 1 → 2

    def test_merge_is_an_increment_not_a_rewrite(self):
        """计数是 UPDATE 自增而不是读-改-写覆盖：记录里已有 5 时再触发一次变 6。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            async def _bump():
                async with self.SessionLocal() as db:
                    await db.execute(text(
                        "UPDATE notifications SET merged_count = 5"))
                    await db.commit()
            asyncio.run(_bump())
            self._run()
        recs = self._notifications()
        self.assertEqual(send.await_count, 1)
        self.assertEqual(recs[0].merged_count, 6)      # ★ 5 + 1，不是覆盖成 1

    def test_different_metric_key_is_not_deduped(self):
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(metric_key="memory")))
        self.assertEqual(send.await_count, 2)
        recs = self._notifications()
        self.assertEqual([r.event_key for r in recs],
                         ["opstk:cpu:10.0.0.9", "opstk:memory:10.0.0.9"])

    def test_different_host_is_not_deduped(self):
        """不同设备（同指标）互不影响：各发各的。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(asset_host="10.0.0.10")))
        self.assertEqual(send.await_count, 2)
        self.assertEqual([r.event_key for r in self._notifications()],
                         ["opstk:cpu:10.0.0.9", "opstk:cpu:10.0.0.10"])

    def test_same_host_different_asset_id_shares_the_window(self):
        """键以设备地址为准：同主机不同 asset_id 视为同一条告警（键含 host）。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(asset_id="a2")))
        self.assertEqual(send.await_count, 1)          # ★ 同 host ⇒ 去重
        self.assertEqual(self._notifications()[0].merged_count, 1)

    def test_event_key_format(self):
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
        self.assertEqual(self._notifications()[0].event_key, "opstk:cpu:10.0.0.9")

    def test_event_key_falls_back_to_asset_id_when_host_empty(self):
        """asset_host 为空 ⇒ 回退 asset_id，保证键有可区分的设备段。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(asset_host="")))
        self.assertEqual(self._notifications()[0].event_key, "opstk:cpu:a1")

    def test_event_key_never_degrades_to_empty_tail(self):
        """asset_host 与 asset_id 都为空 ⇒ 用 "unknown" 占位，不退化成空尾键。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(asset_id="", asset_host="")))
        key = self._notifications()[0].event_key
        self.assertEqual(key, "opstk:cpu:unknown")
        self.assertFalse(key.endswith(":"), "键尾不允许为空段")

    def test_failed_send_is_not_deduped(self):
        """第一次发送失败 ⇒ 不占用去重窗口（也不占合并计数），下条告警仍会重试。"""
        send = mock.AsyncMock(side_effect=[
            FeishuResult(ok=False, detail="发送失败：HTTP 500", attempts=3),
            send_ok(),
        ])
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            self._run()
        self.assertEqual(send.await_count, 2)
        recs = self._notifications()
        self.assertEqual([r.ok for r in recs], [False, True])
        self.assertEqual([r.merged_count for r in recs], [0, 0])  # 失败记录不占窗口

    def test_zero_window_disables_dedup(self):
        self.configure(notify_dedup_window=0)
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            self._run()
        self.assertEqual(send.await_count, 2)

    def test_resend_after_window_carries_merged_note(self):
        """窗口过期后真正重发 ⇒ 消息末尾带「（上一次同类告警之后又触发 N 次，已合并）」。"""
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()                    # 第 1 条：真发，merged_count=0
            self._run()                    # 第 2 次：抑制，merged_count→1
            self._run()                    # 第 3 次：抑制，merged_count→2
            self.assertEqual(send.await_count, 1)
            self._age_all_records(300 + 60)  # 窗口过期
            self._run()                    # 第 4 次：真发，且带上合并提示
        self.assertEqual(send.await_count, 2)
        text = send.call_args_list[1].args[0]
        self.assertIn("（上一次同类告警之后又触发 2 次，已合并）", text)
        recs = self._notifications()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0].merged_count, 2)      # 首条保持 2
        self.assertEqual(recs[1].merged_count, 0)      # 新记录从 0 起


class SendFailureTest(AlertsTestBase):
    """发送失败 ⇒ 仍写 Notification（ok=False + attempts + 中文原因）且不抛。"""

    def test_failed_send_still_recorded_and_never_raises(self):
        self.configure()
        oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        send = mock.AsyncMock(return_value=FeishuResult(
            ok=False, detail="发送失败：飞书错误 code=230002：机器人不在目标群里", attempts=1))
        with mock.patch.object(oncall_mod, "current_oncall", oncall), \
                mock.patch.object(alerts_mod, "send_text", send):
            r = asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        self.assertIsNone(r)                            # ★ 没抛异常
        recs = self._notifications()
        self.assertEqual(len(recs), 1)                  # ★ 记录照写
        rec = recs[0]
        self.assertFalse(rec.ok)
        self.assertEqual(rec.attempts, 1)
        self.assertIn("230002", rec.detail)
        self.assertEqual(rec.channel, "feishu")
        self.assertFalse(rec.degraded)                  # 平台 ok，发送失败不算平台查询失败
        self.assertFalse(rec.oncall_lookup_failed)
        self.assertEqual(rec.merged_count, 0)

    def test_db_failure_is_swallowed(self):
        """连"写记录"这一步失败（如库不可用）也不能把异常抛回巡检。"""
        self.configure()
        oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        send = mock.AsyncMock(return_value=send_ok())
        boom = mock.MagicMock(side_effect=RuntimeError("db down"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall), \
                mock.patch.object(alerts_mod, "send_text", send), \
                mock.patch.object(database_mod, "async_session", boom):
            r = asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        self.assertIsNone(r)


class NotificationRecordTest(AlertsTestBase):
    """发送记录字段：快照要能回答"发给谁/为什么、平台查询成没成"。"""

    def test_record_fields_snapshot(self):
        self.configure(feishu_at_all=True)
        oncall = mock.AsyncMock(return_value=([], "platform_error"))
        send = mock.AsyncMock(return_value=FeishuResult(ok=True, detail="sent", attempts=2))
        with mock.patch.object(oncall_mod, "current_oncall", oncall), \
                mock.patch.object(alerts_mod, "send_text", send):
            asyncio.run(alerts_mod.notify_alert(
                **alert_kwargs(rule=make_rule(id="rule-xyz"), value=95.5,
                               occurred_at="2026-10-06 12:00:00")))
        rec = self._notifications()[0]
        self.assertEqual(rec.event_key, "opstk:cpu:10.0.0.9")
        self.assertEqual(rec.asset_id, "a1")
        self.assertEqual(rec.asset_name, "sw-01")
        self.assertEqual(rec.asset_host, "10.0.0.9")
        self.assertEqual(rec.metric_key, "cpu")
        self.assertEqual(rec.value, "95.5")
        self.assertEqual(rec.operator, "gt")
        self.assertAlmostEqual(rec.threshold, 90.0)
        self.assertEqual(rec.rule_id, "rule-xyz")
        self.assertEqual(rec.rule_name, "CPU 过高")
        self.assertEqual(rec.severity, "critical")
        self.assertEqual(rec.channel, "feishu")
        self.assertTrue(rec.ok)
        self.assertTrue(rec.detail.startswith("sent"))   # 发送结果 + 平台查询失败原因
        self.assertIn("平台不可用", rec.detail)
        self.assertEqual(rec.attempts, 2)
        self.assertTrue(rec.degraded)                    # 未走平台路径
        self.assertTrue(rec.oncall_lookup_failed)        # ★ 平台查询失败
        self.assertEqual(rec.merged_count, 0)
        self.assertIsNone(rec.message_id)               # 预留字段，发送层暂不回传

    def test_ok_platform_record_fields(self):
        """平台 ok 的记录：detail 就是 "sent"、两个降级标记都为 False。"""
        self.configure()
        oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(oncall_mod, "current_oncall", oncall), \
                mock.patch.object(alerts_mod, "send_text", send):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        rec = self._notifications()[0]
        self.assertEqual(rec.detail, "sent")
        self.assertFalse(rec.degraded)
        self.assertFalse(rec.oncall_lookup_failed)
        self.assertEqual(rec.merged_count, 0)


class OncallFailureDegradeTest(AlertsTestBase):
    """★ 降级铁律：no_shift / 401/403 / 404 / 400 / 5xx / 超时 / 未配置 ⇒
    消息照发 + 走 @all + 记录 oncall_lookup_failed=True；404 额外显式提示团队配置错误。"""

    def setUp(self):
        super().setUp()
        self.configure(feishu_at_all=True)
        self.send = mock.AsyncMock(return_value=send_ok())
        p = mock.patch.object(alerts_mod, "send_text", self.send)
        p.start()
        self.addCleanup(p.stop)

    def _fire(self, status):
        oncall = mock.AsyncMock(return_value=([], status))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))

    def _text(self):
        """唯一一次发送的消息文本（调用方先自行断言 await_count）。"""
        return self.send.call_args.args[0]

    def test_each_failure_status_sends_message_at_all_and_marks_failed(self):
        """各失败态：消息照发（ok 记录 + @all）且 oncall_lookup_failed=True。"""
        for status in ("no_shift", "unauthorized", "bad_request",
                       "platform_error", "not_configured"):
            with self.subTest(status=status):
                self._clear_records()
                self.send.reset_mock()
                self._fire(status)
                self.assertEqual(self.send.await_count, 1)            # ★ 消息照发
                got = self._text()
                self.assertIn('<at user_id="all">所有人</at>', got)   # ★ 走 @all
                self.assertIn("已降级", got)
                recs = self._notifications()
                self.assertEqual(len(recs), 1)
                rec = recs[0]
                self.assertTrue(rec.ok)
                self.assertTrue(rec.degraded)
                self.assertTrue(rec.oncall_lookup_failed)             # ★ 记录失败
                if status == "not_configured":
                    # ★ "平台未配置"与"平台查询失败"在记录里可区分（detail 文案）
                    self.assertIn("未配置", rec.detail)
                    self.assertNotIn("值班平台查询失败", rec.detail)
                elif status == "no_shift":
                    # no_shift 是"无人无排班"，不是平台故障 —— detail 单独措辞
                    self.assertIn("无排班", rec.detail)
                else:
                    self.assertIn("值班平台查询失败", rec.detail)

    def test_404_team_not_found_is_not_silent(self):
        """404 = 团队配置错误：消息尾行 + detail 点明团队名 + log.warning。"""
        # 注意：configure() 会整体重钉，这里要把 @all 一起带上
        self.configure(oncall_team="ct-net", feishu_at_all=True)
        with self.assertLogs("app.core.notify.alerts", level="WARNING") as cm:
            self._fire("team_not_found")
        got = self._text()
        self.assertIn("（值班团队配置错误：团队 ct-net 不存在，已 @all）", got)  # ③ 消息末尾
        self.assertIn('<at user_id="all">所有人</at>', got)                       # 降级 @all
        rec = self._notifications()[0]
        self.assertTrue(rec.ok)                                    # 告警照发成功
        self.assertTrue(rec.oncall_lookup_failed)
        self.assertIn("值班团队配置错误", rec.detail)               # ① detail 点明
        self.assertIn("ct-net", rec.detail)                        #    含团队名
        self.assertTrue(any("404" in o and "配置错误" in o for o in cm.output))  # ② log
        # 404 也必须带"已合并"之外的专属 tail：团队名直接可读
        self.assertIn("团队配置错误", "\n".join(got.splitlines()[-1:]))

    def test_404_without_team_config_still_names_the_problem(self):
        """连 oncall_team 都没配就 404：提示里不能出现空团队名。"""
        self.configure(oncall_team="")
        self._fire("team_not_found")
        got = self._text()
        self.assertIn("（值班团队配置错误：团队 （未配置） 不存在，已 @all）", got)
        rec = self._notifications()[0]
        self.assertIn("（未配置）", rec.detail)

    def test_failure_without_at_all_flag_still_sends_message(self):
        """@all 开关没开：不 @ 任何人，但消息照发、失败标记照记。"""
        self.configure(feishu_at_all=False)
        self._fire("no_shift")
        got = self._text()
        self.assertNotIn("<at", got)
        self.assertIn("sw-01", got)                    # 正文照常
        rec = self._notifications()[0]
        self.assertTrue(rec.ok)
        self.assertTrue(rec.oncall_lookup_failed)


class OncallLookupCacheTest(AlertsTestBase):
    """告警链路上的平台缓存：30 秒内多条告警只打一次平台 HTTP（不同指标不去重）。"""

    def test_two_alerts_hit_platform_once_within_cache_ttl(self):
        self.configure(oncall_base_url="http://oncall.example",
                       oncall_token="tok-unit", oncall_team="ct-net")
        body = {"team": "ct-net", "at": "2026-10-06T23:02:48+08:00",
                "primary": {"name": "值班A", "feishu_open_id": "ou_zzz",
                            "phone": None, "email": None},
                "backup": None,
                "shift": {"id": "d-2026-10-06"},
                "source": "rotation", "empty_reason": None}
        gj = mock.AsyncMock(return_value=(200, body))
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(oncall_mod, "_get_json", gj), \
                mock.patch.object(alerts_mod, "send_text", send):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(metric_key="cpu")))
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(metric_key="memory")))
        self.assertEqual(send.await_count, 2)      # 两条都发了（不同指标不去重）
        self.assertEqual(gj.await_count, 1)        # ★ 平台 HTTP 只打了一次（第二条走缓存）
        for c in gj.call_args_list:
            self.assertEqual(c.kwargs["params"], {"team": "ct-net"})
            self.assertEqual(c.kwargs["headers"], {"Authorization": "Bearer tok-unit"})


class CurrentOncallTest(unittest.TestCase):
    """值班人查询客户端：细分状态、契约解析、缓存 —— 全 mock，零联网。"""

    def setUp(self):
        oncall_mod._reset_cache()
        self.addCleanup(oncall_mod._reset_cache)
        self._patches = []
        for k, v in {
            "oncall_base_url": "http://oncall.example",
            "oncall_token": "tok-unit-1",
            "oncall_team": "ct-net",
            "oncall_timeout": 2.0,
        }.items():
            p = mock.patch.object(settings, k, v)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _body(**over):
        """ONCALL-PLATFORM-PLAN.md §4.1 的契约样例（节选）；可用 over 改字段。"""
        body = {
            "team": "ct-net",
            "at": "2026-10-05T13:20:00+08:00",
            "primary": {"name": "张三", "feishu_open_id": "ou_zzz",
                        "phone": "13…", "email": "…"},
            "backup": {"name": "李四", "feishu_open_id": "ou_yyy",
                       "phone": "13…", "email": "…"},
            "shift": {"id": "sh_20261005_a"},
            "source": "rotation",
            "empty_reason": None,
        }
        body.update(over)
        return body

    def test_ok_parses_primary_and_backup(self):
        gj = mock.AsyncMock(return_value=(200, self._body()))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual(status, "ok")
        self.assertEqual(targets, ["ou_zzz|张三", "ou_yyy|李四"])
        url = gj.call_args.args[0]
        self.assertEqual(url, "http://oncall.example/api/oncall/current")
        self.assertEqual(gj.call_args.kwargs["params"], {"team": "ct-net"})
        self.assertEqual(gj.call_args.kwargs["headers"],
                         {"Authorization": "Bearer tok-unit-1"})

    def test_ok_parses_override_source(self):
        """契约：source 只认 rotation | override —— override 一样要能解析。"""
        gj = mock.AsyncMock(return_value=(200, self._body(source="override")))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), (["ou_zzz|张三", "ou_yyy|李四"], "ok"))

    def test_source_field_is_never_judged(self):
        """★ 不判 source（历史 manual 平台已归一化）：任何 source 值都不影响解析。"""
        for source in ("rotation", "override", "manual", "", None):
            with self.subTest(source=source):
                gj = mock.AsyncMock(return_value=(200, self._body(source=source)))
                with mock.patch.object(oncall_mod, "_get_json", gj):
                    targets, status = asyncio.run(oncall_mod.current_oncall())
                self.assertEqual((targets, status),
                                 (["ou_zzz|张三", "ou_yyy|李四"], "ok"))

    def test_backup_null_when_not_requested(self):
        """契约：不请求 backup 时为 null ⇒ 只用 primary，也照常 ok。"""
        body = self._body(backup=None)
        gj = mock.AsyncMock(return_value=(200, body))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), (["ou_zzz|张三"], "ok"))

    def test_not_configured_sends_nothing(self):
        p = mock.patch.object(settings, "oncall_base_url", "")
        p.start()
        self.addCleanup(p.stop)
        gj = mock.AsyncMock()
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "not_configured"))
        self.assertEqual(gj.await_count, 0)             # ★ 一个请求都不发

    def test_team_argument_overrides_settings(self):
        gj = mock.AsyncMock(return_value=(200, self._body()))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            asyncio.run(oncall_mod.current_oncall("it-srv"))
        self.assertEqual(gj.call_args.kwargs["params"], {"team": "it-srv"})

    def test_timeout_returns_platform_error(self):
        """超时 ⇒ platform_error（5xx/超时同属"平台不可用"）。"""
        gj = mock.AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "platform_error"))

    def test_network_error_returns_platform_error(self):
        gj = mock.AsyncMock(side_effect=httpx.ConnectError("refused"))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "platform_error"))

    def test_http_500_returns_platform_error(self):
        gj = mock.AsyncMock(return_value=(500, {"msg": "boom"}))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "platform_error"))

    def test_http_401_and_403_return_unauthorized(self):
        """token 问题（401/403）单独成态，供调用方降级 + 记录。"""
        for http_status in (401, 403):
            with self.subTest(http_status=http_status):
                gj = mock.AsyncMock(return_value=(http_status, {"msg": "denied"}))
                with mock.patch.object(oncall_mod, "_get_json", gj):
                    targets, status = asyncio.run(oncall_mod.current_oncall())
                self.assertEqual((targets, status), ([], "unauthorized"))

    def test_http_404_returns_team_not_found(self):
        """★ 404 = 团队 code 不存在 ⇒ 配置错误，必须让调用方显式提示运维。"""
        gj = mock.AsyncMock(return_value=(404, {"msg": "team not found"}))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "team_not_found"))

    def test_http_400_returns_bad_request(self):
        gj = mock.AsyncMock(return_value=(400, {"msg": "bad team param"}))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "bad_request"))

    def test_non_dict_body_returns_platform_error(self):
        """响应不是 JSON 对象（字段缺失的形态之一）⇒ platform_error，不抛。"""
        for body in (None, ["a", "b"], "ok"):
            with self.subTest(body=body):
                gj = mock.AsyncMock(return_value=(200, body))
                with mock.patch.object(oncall_mod, "_get_json", gj):
                    targets, status = asyncio.run(oncall_mod.current_oncall())
                self.assertEqual((targets, status), ([], "platform_error"))

    def test_no_shift_reason_maps_to_no_shift_status(self):
        """契约：empty_reason="no_shift"（primary 为 null）⇒ no_shift，调用方降级 @all。"""
        body = {"team": "ct-net", "at": "2026-10-06T23:02:48+08:00",
                "primary": None, "backup": None,
                "shift": None, "source": "rotation", "empty_reason": "no_shift"}
        gj = mock.AsyncMock(return_value=(200, body))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "no_shift"))

    def test_unusable_member_without_no_shift_returns_empty(self):
        """平台活着但成员缺 feishu_open_id（且不是 no_shift）⇒ empty。"""
        for body in ({"primary": {"name": "张三"}},
                     {"primary": {"name": "张三", "feishu_open_id": "  "}},
                     {"team": "ct-net", "primary": None, "backup": None,
                      "empty_reason": None}):
            with self.subTest(body=body):
                gj = mock.AsyncMock(return_value=(200, body))
                with mock.patch.object(oncall_mod, "_get_json", gj):
                    targets, status = asyncio.run(oncall_mod.current_oncall())
                self.assertEqual((targets, status), ([], "empty"))

    def test_primary_unusable_but_backup_valid_uses_backup(self):
        """primary 缺 open_id、backup 有效 ⇒ 部分可用也要用（备班总比没人强）。"""
        body = {"primary": {"name": "张三"},
                "backup": {"name": "李四", "feishu_open_id": "ou_yyy"}}
        gj = mock.AsyncMock(return_value=(200, body))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), (["ou_yyy|李四"], "ok"))

    def test_cache_hits_within_30s(self):
        """30 秒缓存：同 team 第二次不发 HTTP；不同 team 各查各的。"""
        gj = mock.AsyncMock(return_value=(200, self._body()))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            t1, s1 = asyncio.run(oncall_mod.current_oncall())
            t2, s2 = asyncio.run(oncall_mod.current_oncall())       # 命中缓存
        self.assertEqual(gj.await_count, 1)                          # ★ HTTP 只调一次
        self.assertEqual((t1, s1), (t2, s2))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            asyncio.run(oncall_mod.current_oncall("other-team"))     # 换 team ⇒ 重查
        self.assertEqual(gj.await_count, 2)

    def test_expired_cache_entry_refetched(self):
        """过期条目（手动把过期时间拨到过去）⇒ 重新发起查询。"""
        gj = mock.AsyncMock(return_value=(200, self._body()))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            asyncio.run(oncall_mod.current_oncall())
            oncall_mod._cache["ct-net"] = (0.0, ["stale"])           # 已过期
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual(gj.await_count, 2)
        self.assertEqual((targets, status),
                         (["ou_zzz|张三", "ou_yyy|李四"], "ok"))

    def test_empty_token_sends_no_auth_header(self):
        p = mock.patch.object(settings, "oncall_token", "")
        p.start()
        self.addCleanup(p.stop)
        gj = mock.AsyncMock(return_value=(200, self._body()))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            asyncio.run(oncall_mod.current_oncall())
        self.assertIsNone(gj.call_args.kwargs["headers"])

    def test_never_raises_for_any_input(self):
        """任意异常（含 _get_json 抛非 HTTP 异常）都不外泄。"""
        gj = mock.AsyncMock(side_effect=RuntimeError("unexpected"))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "platform_error"))

    def test_timeout_reads_settings_not_hardcoded(self):
        """★ 超时读配置 settings.oncall_timeout（不再硬编码 2 秒）。"""
        p = mock.patch.object(settings, "oncall_timeout", 4.5)
        p.start()
        self.addCleanup(p.stop)
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json = mock.MagicMock(return_value=self._body())
        client = mock.MagicMock()
        client.get = mock.AsyncMock(return_value=resp)
        cm = mock.MagicMock()
        cm.__aenter__.return_value = client
        ctor = mock.MagicMock(return_value=cm)
        with mock.patch.object(oncall_mod.httpx, "AsyncClient", ctor):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual(ctor.call_args.kwargs.get("timeout"), 4.5)  # ★ 配置值透传
        self.assertEqual((targets, status), (["ou_zzz|张三", "ou_yyy|李四"], "ok"))

    def test_timeout_setting_fallbacks(self):
        """配置缺失/非法/非正 ⇒ 回退默认 2 秒；合法值原样生效。"""
        for raw, want in [(3.5, 3.5), (2.0, 2.0), (0, 2.0), (-1, 2.0),
                          ("bad", 2.0), (None, 2.0)]:
            with self.subTest(raw=raw):
                p = mock.patch.object(settings, "oncall_timeout", raw)
                p.start()
                self.addCleanup(p.stop)
                self.assertEqual(oncall_mod._timeout_seconds(), want)


_LEGACY_NOTIFICATIONS_DDL = """
    CREATE TABLE notifications (
        id VARCHAR(32) PRIMARY KEY,
        created_at DATETIME,
        event_key VARCHAR(255),
        asset_id VARCHAR(32),
        asset_name VARCHAR(128),
        asset_host VARCHAR(255),
        metric_key VARCHAR(64),
        value VARCHAR(64),
        operator VARCHAR(8),
        threshold FLOAT,
        rule_id VARCHAR(32),
        rule_name VARCHAR(128),
        severity VARCHAR(16),
        channel VARCHAR(16),
        ok BOOLEAN,
        detail TEXT,
        attempts INTEGER,
        at_targets TEXT,
        degraded BOOLEAN,
        message_id VARCHAR(64)
    )
"""


class NotificationAdditiveColumnsTest(unittest.TestCase):
    """存量库补列：对"没有 oncall_lookup_failed / merged_count 的旧表"执行
    database._ensure_additive_columns 后，两列存在且默认值正确（临时 sqlite 造旧表验证）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _engine(self, name):
        return create_async_engine(
            "sqlite+aiosqlite:///" + (pathlib.Path(self._tmp.name) / name).as_posix(),
            poolclass=NullPool)

    @staticmethod
    def _columns(engine):
        async def _go():
            async with engine.connect() as conn:
                rows = (await conn.execute(
                    text("PRAGMA table_info(notifications)"))).all()
                return {r[1]: r for r in rows}
        return asyncio.run(_go())

    @staticmethod
    def _migrate(engine):
        async def _go():
            async with engine.begin() as conn:
                await database_mod._ensure_additive_columns(conn)
        asyncio.run(_go())

    def _seed_legacy_row(self, engine, rid="legacy-1"):
        async def _go():
            async with engine.begin() as conn:
                await conn.execute(text(_LEGACY_NOTIFICATIONS_DDL))
                await conn.execute(text(
                    "INSERT INTO notifications (id, created_at, event_key, ok, degraded) "
                    "VALUES (:i, '2026-01-01 00:00:00', 'opstk:cpu:h1', 1, 0)"),
                    {"i": rid})
        asyncio.run(_go())

    def test_legacy_table_gets_both_columns_with_correct_defaults(self):
        """旧表（20 列）补列后：两列存在、NOT NULL、默认 0；存量行与新行都落到默认值。"""
        engine = self._engine("legacy.db")
        self._seed_legacy_row(engine)
        before = self._columns(engine)
        self.assertNotIn("oncall_lookup_failed", before)
        self.assertNotIn("merged_count", before)

        self._migrate(engine)

        cols = self._columns(engine)
        self.assertIn("oncall_lookup_failed", cols)
        self.assertIn("merged_count", cols)
        for name in ("oncall_lookup_failed", "merged_count"):
            row = cols[name]
            self.assertEqual(row[3], 1, "NOT NULL")        # notnull
            self.assertEqual(str(row[4]), "0")             # dflt_value = 0

        async def _read_and_insert():
            async with engine.connect() as conn:
                old = (await conn.execute(text(
                    "SELECT oncall_lookup_failed, merged_count FROM notifications "
                    "WHERE id='legacy-1'"))).all()
            async with engine.begin() as conn:
                # 迁移后再插一条"旧应用"风格的行（不带新列）⇒ 新列取列默认值
                await conn.execute(text(
                    "INSERT INTO notifications (id, created_at, event_key, ok, degraded) "
                    "VALUES ('legacy-2', '2026-01-02 00:00:00', 'opstk:cpu:h2', 1, 0)"))
                both = (await conn.execute(text(
                    "SELECT id, oncall_lookup_failed, merged_count FROM notifications "
                    "ORDER BY id"))).all()
            return old, both
        old, both = asyncio.run(_read_and_insert())
        self.assertEqual(list(old[0]), [0, 0])             # ★ 存量行补列即得默认值
        self.assertEqual(both, [("legacy-1", 0, 0), ("legacy-2", 0, 0)])
        asyncio.run(engine.dispose())

    def test_migration_is_idempotent(self):
        """重复跑补列逻辑（相当于多次重启）：不报错、不加重复列、数据原样。"""
        engine = self._engine("idem.db")
        self._seed_legacy_row(engine)
        for _ in range(3):
            self._migrate(engine)
        cols = self._columns(engine)
        self.assertEqual(len(cols), 22)                    # 20 旧列 + 2 新列，无重复
        self.assertIn("oncall_lookup_failed", cols)
        self.assertIn("merged_count", cols)
        asyncio.run(engine.dispose())

    def test_fresh_create_all_and_model_defaults(self):
        """新库 create_all 直接带两列；模型默认值 False / 0 生效。"""
        engine = self._engine("fresh.db")
        SessionLocal = async_sessionmaker(engine, class_=AsyncSession,
                                          expire_on_commit=False)

        async def _go():
            async with engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.create_all)
            async with SessionLocal() as db:
                db.add(models.Notification(event_key="opstk:cpu:h", ok=True))
                await db.commit()
                return (await db.execute(select(models.Notification))).scalars().all()

        recs = asyncio.run(_go())
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0].oncall_lookup_failed)     # ★ 默认 False
        self.assertEqual(recs[0].merged_count, 0)          # ★ 默认 0
        asyncio.run(engine.dispose())


class InspectionHookTest(AlertsTestBase):
    """巡检接入点：命中告警 ⇒ fire-and-forget 调 notify_alert（全假数据，不连设备）。"""

    def _seed(self):
        async def _seed():
            async with self.SessionLocal() as db:
                db.add(models.Asset(id="a1", name="sw-01", category="ct",
                                    vendor="h3c", host="10.0.0.9"))
                db.add(models.Asset(id="a2", name="sw-02", category="ct",
                                    vendor="h3c", host="10.0.0.10"))
                db.add(models.AlertRule(id="r1", name="CPU 过高", metric_key="cpu",
                                        operator="gt", threshold=90.0, enabled=True))
                db.add(models.InspectionTask(id="task1", name="巡检 2 台",
                                             asset_ids=["a1", "a2"], status="pending"))
                await db.commit()
        asyncio.run(_seed())

    @staticmethod
    def _results():
        return [
            {"asset_id": "a1", "asset_name": "sw-01", "status": "success",
             "error": "",
             "metrics": {"cpu": {"label": "CPU使用率", "unit": "%",
                                 "summary": "95%", "status": "ok", "value": 95}},
             "raw": []},
            {"asset_id": "a2", "asset_name": "sw-02", "status": "success",
             "error": "",
             "metrics": {"cpu": {"label": "CPU使用率", "unit": "%",
                                 "summary": "50%", "status": "ok", "value": 50}},
             "raw": []},
        ]

    def _run_task(self):
        results = self._results()
        inspect_many = mock.AsyncMock(return_value=results)
        notify = mock.AsyncMock()
        with mock.patch.object(inspection_service, "async_session", self.SessionLocal), \
                mock.patch.object(inspection_service, "inspect_many", inspect_many), \
                mock.patch.object(inspection_service, "notify_alert", notify):
            out = asyncio.run(
                inspection_service.run_task_in_background("task1", None))
        return out, notify

    def test_alert_hit_fires_notify_alert(self):
        self.configure()
        self._seed()
        out, notify = self._run_task()
        self.assertEqual(len(out), 2)
        self.assertTrue(notify.called, "命中告警必须调过 notify_alert（fire-and-forget）")
        self.assertEqual(notify.call_count, 1)          # 只有 a1 命中（95 > 90），a2 没超
        kw = notify.call_args.kwargs
        self.assertEqual(kw["metric_key"], "cpu")
        self.assertEqual(kw["value"], 95)
        self.assertEqual(kw["asset_id"], "a1")
        self.assertEqual(kw["asset_name"], "sw-01")
        self.assertEqual(kw["asset_host"], "10.0.0.9")  # host 从资产表带上了
        self.assertEqual(kw["severity"], "critical")
        self.assertEqual(kw["rule"].id, "r1")

    def test_alert_hit_marks_result_failed_and_persists(self):
        """告警判定本身的行为不变：置 failed + error 里带 [告警] 行。"""
        self.configure()
        self._seed()
        self._run_task()

        async def _q():
            async with self.SessionLocal() as db:
                task = await db.get(models.InspectionTask, "task1")
                rows = (await db.execute(
                    select(models.InspectionResult).order_by(models.InspectionResult.asset_id)
                )).scalars().all()
                return task, rows

        task, rows = asyncio.run(_q())
        self.assertEqual(task.status, "done")
        by_asset = {r.asset_id: r for r in rows}
        self.assertEqual(by_asset["a1"].status, "failed")
        self.assertIn("[告警] CPU 过高: cpu=95 gt 90", by_asset["a1"].error)
        self.assertEqual(by_asset["a2"].status, "success")   # 未超标的不动

    def test_disabled_never_fires_notify(self):
        """notify_enabled=False ⇒ 接入点零开销：notify_alert 不被调用。"""
        self.configure(notify_enabled=False)
        self._seed()
        out, notify = self._run_task()
        self.assertEqual(len(out), 2)
        self.assertFalse(notify.called)
        # 告警判定照旧：页面记录仍是 failed（只是不外发）
        async def _q():
            async with self.SessionLocal() as db:
                return (await db.execute(select(models.InspectionResult))).scalars().all()
        rows = asyncio.run(_q())
        self.assertEqual([r.status for r in rows], ["failed", "success"])


class ContractTest(unittest.TestCase):
    """对外契约：签名钉住（集成方/下一个单元按这个接）。"""

    def test_notify_alert_signature(self):
        sig = inspect.signature(alerts_mod.notify_alert)
        params = sig.parameters
        self.assertEqual(list(params),
                         ["rule", "metric_key", "value", "asset_id", "asset_name",
                          "asset_host", "severity", "occurred_at"])
        for p in params.values():
            self.assertEqual(p.kind, inspect.Parameter.KEYWORD_ONLY, p.name)
        self.assertEqual(params["severity"].default, "critical")
        self.assertIsNone(params["occurred_at"].default)

    def test_current_oncall_signature(self):
        sig = inspect.signature(oncall_mod.current_oncall)
        self.assertEqual(list(sig.parameters), ["team"])
        self.assertEqual(sig.parameters["team"].default, "")

    def test_notification_model_required_fields(self):
        cols = {c.name for c in models.Notification.__table__.columns}
        for name in ("id", "created_at", "event_key", "asset_id", "asset_name",
                     "asset_host", "metric_key", "value", "operator", "threshold",
                     "rule_id", "rule_name", "severity", "channel", "ok",
                     "detail", "attempts", "degraded", "at_targets", "message_id",
                     "oncall_lookup_failed", "merged_count"):
            self.assertIn(name, cols)
        self.assertEqual(models.Notification.__tablename__, "notifications")

    def test_notification_table_auto_created(self):
        """init_db() 的 create_all 会把 notifications 表建出来（验收②的用例版证据）。"""
        async def _main():
            engine = create_async_engine("sqlite+aiosqlite://", poolclass=NullPool)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(models.Base.metadata.create_all)
                    return await conn.run_sync(
                        lambda c: sa_inspect(c).get_table_names())
            finally:
                await engine.dispose()

        tables = asyncio.run(_main())
        self.assertIn("notifications", tables)


if __name__ == "__main__":
    unittest.main()
