# After running export_session.js in the logged-in website's Console, run this script.
# The clipboard export is written only to the project's ignored .local/auth directory.
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$authDirectory = Join-Path $projectRoot '.local\auth'
$exportPath = Join-Path $authDirectory 'session-export.json'
$sessionTool = Join-Path $PSScriptRoot 'session.py'
$configPath = Join-Path $projectRoot 'config\config.toml'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'

$clipboardExport = Get-Clipboard -Raw
if ([string]::IsNullOrWhiteSpace($clipboardExport)) {
    throw 'Clipboard is empty. Run export_session.js in the website Console first.'
}
try {
    $parsedExport = ConvertFrom-Json -InputObject $clipboardExport
} catch {
    throw 'Clipboard does not contain a session JSON export. No clipboard contents were printed.'
}
if ($parsedExport.schema_version -ne 1 -or $parsedExport.origin -ne 'https://sports.sjtu.edu.cn') {
    throw 'Clipboard does not contain a sports.sjtu.edu.cn session export.'
}
New-Item -ItemType Directory -Path $authDirectory -Force | Out-Null
[IO.File]::WriteAllText($exportPath, $clipboardExport, [Text.UTF8Encoding]::new($false))
& $pythonExe $sessionTool --config $configPath import $exportPath
if ($LASTEXITCODE -ne 0) {
    throw 'Session import failed. No credentials were printed.'
}
& $pythonExe $sessionTool --config $configPath check
if ($LASTEXITCODE -ne 0) {
    throw 'Session was imported, but login could not be confirmed. See the status above.'
}
