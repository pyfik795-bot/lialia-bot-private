param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,

    [switch]$IncludeDockerImages
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Destination = [System.IO.Path]::GetFullPath($Destination)
$ProjectDir = [System.IO.Path]::GetFullPath($ProjectDir)

if ($Destination.TrimEnd('\') -eq $ProjectDir.TrimEnd('\')) {
    throw "The destination must not be the project directory"
}
if (Test-Path -LiteralPath $Destination) {
    $Existing = Get-ChildItem -Force -LiteralPath $Destination
    if ($Existing.Count -gt 0) {
        throw "The destination directory is not empty: $Destination"
    }
} else {
    New-Item -ItemType Directory -Path $Destination | Out-Null
}

$RequiredFiles = @(
    ".dockerignore", "Dockerfile", "compose.yaml", "requirements.txt",
    "prepare-docker.ps1", "export-to-flash.ps1", "DOCKER_TRANSPORT.md", "INSTRUCTION.html",
    "channels.py", "logging_setup.py", "main.py", "parsers.py", "risk.py",
    "settings.py", "signal_parser.py", "stats.py", "status.py", "synctime.py",
    "tg_bot.py", "trade_engine.py", "webapp.py", "config.py"
)
$StateFiles = @(
    "BOT.session", "active_trades.json", "authorized_chats.json", "channels.json",
    "log.json", "parsed_signals.json", "parsers.json", "processed_signals.json",
    "risk.json", "risk_state.json", "settings.json", "trade_history.json"
)

foreach ($Name in $RequiredFiles) {
    $Source = Join-Path $ProjectDir $Name
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required file was not found: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination (Join-Path $Destination $Name)
}
foreach ($Name in $StateFiles) {
    $Source = Join-Path $ProjectDir $Name
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        Copy-Item -LiteralPath $Source -Destination (Join-Path $Destination $Name)
    }
}
$RequiredDirectories = @("web")
foreach ($Name in $RequiredDirectories) {
    $Source = Join-Path $ProjectDir $Name
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required directory was not found: $Source"
    }
    Copy-Item -Recurse -LiteralPath $Source -Destination $Destination
}

& (Join-Path $Destination "prepare-docker.ps1")

if ($IncludeDockerImages) {
    Push-Location $Destination
    try {
        docker compose build
        if ($LASTEXITCODE -ne 0) { throw "Could not build the Docker images" }
        docker image save --output (Join-Path $Destination "docker-images.tar") `
            lialia-bot:portable
        if ($LASTEXITCODE -ne 0) { throw "Could not save the Docker images" }
    } finally {
        Pop-Location
    }
}

Write-Host "Portable package created: $Destination"
if ($IncludeDockerImages) {
    Write-Host "Offline images saved: docker-images.tar"
}
