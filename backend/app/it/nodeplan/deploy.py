# -*- coding: utf-8 -*-
"""M2 下发层：SSH 布置脚本包 → 启动一次性 timer → 事后拉取结果。

三条纪律（对应评审的死穴）：
  1. **平台不等结果**：只布置 + 启动 timer 就返回；真正的应用/探测/回滚在目标机本地完成；
  2. **拉取用新 IP 优先、旧 IP 兜底**：新 IP 连得上 = 没回滚；只有旧 IP 连得上 = 已回滚；
  3. SSH 作执行器，单独抽出来（`SshRunner`），测试里可注入假实现，不碰真机。

安全：改**管理口**（SSH 所走的那块网卡）默认禁止 —— 这里没有"哪块是管理口"的信息，
所以采用更保守的规则：调用方必须显式传 `allow_mgmt_change=true` 才允许下发（默认 false），
并在返回里明确写出被拒绝的原因。
"""
import json
import posixpath
import shlex
from typing import Optional

STATE_DIR = "/var/lib/opstk/netconfig"
UNIT_DIR = "/etc/systemd/system"


class SshError(RuntimeError):
    pass


class SshRunner:
    """极薄的 paramiko 封装；只做"写文件 / 跑命令 / 读文件"。"""

    def __init__(self, host: str, user: str, *, key_path: str = "", password: str = "",
                 port: int = 22, timeout: int = 15, use_sudo: bool = True):
        self.host, self.user, self.port = host, user, port
        self.key_path, self.password, self.timeout = key_path, password, timeout
        self.use_sudo = use_sudo

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"username": self.user, "port": self.port, "timeout": self.timeout,
              "banner_timeout": self.timeout, "auth_timeout": self.timeout}
        if self.key_path:
            kw["key_filename"] = self.key_path
        if self.password:
            kw["password"] = self.password
        c.connect(self.host, **kw)
        return c

    def run(self, cmd: str, sudo: bool = False) -> tuple[int, str, str]:
        if sudo and self.use_sudo:
            cmd = "sudo -n bash -lc " + shlex.quote(cmd)
        c = self._connect()
        try:
            _i, o, e = c.exec_command(cmd, timeout=60)
            out = o.read().decode("utf-8", "replace")
            err = e.read().decode("utf-8", "replace")
            return o.channel.recv_exit_status(), out, err
        finally:
            c.close()

    def write_file(self, path: str, content: str, mode: int = 0o755, sudo: bool = False) -> None:
        c = self._connect()
        try:
            tmp = "/tmp/.opstk-%d-%s" % (abs(hash(content)) % 10**8,
                                          posixpath.basename(path).replace("/", "_"))
            sftp = c.open_sftp()
            with sftp.open(tmp, "w") as f:
                f.write(content)
            sftp.close()
            if sudo and self.use_sudo:
                d = posixpath.dirname(path)
                code, out, err = self.run(
                    "mkdir -p %s && install -m %o %s %s"
                    % (shlex.quote(d), mode, shlex.quote(tmp), shlex.quote(path)), sudo=True)
                self.run("rm -f " + shlex.quote(tmp))
                if code != 0:
                    raise SshError("写 %s 失败（sudo）：%s" % (path, (err or out)[:200]))
            else:
                code, out, err = self.run("install -m %o %s %s"
                                          % (mode, shlex.quote(tmp), shlex.quote(path)))
                if code != 0:
                    raise SshError("写 %s 失败：%s" % (path, (err or out)[:200]))
        finally:
            c.close()

    def read_file(self, path: str, sudo: bool = False) -> Optional[str]:
        if sudo and self.use_sudo:
            code, out, _e = self.run("cat " + shlex.quote(path), sudo=True)
            return out if code == 0 else None

        c = self._connect()
        try:
            sftp = c.open_sftp()
            try:
                with sftp.open(path, "r") as f:
                    return f.read().decode("utf-8", "replace")
            except IOError:
                return None
            finally:
                sftp.close()
        finally:
            c.close()

    def exists(self, path: str) -> bool:
        code, _o, _e = self.run("test -e " + shlex.quote(path))
        return code == 0


def deploy_bundle(runner: SshRunner, files: dict[str, str], change_id: str) -> dict:
    """把脚本包写到目标机并启动一次性 timer（立刻返回，不等结果）。"""
    state = "%s/%s" % (STATE_DIR, change_id)
    code, _o, err = runner.run("mkdir -p " + shlex.quote(state) + " && chmod 700 " + shlex.quote(state),
                               sudo=True)
    if code != 0:
        raise SshError("建目录失败: " + err[:200])

    for name, content in files.items():
        if name.startswith("opstk-netconfig-apply@"):
            continue                      # unit/timer 放 systemd 目录
        runner.write_file(posixpath.join(state, name), content,
                          mode=0o755 if name.endswith(".sh") else 0o644, sudo=True)

    unit = "%s/opstk-netconfig-apply@%s.service" % (UNIT_DIR, change_id)
    timer = "%s/opstk-netconfig-apply@%s.timer" % (UNIT_DIR, change_id)
    runner.write_file(unit, files["opstk-netconfig-apply@%s.service" % change_id], mode=0o644, sudo=True)
    runner.write_file(timer, files["opstk-netconfig-apply@%s.timer" % change_id], mode=0o644, sudo=True)

    code, out, err = runner.run(
        "systemctl daemon-reload && systemctl start opstk-netconfig-apply@%s.timer && "
        "systemctl is-active opstk-netconfig-apply@%s.timer" % (change_id, change_id), sudo=True)
    if code != 0:
        raise SshError("启动 timer 失败: " + (err or out)[:300])
    return {"state_dir": state, "timer_active": "active" in out}


def fetch_result(runner: SshRunner, change_id: str) -> dict:
    """读目标机本地结果（result.json + probe.txt）。没有结果 = 还没跑完或机器不可达。"""
    state = "%s/%s" % (STATE_DIR, change_id)
    raw = runner.read_file(posixpath.join(state, "result.json"), sudo=True)
    if raw is None:
        return {"ready": False, "reason": "result.json 还不存在（timer 未到/未跑/机器不可达）"}
    try:
        res = json.loads(raw)
    except ValueError:
        return {"ready": False, "reason": "result.json 不是合法 JSON: " + raw[:120]}
    probe = runner.read_file(posixpath.join(state, "probe.txt"), sudo=True) or ""
    log = runner.read_file(posixpath.join(state, "apply.log"), sudo=True) or ""
    return {"ready": True, "result": res, "probe_text": probe, "apply_log": log[-4000:]}
