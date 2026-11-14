@echo off
chcp 65001 >nul
title MediaFlow 本地服务
cd /d "%~dp0"
echo ========================================================
echo   MediaFlow 本地开发服务启动中...
echo   访问地址: http://127.0.0.1:8080 或 http://localhost:8080
echo   按 Ctrl+C 即可停止服务
echo ========================================================
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
pause
