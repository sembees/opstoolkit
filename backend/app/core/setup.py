# -*- coding: utf-8 -*-
"""首次访问引导向导（5 步）的后端逻辑：状态、体检、存储盘点。

设计约束（外部任务书）：
  · 全部**只读** —— 体检只看、不修；修的动作都交给用户照 `fix` 里的命令自己做；
  · 向导完成（app_meta.setup_completed_at 存在）后，checks / complete 一律 403，
    不留"可以一直改设置的后门"；/setup/status 永远可用（只回状态，不泄配置）；
  · needs_setup 的口径是数据面判据（2026-10-08 误判修正）：完成标记不存在
    **且** 库看起来全新才算"未安装"，详见 db_looks_fresh 上方的注释块。
  · 路径如实展示：容器内路径是固定的（/srv/opstk/iso、/srv/tftp、/etc/dnsmasq.d、
    /var/lib/dnsmasq），换盘由 compose 变量解决 —— 向导**不改路径**，只如实报告。

端口/进程探测的边界（如实说明）：容器与宿主机共享**网络**命名空间但不共享 PID，
/proc/net/{udp,tcp} 能看到监听端点，却看不到**是谁**在监听。所以"谁占的端口"只能
间接判断：本机 dnsmasq 在跑（复用 dhcp.dhcp_status 的判定）⇒ 端口大概率是它的；
否则按"被未知程序占用"报 error，fix 里给 systemd-resolved 的标准处置（doctor.sh 同款）。
"""
from __future__ import annotations

import os
import shutil

from sqlalchemy import func, select

from app.core import dhcp as _dhcp
from app.core import models
from app.core.models import AppMeta
from app.core.timeutil import utcnow

# ── app_meta 的键（向导状态的唯一落点） ──────────────────────────────────────
KEY_SETUP_COMPLETED_AT = "setup_completed_at"
KEY_ADMIN_PASSWORD_CHANGED_AT = "admin_password_changed_at"

# 向导涉及的容器内固定路径（与 deploy/docker-compose.yml 的挂载逐字一致）。
# name 是给用户看的短名；writable_required=False 的目录按设计就是只读挂载
# （/var/lib/dnsmasq 由**宿主机** dnsmasq 写租约，容器只读；mnt 只做挂载点）。
WIZARD_DIRS = (
    # (key, 名称, 路径, 必须可写?)
    ("iso", "ISO 镜像目录", "/srv/opstk/iso", True),
    ("tftp", "TFTP 根目录", "/srv/tftp", True),
    ("pxe_web", "PXE HTTP 根目录", "/srv/opstk/pxe-web", True),
    ("ztp_web", "ZTP HTTP 根目录", "/srv/opstk/ztp-web", True),
    ("state", "dnsmasq 重载状态目录", "/srv/opstk/state", True),
    ("dnsmasq_conf", "dnsmasq 配置目录", "/etc/dnsmasq.d", True),
    ("dnsmasq_leases", "dnsmasq 租约目录", "/var/lib/dnsmasq", False),
    ("mnt", "ISO 挂载点", "/srv/opstk/mnt", False),
)

# 宿主机重载单元在容器内的只读挂载位置（compose: ./deploy/host:/app/deploy/host:ro）。
# 应用态 compose（deploy/docker-compose.yml）**没有**这个挂载 ⇒ 生产容器里常见"看不到"，
# 这不代表宿主机没装 —— 只能如实说明"无法判断，请在宿主机执行安装脚本"。
HOST_UNIT_DIR = "/app/deploy/host"
HOST_UNIT_FILES = (
    "opstk-dnsmasq-reload.path",
    "opstk-dnsmasq-reload.service",
    "opstk-dnsmasq-reload.sh",
)

# 磁盘余量阈值（对齐 packaging/doctor.sh 的口径：≥20 ok / ≥5 warn / <5 error）。
DISK_OK_BYTES = 20 * 1024**3
DISK_WARN_BYTES = 5 * 1024**3


# ── app_meta 读写 ────────────────────────────────────────────────────────────

async def get_meta(db, key: str) -> str | None:
    """读一个 meta 键；不存在返回 None（"没做过这件事"与"值为空"要分开）。"""
    row = await db.get(AppMeta, key)
    return row.value if row is not None else None


async def set_meta(db, key: str, value: str | None = None) -> None:
    """upsert 一个 meta 键；value 缺省 = 当前 UTC 时刻（完成类标记的惯例）。"""
    row = await db.get(AppMeta, key)
    if row is None:
        row = AppMeta(key=key, value=value if value is not None else _stamp())
        db.add(row)
    else:
        row.value = value if value is not None else _stamp()
    await db.commit()


def _stamp() -> str:
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


async def password_changed(db) -> bool:
    """管理员是否已经改过初始口令（方案 (a)：改密接口成功时写入标记）。

    为什么选 (a) 不选 (b)：(b) 需要拿到"初始口令原文"来比对，而初始口令是
    安装时生成的一次性随机串，只打印过一次 —— 容器重启后**任何人都拿不回来**，
    拿 .env 里可能存在的 ADMIN_PASSWORD 当"初始值"也不可靠（运维中途改过 .env
    或从没设过）。改密动作发生时写标记（app/api/auth.py 埋点）既准确又零猜测。
    """
    return await get_meta(db, KEY_ADMIN_PASSWORD_CHANGED_AT) is not None


async def record_admin_password_changed(db, username: str) -> bool:
    """改密成功后调用：只有**初始管理员账号**改密才记标记。

    为什么限定 username：向导关心的是"安装脚本建的那个 admin 的初始口令换掉没有"，
    其它管理员/操作员改自己的口令与此无关，不能把向导第一步"冒充"成已完成。
    返回是否真的写了标记（供接口层/用例核对）。
    """
    from app.config import settings

    if (username or "") != settings.admin_username:
        return False
    await set_meta(db, KEY_ADMIN_PASSWORD_CHANGED_AT)
    return True


# ── "库是不是全新的"（needs_setup 的数据面判据；2026-10-08 误判修正） ────────
#
# 事故：旧口径 needs_setup = (setup_completed_at 不存在) OR (admin_password_changed_at
# 不存在)，只看 app_meta 标记。人工配置好的实例（如生产 10.128.118.113）管理员口令是
# 人工设的、从没走过改密接口 ⇒ 两个标记都不存在 ⇒ 运行了数周、库里有资产/模板/巡检
# 记录的实例被误判成"未安装"：登录后被分流进向导、每页顶部常驻横幅。
#
# 新口径：needs_setup = (setup_completed_at 不存在) AND (库看起来全新)。
# "已在用"的证据只能来自业务数据本身 —— 数据不会撒谎：库里只要有一条业务数据，就说明
# 有人真实用过，与 app_meta 有没有标记无关；而 setup_completed_at 只会由向导
# （POST /setup/complete）写入，不会凭空冒出来，有标记 ⇒ 一定完成过引导。
#
# "全新"判据（下面全部满足才算全新；任一不满足 ⇒ 视为已在用 ⇒ needs_setup=False）：
#   ① assets / inspection_templates / pxe_profiles / ztp_templates /
#      inspection_results / alert_rules / notifications 业务表全为空（行数为 0）；
#   ② users 表为空（连启动初始化都没跑过）或只有默认那一个管理员
#      （username == settings.admin_username 且 role == admin）。
#
# ★ inspection_templates 的特例：应用每次启动都会跑 seed_default_templates()
#   （app/ct/seeding.py），往这张表写入各厂商的**系统内置模板**（is_system=True）——
#   全新安装一启动就有若干行，这是"出厂预装"，不是"用户用过"的证据。
#   所以本表只把**用户自建**模板（is_system 非 True）算作"已在用"的证据，
#   否则真·全新安装永远满足不了"全为空"、永远进不了向导。

# 参与判定的表（表名/模型以 app/core/models.py 为准，不猜表名）。
# 元组 = (给人看的表名, 模型, 额外过滤)；新增业务表时在这里登记一行即可。
_FRESHNESS_TABLES: tuple[tuple[str, type, tuple], ...] = (
    ("assets", models.Asset, ()),
    ("inspection_templates", models.InspectionTemplate,
     (models.InspectionTemplate.is_system.isnot(True),)),  # 只数用户自建（见上；SQLite 的
     # IS NOT 对 NULL 也是"真"，万一出现 is_system 为 NULL 的行同样按用户自建计，宁严勿漏）
    ("pxe_profiles", models.PxeProfile, ()),
    ("ztp_templates", models.ZtpTemplate, ()),
    ("inspection_results", models.InspectionResult, ()),
    ("alert_rules", models.AlertRule, ()),
    ("notifications", models.Notification, ()),
)


def _admin_username() -> str:
    """配置的默认管理员用户名（延迟导入 settings，与本模块其它函数同款）。"""
    from app.config import settings

    return settings.admin_username


async def _count_rows(db, model, *where_clauses) -> int | None:
    """对一张表做 SELECT COUNT(*) —— 纯只读，无任何写入/副作用。

    查询失败（极端老库缺表、瞬时锁冲突等）返回 None：调用方把 None 当作
    "未知 ⇒ 可能在用"处理。宁可少弹一条横幅，也绝不让一个计数查询把
    /setup/status（登录后的第一跳）打挂。
    """
    stmt = select(func.count()).select_from(model)
    for clause in where_clauses:
        stmt = stmt.where(clause)
    try:
        return int((await db.execute(stmt)).scalar() or 0)
    except Exception:  # noqa: BLE001 —— 见上：计数失败按"未知"处理，不炸状态接口
        return None


async def db_usage_fingerprint(db) -> dict[str, int | None]:
    """逐表只读计数：{表名: 行数}；查询失败的表记 None。

    只做 SELECT COUNT(*)，不改任何数据、不写 app_meta；/setup/status 的响应
    里**没有**它（契约不漂移），仅供 needs_setup 判定与运维只读核查用。
    """
    out: dict[str, int | None] = {}
    for name, model, clauses in _FRESHNESS_TABLES:
        out[name] = await _count_rows(db, model, *clauses)
    out["users"] = await _count_rows(db, models.User)
    out["users_default_admin"] = await _count_rows(
        db, models.User,
        models.User.username == _admin_username(),
        models.User.role == "admin",   # "默认那一个管理员"：名字对，角色也必须是 admin
    )
    return out


async def db_looks_fresh(db) -> bool:
    """库看起来是不是"全新"（判据见上方注释块；全部只读）。

    方向性取舍：任何一张表数不出来（None）都按"已在用"处理 ——
    误判成"在用"的代价只是"少弹一条可随手关掉的横幅"；
    误判成"全新"的代价是"用了几周的系统被重新拽进向导"。后者严重得多。
    """
    try:
        fp = await db_usage_fingerprint(db)
    except Exception:  # noqa: BLE001 —— 兜底：判定本身绝不能把状态接口拖垮
        return False
    for name, _model, _clauses in _FRESHNESS_TABLES:
        if fp.get(name) != 0:
            return False
    # users 两种全新形态：
    #   · 0 个账号 —— 连启动初始化都没跑过的"绝对全新"库；
    #   · 恰好 1 个账号且就是默认管理员（username == settings.admin_username 且
    #     role == admin）—— 启动自动创建后没人动过的标准初始态。
    # 其余（多账号 / 唯一账号被改名或降级）都说明有人真实配置过 ⇒ 已在用。
    users_total = fp.get("users")
    if users_total == 0:
        return True
    return users_total == 1 and fp.get("users_default_admin") == 1


async def setup_state(db) -> dict:
    """/setup/status 的载荷（字段名与旧版逐字相同，避免前端契约漂移）。

    needs_setup 新口径（2026-10-08 误判修正，见 db_looks_fresh 上方注释）：
        (setup_completed_at 不存在) AND (库看起来全新)
    旧口径"(未完成) OR (初始口令未更换)"废弃：它把"人工设口令、没走改密接口"的
    在用实例永远钉成 needs_setup=True。steps_done 仍如实回报两步状态，供向导页展示。
    """
    completed_at = await get_meta(db, KEY_SETUP_COMPLETED_AT)
    changed = await password_changed(db)
    # 短路：有完成标记 ⇒ 一定完成过引导（标记只能由 POST /setup/complete 写入），
    # 连业务表的计数查询都不必跑。
    needs_setup = completed_at is None and await db_looks_fresh(db)
    return {
        "needs_setup": needs_setup,
        "steps_done": {
            "password_changed": changed,
            "completed": completed_at is not None,
        },
        "password_changed_at": await get_meta(db, KEY_ADMIN_PASSWORD_CHANGED_AT),
        "setup_completed_at": completed_at,
    }


# ── 存储盘点（/api/system/storage） ─────────────────────────────────────────

def _writable_dir_probe(path: str) -> tuple[bool, str]:
    """目录可写探测（建/删一个探针文件）。复用 dhcp 模块里同一实现，口径一致。"""
    return _dhcp._writable_dir_probe(path)


def _disk_usage(path: str) -> tuple[int, int]:
    """(total, free)；目录不存在或取不到时给 (0, 0)，由上层按"未知"处理。"""
    try:
        u = shutil.disk_usage(path)
        return int(u.total), int(u.free)
    except OSError:
        return 0, 0


def storage_dirs() -> list[dict]:
    """逐个目录如实报告：存在 / 可写 / 可用空间。不改任何东西。"""
    out = []
    for key, name, path, need_write in WIZARD_DIRS:
        exists = os.path.isdir(path)
        writable = False
        write_err = ""
        if exists:
            writable, write_err = _writable_dir_probe(path)
        total, free = _disk_usage(path) if exists else (0, 0)
        out.append({
            "key": key,
            "name": name,
            "path": path,
            "exists": exists,
            "writable": writable,
            "writable_required": need_write,
            "write_err": write_err if exists and not writable else "",
            "free_bytes": free,
            "total_bytes": total,
        })
    return out


def fmt_gb(n: int) -> str:
    """给用户看的字节数（小数点后 1 位 GB；0 显示为 0 GB）。"""
    return "%.1f GB" % (n / 1024**3)


# ── 端口探测（/proc，只读；容器里没有 iproute2 也能跑） ─────────────────────

def _listen_endpoints(ports, udp_path="/proc/net/udp", udp6_path="/proc/net/udp6",
                      tcp_path="/proc/net/tcp", tcp6_path="/proc/net/tcp6") -> dict[int, list[str]]:
    """从 /proc/net/{udp,tcp}{,6} 取本机监听的指定端口端点。

    口径与 it/pxe/server._listen_ports_from_proc 一致（hex 小端还原、去重），
    但泛化到任意端口并同时覆盖 TCP（DNS 的 53 是 UDP+TCP 都要）。
    读不到文件（非 Linux）→ 全部视为空闲。
    """
    want = {int(p) for p in ports}
    found: dict[int, list[str]] = {}

    def _read_lines(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    import socket
    import struct

    for path, proto in ((udp_path, "udp"), (udp6_path, "udp6"),
                        (tcp_path, "tcp"), (tcp6_path, "tcp6")):
        for line in _read_lines(path)[1:]:
            parts = line.split()
            if len(parts) < 2 or ":" not in parts[1]:
                continue
            hexip, hexport = parts[1].rsplit(":", 1)
            try:
                port = int(hexport, 16)
            except ValueError:
                continue
            if port not in want:
                continue
            try:
                if len(hexip) == 8:  # IPv4，小端
                    ip = socket.inet_ntoa(struct.pack("<I", int(hexip, 16)))
                elif len(hexip) == 32:  # IPv6，每 4 字节一组小端
                    raw = b"".join(struct.pack("<I", int(hexip[i:i + 8], 16))
                                   for i in range(0, 32, 8))
                    ip = "[" + socket.inet_ntop(socket.AF_INET6, raw) + "]"
                else:
                    continue
            except (OSError, ValueError, struct.error):
                continue
            found.setdefault(port, []).append("%s %s:%d" % (proto, ip, port))
    return found


def _dnsmasq_running() -> bool:
    """复用既有 dnsmasq 状态查询（dhcp.dhcp_status），不在体检里另立口径。"""
    try:
        return bool(_dhcp.dhcp_status().get("running"))
    except Exception:  # noqa: BLE001 —— 体检不能被状态查询的异常拖垮
        return False


def _opstk_conf_files() -> list[str]:
    """/etc/dnsmasq.d 里已有本应用的装机配置吗（在跑 dnsmasq 与否的语义参照）。"""
    return [c for c in ("opstk-pxe.conf", "opstk-ztp.conf")
            if os.path.isfile(os.path.join(_dhcp.CONF_DIR, c))]


def _host_unit_status() -> dict:
    """宿主机 dnsmasq 重载单元：容器里能看到的证据 + 诚实的限制。

    返回 {"files_present": int, "link_version": int, "link_err": str}。
    link_version > 0 = 宿主机脚本**确实跑过**（.dnsmasq-reload.link 是它写下的）；
    只看到单元文件 ≠ 已在宿主机 install（install 动作发生在宿主机，容器看不见）。
    """
    files = sum(1 for f in HOST_UNIT_FILES if os.path.isfile(os.path.join(HOST_UNIT_DIR, f)))
    link = _dhcp.host_reload_link_status()
    return {"files_present": files, "link_version": int(link.get("version") or 0),
            "link_err": link.get("err") or ""}


# ── 体检清单 ────────────────────────────────────────────────────────────────

def checks() -> list[dict]:
    """体检清单：[{id, title, level, detail, fix}]，全只读。

    level：ok 一切正常 / warn 提示（不算错，不挡完成）/ error 必须修（挡"完成"）。
    通知未配置 = 仅 warn（任务书明确：可选功能，不算错）。
    """
    items: list[dict] = []
    listen = _listen_endpoints((53, 67, 69))
    dnsmasq_up = _dnsmasq_running()

    # 1) 端口 53 / 67 / 69
    for port, label in ((53, "DNS 服务（dnsmasq 要用）"),
                        (67, "DHCP 服务（PXE/ZTP 都靠它）"),
                        (69, "TFTP 服务（引导文件下载）")):
        items.append(_port_check(port, label, listen.get(port) or [], dnsmasq_up))

    # 2) dnsmasq 是否在跑
    if dnsmasq_up:
        items.append({
            "id": "dnsmasq", "title": "dnsmasq 服务", "level": "ok",
            "detail": "dnsmasq 正在运行（DHCP/TFTP 由它提供）。",
            "fix": "",
        })
    else:
        confs = _opstk_conf_files()
        if confs:
            items.append({
                "id": "dnsmasq", "title": "dnsmasq 服务", "level": "warn",
                "detail": ("已存在装机配置（%s），但 dnsmasq 没在运行 —— "
                           "PXE/ZTP 装机目前不会工作。" % "、".join(confs)),
                "fix": "sudo systemctl enable --now dnsmasq",
            })
        else:
            # 与 doctor.sh 同一口径：还没部署过装机，dnsmasq 未运行属正常
            items.append({
                "id": "dnsmasq", "title": "dnsmasq 服务", "level": "ok",
                "detail": "dnsmasq 尚未运行；还没有部署过装机配置，属正常。"
                          "首次在页面上部署 PXE/ZTP 后会自动启动。",
                "fix": "",
            })

    # 3) 数据目录（存在 / 可写 / 只读目录只要求可读）
    dirs = storage_dirs()
    for info in dirs:
        need_write = info["writable_required"]
        if not info["exists"]:
            items.append({
                "id": "dir_" + info["key"], "title": info["name"] + "（" + info["path"] + "）",
                "level": "error",
                "detail": "目录不存在。它应当由安装脚本/compose 自动创建；不存在说明挂载或安装不完整。",
                "fix": "在宿主机执行：sudo mkdir -p %s && sudo chown -R root:root %s"
                       % (info["path"], info["path"]),
            })
        elif need_write and not info["writable"]:
            why = info.get("write_err") or "权限不足"
            items.append({
                "id": "dir_" + info["key"], "title": info["name"] + "（" + info["path"] + "）",
                "level": "error",
                "detail": "目录存在但不可写（%s）——镜像上传、配置写入都会失败。" % why,
                "fix": "在宿主机执行：sudo chown -R root:root %s && sudo chmod -R u+rwX %s"
                       % (info["path"], info["path"]),
            })
        elif not need_write and not info["writable"]:
            # 按设计只读的目录（如 /var/lib/dnsmasq 的 :ro 挂载）：可写不了不是错
            items.append({
                "id": "dir_" + info["key"], "title": info["name"] + "（" + info["path"] + "）",
                "level": "ok",
                "detail": "目录存在；容器内按只读方式使用（写入由宿主机负责），不可写属正常。",
                "fix": "",
            })
        else:
            items.append({
                "id": "dir_" + info["key"], "title": info["name"] + "（" + info["path"] + "）",
                "level": "ok",
                "detail": "目录存在%s。" % ("且可写" if info["writable"] else ""),
                "fix": "",
            })

    # 4) 磁盘余量（ISO/数据根所在分区，口径与 doctor.sh 一致）
    iso = next(i for i in dirs if i["key"] == "iso")
    if not iso["exists"]:
        items.append({
            "id": "disk_space", "title": "磁盘可用空间", "level": "warn",
            "detail": "ISO 目录不存在，暂时取不到空间数据（修好上面的目录项后再体检一次）。",
            "fix": "",
        })
    elif iso["free_bytes"] < DISK_WARN_BYTES:
        items.append({
            "id": "disk_space", "title": "磁盘可用空间", "level": "error",
            "detail": "数据根分区只剩 %.1f GB（不足 5 GB）—— 很快会写满，一个系统 ISO 就要 5~10 GB。"
                      % (iso["free_bytes"] / 1024**3),
            "fix": "在宿主机清理磁盘（docker system prune / 删旧文件），或换大盘后在 .env 调整 "
                   "OPSTK_DATA_ROOT / OPSTK_TFTP_ROOT 再重启容器。",
        })
    else:
        level = "ok" if iso["free_bytes"] >= DISK_OK_BYTES else "warn"
        tail = ("，够放系统镜像。" if level == "ok"
                else "，不足 20 GB —— 放一个 DVD 镜像（约 5~10 GB）前先看看够不够。")
        items.append({
            "id": "disk_space", "title": "磁盘可用空间", "level": level,
            "detail": "数据根分区（/srv/opstk 所在盘）可用 %.1f GB%s"
                      % (iso["free_bytes"] / 1024**3, tail),
            "fix": "",
        })

    # 5) 宿主机 dnsmasq 重载单元（只报证据 + 给安装命令，无法判断是否已安装则如实说明）
    st = _host_unit_status()
    if st["link_version"] > 0:
        items.append({
            "id": "host_reload_unit", "title": "宿主机 dnsmasq 重载单元", "level": "ok",
            "detail": "检测到宿主机脚本留下的运行标记（版本 %d）：重载单元已安装且工作过。"
                      % st["link_version"],
            "fix": "",
        })
    else:
        seen = ("容器内能看到 %d/%d 个单元文件（deploy/host 的只读挂载）；"
                % (st["files_present"], len(HOST_UNIT_FILES))
                if st["files_present"] else
                "容器内看不到单元文件（应用态部署不挂载 deploy/host，属正常现象）；")
        items.append({
            "id": "host_reload_unit", "title": "宿主机 dnsmasq 重载单元", "level": "warn",
            "detail": ("未检测到『已安装』的证据（%s）。**容器无法判断宿主机是否已执行过安装**——"
                       "这个动作只发生在宿主机上。没装的话：PXE/ZTP 部署时配置写进去了，"
                       "但 dnsmasq 不会自动重载，部署会报『宿主机 dnsmasq 未加载新配置』。"
                       % (st["link_err"] or "无运行标记")),
            "fix": "在宿主机执行一次（装过再执行也无害，会原地更新）："
                   "sudo bash deploy/host/install-opstk-dnsmasq-reload.sh",
        })

    # 6) 通知配置（仅提示，不算错）
    items.append(_notify_check())

    return items


def _port_check(port: int, label: str, endpoints: list[str], dnsmasq_up: bool) -> dict:
    cid = {53: "port_dns", 67: "port_dhcp", 69: "port_tftp"}[port]
    shown = "、".join(endpoints) if endpoints else ""
    if not endpoints:
        return {
            "id": cid, "title": "端口 %d（%s）" % (port, label.split("（")[0]),
            "level": "ok",
            "detail": "端口 %d 空闲，%s 部署后可正常绑定。" % (port, label),
            "fix": "",
        }
    if dnsmasq_up:
        return {
            "id": cid, "title": "端口 %d（%s）" % (port, label.split("（")[0]),
            "level": "ok",
            "detail": "端口 %d 已被占用（%s）—— 本机 dnsmasq 正在运行，这个端口就是它的，属正常。"
                      % (port, shown),
            "fix": "",
        }
    extra = ""
    if port == 53:
        extra = "最常见的占用者是 systemd-resolved（Ubuntu 桌面/服务器版默认开启）。"
    return {
        "id": cid, "title": "端口 %d（%s）" % (port, label.split("（")[0]),
        "level": "error",
        "detail": "端口 %d 被其它程序占用（%s）。%s不腾出来，dnsmasq 起不来，PXE/ZTP 都用不了。"
                  % (port, shown, extra),
        "fix": ("sudo systemctl disable --now systemd-resolved"
                if port == 53 else
                "先看是谁占用（在宿主机执行）：ss -lunp | grep :%d；确认不要的服务后停掉/迁移它"
                % port),
    }


def _notify_check() -> dict:
    """通知只提示不算错：飞书通知是可选能力，没配不影响装机/巡检主流程。"""
    from app.config import settings
    from app.core.notify import feishu_configured

    if feishu_configured():
        return {
            "id": "notify", "title": "飞书通知", "level": "ok",
            "detail": "通知已启用且凭据齐全（巡检告警会发到飞书群）。",
            "fix": "",
        }
    missing = [n for n, v in (("NOTIFY_ENABLED", settings.notify_enabled),
                              ("FEISHU_APP_ID", settings.feishu_app_id),
                              ("FEISHU_APP_SECRET", settings.feishu_app_secret),
                              ("FEISHU_RECEIVE_ID", settings.feishu_receive_id)) if not v]
    return {
        "id": "notify", "title": "飞书通知", "level": "warn",
        "detail": "尚未配置飞书通知（缺 %s）。这是**可选**功能：不配只是收不到巡检告警推送，"
                  "不影响装机/巡检本身，以后随时能配。" % ("、".join(missing) or "配置"),
        "fix": "编辑 backend/.env：设 NOTIFY_ENABLED=true 并填 FEISHU_APP_ID / "
               "FEISHU_APP_SECRET / FEISHU_RECEIVE_ID，然后 docker compose restart。"
               "详见「使用帮助 → 部署指南」。",
    }


# ── 完成守卫 ────────────────────────────────────────────────────────────────

async def completion_blockers(db) -> list[dict]:
    """POST /setup/complete 的前置条件；返回中文原因列表（空 = 可以完成）。
    每项 {id, title}，API 层拼成 detail 文案。"""
    reasons: list[dict] = []
    if not await password_changed(db):
        reasons.append({"id": "password", "title": "尚未修改初始管理员口令"})
    try:
        bad = [c for c in checks() if c.get("level") == "error"]
    except Exception:  # noqa: BLE001 —— 体检本身挂了不能让人永远完不成，但要把话说清楚
        bad = []
        reasons.append({"id": "checks_error", "title": "体检未能完成，请重试或查看服务日志"})
    for c in bad:
        reasons.append({"id": c.get("id", ""), "title": c.get("title", "")})
    return reasons


async def mark_completed(db) -> str:
    """写入完成标记，返回时间戳。调用方（API 层）已把守卫查完。"""
    await set_meta(db, KEY_SETUP_COMPLETED_AT)
    return (await get_meta(db, KEY_SETUP_COMPLETED_AT)) or _stamp()
