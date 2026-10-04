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
    # ★ 外部审查第 J 条：`/pxe/serve` 是**无认证**的 HTTP 根，它提供的 ks.cfg / user-data 里
    #   含 root 与管理员口令的**哈希**（`rootpw --iscrypted $6$…`）。主要缓解是网络隔离，
    #   这一项是第二层：URL 里带一个不可猜的串，装机机自己取得到、别人猜不到。
    #   **空 = 不启用**（与改造前逐字相同）—— 开关是运维**有意**做的，不是"重启就变"。
    #   启用/更换之后必须**重新部署**一次（dnsmasq 的 dhcp-boot 与生成的 boot.ipxe/ks.cfg
    #   里都带 URL）。细节见 app/core/serve_token.py。
    pxe_serve_token: str = ""
    # 同一套机制的第二个落点：ZTP 的静态根 `/ztp`（`/srv/opstk/ztp-web`）。
    # 它比 /pxe/serve 更敏感 —— 设备配置里是**明文**口令（H3C `password simple …` /
    # VRP8 `irreversible-cipher …`）。**同样：空 = 不启用**。
    # 注意：H3C 的 auto-config 走 DHCP option 67 + **TFTP**，不经过 HTTP ⇒ 这条开关
    # 对 H3C 那条链路既无保护也无影响；它保护的是 HTTP 取配置/下载的路径。
    ztp_serve_token: str = ""

    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DB}"

    inspection_timeout: int = 60
    inspection_concurrency: int = 10
    enable_pager_disable: bool = True
    # ★ 连接阶段单独一个短超时（与"命令读取超时"分开）：
    #   实测过不可达设备（如 192.168.1.2 被丢给默认网关）时，TCP SYN 会被静默丢弃，
    #   而**这一阶段不受 netmiko 的 conn_timeout 管**，由操作系统 SYN 重试决定（Linux 默认 ~127s）
    #   ⇒ 界面上就是"点了开始巡检一直卡着"。配合下面的预检，把等待压到秒级。
    inspection_connect_timeout: int = 10
    # 连接前的 TCP 预检超时（秒）：不通就立刻报"设备不可达"，不进入 SSH 流程。
    inspection_preflight_timeout: float = 3.0

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"

    admin_username: str = "admin"
    # 不再硬编码默认口令。本仓库是公开的，写死的默认口令等于把后台入口贴在墙上，
    # 而且线上极可能原样沿用。留空时由 database.init_db() 在首次初始化生成一次性随机口令
    # 并打印到服务日志（docker logs opstoolkit）；要固定口令就设环境变量 ADMIN_PASSWORD。
    admin_password: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEXTFSM_DIR = Path(__file__).resolve().parent / "ct" / "inspection" / "textfsm_templates"