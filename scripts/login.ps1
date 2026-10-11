param([switch]$InspectOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$argList = @((Join-Path $PSScriptRoot 'login.py'), '--config', (Join-Path $projectRoot 'config\config.toml'))
if ($InspectOnly) { $argList += '--inspect-only' }
& (Join-Path $projectRoot '.venv\Scripts\python.exe') @argList
if ($LASTEXITCODE -ne 0) { throw '登录助手未完成。请查看上面的状态。' }
