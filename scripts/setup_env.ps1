param([string]$BasePython = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$environment = Join-Path $projectRoot '.venv'
$python = Join-Path $environment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & $BasePython -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)'
    if ($LASTEXITCODE -ne 0) { throw '请安装Python 3.13，或用-BasePython指定其可执行文件路径。' }
    & $BasePython -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw '独立环境创建失败。' }
}
& $python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)'
if ($LASTEXITCODE -ne 0) { throw '现有.venv不是Python 3.13，请使用Python 3.13重新建立项目环境。' }
$argList = @('-m','pip','--isolated','install','--index-url','https://pypi.org/simple','--requirement',(Join-Path $projectRoot 'requirements.txt'))
& $python @argList
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw '依赖检查失败。' }
& $python (Join-Path $PSScriptRoot 'manage_config.py') init
if ($LASTEXITCODE -ne 0) { throw '本地配置初始化失败。' }
Write-Output '本项目独立环境已准备好。'
