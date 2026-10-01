"""无认证静态根的**种子 token**（外部审查第 J 条的处置；`/pxe/serve` 与 `/ztp` 各一个开关）。

### 为什么要它

`/pxe/serve` 与 `/ztp` 都是**无认证**的静态根（`app/main.py` 直接 `StaticFiles` 挂上去的）：
装机网段 / 开局网段上任何机器都能 GET。而它们给出的东西里有**机密**：

  · `/pxe/serve` 的 `ks.cfg` / `user-data`：root 与管理员口令的**哈希**（`rootpw --iscrypted $6$…`）；
  · `/ztp` 的设备配置：`password simple <明文>`（H3C）/ `irreversible-cipher <明文>`（VRP8）——
    **比哈希更敏感**。

主要缓解是网络隔离（装机/开局网段无上联），token 是第二层：
设备自己拿到的 URL 里带 token，别人拿不到 URL 就取不走这些文件。

### 为什么是"URL 里带 token"，不是 HTTP Basic / 摘要认证

要同时伺候这些客户端，而它们对认证头的支持并不一致：
  · **iPXE**（链式加载自己的菜单）、**anaconda**（`inst.ks=`）、
  · **cloud-init**（`ds=nocloud;s=` —— NoCloud 数据源**不支持**认证头）、
  · **网络设备**的 ZTP/auto-config（按 URL 取配置，对认证头的支持看厂商）。
"路径段/查询串里带一个不可猜的串"是它们都天然支持的写法，也不需要动任何客户端配置。

### 两种写法都接受

  · 路径段：`/pxe/serve/<token>/ks.cfg`、`/ztp/<token>/ztp/SW1.cfg` —— **生成器默认用这种**
    （`serve_base()` / `with_token()` 把 token 接在根上，于是所有由该根拼出来的 URL 自动带上）；
  · 查询串：`/ztp/ztp/SW1.cfg?t=<token>` —— 手工核对、用浏览器打开时方便。

### 开关语义（两个 scope 各自独立）

`.env` 里的 `pxe_serve_token` / `ztp_serve_token`（与 `credential_key` / `secret_key` 同一份文件、
同一套"加锁读-改-写"机制，见 `core/crypto.py`）：
  · **为空 = 不启用**：与改造前**逐字相同**的行为。升级是一个**有意的开关**，而不是"重启就变"
    —— 否则一次升级就会让正在装机/正在开局的设备全部取不到文件；
  · 启用（或换掉）之后**必须重新部署**一次：dnsmasq 的 `dhcp-boot=` 与生成的菜单/应答文件/
    中间文件里都写着 URL，不重新部署就还是旧的无 token URL。

### 它**不能**做什么（别把它当万能）

  · **TFTP** 那侧（`/srv/tftp`）本来就没有认证可言 —— 而 PXE 的第一跳（`undionly.kpxe`）与
    **H3C 的 ZTP**（按 DHCP option 67 的 `ztp/<stem>.cfg` 取配置）走的正是 TFTP；
    也就是说 **H3C 那条设备链路根本不经过 HTTP**，token 对它既无保护也无影响（真机复验见 §5.78）。
  · 拿到 token 的设备可以把 token 打进自己的日志/串口 —— 这是"第二层"，不是"机密"；
  · `/pxe/iso` 不在这道门禁里（它已经用 `_IsoOnlyStatic` 限制成只发 `.iso`，是二进制不是机密）。
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from urllib.parse import parse_qs

from app.config import settings
from app.core import crypto as _crypto

# 太短的 token 等于没有（可爆破）。生成时用 32 字符；运维手填时低于这个长度直接拒绝，
# 不给"看起来启用了、其实一猜就中"的假安全感。
MIN_LEN = 16
_log = logging.getLogger(__name__)


class StaticScope:
    """一个无认证静态根 = 一个 scope（各自一个开关、各自一个 token、各自一个挂载点）。"""

    def __init__(self, env_key: str, mount: str, label: str):
        self.env_key = env_key
        self.mount = mount            # "/pxe/serve" / "/ztp"
        self.label = label

    # ── 读 / 写 ──────────────────────────────────────────────

    def get_token(self) -> str:
        """当前生效的 token；"" = 不启用（与改造前逐字相同的行为）。"""
        return (getattr(settings, self.env_key, "") or "").strip()

    def set_token(self, value: str) -> str:
        """设置并持久化（空值 = 关闭这道门禁）。低于 MIN_LEN 直接拒绝。"""
        v = (value or "").strip()
        if v and len(v) < MIN_LEN:
            raise ValueError("%s 至少 %d 个字符（太短等于没有：可被暴力枚举）"
                             % (self.env_key, MIN_LEN))
        _crypto._write_env(self.env_key, v)
        return v

    def ensure_token(self) -> str:
        """已有就返回；没有就生成一个并写进 .env（可选调用，运维显式要求时用）。"""
        tok = self.get_token()
        if tok:
            return tok
        with _crypto._env_lock():
            self._reload()                  # 等锁期间可能别的进程已经写好了
            tok = self.get_token()
            if tok:
                return tok
            tok = secrets.token_urlsafe(24)  # ~32 字符
            _crypto._write_env_locked(self.env_key, tok)
            _log.warning("已生成 %s 并写入 .env —— 请**重新部署**一次（旧的无 token URL 会取不到文件）",
                         self.env_key)
            return tok

    def _reload(self) -> None:
        """把 .env 里的本 scope 键读回内存（等锁期间别的进程可能刚写过）。"""
        path = _crypto._ENV_PATH
        if not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == self.env_key:
                    setattr(settings, self.env_key, v.strip())
                    os.environ[self.env_key] = v.strip()
        except OSError:
            return

    # ── 校验 ────────────────────────────────────────────────

    def token_ok(self, provided: str) -> bool:
        """恒定时间比较（避免用响应时间把 token 一位位试出来）。"""
        want = self.get_token()
        if not want:
            return True                     # 不启用 ⇒ 一律放行（与改造前一致）
        return hmac.compare_digest((provided or "").strip(), want)

    def take_from_path(self, path: str) -> tuple[str, str]:
        """把 URL 路径**开头那一段** token 摘掉，返回 (摘掉后的路径, 提供的 token)。

        只认第一段：`/<token>/ks.cfg`（挂载点由 Starlette 放在 root_path 里，不在 path 里）。
        第一段不是 token 时原样返回（可能是查询串写法）。
        """
        want = self.get_token()
        if not want:
            return path, ""
        seg, _, rest = (path or "/").lstrip("/").partition("/")
        if seg and hmac.compare_digest(seg, want):
            return "/" + rest, seg
        return path, ""

    @staticmethod
    def from_query(query_string: str | bytes) -> str:
        """从查询串里取 `t=`（设备/脚本用路径段，浏览器与手工核对用这个）。"""
        if isinstance(query_string, bytes):
            query_string = query_string.decode("latin-1")
        try:
            vals = parse_qs(query_string or "").get("t") or []
        except Exception:  # noqa: BLE001
            return ""
        return vals[0] if vals else ""

    # ── 生成 URL ────────────────────────────────────────────

    def serve_base(self, server_ip: str) -> str:
        """`http://<ip>:8000<mount>`（启用时把 token 作为**路径段**接在后面）。

        ★ 生成器里所有由这个根拼出来的 URL（内核/initrd/应答文件/菜单/仓库）都从它派生，
          所以在这一处接上 token，等于**整条链路**都自动带上。
        """
        base = "http://" + (server_ip or "192.168.1.100") + ":8000" + self.mount
        tok = self.get_token()
        return base + "/" + tok if tok else base

    def with_token(self, url: str) -> str:
        """在**已有** URL 的挂载点后面接 token（幂等）；不是本 scope 的根就原样返回。

        ZTP 的 `http_root` 是模板里存好的完整 URL（不是从 IP 拼的），所以要这条路径。
        """
        u = (url or "").rstrip("/")
        tok = self.get_token()
        if not tok or not u:
            return url or ""
        if u.endswith(self.mount):
            return u + "/" + tok
        if u.endswith(self.mount + "/" + tok):
            return u                       # 已经带过 ⇒ 幂等（重复调用不会越接越长）
        return url                         # 不是本 scope 的根，别把别人的 URL 改坏

    def strip_for_local_path(self, rel: str) -> str:
        """把"URL 里相对挂载点的那一段"里的 token 段去掉。

        `api/pxe.py::_local_served_path()` 靠它把 URL 映射回真实目录；不剥掉的话会去
        `<web_root>/<token>/...` 找文件（必然找不到 ⇒ 生成出来的仓库 URL 全错）。
        """
        want = self.get_token()
        if not want or not rel:
            return rel
        seg, _, rest = rel.lstrip("/").partition("/")
        if seg and hmac.compare_digest(seg, want):
            return rest
        return rel

    def enforcement_state(self) -> dict:
        """给健康检查/运维看的现状（**不返回 token 本身**）。"""
        tok = self.get_token()
        return {"enabled": bool(tok), "len": len(tok), "mount": self.mount, "label": self.label,
                "hint": "" if tok else
                "%s：未启用 —— %s 仍然是无认证的（%s）。要启用请设 .env 的 %s"
                "（或调用 serve_token.%s.ensure_token()），然后**重新部署**一次。"
                % (self.label, self.mount, "口令哈希/明文口令任何人可取", self.env_key,
                   "PXE" if self.mount.startswith("/pxe") else "ZTP")}


#: PXE 应答文件（`/pxe/serve`）
PXE = StaticScope("pxe_serve_token", "/pxe/serve", "PXE 应答文件")
#: ZTP 设备配置（`/ztp`）
ZTP = StaticScope("ztp_serve_token", "/ztp", "ZTP 设备配置")
SCOPES = (PXE, ZTP)

# ── 兼容层：模块级函数 = PXE scope 的同名方法 ───────────────────
# （`/pxe/serve` 是这套机制的第一个落点，已有调用点与用例都按模块级函数写的；
#   保留它们，避免为了"两个 scope"去动一大片本来正确的代码。）
get_token = PXE.get_token
set_token = PXE.set_token
ensure_token = PXE.ensure_token
token_ok = PXE.token_ok
take_from_path = PXE.take_from_path
from_query = PXE.from_query
serve_base = PXE.serve_base
strip_for_local_path = PXE.strip_for_local_path
enforcement_state = PXE.enforcement_state


def states() -> dict:
    """两个 scope 的现状（给状态接口用）。"""
    return {"pxe": PXE.enforcement_state(), "ztp": ZTP.enforcement_state()}
