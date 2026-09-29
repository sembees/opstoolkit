"""凭证加解密（Fernet 对称加密）。首次启动自动生成密钥。

外部审查 U4-F6 的三条（本模块）：
  1. `credential_key` 只判"非空"、不判合法 ⇒ 一个被截断的 key 会在**用的时候**炸出
     Fernet 的英文异常，而调用方（pxe/ztp 的 `_safe_decrypt`）把它吞成空串，
     最终报成"管理员密码不能为空"——运维按提示去填口令，填完还是同样的问题。
     现在提供 `valid_fernet_key()` / `credential_key_health()`，启动期就能给出可操作的长提示。
  2. 生成密钥**没有锁**：两个进程（或一个脚本与容器同时起来）会各自生成一把，
     `_write_env` 又是"读-改-写"整份 .env ⇒ 后写的覆盖先写的：

       · 两把 credential_key 里，先写的那把加密出来的密文永久打不开；
       · 而且 `secret_key` 与 `credential_key` 的读-改-写会互相**丢更新**（一方把另一方的行挤掉）。

     现在：`.env.lock` 上 fcntl.flock（跨进程）+ RLock（同进程），并且 `_write_env` 的
     读-改-写在锁内完成；生成密钥时**双检**（等锁期间别人写好了就用它的）。
  3. 解密失败与"本来就没填"被混为一谈 —— 见 api/pxe.py、api/ztp.py 的 `_safe_decrypt`。
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading

from cryptography.fernet import Fernet

from app.config import BASE_DIR, settings

try:                     # Windows 上没有 fcntl（本机开发会走这条分支）
    import fcntl
except ImportError:      # pragma: no cover
    fcntl = None

_ENV_PATH = BASE_DIR / ".env"
_LOCK_PATH = BASE_DIR / ".env.lock"
_fernet: Fernet | None = None
_lock = threading.RLock()          # 同进程互斥（RLock：允许同一线程重入，避免自锁）
_log = logging.getLogger(__name__)


def valid_fernet_key(value: str | bytes | None) -> bool:
    """这个值能不能当 Fernet 密钥用（32 字节 url-safe base64）。"""
    raw = value
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        Fernet((raw or "").strip().encode())
        return True
    except Exception:  # noqa: BLE001
        return False


def credential_key_health() -> str:
    """启动期体检：凭证密钥缺失/非法时返回**可操作**的说明（"" = 正常）。"""
    raw = (settings.credential_key or "").strip()
    if not raw:
        return ""
    if not valid_fernet_key(raw):
        return (
            "credential_key 不是合法的 Fernet 密钥（应为 32 字节 url-safe base64）："
            "所有已保存的凭据 / 模板口令都**无法解密**，巡检与装机都会失败。"
            "请恢复原来那把密钥（.env 里的 credential_key），"
            "**不要重新生成** —— 换一把会让已有密文永久打不开。"
        )
    return ""


@contextlib.contextmanager
def _env_lock():
    """跨进程（fcntl.flock）+ 同进程（RLock）互斥。

    只保护"写 .env"这一小段，不把它变成热路径锁（密钥只读一次，之后走内存缓存）。
    """
    with _lock:
        fh = None
        try:
            fh = open(_LOCK_PATH, "a+")
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fh is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                finally:
                    fh.close()


def _reload_env_keys() -> None:
    """把 .env 里我们关心的两个键读回内存（等锁期间可能已被别的进程写好）。"""
    if not _ENV_PATH.exists():
        return
    try:
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k in ("credential_key", "secret_key") and v:
                setattr(settings, k, v)
                os.environ.setdefault(k, v)
    except OSError:
        return


def _write_env_locked(key: str, value: str) -> None:
    """读-改-写整份 .env。**必须在 `_env_lock()` 内调用**（否则会丢更新）。"""
    lines = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 同步到内存配置
    os.environ[key] = value
    setattr(settings, key, value)


def _write_env(key: str, value: str) -> None:
    with _env_lock():
        _write_env_locked(key, value)


def _ensure_key() -> bytes:
    """获取或生成凭证密钥，并持久化回 .env（生成过程有锁 + 双检）。"""
    key = (settings.credential_key or "").strip()
    if key:
        if not valid_fernet_key(key):
            # 非法密钥不能当"没配"处理：重新生成会让已有密文永久打不开。
            raise RuntimeError(credential_key_health())
        return key.encode()
    with _env_lock():
        _reload_env_keys()                     # 等锁期间可能已经有别的进程写好了
        key = (settings.credential_key or "").strip()
        if key:
            if not valid_fernet_key(key):
                raise RuntimeError(credential_key_health())
            return key.encode()
        new_key = Fernet.generate_key()
        _write_env_locked("credential_key", new_key.decode())
        _log.warning("已生成新的 credential_key 并写入 %s（请备份它：换掉后旧密文永久打不开）",
                     _ENV_PATH)
        return new_key


def ensure_secret_key() -> str:
    """确保 JWT secret 已配置；为空时生成随机值并持久化到 .env。"""
    key = (settings.secret_key or "").strip()
    if key:
        return key
    import secrets

    with _env_lock():
        _reload_env_keys()
        key = (settings.secret_key or "").strip()
        if key:
            return key
        new_key = secrets.token_urlsafe(48)
        _write_env_locked("secret_key", new_key)
        settings.secret_key = new_key
        return new_key


def _fernet_obj() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_ensure_key())
    return _fernet


def encrypt(plaintext: str | None) -> str:
    if not plaintext:
        return ""
    return _fernet_obj().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str | None) -> str:
    if not ciphertext:
        return ""
    return _fernet_obj().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
