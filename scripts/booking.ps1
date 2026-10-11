param(
    [ValidateSet('inspect','prepare','simulate','submit','reconcile')]
    [string]$Mode = 'simulate',
    [string]$Activity = '台球',
    [string]$Date = '',
    [string]$Time = '12:00-13:00',
    [string]$Field = '',
    [string]$ConfirmationSha256 = '',
    [switch]$Headed
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$bookingScript = Join-Path $PSScriptRoot 'booking.py'
$pythonExe = Join-Path $taskRoot '.venv\Scripts\python.exe'
$nativeArgs = @($bookingScript, '--config', (Join-Path $taskRoot 'config\config.toml'), $Mode)
if ($Mode -in @('inspect','prepare','simulate')) {
    $nativeArgs += @('--activity', $Activity)
    if ($Date) { $nativeArgs += @('--date', $Date) }
    if ($Time -and $Mode -ne 'inspect') { $nativeArgs += @('--time', $Time) }
    if ($Field) { $nativeArgs += @('--field', $Field) }
}
if ($Mode -eq 'submit') {
    if (-not $ConfirmationSha256) { throw '请先准备预约、查看预订须知，并填写其确认值。' }
    $nativeArgs += @('--confirm-reservation-sha256', $ConfirmationSha256)
}
if ($Headed -and $Mode -ne 'reconcile') { $nativeArgs += '--headed' }
& $pythonExe @nativeArgs
if ($LASTEXITCODE -ne 0) { throw '预约流程停止，请查看上方提示和本地结果。' }
