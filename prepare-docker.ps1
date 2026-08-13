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

$ConfigText = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath
$SecretMatch = [regex]::Match(
    $ConfigText,
    '(?m)^\s*PROXY_SECRET\s*=\s*["'']([0-9a-fA-F]{32})["'']\s*$'
)
if (-not $SecretMatch.Success) {
    throw "Could not read a 32-character PROXY_SECRET from config.py"
}

$Lines = @(
    "MTPROXY_SECRET=$($SecretMatch.Groups[1].Value.ToLowerInvariant())"
    "LIALIA_WEB_PORT=$WebPort"
    "TZ=Europe/Moscow"
)
Set-Content -Encoding ASCII -LiteralPath $EnvPath -Value $Lines

Write-Host "Ready: created $EnvPath"
Write-Host "Start: docker compose up -d --build"
Write-Host "Dashboard: http://localhost:$WebPort"
