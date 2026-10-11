$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
& (Join-Path $PSScriptRoot 'setup_env.ps1')
