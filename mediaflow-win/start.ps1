# MediaFlow PowerShell 启动脚本
$Host.UI.RawUI.WindowTitle = "MediaFlow - Windows Service"
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " MediaFlow 媒体库智能整理与归档工作台 (Windows 原生版) " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "正在创建 Python 虚拟环境 (.venv)..." -ForegroundColor Yellow
    python -m venv .venv
}

& .venv\Scripts\Activate.ps1
Write-Host "校验依赖包..." -ForegroundColor DarkGray
pip install -r requirements.txt --quiet

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "已生成 .env，请配置后重启！" -ForegroundColor Yellow
    notepad .env
    Exit
}

Start-Process "http://127.0.0.1:8080"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
