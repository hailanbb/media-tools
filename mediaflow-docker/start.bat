@echo off
chcp 65001 >nul
title MediaFlow 控制台
color 0B

echo ======================================================================
echo          MediaFlow 媒体库智能整理与归档工作台
echo ======================================================================
echo.

cd /d "%~dp0"

echo [启动] 正在启动 MediaFlow 服务进程 (监听端口: 8080)...
echo [提示] 网页工作台地址: http://127.0.0.1:8080
echo.

start "" "http://127.0.0.1:8080"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
pause
