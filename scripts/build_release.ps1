$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先运行scripts/setup_env.ps1准备开发环境。' }
& $python -m pip --isolated install --index-url 'https://pypi.org/simple' --requirement (Join-Path $projectRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw '打包工具安装失败。' }
& $python (Join-Path $PSScriptRoot 'build_release.py')
if ($LASTEXITCODE -ne 0) { throw '发布包未生成，请查看打包输出和日志。' }
