@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
if errorlevel 1 (
    echo 无法进入工程目录。
    pause
    exit /b 1
)
where node >nul 2>&1
if errorlevel 1 (
    echo 未找到 Node.js。请安装 Node.js 20 或更高版本后重新启动。
    pause
    popd
    exit /b 1
)
if not exist "node_modules\xlsx-js-style\package.json" (
    echo 缺少程序依赖。请按照 README.md 完成首次安装后重新启动。
    pause
    popd
    exit /b 1
)
node "bin\tray.js"
set "workflow_exit=%errorlevel%"
if not "%workflow_exit%"=="0" (
    echo 程序异常退出，请查看上方提示。
    pause
)
popd
exit /b %workflow_exit%
