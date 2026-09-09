param(
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8080
)

$ErrorActionPreference = "Stop"
$RuleName = "Lialia Bot dashboard (Private LAN)"
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)

if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator, then start this script again."
}

$ExistingRule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($ExistingRule) {
    $ExistingRule | Remove-NetFirewallRule
}

New-NetFirewallRule `
    -DisplayName $RuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $WebPort `
    -Profile Private `
    -RemoteAddress LocalSubnet | Out-Null

Write-Host "Ready: TCP port $WebPort is allowed only from the private local subnet."
Write-Host "Open from another PC: http://$($env:COMPUTERNAME):$WebPort"
Write-Host "Do not forward this port on the router."
