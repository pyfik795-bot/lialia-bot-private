param(
    [int]$WebPort = 8080
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ProjectDir "config.py"
$EnvPath = Join-Path $ProjectDir ".env"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "config.py was not found: $ConfigPath"
}

$Lines = @(
    "LIALIA_WEB_PORT=$WebPort"
    "TZ=Europe/Moscow"
)
Set-Content -Encoding ASCII -LiteralPath $EnvPath -Value $Lines

Write-Host "Ready: created $EnvPath"
Write-Host "Start: docker compose up -d --build"
Write-Host "Dashboard: http://localhost:$WebPort"
