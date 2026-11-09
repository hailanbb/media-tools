# MediaFlow PowerShell 启动脚本
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  MediaFlow 本地开发服务启动中..." -ForegroundColor Green
Write-Host "  访问地址: http://127.0.0.1:8080 或 http://localhost:8080" -ForegroundColor Yellow
Write-Host "  按 Ctrl+C 即可停止服务" -ForegroundColor Gray
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
