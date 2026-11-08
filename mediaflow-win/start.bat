@echo off
chcp 65001 >nul
title MediaFlow - Windows 服务控制台
color 0B

echo ======================================================================
echo          MediaFlow 媒体库智能整理与归档工作台 (Windows 原生版)
echo ======================================================================
echo.

cd /d "%~dp0"

:: 1. 检查 Python 环境
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python 环境！
    echo 请先安装 Python 3.10 或更高版本，并在安装时勾选 "Add python.exe to PATH"。
    echo 下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: 2. 检查或创建虚拟环境
if not exist ".venv" (
    echo [初始化] 正在创建 Python 虚拟环境 (.venv)...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [错误] 创建虚拟环境失败，请检查 Python 权限！
        pause
        exit /b 1
    )
)

:: 3. 激活虚拟环境并安装依赖
echo [环境] 激活虚拟环境...
call .venv\Scripts\activate.bat

echo [检查] 正在校验核心依赖库...
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet

:: 4. 检查 .env 配置文件
if not exist ".env" (
    echo [提示] 未检测到 .env 配置文件，正在自动从 .env.example 复制模板...
    copy .env.example .env >nul
    echo.
    echo ======================================================================
    echo [重要提示] 已为您生成 .env 配置文件！
    echo 请打开当前目录下的 .env 文件，填写真实的 CloudDrive2 和 MoviePilot 配置后重新启动。
    echo ======================================================================
    echo.
    notepad .env
    pause
    exit /b 0
)

:: 5. 启动服务
echo.
echo [启动] 正在启动 MediaFlow 服务进程 (监听端口: 8080)...
echo [提示] 网页工作台地址: http://127.0.0.1:8080
echo [提示] 按 Ctrl+C 可停止服务
echo.

start "" "http://127.0.0.1:8080"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
pause
