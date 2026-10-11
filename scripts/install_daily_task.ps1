param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'SJTUFitMan-DailyReservation'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output '已移除本项目的登录启动任务。'
    exit 0
}
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonWindowless = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$checker = Join-Path $PSScriptRoot 'poller.py'
& $python $checker check
if ($LASTEXITCODE -ne 0) { throw '请先填写有效的预约和邮箱配置。' }
$launcher = Join-Path $projectRoot '交大健身房助手.pyw'
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('"' + $launcher + '" --autostart') -WorkingDirectory $projectRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description '启动本地交大健身房预约助手，由程序按北京时间12:00预约并提醒付款。' -Force | Out-Null
Write-Output '已设置Windows登录后打开统一健身房助手；每日配置启用后自动启动。'
