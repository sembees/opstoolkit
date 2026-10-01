"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.crypto import ensure_secret_key

    ensure_secret_key()
    await init_db()
    from app.ct.inspection.service import recover_interrupted_tasks

    await recover_interrupted_tasks()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, debug=settings.debug)

# 允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import api_router  # noqa: E402

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


# PXE/ZTP HTTP 文件服务 (本机部署后生效，必须在根路径前注册)
# 如果本机已部署 PXE/ZTP ，挂载静态文件服务
class _TokenStatic(StaticFiles):
    """给**无认证静态根**加一道 token 门禁（外部审查第 J 条；`/pxe/serve` 与 `/ztp` 共用一个实现）。

    为什么需要：这两个根都是无认证的，而它们给出的东西里有**机密**
    （`/pxe/serve` 是口令哈希，`/ztp` 是**明文**设备口令）。主要缓解是网络隔离，token 是第二层。

    为什么包在 StaticFiles 外面而不是自己写 FileResponse：Starlette 的 StaticFiles 已经处理好了
    路径归一化、目录穿越（`..`）、符号链接与 range 请求 —— 自己重写一遍就是重造一个更容易出错的轮子。
    这里只做"校验 → 摘掉路径里的 token 段 → 交给 StaticFiles"。

    token 两种写法都接受（见 core/serve_token.py 的说明）：
      · 路径段 `/pxe/serve/<token>/ks.cfg`（生成器默认；根 URL 里带一段，链路全自动带上）
      · 查询串 `/pxe/serve/ks.cfg?t=<token>`（浏览器/手工核对方便）
    **未配置 token ⇒ 与改造前逐字相同：不校验。**
    """

    def __init__(self, *args, scope=None, **kwargs):
        # scope 省略 = PXE（历史调用点与用例都按这个默认写）
        self.scope = scope or serve_token.PXE
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.scope.get_token():
            return await super().__call__(scope, receive, send)
        # ★ 坑：Starlette 的 `Mount` **不改写 `scope["path"]`** —— 它把挂载点写进
        #   `root_path`，子应用要自己用 `get_route_path(scope)` 求出"挂载点之后那一段"。
        #   我第一版直接拿 `scope["path"]` 当相对路径，于是 `/pxe/serve/<token>/ks.cfg`
        #   里的 token 段被当成"文件名的一部分"，永远匹配不上 ⇒ 一律 403。
        #   （StaticFiles 内部也是用 get_route_path 求相对路径的，所以要改写也得按同一口径。）
        root = scope.get("root_path", "") or ""
        route_path = get_route_path(scope)
        new_route, provided = self.scope.take_from_path(route_path)
        if not provided:
            provided = self.scope.from_query(scope.get("query_string", b""))
        if not self.scope.token_ok(provided):
            resp = Response("403：%s 需要 URL 里带 token（见 README）" % self.scope.mount,
                            status_code=403)
            return await resp(scope, receive, send)
        if new_route != route_path:
            # 交给 StaticFiles 之前把 token 段摘掉（否则它会去 <root>/<token>/... 找文件）
            full = root + new_route
            scope = dict(scope, path=full, raw_path=full.encode("utf-8"))
        return await super().__call__(scope, receive, send)


try:
    from pathlib import Path

    from starlette.routing import get_route_path

    from app.core import serve_token

    _pxe_web = Path("/srv/opstk/pxe-web")
    if _pxe_web.is_dir():
        app.mount("/pxe/serve", _TokenStatic(directory=str(_pxe_web), scope=serve_token.PXE),
                  name="pxe-serve")
except Exception:  # noqa: BLE001
    pass

try:
    from pathlib import Path

    from app.core import serve_token

    _ztp_web = Path("/srv/opstk/ztp-web")
    if _ztp_web.is_dir():
        # ★ J 的第二个落点：ZTP 静态根同样是"无认证 + 含机密"（这里是**明文**设备口令）。
        app.mount("/ztp", _TokenStatic(directory=str(_ztp_web), scope=serve_token.ZTP),
                  name="ztp-serve")
except Exception:  # noqa: BLE001
    pass

# 装机镜像（ISO）单独挂一个静态路径。
# 现有的 ISO 上传/列表/删除都作用于 /srv/opstk/iso，所以挂这一个目录就实现
# "上传完即可用"，且支持任意多个不同系统的镜像。
# casper 的 url=<iso> 会把这个文件取到内存并 loop 挂载，再从里面找 casper/*.squashfs——
# 这是 live 介质能被找到的前提（只给一个裸 squashfs 是不行的）。
class _IsoOnlyStatic(StaticFiles):
    """只对外提供 *.iso。

    /srv/opstk/iso 除了镜像，还可能混进运维产物（例如 download-iso.sh、
    download-complete.flag）。装机网段通常是无认证的，这些文件不该被任何人 HTTP 取走，
    所以这里只放行 .iso，其余一律 404（目录列表本来就没开）。
    """

    async def get_response(self, path, scope):
        if not str(path).lower().endswith(".iso"):
            return Response(status_code=404)
        return await super().get_response(path, scope)


try:
    from pathlib import Path

    _pxe_iso = Path("/srv/opstk/iso")
    if _pxe_iso.is_dir():
        app.mount("/pxe/iso", _IsoOnlyStatic(directory=str(_pxe_iso)), name="pxe-iso")
except Exception:  # noqa: BLE001
    pass

# 部署时把前端构建产物挂到根路径（可选）
# 前端 SPA 静态页（若已构建），使用 SPA fallback 路由
try:
    from pathlib import Path

    _dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if _dist.exists():
        # 挂载 assets 为静态文件（JS/CSS/图片）
        app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="frontend-assets")

        # SPA fallback：所有非 API 路由返回 index.html（Vue Router history mode）
        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            # 未匹配的 /api/* 必须返回 404 JSON，不能被 SPA 兜底吞成 200+HTML
            if full_path.startswith(settings.api_prefix.strip("/")):
                raise HTTPException(status_code=404, detail="Not Found")
            index = _dist / "index.html"
            if index.exists():
                return FileResponse(index)
            return {"detail": "Not Found"}
except Exception:  # noqa: BLE001
    pass
