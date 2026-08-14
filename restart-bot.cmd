@echo off
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-bot.ps1"
if errorlevel 1 (
  echo.
  echo Restart failed. See the error above.
  pause
)
