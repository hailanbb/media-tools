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
set PY_EXE=python
if exist "%~dp0.venv\Scripts\python.exe" set PY_EXE="%~dp0.venv\Scripts\python.exe"
if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" set PY_EXE="%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"

%PY_EXE% -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
pause
