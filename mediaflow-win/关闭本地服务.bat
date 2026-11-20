@echo off
chcp 65001 >nul
title MediaFlow - 停止服务
color 0C

echo 正在停止占用 8080 端口的 MediaFlow 进程...

for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8080" ^| findstr "LISTENING"') do (
    echo 正在结束 PID: %%a
    taskkill /F /PID %%a >nul 2>&1
)

echo.
echo MediaFlow 服务已安全停止！
pause
