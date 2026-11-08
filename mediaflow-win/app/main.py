"""MediaFlow 主入口 — FastAPI 应用初始化、路由挂载、静态文件服务。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path
from logging.handlers import RotatingFileHandler

from app.config import get_settings
from app.database import init_db
from app.api.router import api_router

settings = get_settings()

# ── 日志持久化配置（控制台 + 文件双写）────────────────────────
log_dir = Path(settings.data_dir) / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "mediaflow.log"

formatter = logging.Formatter(
    fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# 轮转文件处理器：单文件最大 10MB，保留 5 个备份，UTF-8 编码
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

# 降低外部高频网络库与底层驱动噪音
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if settings.debug else logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期：启动时初始化数据库。"""
    await init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
    lifespan=lifespan,
)

# ── CORS（开发/外部访问需要）────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 简易鉴权中间件（可选）────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """如果配置了 auth_token，则要求请求头携带 Authorization: Bearer <token>。

    静态资源、健康检查、docs 路径不做鉴权。
    """
    if settings.auth_token:
        path = request.url.path
        # 白名单
        skip = path.startswith(("/docs", "/openapi.json", "/api/system/health"))
        if not skip and not path.endswith((".html", ".css", ".js", ".ico", ".png", ".svg")):
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {settings.auth_token}":
                return JSONResponse(status_code=401, content={"detail": "未授权"})
    return await call_next(request)


# ── API 路由 ────────────────────────────────────────────
app.include_router(api_router)


# ── 前端静态文件 ────────────────────────────────────────
import pathlib

_frontend_dir = pathlib.Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def root():
    """返回前端首页。"""
    index = _frontend_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "MediaFlow API is running. Frontend not found."}


# 挂载整个 frontend 目录作为静态资源（CSS/JS/图片等）
if _frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
