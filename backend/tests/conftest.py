# -*- coding: utf-8 -*-
"""全量用例的**环境隔离**（pytest 自动发现，不需要 import）。

为什么需要这个文件：用例在**容器里**跑，而容器读的是真实的 `backend/.env` ——
也就是说运维在 `.env` 里配了什么，用例就会看到什么。这会制造两类问题：
  1. **用例随环境变红/变绿**：最典型的就是 `/pxe/serve` 的 token（J）——生产上启用之后，
     凡是断言 `http://ip:8000/pxe/serve/...` 这类**逐字 URL** 的用例都会多出一段 token 段而失败。
     用例失败不是产品坏了，而是"用例依赖了运行环境的配置"。
  2. **危险**：任何会写 `.env` 的代码路径（`credential_key` / `secret_key` / `pxe_serve_token`）
     在用例里被触发，就会去改**生产那份 .env**（容器里它是 bind mount）。

这里把"与环境有关的那几个设置"在**每个用例开始前**钉成确定值（用例自己要用别的值，
仍然可以用 `mock.patch.object(settings, ...)` 覆盖，作用域更小、优先级更高）。
"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """把所有"会被 .env 影响"的设置钉死，让用例的结果只取决于代码本身。"""
    # J：`/pxe/serve` 的 token 门禁。默认**关**（= 与改造前逐字相同的行为），
    # 需要测门禁的用例自己 patch 成想要的值（见 tests/test_serve_token.py）。
    monkeypatch.setattr(settings, "pxe_serve_token", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _protect_env_file(monkeypatch, tmp_path_factory):
    """兜底：把 crypto 的 .env/.lock 路径指到临时目录。

    这样即使某个用例（现在或将来）走到了 `_write_env` / `ensure_token` 那条路，
    动的也是临时文件，**不会碰到生产那份 .env**。需要断言"写进去了"的用例
    自己再 patch 到临时路径即可（见 tests/test_serve_token.py::_temp_env）。

    ★ 用 `tmp_path_factory`（会话级的另一棵树），**不能**用 `tmp_path`：
      `tests/test_host_reload.py` 有一个用例 `sorted(tmp_path.iterdir()) ==
      ["opstk-pxe.conf"]` —— 只要 per-test 的 tmp_path 里多出任何东西（哪怕是个子目录）
      那条用例就会红。那是"测试夹具改变了别人观察到的状态"，不是产品问题
      （我第一次就踩了：先写 `.env` 进去，再改成子目录，两次都红）。
    """
    import pathlib

    envdir = tmp_path_factory.mktemp("dothome")
    env = envdir / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr("app.core.crypto._ENV_PATH", pathlib.Path(env))
    monkeypatch.setattr("app.core.crypto._LOCK_PATH", pathlib.Path(envdir / ".env.lock"))
    yield
