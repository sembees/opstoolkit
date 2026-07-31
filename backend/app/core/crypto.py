"""凭证加解密（Fernet 对称加密）。首次启动自动生成密钥。"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

from app.config import BASE_DIR, settings

_ENV_PATH = BASE_DIR / ".env"
_fernet: Fernet | None = None


def _ensure_key() -> bytes:
    """获取或生成凭证密钥，并持久化回 .env。"""
    key = settings.credential_key.strip()
    if key:
        try:
            return key.encode() if isinstance(key, str) else key
        except Exception:  # noqa: BLE001
            pass
    # 自动生成
    new_key = Fernet.generate_key()
    _write_env("credential_key", new_key.decode())
    return new_key


def _write_env(key: str, value: str) -> None:
    lines = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 同步到内存配置
    os.environ[key] = value
    settings.credential_key = value


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
