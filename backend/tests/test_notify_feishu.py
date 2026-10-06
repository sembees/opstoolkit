# -*- coding: utf-8 -*-
"""飞书通知发送模块单元测试 —— **全部 mock，绝不真实联网**。

被测对象：app/core/notify/feishu.py（只测"发消息"这一层；巡检/告警集成是下一个单元）。

联网边界（读用例前先看这里）：
  · 所有 HTTP 都收敛在 feishu._post_json，用例把它整个 mock 掉 ⇒ 不可能有真实请求；
  · 重试退避 feishu._sleep 也 mock 掉 ⇒ 用例不会真等 0.5s/1s；
  · 凭据一律用假值（cli_unit_test_appid / unit_test_secret），不出现任何真实 app_id/secret。
"""
import asyncio
import inspect
import json
import time
import unittest
from unittest import mock

import httpx

from app.config import Settings, settings
from app.core.notify import feishu as feishu_mod
from app.core.notify.feishu import FeishuResult, build_alert_text, feishu_configured, send_text


class NotifyTestBase(unittest.TestCase):
    """公共脚手架：清 token 缓存 + mock 掉 sleep。settings 由各用例自行 configure()。"""

    def setUp(self):
        feishu_mod._reset_token_cache()
        self.addCleanup(feishu_mod._reset_token_cache)
        self._sleep = mock.AsyncMock()
        p = mock.patch.object(feishu_mod, "_sleep", self._sleep)
        p.start()
        self.addCleanup(p.stop)

    def configure(self, **over):
        """把"是否已配置"相关设置钉成确定值（默认：全配 + 启用），个别项用 over 覆盖。"""
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
        }
        values.update(over)
        for k, v in values.items():
            p = mock.patch.object(settings, k, v)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _token_ok(token="t-unit"):
        return (200, {"code": 0, "tenant_access_token": token, "expire": 2020})

    @staticmethod
    def _send_ok(message_id="om_unit"):
        return (200, {"code": 0, "data": {"message_id": message_id}})

    @staticmethod
    def _token_calls(pj):
        return [c for c in pj.call_args_list
                if c.args and "tenant_access_token" in str(c.args[0])]


class BuildAlertTextTest(unittest.TestCase):
    """build_alert_text：字段齐全、多指标逐行、@ 行首与占位规则。"""

    def _build(self, **over):
        kw = dict(
            title="CPU 使用率过高",
            severity="critical",
            asset_name="core-sw-01",
            asset_host="192.168.1.10",
            metrics=[{"name": "CPU使用率", "value": "95.2", "unit": "%",
                      "threshold": 90, "op": "gt"}],
            rule_name="CPU 过高",
            occurred_at="2026-01-01 12:00:00",
        )
        kw.update(over)
        return build_alert_text(**kw)

    def test_full_fields_present(self):
        """设备名/IP/指标名/实测值/阈值与比较符/规则名/时间/严重级/来源 一个都不能少。"""
        text = self._build()
        for frag in ("CPU 使用率过高", "core-sw-01", "192.168.1.10", "CPU使用率",
                     "95.2", "90", ">", "CPU 过高", "2026-01-01 12:00:00",
                     "严重", "OpsToolkit 巡检"):
            self.assertIn(frag, text)

    def test_severity_labels(self):
        for raw, want in [("critical", "严重"), ("warning", "警告"),
                          ("info", "提示"), (" unknown_sev ", "unknown_sev")]:
            with self.subTest(severity=raw):
                self.assertIn("【%s告警】" % want, self._build(severity=raw))

    def test_multi_metrics_one_per_line(self):
        text = self._build(metrics=[
            {"name": "CPU使用率", "value": "95.2", "unit": "%", "threshold": 90, "op": "gt"},
            {"name": "内存使用率", "value": "88", "unit": "%", "threshold": 85, "op": "gte"},
        ])
        cpu_lines = [ln for ln in text.splitlines() if "CPU使用率" in ln and "95.2" in ln]
        mem_lines = [ln for ln in text.splitlines() if "内存使用率" in ln]
        self.assertEqual(len(cpu_lines), 1)
        self.assertEqual(len(mem_lines), 1)
        self.assertNotEqual(cpu_lines[0], mem_lines[0])  # 逐行，不挤在一行
        self.assertIn("≥ 85%", mem_lines[0])

    def test_metric_op_symbols(self):
        for op, sym in [("gt", ">"), ("gte", "≥"), ("lt", "<"), ("lte", "≤"),
                        ("eq", "="), ("ne", "≠"), (">=", ">=")]:
            with self.subTest(op=op):
                text = self._build(metrics=[{"name": "m", "value": "1", "unit": "",
                                             "threshold": 5, "op": op}])
                self.assertIn("（阈值 %s 5）" % sym, text)

    def test_metric_missing_fields_get_placeholders(self):
        text = self._build(metrics=[{}])
        self.assertIn("未命名指标", text)
        self.assertIn("未知", text)

    def test_at_with_name_and_fallback(self):
        text = self._build(at_open_ids=["ou_abc|张三", "ou_xyz"])
        self.assertIn('<at user_id="ou_abc">张三</at>', text)
        self.assertIn('<at user_id="ou_xyz">ou_xyz</at>', text)  # 没有名字 → 写 user_id 本身

    def test_at_all_marker(self):
        self.assertIn('<at user_id="all">所有人</at>', self._build(at_all=True))

    def test_no_at_by_default(self):
        self.assertNotIn("<at", self._build())

    def test_at_lines_are_at_line_start(self):
        """飞书 text 的惯例：@ 放在行首。"""
        text = self._build(at_open_ids=["ou_abc|张三", "ou_xyz"], at_all=True)
        at_lines = [ln for ln in text.splitlines() if "<at " in ln]
        self.assertEqual(len(at_lines), 3)
        for ln in at_lines:
            self.assertTrue(ln.startswith("<at"), "@ 必须在行首：%r" % ln)

    def test_extra_note(self):
        self.assertIn("备注：机房空调检修中", self._build(extra_note="机房空调检修中"))
        self.assertNotIn("备注：", self._build())

    def test_source_default_and_custom(self):
        self.assertIn("来源：OpsToolkit 巡检", self._build())
        self.assertIn("来源：备用链路巡检", self._build(source="备用链路巡检"))


class ApiContractTest(unittest.TestCase):
    """对外接口契约：下一个单元按这个签名集成，签名变了这里先红。"""

    def test_feishu_result_defaults(self):
        r = FeishuResult(ok=False, detail="x")
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "x")
        self.assertEqual(r.attempts, 0)

    def test_package_exports(self):
        from app.core import notify as pkg

        for name in ("FeishuResult", "feishu_configured", "build_alert_text", "send_text"):
            self.assertTrue(hasattr(pkg, name), name)

    def test_build_alert_text_signature(self):
        sig = inspect.signature(build_alert_text)
        params = sig.parameters
        self.assertEqual(
            list(params),
            ["title", "severity", "asset_name", "asset_host", "metrics", "rule_name",
             "occurred_at", "source", "at_open_ids", "at_all", "extra_note"],
        )
        for p in params.values():
            self.assertEqual(p.kind, inspect.Parameter.KEYWORD_ONLY, p.name)
        self.assertEqual(params["source"].default, "OpsToolkit 巡检")
        self.assertIsNone(params["at_open_ids"].default)
        self.assertIs(params["at_all"].default, False)
        self.assertEqual(params["extra_note"].default, "")

    def test_send_text_signature(self):
        sig = inspect.signature(send_text)
        self.assertEqual(list(sig.parameters), ["text", "at_open_ids", "at_all"])
        kinds = [p.kind for p in sig.parameters.values()]
        self.assertEqual(kinds[0], inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertEqual(kinds[1:], [inspect.Parameter.KEYWORD_ONLY] * 2)
        self.assertIsNone(sig.parameters["at_open_ids"].default)
        self.assertIs(sig.parameters["at_all"].default, False)


class FeishuConfiguredTest(NotifyTestBase):
    """feishu_configured：缺任一配置 → False；默认（不配置）→ False；全配+启用 → True。"""

    def test_settings_defaults_are_off(self):
        """★ 类级默认值就是"不配置就不发"：notify_enabled=False、各凭据为空。"""
        f = Settings.model_fields
        for name, want in [
            ("notify_enabled", False), ("feishu_app_id", ""), ("feishu_app_secret", ""),
            ("feishu_receive_id", ""), ("feishu_receive_id_type", "chat_id"),
            ("feishu_at_open_ids", ""), ("feishu_at_all", False),
            ("feishu_timeout", 8.0), ("notify_dedup_window", 300),
            # 值班平台客户端（模式 A）同样默认全关；查询超时默认 2 秒（供 oncall.py 读取）
            ("oncall_base_url", ""), ("oncall_token", ""), ("oncall_team", ""),
            ("oncall_timeout", 2.0),
        ]:
            with self.subTest(field=name):
                self.assertEqual(f[name].default, want)

    def test_missing_any_field_false(self):
        for over in (
            {"notify_enabled": False},
            {"feishu_app_id": ""},
            {"feishu_app_secret": ""},
            {"feishu_receive_id": ""},
            {"feishu_app_id": "   "},  # 纯空白视同未配置
        ):
            with self.subTest(over=over):
                self.configure(**over)
                self.assertFalse(feishu_configured())

    def test_all_configured_true(self):
        self.configure()
        self.assertTrue(feishu_configured())

    def test_disabled_even_if_configured(self):
        """★ 全配了但 notify_enabled=False（也是默认值）→ False，默认不发。"""
        self.configure(notify_enabled=False)
        self.assertFalse(feishu_configured())


class SendTextTest(NotifyTestBase):
    """send_text：全 mock。_post_json 被替换 ⇒ 零真实联网。"""

    def setUp(self):
        super().setUp()
        self.configure()

    def test_not_configured_sends_nothing(self):
        for over in ({"notify_enabled": False},
                     {"feishu_app_id": "", "feishu_app_secret": "", "feishu_receive_id": ""}):
            with self.subTest(over=over):
                self.configure(**over)
                pj = mock.AsyncMock()
                with mock.patch.object(feishu_mod, "_post_json", pj):
                    r = asyncio.run(send_text("不应发出去"))
                self.assertIsInstance(r, FeishuResult)
                self.assertFalse(r.ok)
                self.assertEqual(r.attempts, 0)
                self.assertEqual(pj.await_count, 0)  # ★ 一个请求都不发
                self.assertIn("未启用", r.detail)

    def test_success_path(self):
        pj = mock.AsyncMock(side_effect=[self._token_ok("t-ok"), self._send_ok("om_1")])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("磁盘告警\n第二行"))
        self.assertTrue(r.ok)
        self.assertEqual(r.detail, "sent")
        self.assertEqual(r.attempts, 1)
        self.assertEqual(len(pj.call_args_list), 2)
        tok_call, send_call = pj.call_args_list
        self.assertIn("tenant_access_token", str(tok_call.args[0]))
        self.assertIn("receive_id_type=chat_id", str(send_call.args[0]))
        payload = send_call.args[1]
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["receive_id"], "oc_unit_test_chat")
        self.assertEqual(json.loads(payload["content"])["text"], "磁盘告警\n第二行")
        self.assertEqual(send_call.kwargs["headers"]["Authorization"], "Bearer t-ok")

    def test_business_error_bot_not_in_group(self):
        """code=230002（机器人不在目标群）：不抛异常，ok=False + 人话。"""
        pj = mock.AsyncMock(side_effect=[
            self._token_ok(),
            (400, {"code": 230002, "msg": "The bot can not be outside the group."}),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertFalse(r.ok)
        self.assertIn("230002", r.detail)
        self.assertIn("机器人不在", r.detail)
        self.assertEqual(r.attempts, 1)  # 业务错误不重试
        self.assertEqual(pj.await_count, 2)

    def test_http_400_permission_missing_code_99991672(self):
        """★ 2026-10-05 监工复算改：原来这条夹具编了 msg「tenant access token is invalid」并断言含「令牌」，
        但实测 99991672 的真实返回是 Access denied / scopes required（缺权限），与令牌无关。
        这里改用**真实返回文本**并断言提示指向「权限」。"""
        pj = mock.AsyncMock(side_effect=[
            self._token_ok(),
            (400, {"code": 99991672,
                   "msg": "Access denied. One of the following scopes is required: "
                          "[contact:user.id:readonly]."}),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertFalse(r.ok)
        self.assertIn("权限", r.detail)
        self.assertIn("99991672", r.detail)
        self.assertEqual(r.attempts, 1)

    def test_http_401_token_invalid_code_99991663(self):
        """真正的令牌失效码（99991663）才应该被提示成「令牌」问题。"""
        pj = mock.AsyncMock(side_effect=[
            self._token_ok(),
            (401, {"code": 99991663, "msg": "invalid tenant access token"}),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertFalse(r.ok)
        self.assertIn("令牌", r.detail)
        self.assertIn("401", r.detail)
        self.assertEqual(r.attempts, 1)

    def test_permission_missing_code_230027(self):
        pj = mock.AsyncMock(side_effect=[
            self._token_ok(),
            (400, {"code": 230027, "msg": "Lack of necessary permissions."}),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertFalse(r.ok)
        self.assertIn("权限", r.detail)
        self.assertEqual(r.attempts, 1)

    def test_network_error_retries_then_fails(self):
        pj = mock.AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 3)          # 1 首发 + 2 重试
        self.assertEqual(pj.await_count, 3)
        self.assertEqual(self._sleep.await_count, 2)  # 指数退避两次
        self.assertIn("网络异常", r.detail)

    def test_server_error_then_success(self):
        """token 请求先吃 503（可重试）→ 第二轮取到 token 并发送成功。"""
        pj = mock.AsyncMock(side_effect=[
            (503, {}),
            self._token_ok("t-5xx"),
            self._send_ok("om_5xx"),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(pj.await_count, 3)

    def test_rate_limit_retried_then_success(self):
        """频控（HTTP 429 + code 99991400）走重试；token 已缓存，第二轮不再取令牌。"""
        pj = mock.AsyncMock(side_effect=[
            self._token_ok("t-429"),
            (429, {"code": 99991400, "msg": "request trigger frequency limit"}),
            self._send_ok("om_429"),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(len(self._token_calls(pj)), 1)  # 第二轮用的是缓存 token
        self.assertEqual(pj.await_count, 3)

    def test_at_in_text_not_duplicated(self):
        """text 已带 @ ⇒ 不重复添加（即使配置里也配了 @ 和 at_all）。"""
        self.configure(feishu_at_open_ids="ou_cfg1", feishu_at_all=True)
        pj = mock.AsyncMock(side_effect=[self._token_ok(), self._send_ok()])
        text = '群里的 <at user_id="ou_x">x</at> 请关注'
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text(text))
        self.assertTrue(r.ok)
        sent = json.loads(pj.call_args_list[1].args[1]["content"])["text"]
        self.assertEqual(sent, text)  # 逐字相同：既没加 ou_cfg1 也没加 <at user_id="all">

    def test_at_from_settings_prepended(self):
        """text 没 @ ⇒ 回落到配置：逗号拆分、支持 显示名、@ 在行首；显式空列表可覆盖。"""
        self.configure(feishu_at_open_ids="ou_cfg1|运维, ou_cfg2", feishu_at_all=False)
        pj = mock.AsyncMock(side_effect=[self._token_ok(), self._send_ok(), self._send_ok()])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r1 = asyncio.run(send_text("磁盘满了"))
            r2 = asyncio.run(send_text("无可 @ 对象", at_open_ids=[]))
        self.assertTrue(r1.ok and r2.ok)
        sent1 = json.loads(pj.call_args_list[1].args[1]["content"])["text"]
        self.assertTrue(sent1.startswith('<at user_id="ou_cfg1">运维</at>'))
        self.assertIn('<at user_id="ou_cfg2">ou_cfg2</at>', sent1)
        self.assertIn("磁盘满了", sent1)
        for ln in sent1.splitlines():
            if "<at " in ln:
                self.assertTrue(ln.startswith("<at"))
        sent2 = json.loads(pj.call_args_list[2].args[1]["content"])["text"]
        self.assertNotIn("<at", sent2)  # 显式 at_open_ids=[] ⇒ 不从配置兜底

    def test_invalid_receive_id_type_sends_nothing(self):
        self.configure(feishu_receive_id_type="phone")
        pj = mock.AsyncMock()
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r = asyncio.run(send_text("hello"))
        self.assertFalse(r.ok)
        self.assertIn("receive_id_type", r.detail)
        self.assertEqual(r.attempts, 0)
        self.assertEqual(pj.await_count, 0)


class TokenCacheTest(NotifyTestBase):
    """token 缓存：连续两次发送只取一次令牌；过期（含 300 秒提前量）后重新取。"""

    def test_token_fetched_once_for_two_sends(self):
        self.configure()
        pj = mock.AsyncMock(side_effect=[
            self._token_ok("t-cache"), self._send_ok(), self._send_ok(),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            r1 = asyncio.run(send_text("第一条"))
            r2 = asyncio.run(send_text("第二条"))
        self.assertTrue(r1.ok and r2.ok)
        self.assertEqual(len(self._token_calls(pj)), 1)   # ★ 只取一次 token
        self.assertEqual(pj.await_count, 3)               # token + 两条消息

    def test_expired_token_refetched(self):
        self.configure()
        pj = mock.AsyncMock(side_effect=[
            self._token_ok("t-old"), self._send_ok(), self._token_ok("t-new"), self._send_ok(),
        ])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            asyncio.run(send_text("第一条"))
            feishu_mod._token_expire_at = 0.0  # 人为令缓存过期
            r2 = asyncio.run(send_text("第二条"))
        self.assertTrue(r2.ok)
        self.assertEqual(len(self._token_calls(pj)), 2)
        self.assertEqual(pj.call_args_list[3].kwargs["headers"]["Authorization"], "Bearer t-new")

    def test_refresh_margin_300s(self):
        """expire=2020 ⇒ 缓存期 ≈ 2020-300=1720 秒（提前 300 秒刷新）。"""
        self.configure()
        pj = mock.AsyncMock(side_effect=[self._token_ok(), self._send_ok()])
        with mock.patch.object(feishu_mod, "_post_json", pj):
            asyncio.run(send_text("hello"))
        delta = feishu_mod._token_expire_at - time.time()
        self.assertLess(delta, 2020 - 300 + 5)
        self.assertGreater(delta, 2020 - 300 - 60)


class HealthNotifyTest(NotifyTestBase):
    """/health 的 notify 字段：布尔、不联网、跟随 feishu_configured()。"""

    def test_health_notify_false_when_unconfigured(self):
        from fastapi.testclient import TestClient

        from app.main import app

        for over in ({"notify_enabled": False},
                     {"notify_enabled": True, "feishu_app_id": "",
                      "feishu_app_secret": "", "feishu_receive_id": ""}):
            with self.subTest(over=over):
                self.configure(**over)
                resp = TestClient(app).get("/health")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertIn("notify", body)
                self.assertIsInstance(body["notify"], bool)
                self.assertFalse(body["notify"])

    def test_health_notify_true_when_configured(self):
        from fastapi.testclient import TestClient

        from app.main import app

        self.configure()
        resp = TestClient(app).get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["notify"])


if __name__ == "__main__":
    unittest.main()
