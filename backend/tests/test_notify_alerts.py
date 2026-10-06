# -*- coding: utf-8 -*-
"""告警通知集成单元测试 —— **全部 mock，绝不真实联网、绝不真发飞书消息**。

被测对象：
  · app/core/notify/alerts.py  notify_alert（@ 目标四级降级 / 去重 / 发送记录 / 绝不抛）
  · app/core/oncall.py         current_oncall（超时/报错降级、30 秒缓存、绝不抛）
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
from unittest import mock

import httpx
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import database as database_mod
from app.config import settings
from app.core import models
from app.core import oncall as oncall_mod
from app.core.notify import alerts as alerts_mod
from app.core.notify.feishu import FeishuResult
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
        self.assertEqual(send.await_count, 0)      # ★ 零发送
        self.assertEqual(oncall.await_count, 0)    # ★ 连值班平台都不查
        self.assertEqual(self._notifications(), [])  # ★ 零记录


class AtTargetsTest(AlertsTestBase):
    """@ 目标四级路径：平台 / 配置 / @all / 都没有。"""

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
        """① 平台查到当班人 ⇒ 用平台结果，不降级、不叠配置。"""
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
        self.assertEqual(json.loads(rec.at_targets), ["ou_zhang|张三", "ou_li|李四"])

    def test_path2_platform_timeout_falls_back_to_config(self):
        """② 平台超时 ⇒ 降级用 feishu_at_open_ids。"""
        self.configure(feishu_at_open_ids="ou_cfg1|值班-配置, ou_cfg2")
        oncall = mock.AsyncMock(return_value=([], "timeout"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertIn('<at user_id="ou_cfg1">值班-配置</at>', text)
        self.assertIn('<at user_id="ou_cfg2">ou_cfg2</at>', text)
        self.assertIn("已降级", text)                 # 备注里说清降级原因
        rec = self._notifications()[0]
        self.assertTrue(rec.degraded)
        self.assertEqual(json.loads(rec.at_targets), ["ou_cfg1|值班-配置", "ou_cfg2"])

    def test_path3_only_at_all(self):
        """③ 配置里没有 open_id、但 feishu_at_all=True ⇒ @所有人。"""
        self.configure(feishu_at_all=True)
        oncall = mock.AsyncMock(return_value=([], "error"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertIn('<at user_id="all">所有人</at>', text)
        self.assertNotIn("ou_cfg", text)
        rec = self._notifications()[0]
        self.assertTrue(rec.degraded)
        self.assertEqual(json.loads(rec.at_targets), [])

    def test_path4_nothing_to_at_but_message_still_sent(self):
        """④ 都没有 ⇒ 不 @，但消息照发（告警事实不能丢）。"""
        oncall = mock.AsyncMock(return_value=([], "not_configured"))
        with mock.patch.object(oncall_mod, "current_oncall", oncall):
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))
        text = self._text()
        self.assertNotIn("<at", text)
        self.assertIn("已降级", text)
        self.assertIn("sw-01", text)                  # 正文照常
        rec = self._notifications()[0]
        self.assertTrue(rec.degraded)
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
    """去重：同 event_key 窗口内只发一条；失败记录不算成功、可重试。"""

    def setUp(self):
        super().setUp()
        self.configure()
        self.oncall = mock.AsyncMock(return_value=(["ou_z|张三"], "ok"))
        p = mock.patch.object(oncall_mod, "current_oncall", self.oncall)
        p.start()
        self.addCleanup(p.stop)

    def _run(self):
        asyncio.run(alerts_mod.notify_alert(**alert_kwargs()))

    def test_second_send_within_window_is_suppressed(self):
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            self._run()                                # 同资产同指标 ⇒ 同 event_key
        self.assertEqual(send.await_count, 1)          # ★ 第二条没发
        self.assertEqual(len(self._notifications()), 1)

    def test_different_metric_key_is_not_deduped(self):
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            asyncio.run(alerts_mod.notify_alert(**alert_kwargs(metric_key="memory")))
        self.assertEqual(send.await_count, 2)
        recs = self._notifications()
        self.assertEqual([r.event_key for r in recs],
                         ["opstk:a1:cpu", "opstk:a1:memory"])

    def test_failed_send_is_not_deduped(self):
        """第一次发送失败 ⇒ 不占用去重窗口，下条告警仍会重试。"""
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

    def test_zero_window_disables_dedup(self):
        self.configure(notify_dedup_window=0)
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
            self._run()
        self.assertEqual(send.await_count, 2)

    def test_event_key_format(self):
        send = mock.AsyncMock(return_value=send_ok())
        with mock.patch.object(alerts_mod, "send_text", send):
            self._run()
        self.assertEqual(self._notifications()[0].event_key, "opstk:a1:cpu")


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
    """发送记录字段：快照要能回答"发给谁/为什么"."""

    def test_record_fields_snapshot(self):
        self.configure(feishu_at_open_ids="ou_cfg|配置兜底")
        oncall = mock.AsyncMock(return_value=([], "timeout"))
        send = mock.AsyncMock(return_value=FeishuResult(ok=True, detail="sent", attempts=2))
        with mock.patch.object(oncall_mod, "current_oncall", oncall), \
                mock.patch.object(alerts_mod, "send_text", send):
            asyncio.run(alerts_mod.notify_alert(
                **alert_kwargs(rule=make_rule(id="rule-xyz"), value=95.5,
                               occurred_at="2026-10-06 12:00:00")))
        rec = self._notifications()[0]
        self.assertEqual(rec.event_key, "opstk:a1:cpu")
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
        self.assertEqual(rec.detail, "sent")
        self.assertEqual(rec.attempts, 2)
        self.assertTrue(rec.degraded)
        self.assertIsNone(rec.message_id)               # 预留字段，发送层暂不回传


class CurrentOncallTest(unittest.TestCase):
    """值班人查询客户端：解析、降级状态码、缓存 —— 全 mock，零联网。"""

    def setUp(self):
        oncall_mod._reset_cache()
        self.addCleanup(oncall_mod._reset_cache)
        self._patches = []
        for k, v in {
            "oncall_base_url": "http://oncall.example",
            "oncall_token": "tok-unit-1",
            "oncall_team": "ct-net",
        }.items():
            p = mock.patch.object(settings, k, v)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _body():
        """ONCALL-PLATFORM-PLAN.md §4.1 的契约样例（节选）。"""
        return {
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

    def test_timeout_returns_timeout_status(self):
        gj = mock.AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "timeout"))

    def test_network_error_returns_error_status(self):
        gj = mock.AsyncMock(side_effect=httpx.ConnectError("refused"))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "error"))

    def test_http_500_returns_error_status(self):
        gj = mock.AsyncMock(return_value=(500, {"msg": "boom"}))
        with mock.patch.object(oncall_mod, "_get_json", gj):
            targets, status = asyncio.run(oncall_mod.current_oncall())
        self.assertEqual((targets, status), ([], "error"))

    def test_non_dict_body_returns_error_status(self):
        """响应不是 JSON 对象（字段缺失的形态之一）⇒ error，不抛。"""
        for body in (None, ["a", "b"], "ok"):
            with self.subTest(body=body):
                gj = mock.AsyncMock(return_value=(200, body))
                with mock.patch.object(oncall_mod, "_get_json", gj):
                    targets, status = asyncio.run(oncall_mod.current_oncall())
                self.assertEqual((targets, status), ([], "error"))

    def test_missing_members_returns_empty_status(self):
        """平台活着但拿不到任何可用成员（无班次 / 成员缺 feishu_open_id）⇒ empty。"""
        for body in ({"team": "ct-net", "empty_reason": "no_shift"},
                     {"primary": {"name": "张三"}},
                     {"primary": {"name": "张三", "feishu_open_id": "  "}}):
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
        self.assertEqual((targets, status), ([], "error"))


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
                     "detail", "attempts", "at_targets", "degraded", "message_id"):
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
