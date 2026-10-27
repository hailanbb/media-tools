"""
跨平台通知模块

支持：
1. Linux / Ubuntu Server:
   - 若处于图形界面环境，自动调用 `notify-send` 发送桌面通知
   - 若配置了 Webhook (钉钉/企业微信/Bark/Server酱/Telegram/自定义 URL)，发送 HTTP Webhook 通知
2. Windows:
   - 调用 PowerShell NotifyIcon 发送桌面 Toast
3. 降级模式:
   - 无桌面环境且无 Webhook 时，静默安全跳过，绝不阻塞主流程
"""

import os
import platform
import shutil
import subprocess
from typing import Any, Dict, Optional

import requests


def notify(title: str, body: str, config: Optional[Any] = None) -> None:
    """发送系统通知（跨平台非阻塞调用）"""
    safe_title = str(title).replace('"', '\\"').replace("`", "")
    safe_body = str(body).replace('"', '\\"').replace("`", "")[:200]

    # 1. 检查配置中是否启用了 Webhook
    webhook_url = None
    if config:
        notif_cfg = config.get("notifications") if isinstance(config, dict) else getattr(config, "notifications", {})
        if isinstance(notif_cfg, dict):
            webhook_url = notif_cfg.get("webhook")

    if webhook_url:
        try:
            payload = {"title": safe_title, "content": safe_body, "text": f"{safe_title}\n{safe_body}"}
            requests.post(webhook_url, json=payload, timeout=5)
        except Exception:
            pass

    system = platform.system().lower()

    # 2. Linux 环境 (Ubuntu Desktop / X11 / Wayland)
    if "linux" in system:
        if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("notify-send"):
            try:
                subprocess.run(
                    ["notify-send", safe_title, safe_body, "-t", "5000"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False
                )
            except Exception:
                pass
        return

    # 3. Windows 环境
    if "windows" in system:
        ps_script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.BalloonTipTitle = "{safe_title}"
        $n.BalloonTipText = "{safe_body}"
        $n.Visible = $true
        $n.ShowBalloonTip(5000)
        Start-Sleep -Seconds 6
        $n.Dispose()
        """
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
