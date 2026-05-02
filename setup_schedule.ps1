# Windows 计划任务设置脚本
# 在 PowerShell 中以管理员身份运行此脚本

$taskName = "GameMale每日签到"
$scriptPath = Join-Path $PSScriptRoot "run.bat"
$pythonPath = (Get-Command python).Source

if (-not (Test-Path $pythonPath)) {
    Write-Error "未找到 Python，请确认 Python 已安装并在 PATH 中"
    exit 1
}

# 删除旧任务（如果存在）
schtasks /delete /tn $taskName /f 2>$null

# 创建计划任务：每天 8:00 触发
# Python 脚本内部会随机延迟 0~120 分钟，所以实际执行在 8:00~10:00
schtasks /create /tn $taskName `
    /tr "cmd /c `"$scriptPath`"" `
    /sc daily `
    /st 08:00 `
    /f

Write-Host "========================================"
Write-Host "  计划任务已创建！"
Write-Host "  任务名: $taskName"
Write-Host "  每天 8:00 触发，脚本内部随机延迟 0~120 分钟"
Write-Host "  日志文件: bot.log"
Write-Host "========================================"

# 显示任务详情
schtasks /query /tn $taskName /fo LIST
