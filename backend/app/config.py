"""应用配置。通过环境变量或 .env 文件注入。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DB = (_APP_DIR / "data" / "ops.db").as_posix()
_ENV_FILE = _APP_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    app_name: str = "OpsToolkit 运维工具合集"
    debug: bool = False
    api_prefix: str = "/api"

    secret_key: str = ""
    access_token_expire_minutes: int = 1440
    credential_key: str = ""

    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DB}"

    inspection_timeout: int = 60
    inspection_concurrency: int = 10
    enable_pager_disable: bool = True

    admin_username: str = "admin"
    admin_password: str = "admin@123"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEXTFSM_DIR = Path(__file__).resolve().parent / "ct" / "inspection" / "textfsm_templates"