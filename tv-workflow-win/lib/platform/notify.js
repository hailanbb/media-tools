/**
 * Win11 Toast 通知模块
 * 通过 PowerShell 调用 Windows.UI.Notifications 发送系统通知
 */
import { execFile } from 'node:child_process';

/**
 * 发送 Win11 Toast 通知
 * @param {string} title - 通知标题
 * @param {string} body - 通知正文
 */
export function notify(title, body) {
  const safeTitle = String(title).replace(/[`"$]/g, '');
  const safeBody = String(body).replace(/[`"$]/g, '').slice(0, 200);

  const script = `
    Add-Type -AssemblyName System.Windows.Forms
    $n = New-Object System.Windows.Forms.NotifyIcon
    $n.Icon = [System.Drawing.SystemIcons]::Information
    $n.BalloonTipTitle = "${safeTitle}"
    $n.BalloonTipText = "${safeBody}"
    $n.Visible = $true
    $n.ShowBalloonTip(5000)
    Start-Sleep -Seconds 6
    $n.Dispose()
  `;

  try {
    const child = execFile('powershell', [
      '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
      '-Command', script
    ], { timeout: 15000 });
    // 通知失败不应中断主流程
    child.on('error', () => {});
    child.stderr?.on('data', () => {});
  } catch {
    // 静默处理
  }
}
