"""MediaFlow 主入口 — FastAPI 应用初始化、路由挂载、静态文件服务。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path
from logging.handlers import RotatingFileHandler

from app.config import get_settings, VERSION
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
    """应用生命周期：启动时初始化数据库与自愈孤儿任务。"""
    await init_db()
    try:
        from app.database import async_session
        from app.models.task import Task
        from sqlalchemy import select, update
        async with async_session() as db:
            result = await db.execute(
                select(Task).where(Task.status.in_(["running", "stopping", "paused"]))
            )
            orphan_tasks = result.scalars().all()
            if orphan_tasks:
                orphan_ids = [t.id for t in orphan_tasks]
                logging.getLogger("mediaflow.startup").warning(
                    "检测到服务重启前遗留的孤儿运行任务 %d 个 (%s)，正在自动自愈并标记为 stopped...",
                    len(orphan_ids),
                    orphan_ids,
                )
                await db.execute(
                    update(Task)
                    .where(Task.id.in_(orphan_ids))
                    .values(status="stopped")
                )
                await db.commit()
    except Exception as e:
        logging.getLogger("mediaflow.startup").error("启动自愈孤儿任务异常: %s", e)
    yield


app = FastAPI(
    title=settings.app_name,
    version=VERSION,
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
        # 白名单：首页、静态文件、文档与健康检查放行
        is_public = (
            path in ("/", "/index.html", "/favicon.ico", "/api/system/version")
            or path.startswith(("/static/", "/docs", "/openapi.json", "/api/system/health"))
            or path.endswith((".html", ".css", ".js", ".ico", ".png", ".svg", ".woff", ".woff2"))
        )
        if not is_public:
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
@app.get("/index.html", include_in_schema=False)
@app.get("/static/index.html", include_in_schema=False)
async def root():
    """返回前端首页。"""
    index = _frontend_dir / "index.html"
    if index.exists():
        return HTMLResponse(
            index.read_text(encoding="utf-8").replace("__MEDIAFLOW_VERSION__", VERSION)
                .replace("<!--MEDIAFLOW_VERSION-->版本读取中", f"v{VERSION}"),
            headers={"Cache-Control": "no-store", "X-MediaFlow-Version": VERSION},
        )
    return {"message": "MediaFlow API is running. Frontend not found."}


# 挂载整个 frontend 目录作为静态资源（CSS/JS/图片等）
if _frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")
