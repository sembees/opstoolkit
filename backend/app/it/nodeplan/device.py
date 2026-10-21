# -*- coding: utf-8 -*-
"""M4b：网络设备下发（paramiko 交互式 shell）+ 回读对账。

设备侧和 Linux 侧完全不同，三条必须写死的规则：

1. **没有 systemd timer**：交换机不会自己"90 秒后自愈"，所以下发必须在一次交互会话里完成
   推配置 → 回读 → 判定；超时/断线就当作失败（且**不能保存**）。
2. **"回滚" = 不保存**：H3C/Huawei/Cisco 都是"运行时配置 + 启动配置"两份。
   先推、再回读比对，只有**对账通过才 `save` / `write memory`**；对不上就 `return`
   丢弃未保存的运行时配置，重启即回到原样。这比"再推一遍旧配置"可靠得多。
3. **必须回读**：厂商对不认识的命令经常只回一行 `% Unrecognized` 或 `Error:`，
   `send_config_set` 不抛异常；不回读就等于"以为成功"。

本模块把"会话"抽象成 `Shell`（send/expect），既可以用真 paramiko，也可以在测试里
用假的 shell 把成功/失败两条路径走一遍。
"""
import re
import time

# 各厂商进入/退出配置视图、保存、回读的命令
VENDOR_CMDS = {
    "h3c": {"enter": "system-view", "exit": "return", "save": "save force",
            "show": "display current-configuration", "prompt_re": r"[<\[][^>\]\r\n]+[>\]]\s*$"},
    "huawei": {"enter": "system-view", "exit": "return", "save": "save",
               "show": "display current-configuration", "prompt_re": r"[<\[][^>\]\r\n]+[>\]]\s*$"},
    "cisco": {"enter": "configure terminal", "exit": "end", "save": "write memory",
              "show": "show running-config", "prompt_re": r"[>#]\s*$"},
}

# 厂商的"命令不认/配置错"特征（不同型号措辞不一，宁可多收）
ERROR_RE = re.compile(
    r"(Unrecognized|Error:|% ?Invalid|% ?Incomplete|% ?Ambiguous|"
    r"Wrong parameter|Too many parameters|^%\s|^Error\b|Incomplete command)",
    re.I | re.M)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ShellError(RuntimeError):
    pass


class Shell:
    """真 paramiko 交互式 shell（懒连接，便于测试替换）。"""

    def __init__(self, host: str, user: str, *, password: str = "", key_path: str = "",
                 port: int = 22, timeout: int = 20):
        self.host, self.user, self.port = host, user, port
        self.password, self.key_path, self.timeout = password, key_path, timeout
        self._client = None
        self._chan = None

    def open(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # 老设备（H3C/华为）常只支持 ssh-rsa + group14-sha1，沿用巡检侧的兼容参数
        kw = {}
        try:
            kw = {"disabled_algorithms": {}, "allow_agent": False, "look_for_keys": False}
        except Exception:                                     # noqa: BLE001
            kw = {}
        if self.key_path:
            kw["key_filename"] = self.key_path
        if self.password:
            kw["password"] = self.password
        c.connect(self.host, port=self.port, username=self.user, timeout=self.timeout,
                  banner_timeout=self.timeout, auth_timeout=self.timeout, **kw)
        self._client = c
        self._chan = c.invoke_shell(width=200, height=1000)
        self._chan.settimeout(30)
        time.sleep(0.6)
        self._drain()
        return self

    def _drain(self, wait: float = 0.4) -> str:
        buf = b""
        end = time.time() + wait
        while time.time() < end:
            if self._chan.recv_ready():
                buf += self._chan.recv(65535)
                end = time.time() + 0.25
            else:
                time.sleep(0.05)
        return ANSI_RE.sub("", buf.decode("utf-8", "replace"))

    def send(self, line: str) -> str:
        if self._chan is None:
            raise ShellError("shell 没打开")
        self._chan.send(line + "\n")
        return self._drain()

    def close(self):
        try:
            if self._chan:
                self._chan.close()
            if self._client:
                self._client.close()
        except Exception:                                     # noqa: BLE001
            pass


def push_config(shell, vendor: str, config_text: str, *, persist: bool = True,
                enter: bool = True) -> dict:
    """推配置 → 检测报错 → 回读 → 对账 → 只有对账通过才保存。

    返回 {ok, saved, errors, transcript, running_config}
    """
    v = VENDOR_CMDS.get((vendor or "").lower())
    if not v:
        raise ValueError("不支持的厂商：%s（只支持 %s）" % (vendor, "/".join(VENDOR_CMDS)))

    transcript, errors = [], []
    if enter:
        transcript.append(shell.send(v["enter"]))

    for raw in (config_text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue                      # 注释/分隔线不推给设备（有的型号会当成错命令）
        # 视图切换命令由代码自己控制（进入/退出各一次），不要从配置文本里再推一遍：
        # 配置里若中途出现 return/end，会把后续命令推到系统视图外，产生莫名其妙的报错。
        # 注意 quit 必须保留 —— 设备靠它退出 interface/vlan 子视图。
        if stripped.lower() in ("system-view", "configure terminal", "return", "end"):
            continue
        out = shell.send(line)
        transcript.append(out)
        if ERROR_RE.search(out):
            errors.append({"line": line, "output": out.strip()[:300]})

    if errors:
        # 对不上就**不保存**：`return`/`end` 丢弃未保存的运行时配置，重启即恢复原样
        transcript.append(shell.send(v["exit"]))
        return {"ok": False, "saved": False, "errors": errors,
                "transcript": "\n".join(transcript), "running_config": ""}

    transcript.append(shell.send(v["show"]))
    running = "\n".join(transcript[-2:])
    return {"ok": True, "saved": False, "errors": [],
            "transcript": "\n".join(transcript), "running_config": running}


def save_config(shell, vendor: str) -> dict:
    """确认无误后再保存启动配置（这一步失败要明确报出来，不能假装成功）。"""
    v = VENDOR_CMDS[(vendor or "").lower()]
    out = shell.send(v["save"])
    # 有的型号会追问 y/n
    if re.search(r"\[Y/N\]|\(y/n\)|continue\?", out, re.I):
        out += shell.send("y")
    bad = bool(re.search(r"(Unrecognized|Error:|failed|denied)", out, re.I))
    return {"saved": not bad, "output": out.strip()[:600]}


def verify_device(expected: dict, running_config: str) -> dict:
    """关键段对账：规划里的每一条"必须在运行配置里出现"都逐项检查。

    expected 形如 {"lines": ["vlan 10", "ip address 10.0.0.21 255.255.255.0"]}
    —— 不做模糊匹配（大小写/空格归一化后**整段包含**），缺一条就算不一致。
    """
    norm = _norm(running_config)
    items = []
    ok = True
    for want in (expected or {}).get("lines") or []:
        hit = _norm(want) in norm
        items.append({"item": "config_line", "expected": want, "ok": hit})
        ok = ok and hit
    return {"ok": ok, "items": items,
            "counts": {"total": len(items), "failed": len([i for i in items if not i["ok"]])}}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def expected_lines_from_config(config_text: str, limit: int = 40) -> dict:
    """从渲染出的配置里抽出"必须出现"的关键行（用于回读对账）。

    只取有实质内容的命令，丢掉视图切换/注释/空行 —— 那些在回读里未必原样出现。
    """
    skip = {"system-view", "configure terminal", "return", "end", "quit", "exit",
            "save", "save force", "write memory", "y"}
    out = []
    for raw in (config_text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        if s.lower() in skip:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return {"lines": out}
