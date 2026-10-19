# GRGPZS 任务失败桌面通知（P-06 修复轮；通知层过渡方案——整体产品形态确定后可替换）
param(
    [string]$Title = "GRGPZS 任务失败",
    [string]$Message = "详情见 data\processed\logs 日志"
)
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Error
$n.Visible = $true
$n.ShowBalloonTip(8000, $Title, $Message, [System.Windows.Forms.ToolTipIcon]::Error)
Start-Sleep -Seconds 9
$n.Dispose()
