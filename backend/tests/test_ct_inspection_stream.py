# -*- coding: utf-8 -*-
"""巡检"点了没反应"那一轮的回归用例（2026-10-04）。

### 修的是什么

用户反馈：**自定义命令巡检点了之后界面一直卡着**。查证后发现是**两个叠加的问题**：

1. **事件一个都发不出去**（真病根）：`inspect_many(on_event=...)` 收的是**异步**回调，
   而服务层是同步 `emit(ev)` 调用 ⇒ 每次只产生一个没人 await 的协程被丢弃。
   前端因此永远停在它自己写的那行"连接已建立"，看着就像卡死。
2. **不可达设备要等两分钟**：`192.168.1.2` 不在任何本地网段、被丢给默认网关，
   TCP SYN 被静默丢弃，而**连接阶段不受 netmiko 的 conn_timeout 管**（由 OS 的 SYN 重试决定，
   Linux 默认 ~127s）。且原来 `conn_timeout` 与读取超时都是 60s，没有快速失败。

另外：WS 这条链路**跑完什么都不落库** ⇒ 历史记录与仪表盘"巡检任务"永远是 0，也没有可下载的数据源。

### 这一组用例守什么

  · TCP 预检：不可达要**立刻**抛"设备不可达"，可达不能误报；
  · 连接超时与读取超时**必须拆开**（连接短、读取长）；
  · `run_task_in_background` 要支持同步 `on_event` 回调并**返回结果**（WS 靠它发 complete）；
  · **AST 护栏**：WS 里传给 `run_task_in_background` 的 `on_event` 必须是**普通 def**
    （写成 `async def` 就会重演"事件全丢、界面假死"）；
  · 导出渲染：txt 含原始输出、json 可解析、csv 带 BOM 与表头。
"""
import ast
import inspect as _ins
import json
import pathlib
import socket
import unittest

from app.api import inspection
from app.config import settings
from app.core import models
from app.ct.inspection import service


class PreflightTest(unittest.TestCase):
    def test_unreachable_port_fails_fast_with_a_clear_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            service._preflight("127.0.0.1", 1, 1.0)      # 端口 1 基本必然被拒
        msg = str(ctx.exception)
        self.assertIn("设备不可达", msg)
        self.assertIn("127.0.0.1", msg)

    def test_open_port_passes(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            service._preflight("127.0.0.1", port, 2.0)   # 不该抛
        finally:
            srv.close()


class TimeoutSplitTest(unittest.TestCase):
    def test_connect_timeout_is_short_and_read_timeout_is_long(self):
        asset = models.Asset(name="t", host="10.0.0.1", vendor="h3c", device_role="switch")
        p = service._build_connect_params(asset, {})
        self.assertEqual(p["conn_timeout"], settings.inspection_connect_timeout)
        self.assertEqual(p["timeout"], settings.inspection_timeout)
        self.assertLess(p["conn_timeout"], p["timeout"],
                        "连接超时必须短于读取超时，否则不可达设备会长时间占着界面")


class RunnerContractTest(unittest.TestCase):
    def test_runner_accepts_sync_on_event_and_returns_results(self):
        sig = _ins.signature(service.run_task_in_background)
        self.assertIn("on_event", sig.parameters, "WS 直播要靠这个回调转发事件")

    def test_ws_passes_a_SYNC_callback(self):
        """★ 回归护栏：WS 里的 on_event 必须是普通 def。

        写成 `async def` 会重演这次的真病根：服务层同步 emit ⇒ 协程被丢弃 ⇒
        一个事件都发不出去，前端停在"连接已建立"像卡死。
        """
        src = pathlib.Path(inspection.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        ws = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "inspection_ws")
        fns = {n.name: n for n in ast.walk(ws)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("on_event", fns, "WS 里应定义 on_event 回调")
        self.assertIsInstance(fns["on_event"], ast.FunctionDef)
        self.assertNotIsInstance(fns["on_event"], ast.AsyncFunctionDef,
                                 "on_event 不能是 async def —— 服务层是同步调用的")
        # 并且调用 run_task_in_background 时确实把它传了进去
        passed = [k.arg for c in ast.walk(ws) if isinstance(c, ast.Call) for k in c.keywords]
        self.assertIn("on_event", passed)

    def test_ws_persists_a_task_before_running(self):
        """WS 必须**先落库**再跑：否则历史记录为空、也没有可下载的数据源。"""
        src = pathlib.Path(inspection.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        ws = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "inspection_ws")
        makes_task = any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "InspectionTask"
            for n in ast.walk(ws)
        )
        self.assertTrue(makes_task, "WS 里应创建 InspectionTask 记录")


class ExportRenderTest(unittest.TestCase):
    class _Task:
        id = "abcdef1234567890"
        name = "巡检 1 台设备"
        kind = "custom"
        template = "standard"
        status = "done"
        created_at = None
        finished_at = None

    class _Result:
        asset_id = "a1"
        asset_name = "sw1"
        status = "success"
        error = ""
        metrics = {"cpu": {"label": "CPU", "value": 3}}
        raw = [{"cmd": "display cpu-usage", "label": "CPU", "status": "ok",
                "summary": "3%", "output": "CPU utilization 3%"}]

    def test_txt_contains_command_and_raw_output(self):
        content, media, ext = inspection._render_task_export(
            self._Task(), [self._Result()], "txt")
        text = content.decode("utf-8")
        self.assertIn("巡检报告", text)
        self.assertIn("display cpu-usage", text)
        self.assertIn("CPU utilization 3%", text)        # 原始输出必须在
        self.assertIn("sw1", text)
        self.assertEqual(ext, "txt")
        self.assertIn("text/plain", media)

    def test_json_is_parseable_and_complete(self):
        content, media, ext = inspection._render_task_export(
            self._Task(), [self._Result()], "json")
        data = json.loads(content.decode("utf-8"))
        self.assertEqual(data["task"]["id"], self._Task.id)
        self.assertEqual(data["results"][0]["raw"][0]["cmd"], "display cpu-usage")
        self.assertEqual(data["results"][0]["metrics"]["cpu"]["value"], 3)
        self.assertEqual(ext, "json")

    def test_csv_has_bom_and_header(self):
        content, media, ext = inspection._render_task_export(
            self._Task(), [self._Result()], "csv")
        text = content.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"), "CSV 要带 BOM，否则 Excel 打开中文乱码")
        self.assertIn("设备", text.splitlines()[0])
        self.assertIn("display cpu-usage", text)
        self.assertEqual(ext, "csv")

    def test_unknown_format_falls_back_to_txt(self):
        content, media, ext = inspection._render_task_export(
            self._Task(), [self._Result()], "docx")
        self.assertEqual(ext, "txt")


class ServiceModuleBindingsTest(unittest.TestCase):
    def test_select_is_imported_in_service_module(self):
        """★ 回归（端到端探针抓到的真 bug）：

        `run_task_in_background` 的告警检查里写了 `select(models.AlertRule)`，
        但模块当初**没有导入 `select`**（作者习惯在函数里局部导入，这里漏了）。
        后果远不止"报个错"：NameError 被外层 `except` 静默吞掉 ⇒ **任务被标 failed、
        InspectionResult 一行都不落库** ⇒ 历史记录、回放、导出全都没数据，
        而且日志里什么都看不到（现在 except 里已加 traceback）。
        """
        self.assertTrue(hasattr(service, "select"),
                        "app/ct/inspection/service.py 必须导入 sqlalchemy.select（告警检查用）")


class LegacySshHintTest(unittest.TestCase):
    """老式 SSH 设备的握手失败要翻译成"能照着做"的提示（真机实测踩到的坑）。

    背景：paramiko 5.x 已移除 `ssh-rsa` 主机密钥与老式 kex ⇒ 只支持 ssh-rsa 的老设备
    （大量在用机型）握手阶段就失败，报 "no acceptable host key / kex algorithm"，
    运维极易误判成"账号密码错"。这里守三件事：能识别、给出处置步骤、**原始错误不被吞掉**。
    """

    def test_host_key_error_gets_actionable_hint(self):
        msg = service._friendly_ssh_error(
            Exception("Incompatible ssh peer (no acceptable host key)"))
        self.assertIn("老式 SSH 主机密钥", msg)
        self.assertIn("public-key local create ecdsa secp256r1", msg)   # 华三
        self.assertIn("ecc local-key-pair create", msg)                  # 华为
        self.assertIn("crypto key generate rsa", msg)                    # 思科
        self.assertIn("no acceptable host key", msg)                     # ★ 原始错误必须保留

    def test_kex_error_gets_hint(self):
        msg = service._friendly_ssh_error(
            Exception("Incompatible ssh peer (no acceptable kex algorithm)"))
        self.assertIn("老式 SSH", msg)
        self.assertIn("no acceptable kex algorithm", msg)

    def test_paramiko5_keyerror_ssh_rsa_gets_hint(self):
        """paramiko 5 遇到 ssh-rsa 时会抛 KeyError('ssh-rsa')，也要能识别。"""
        msg = service._friendly_ssh_error(KeyError("ssh-rsa"))
        self.assertIn("老式 SSH", msg)
        self.assertIn("ssh-rsa", msg)

    def test_auth_failure_is_not_mislabeled(self):
        """★ 反向护栏：认证失败**不能**被误报成"老式算法"问题，否则运维会去改错方向。"""
        msg = service._friendly_ssh_error(Exception(
            "Authentication to device failed. Common causes of this problem are: "
            "1. Invalid username and password 2. Incorrect SSH-key 3. Incorrect configuration"))
        self.assertNotIn("老式 SSH", msg)
        self.assertIn("Authentication to device failed", msg)

    def test_unrelated_error_passes_through_unchanged(self):
        self.assertEqual(service._friendly_ssh_error(TimeoutError("connect timed out")),
                         "TimeoutError: connect timed out")


if __name__ == "__main__":
    unittest.main()
