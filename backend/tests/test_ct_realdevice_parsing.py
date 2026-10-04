# -*- coding: utf-8 -*-
"""CT 巡检解析 / 候选命令回退 —— 测试数据一律用**真机回显基线**，不凭印象造字符串。

基线来源（真机采集，原样复制进 backend/tests/captures/，容器内 pytest 可直接读取）：
  · h3c_vfw_capture.json     H3C SecPath vFW1000 防火墙（CMW 7.1.064 ESS 1190P02）
  · h3c_vsr_capture.json     H3C VSR1000 路由器（CMW 7.1.064 R0633P13）
  · h3c_switch_capture.json  H3C S10508X 交换机（CMW 7.1.070，型号 V6850-56HF-DTN）
每个 JSON 形如 {"cpu": {"cmd": "display cpu-usage", "output": "..."}, ...}，
键依次为 version/cpu/memory/device/environment/power/fan/interface/alarm/logbuffer。

修的三个问题（2026-10-05）：
  1) MetricCommand.alt_commands 候选命令回退（主命令被设备拒绝时自动试候选）；
  2) H3C cpu/memory/environment 按真机基线格式解析（此前 cpu=unknown、
     内存 0%/2%/5%——正则逮住 FreeRatio 小数尾巴、环境温度表解析错位）；
  3) 全部候选命令都被拒 ⇒ status="unsupported"（"该机型不支持此指标"），
     不再与"解析失败 unknown"混为一谈。
"""
import json
import pathlib
import unittest
from unittest import mock

from app.ct.drivers.base import MetricCommand
from app.ct.drivers.h3c import H3CDriver
from app.ct.drivers.huawei import HuaweiDriver
from app.ct.inspection import service
from app.ct.inspection.parser import is_command_rejected, parse_output

CAPTURES_DIR = pathlib.Path(__file__).resolve().parent / "captures"


def _capture(name):
    """读取真机基线；文件缺失则跳过（基线在仓库里，正常情况必然存在）。"""
    path = CAPTURES_DIR / (name + ".json")
    if not path.exists():
        raise unittest.SkipTest("真机基线缺失：%s" % path)
    return json.loads(path.read_text(encoding="utf-8"))


def _parse(key, cap, device_type="hp_comware"):
    """按服务链路的实际参数把基线回显喂给 parse_output。"""
    item = cap[key]
    return parse_output(key, item["output"], textfsm_name="h3c_%s.textfsm" % key,
                        device_type=device_type, command=item["cmd"])


# ─────────────────────────── 问题 2：真机格式解析 ───────────────────────────

class H3CRealCpuParsingTest(unittest.TestCase):
    """真机基线：vFW 24% / vSR 10% / 交换机 0%（取 "last 5 seconds"）。"""

    def test_vfw_cpu_is_24(self):
        pr = _parse("cpu", _capture("h3c_vfw_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertEqual(pr["parsed"]["cpu"], 24)
        self.assertIn("24", pr["summary"])

    def test_vsr_cpu_is_10(self):
        pr = _parse("cpu", _capture("h3c_vsr_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertEqual(pr["parsed"]["cpu"], 10)

    def test_switch_cpu_is_0_and_slot_named(self):
        pr = _parse("cpu", _capture("h3c_switch_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertEqual(pr["parsed"]["cpu"], 0)
        # 多槽位时取最大值并在摘要里写清是哪几个槽：交换机只有 Slot 1
        self.assertEqual(pr["parsed"]["slots"], [{"slot": "Slot 1 CPU 0", "cpu": 0}])
        self.assertIn("Slot 1 CPU 0", pr["summary"])

    def test_multi_slot_takes_max_and_lists_slots(self):
        """多槽位取最大值：用真机 vSR 输出拼两段（Slot 2 比 Slot 1 高）验证。"""
        vsr = _capture("h3c_vsr_capture")["cpu"]["output"]
        text = ("Slot 1 CPU 0 CPU usage:\n       3% in last 5 seconds\n"
                + vsr.replace("Unit", "Slot 2 CPU 0", 1))
        pr = parse_output("cpu", text, textfsm_name="", device_type="hp_comware")
        self.assertEqual(pr["parsed"]["cpu"], 10)
        self.assertEqual(len(pr["parsed"]["slots"]), 2)
        self.assertIn("Slot 2 CPU 0", pr["summary"])


class H3CRealMemoryParsingTest(unittest.TestCase):
    """真机基线：使用率 = 100 − FreeRatio ⇒ 53.0 / 30.8 / 18.5（±0.5）。

    回归背景：旧正则 (\\d+)% 只逮到 FreeRatio 的小数尾巴，真机上报 0%/2%/5%。
    """

    def test_vfw_memory_usage(self):
        pr = _parse("memory", _capture("h3c_vfw_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertAlmostEqual(pr["parsed"]["memory"], 53.0, delta=0.5)
        self.assertNotEqual(pr["parsed"]["memory"], 0, "不得再出现小数尾巴错值")

    def test_vsr_memory_usage(self):
        pr = _parse("memory", _capture("h3c_vsr_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertAlmostEqual(pr["parsed"]["memory"], 30.8, delta=0.5)
        self.assertNotEqual(pr["parsed"]["memory"], 2, "不得再出现小数尾巴错值")

    def test_switch_memory_usage(self):
        pr = _parse("memory", _capture("h3c_switch_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertAlmostEqual(pr["parsed"]["memory"], 18.5, delta=0.5)
        self.assertNotEqual(pr["parsed"]["memory"], 5, "不得再出现小数尾巴错值")


class H3CRealEnvironmentParsingTest(unittest.TestCase):
    """真机基线：交换机是温度表（6 个传感器，30~44C）；vFW/vSR 无此命令。"""

    def test_switch_environment_temperature_and_status(self):
        pr = _parse("environment", _capture("h3c_switch_capture"))
        self.assertEqual(pr["status"], "ok")
        self.assertEqual(pr["parsed"]["temperature"], 30)
        self.assertEqual(pr["parsed"]["max_temperature"], 44)
        sensors = pr["parsed"]["sensors"]
        self.assertEqual(len(sensors), 6)
        self.assertEqual(sensors[0]["sensor"], "Inflow  1")
        self.assertEqual(sensors[0]["lower_limit"], -5)
        self.assertEqual(sensors[0]["warning_limit"], 66)
        self.assertEqual(sensors[0]["alarm_limit"], 76)
        self.assertIn("30", pr["summary"])

    def test_switch_hotspot_over_warning_limit_becomes_warning(self):
        """任一传感器越过 WarningLimit ⇒ warning（用真机回显改一个温度验证判定）。"""
        sw = _capture("h3c_switch_capture")["environment"]["output"]
        text = sw.replace(" 0/1  Hotspot 3   44", " 0/1  Hotspot 3   70")
        pr = parse_output("environment", text, textfsm_name="", device_type="hp_comware")
        self.assertEqual(pr["status"], "warning")

    def test_vfw_environment_is_unsupported(self):
        pr = _parse("environment", _capture("h3c_vfw_capture"))
        self.assertEqual(pr["status"], "unsupported")
        self.assertIn("不支持", pr["summary"])
        self.assertIsNone(pr["parsed"])

    def test_vsr_environment_is_unsupported(self):
        pr = _parse("environment", _capture("h3c_vsr_capture"))
        self.assertEqual(pr["status"], "unsupported")
        self.assertIn("不支持", pr["summary"])

    def test_switch_alarm_urgent_too_many_params_is_unsupported_at_parse_level(self):
        pr = _parse("alarm", _capture("h3c_switch_capture"))
        self.assertEqual(pr["status"], "unsupported")
        self.assertIn("display alarm urgent", pr["summary"])  # 摘要里保留被拒命令名


# ─────────────────── 问题 1 的基础：拒绝识别判据 ───────────────────

class CommandRejectionDetectionTest(unittest.TestCase):
    """"设备不认这条命令"的判定：短输出（≤6 行）+ 特征串，且跳过 syslog 行。"""

    def test_each_signature_in_short_output_is_rejected(self):
        signatures = [
            "% Unrecognized command found at '^' position.",      # Comware
            "% Too many parameters found at '^' position.",       # 交换机 alarm 真机实测
            "% Wrong parameter found at '^' position.",
            "% Ambiguous command",
            "% Incomplete command",
            "% Invalid input detected at '^' marker.",            # Cisco IOS
            "Error: Unrecognized command found at '^' position.",  # 华为 VRP
            "% Unrecognized command",                              # 部分机型简写
        ]
        for sig in signatures:
            self.assertTrue(is_command_rejected("\n     ^\n" + sig), sig)

    def test_real_vfw_rejection_output_is_rejected(self):
        out = _capture("h3c_vfw_capture")["environment"]["output"]
        self.assertTrue(is_command_rejected(out))

    def test_real_switch_alarm_rejection_output_is_rejected(self):
        out = _capture("h3c_switch_capture")["alarm"]["output"]
        self.assertTrue(is_command_rejected(out))

    def test_long_output_containing_signature_is_not_rejected(self):
        """★ 防误判：正常长回显里出现报错字样（如日志回放）不算被拒。"""
        lines = ["line %d of normal output" % i for i in range(7)]
        lines.insert(3, "% Unrecognized command found at '^' position.")
        self.assertFalse(is_command_rejected("\n".join(lines)), "长输出一律不算命令被拒")

    def test_real_logbuffer_capture_is_not_rejected(self):
        """真机 logbuffer 全量回显（含 SHELL_CMD_MATCHFAIL 日志）绝不能被当成被拒。"""
        out = _capture("h3c_switch_capture")["logbuffer"]["output"]
        self.assertFalse(is_command_rejected(out))

    def test_syslog_line_quoting_the_error_is_not_rejected(self):
        """★ 防误判：日志行复述报错字样（时间戳形态）不算当前命令被拒。"""
        line = ("%Oct  4 13:16:33:843 2026 SW SHELL/4/SHELL_CMD_MATCHFAIL: "
                "% Unrecognized command found at '^' position.")
        self.assertFalse(is_command_rejected(line))

    def test_normal_short_metric_output_is_not_rejected(self):
        for out in [
            "Unit CPU usage:\n      24% in last 5 seconds",
            "Slot 1 CPU 0 CPU usage:\n       0% in last 5 seconds",
            "Device Info on Slot 1:\nDevice ID.  Status\n 1          Normal",
        ]:
            self.assertFalse(is_command_rejected(out), out)

    def test_case_insensitive_and_empty(self):
        self.assertTrue(is_command_rejected("  % unrecognized COMMAND found at '^' position."))
        self.assertFalse(is_command_rejected(""))
        self.assertFalse(is_command_rejected(None))


# ─────────────────── 问题 1：候选命令回退（service 层） ───────────────────

class _FakeNetmikoConn:
    """按命令返回预置回显的假连接（记录实际下发顺序作为证据）。"""

    def __init__(self, responses):
        self.responses = responses
        self.sent = []

    def send_command(self, cmd, read_timeout=60, **kwargs):
        self.sent.append(cmd)
        return self.responses[cmd]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CandidateCommandFallbackTest(unittest.TestCase):
    """_connect_and_run：主命令被拒 → 自动试候选；全部被拒 → rejected 标记。"""

    def _run(self, responses, metric_cmds):
        conn = _FakeNetmikoConn(responses)
        events = []
        with mock.patch("netmiko.ConnectHandler", return_value=conn):
            outputs = service._connect_and_run(
                {"device_type": "hp_comware"}, "", metric_cmds, [], False, events.append)
        return outputs, events, conn

    def test_falls_back_to_alt_when_primary_rejected(self):
        """证据：主命令回显为真机基线里的拒绝报错 → 自动切到候选命令。"""
        rejected_out = _capture("h3c_vfw_capture")["alarm"]["output"]  # % Unrecognized command…
        mc = MetricCommand("alarm", "告警信息", "display alarm urgent",
                           alt_commands=("display alarm",))
        outputs, events, conn = self._run(
            {"display alarm urgent": rejected_out, "display alarm": "Alarm information: none"},
            [mc])
        # 实际下发顺序：先主命令，被拒后补发候选
        self.assertEqual(conn.sent, ["display alarm urgent", "display alarm"])
        self.assertEqual(outputs[0]["cmd"], "display alarm", "回显属于实际执行的候选命令")
        self.assertTrue(outputs[0]["is_alt"])
        self.assertFalse(outputs[0]["rejected"])
        # on_line 事件：cmd 事件带"实际执行的命令"与"是否候选"；output 事件同
        cmd_events = [e for e in events if e["type"] == "cmd"]
        self.assertEqual([e["cmd"] for e in cmd_events], ["display alarm urgent", "display alarm"])
        self.assertEqual([e["is_alt"] for e in cmd_events], [False, True])
        out_event = [e for e in events if e["type"] == "output"][-1]
        self.assertEqual(out_event["cmd"], "display alarm")
        self.assertTrue(out_event["is_alt"])
        # 解析用候选命令的回显，不会误标 unsupported
        pr = service._finalize_metric_parse(outputs[0])
        self.assertNotEqual(pr["status"], "unsupported")

    def test_all_candidates_rejected_marks_unsupported_with_tried_list(self):
        """vFW/vSR 真机：display environment 不存在 → unsupported + 人话摘要。"""
        rejected_out = _capture("h3c_vfw_capture")["environment"]["output"]
        mc = MetricCommand("environment", "温度", "display environment")
        outputs, _, conn = self._run({"display environment": rejected_out}, [mc])
        self.assertEqual(conn.sent, ["display environment"])
        self.assertTrue(outputs[0]["rejected"])
        pr = service._finalize_metric_parse(outputs[0])
        self.assertEqual(pr["status"], "unsupported")
        self.assertEqual(pr["summary"], "该机型不支持此指标（已尝试：display environment）")
        self.assertIsNone(pr["parsed"])

    def test_tried_list_lists_every_attempted_command(self):
        vfw_env = _capture("h3c_vfw_capture")["environment"]["output"]
        mc = MetricCommand("environment", "温度", "display environment",
                           alt_commands=("display environment all",))
        outputs, _, conn = self._run(
            {"display environment": vfw_env, "display environment all": vfw_env}, [mc])
        self.assertEqual(conn.sent, ["display environment", "display environment all"])
        pr = service._finalize_metric_parse(outputs[0])
        self.assertEqual(pr["summary"],
                         "该机型不支持此指标（已尝试：display environment、display environment all）")

    def test_primary_ok_does_not_try_alt(self):
        """主命令正常 → 不多发候选命令（不给设备添乱）。"""
        switch_cpu = _capture("h3c_switch_capture")["cpu"]["output"]
        mc = MetricCommand("cpu", "CPU 使用率", "display cpu-usage",
                           textfsm="h3c_cpu.textfsm", unit="%", alt_commands=("display cpu",))
        outputs, events, conn = self._run({"display cpu-usage": switch_cpu}, [mc])
        self.assertEqual(conn.sent, ["display cpu-usage"])
        self.assertFalse(outputs[0]["is_alt"])
        self.assertFalse(outputs[0]["rejected"])
        self.assertEqual([e["cmd"] for e in events if e["type"] == "cmd"], ["display cpu-usage"])
        self.assertEqual(outputs[0]["tried"], ["display cpu-usage"])

    def test_second_alt_is_tried_when_first_alt_also_rejected(self):
        primary_rej = _capture("h3c_vfw_capture")["alarm"]["output"]
        mc = MetricCommand("alarm", "告警信息", "display alarm urgent",
                           alt_commands=("display alarm", "display alarm verbose"))
        outputs, _, conn = self._run(
            {"display alarm urgent": primary_rej, "display alarm": primary_rej,
             "display alarm verbose": "Alarm information: none"}, [mc])
        self.assertEqual(conn.sent,
                         ["display alarm urgent", "display alarm", "display alarm verbose"])
        self.assertEqual(outputs[0]["cmd"], "display alarm verbose")
        self.assertFalse(outputs[0]["rejected"])

    def test_normal_output_containing_rejection_words_is_not_treated_as_rejected(self):
        """★ 防误判：≤6 行但含报错字样的"正常"回显——只要不是报错形态（syslog 时间戳行）就不切候选。"""
        mc = MetricCommand("logbuffer", "日志缓冲", "display logbuffer reverse")
        out = ("%Oct  4 13:16:33:843 2026 SW SHELL/4/SHELL_CMD_MATCHFAIL: "
               "% Unrecognized command found at '^' position.")
        outputs, _, conn = self._run({"display logbuffer reverse": out}, [mc])
        self.assertEqual(conn.sent, ["display logbuffer reverse"], "不得误切候选命令")
        self.assertFalse(outputs[0]["rejected"])


# ─────────────────── 问题 3：unsupported 状态与模板候选 ───────────────────

class UnsupportedStatusTest(unittest.TestCase):
    def test_unsupported_summary_is_human_readable(self):
        item = {"rejected": True, "tried": ["display alarm urgent", "display alarm"],
                "cmd": "display alarm", "key": "alarm", "output": "", "textfsm": ""}
        pr = service._finalize_metric_parse(item)
        self.assertEqual(pr["status"], "unsupported")
        self.assertEqual(pr["summary"], "该机型不支持此指标（已尝试：display alarm urgent、display alarm）")

    def test_non_rejected_items_keep_original_semantics(self):
        item = {"rejected": False, "key": "cpu", "output": "CPU utilization: 45% in 5 seconds",
                "textfsm": "", "cmd": "display cpu-usage"}
        pr = service._finalize_metric_parse(item)
        self.assertEqual(pr["status"], "ok")
        self.assertEqual(pr["parsed"]["cpu"], 45)


class DriverAltCommandsTest(unittest.TestCase):
    def test_h3c_alarm_has_candidate(self):
        metrics = {m.key: m for m in H3CDriver().standard_metrics()}
        self.assertEqual(metrics["alarm"].command, "display alarm urgent")
        self.assertIn("display alarm", metrics["alarm"].alt_commands)

    def test_huawei_candidates(self):
        metrics = {m.key: m for m in HuaweiDriver().standard_metrics()}
        self.assertIn("display cpu", metrics["cpu"].alt_commands)
        self.assertIn("display memory", metrics["memory"].alt_commands)
        self.assertEqual(metrics["alarm"].command, "display alarm urgent")
        self.assertIn("display alarm", metrics["alarm"].alt_commands)

    def test_metric_command_default_is_empty(self):
        self.assertEqual(MetricCommand("x", "X", "display x").alt_commands, ())

    def test_duplicate_alt_is_deduplicated_at_runtime(self):
        """候选里若误配了主命令本身，被拒后不应把同一条命令重复下发两遍。"""
        from app.ct.inspection.service import _run_metric_command

        rej = "% Unrecognized command found at '^' position."
        conn = _FakeNetmikoConn({"display foo": rej, "display bar": "ok"})
        mc = MetricCommand("x", "X", "display foo", alt_commands=("display foo", "display bar"))
        out, tried, rejected, used = _run_metric_command(conn, mc, lambda e: None, 5)
        self.assertEqual(conn.sent, ["display foo", "display bar"], "去重后不得重复下发主命令")
        self.assertEqual(used, "display bar")
        self.assertFalse(rejected)


class InheritAltCommandsTest(unittest.TestCase):
    """存量 DB 模板（无 alt_commands 字段）要从内置驱动回填候选命令。"""

    def test_db_item_without_alt_inherits_from_driver(self):
        cmds = [MetricCommand("alarm", "告警信息", "display alarm urgent")]
        out = service._inherit_alt_commands(cmds, "h3c")
        self.assertIn("display alarm", out[0].alt_commands)

    def test_explicit_alt_not_overridden(self):
        cmds = [MetricCommand("cpu", "CPU", "display cpu-usage", alt_commands=("display cpu manual",))]
        out = service._inherit_alt_commands(cmds, "h3c")
        self.assertEqual(out[0].alt_commands, ("display cpu manual",))

    def test_no_matching_driver_metric_keeps_empty(self):
        cmds = [MetricCommand("cpu", "CPU", "display custom-cpu")]
        out = service._inherit_alt_commands(cmds, "h3c")
        self.assertEqual(out[0].alt_commands, ())

    def test_unknown_vendor_is_safe(self):
        cmds = [MetricCommand("cpu", "CPU", "display cpu-usage")]
        self.assertEqual(service._inherit_alt_commands(cmds, "no-such-vendor"), cmds)


class H3CTextFSMTemplateTest(unittest.TestCase):
    """重写后的自定义模板对真机基线的独立护栏（不经 parse_output 的兜底链）。

    注：模板对象按名字缓存，textfsm 1.1.3 的 ParseText 会往对象内追加结果，
    直接复用前必须 Reset()（parse_output 已统一处理，这里手动调用）。
    """

    def _rows(self, name, output):
        from app.ct.inspection.parser import load_textfsm
        tmpl = load_textfsm(name)
        self.assertIsNotNone(tmpl)
        tmpl.Reset()
        return tmpl.ParseText(output)

    def test_cpu_template(self):
        self.assertEqual(self._rows("h3c_cpu.textfsm", _capture("h3c_vfw_capture")["cpu"]["output"]),
                         [["Unit CPU usage:", "24", "24", "24"]])
        self.assertEqual(self._rows("h3c_cpu.textfsm", _capture("h3c_switch_capture")["cpu"]["output"]),
                         [["Slot 1 CPU 0 CPU usage:", "0", "0", "0"]])

    def test_memory_template(self):
        self.assertEqual(self._rows("h3c_memory.textfsm", _capture("h3c_vfw_capture")["memory"]["output"]),
                         [["0", "8062308", "4318124", "47.0"]])

    def test_environment_template(self):
        rows = self._rows("h3c_environment.textfsm",
                          _capture("h3c_switch_capture")["environment"]["output"])
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0], ["0/1", "Inflow  1", "30", "-5", "66", "76"])
        self.assertEqual(rows[-1], ["0/1", "Hotspot 3", "44", "-5", "66", "76"])


if __name__ == "__main__":
    unittest.main()
