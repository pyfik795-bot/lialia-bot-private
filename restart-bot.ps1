$ErrorActionPreference = "Stop"

$projectDir = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$mainPath = Join-Path $projectDir "main.py"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python virtual environment was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $mainPath)) {
    throw "Bot entry point was not found: $mainPath"
}

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    $commandLine = [string]$process.CommandLine
    if ($process.Name -notmatch "^python(w)?\.exe$" -or $commandLine -notmatch "main\.py") {
        throw "Port 8080 is occupied by an unexpected process (PID $($listener.OwningProcess)). Nothing was stopped."
    }

    & taskkill.exe /PID $listener.OwningProcess /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not stop the previous bot process (PID $($listener.OwningProcess))."
    }
}

$deadline = (Get-Date).AddSeconds(15)
do {
    $stillListening = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
    if (-not $stillListening) {
        break
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

if ($stillListening) {
    throw "The previous process did not release port 8080."
}

$started = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList "-u", "main.py" `
    -WorkingDirectory $projectDir `
    -WindowStyle Hidden `
    -PassThru

$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $listener = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
    if ($listener) {
        Write-Host "Lialia Bot restarted successfully (PID $($listener.OwningProcess), dashboard http://localhost:8080)."
        exit 0
    }
    if ($started.HasExited) {
        throw "The new bot process exited with code $($started.ExitCode). Check logs\bot.log."
    }
} while ((Get-Date) -lt $deadline)

throw "The bot started, but dashboard port 8080 did not become ready within 30 seconds."
