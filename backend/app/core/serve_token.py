"""`/pxe/serve` 的**种子 token**（外部审查第 J 条的处置）。

### 为什么要它

`/pxe/serve`（`app/main.py` 里挂的静态服务）是**无认证**的：装机网段上任何机器都能 GET。
它提供的 `ks.cfg` / `user-data` 里有 root 与管理员口令的**哈希**
（`rootpw --iscrypted $6$…`、`user --iscrypted --password=$6$…`）—— 拿到哈希可以离线爆破。
当前的主要缓解是**网络隔离**（装机网段无上联），token 是第二层：
装机机自己拿到的 URL 里带 token，别人拿不到 URL 就取不走应答文件。

### 为什么是"URL 里带 token"，不是 HTTP Basic / 摘要认证

因为要同时伺候三种客户端，而它们对认证头的支持并不一致：
  · **iPXE**：链式加载自己的脚本（`boot.ipxe` → `boot/<mac>.ipxe` → `ks.cfg`）；
  · **anaconda**：`inst.ks=<url>`；
  · **cloud-init**：`ds=nocloud;s=<url>` —— NoCloud 数据源**不支持**认证头。
"路径段/查询串里带一个不可猜的串"是三者都天然支持的写法，也不需要动任何客户端配置。

### 两种写法都接受

  · 路径段：`/pxe/serve/<token>/ks.cfg` —— 生成器默认用这种（`http_root` 里直接带一段，
    于是**所有**由 `http_root` 拼出来的 URL —— 内核/initrd/应答文件/菜单 —— 自动全都带上）；
  · 查询串：`/pxe/serve/ks.cfg?t=<token>` —— 手工核对、用浏览器打开时方便。

### 启用与开关语义

`.env` 里的 `pxe_serve_token`（与 `credential_key` / `secret_key` 同一份文件、同一套
"加锁读-改-写"机制，见 `core/crypto.py`）：
  · **为空 = 不启用**：与改造前**逐字相同**的行为。生产升级是一个**有意的开关**，
    而不是"重启就变"——否则一次升级就会让正在装机的机器全部取不到文件；
  · 启用（或换掉）之后**必须重新部署**一次：dnsmasq 的 `dhcp-boot=` 与生成的
    `boot.ipxe` / `ks.cfg` 里都写着 URL，不重新部署就还是旧的无 token URL。

### 它**不能**做什么（别把它当万能）

  · TFTP 那侧（`/srv/tftp`，iPXE 的第一跳 `undionly.kpxe`）本来就没有认证可言；
  · 拿到 token 的机器可以把 token 打进自己的日志/串口 —— 这是"第二层"，不是"机密";
  · ZTP 的 `/ztp`（**明文**设备口令）与 `/pxe/iso` 不在这道门禁里（那是另一条开项）。
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from urllib.parse import parse_qs

from app.config import settings
from app.core import crypto as _crypto

ENV_KEY = "pxe_serve_token"
# 太短的 token 等于没有（可爆破）。生成时用 32 字符；运维手填时低于这个长度直接拒绝，
# 不给"看起来启用了、其实一猜就中"的假安全感。
MIN_LEN = 16
_log = logging.getLogger(__name__)


def get_token() -> str:
    """当前生效的 token；"" = 不启用（与改造前逐字相同的行为）。"""
    return (getattr(settings, ENV_KEY, "") or "").strip()


def set_token(value: str) -> str:
    """设置并持久化（空值 = 关闭这道门禁）。低于 MIN_LEN 直接拒绝。"""
    v = (value or "").strip()
    if v and len(v) < MIN_LEN:
        raise ValueError("pxe_serve_token 至少 %d 个字符（太短等于没有：可被暴力枚举）" % MIN_LEN)
    _crypto._write_env(ENV_KEY, v)
    return v


def ensure_token() -> str:
    """已有就返回；没有就生成一个并写进 .env（可选调用，运维显式要求时用）。"""
    tok = get_token()
    if tok:
        return tok
    with _crypto._env_lock():
        _reload()                       # 等锁期间可能别的进程已经写好了
        tok = get_token()
        if tok:
            return tok
        tok = secrets.token_urlsafe(24)  # ~32 字符
        _crypto._write_env_locked(ENV_KEY, tok)
        _log.warning("已生成 pxe_serve_token 并写入 .env —— 请**重新部署**一次 PXE 模板"
                     "（旧的无 token URL 会取不到文件）")
        return tok


def _reload() -> None:
    """把 .env 里的 pxe_serve_token 读回内存（等锁期间别的进程可能刚写过）。"""
    path = _crypto._ENV_PATH
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == ENV_KEY:
                setattr(settings, ENV_KEY, v.strip())
                os.environ[ENV_KEY] = v.strip()
    except OSError:
        return


def token_ok(provided: str) -> bool:
    """恒定时间比较（避免用响应时间把 token 一位位试出来）。"""
    want = get_token()
    if not want:
        return True                     # 不启用 ⇒ 一律放行（与改造前一致）
    return hmac.compare_digest((provided or "").strip(), want)


def take_from_path(path: str) -> tuple[str, str]:
    """把 URL 路径**开头那一段** token 摘掉，返回 (摘掉后的路径, 提供的 token)。

    只认第一段：`/pxe/serve/<token>/ks.cfg`（挂载点已被 Starlette 剥掉，
    这里看到的是 `/<token>/ks.cfg`）。第一段不是 token 时原样返回（可能是查询串写法）。
    """
    want = get_token()
    if not want:
        return path, ""
    seg, _, rest = (path or "/").lstrip("/").partition("/")
    if seg and hmac.compare_digest(seg, want):
        return "/" + rest, seg
    return path, ""


def from_query(query_string: str | bytes) -> str:
    """从查询串里取 `t=`（iPXE/anaconda 用路径段，浏览器/手工核对用这个）。"""
    if isinstance(query_string, bytes):
        query_string = query_string.decode("latin-1")
    try:
        vals = parse_qs(query_string or "").get("t") or []
    except Exception:  # noqa: BLE001
        return ""
    return vals[0] if vals else ""


def serve_base(server_ip: str) -> str:
    """`http://<ip>:8000/pxe/serve`（启用时把 token 作为**路径段**接在后面）。

    ★ 生成器里所有由 `http_root` 拼出来的 URL（内核/initrd/应答文件/菜单）都从它派生，
      所以在这一处接上 token，等于**整个 iPXE → anaconda/cloud-init 链路**都自动带上。
    """
    base = "http://" + (server_ip or "192.168.1.100") + ":8000/pxe/serve"
    tok = get_token()
    return base + "/" + tok if tok else base


def strip_for_local_path(rel: str) -> str:
    """把"URL 里相对 /pxe/serve/ 的那一段"里的 token 段去掉。

    `api/pxe.py::_local_served_path()` 靠它把 URL 映射回真实目录；不剥掉的话会去
    `<web_root>/<token>/...` 找文件（必然找不到 ⇒ 生成出来的仓库 URL 全错）。
    """
    want = get_token()
    if not want or not rel:
        return rel
    seg, _, rest = rel.lstrip("/").partition("/")
    if seg and hmac.compare_digest(seg, want):
        return rest
    return rel


def enforcement_state() -> dict:
    """给健康检查/运维看的现状（不返回 token 本身）。"""
    tok = get_token()
    return {"enabled": bool(tok), "len": len(tok),
            "hint": "" if tok else
            "未启用：/pxe/serve 仍然是无认证的（ks.cfg/user-data 里的口令哈希任何人可取）。"
            "要启用请设 .env 的 pxe_serve_token（或调用 serve_token.ensure_token()），"
            "然后**重新部署**一次 PXE 模板。"}
