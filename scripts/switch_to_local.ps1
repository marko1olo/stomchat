param (
    [string]$Vps = $env:FAILOVER_VPS_HOST,
    [string]$Key = $env:FAILOVER_SSH_KEY,
    [string]$RemoteDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

if (-not $Vps) {
    Write-Host "[ОШИБКА] Не указан адрес VPS." -ForegroundColor Red
    Write-Host "Использование: .\\scripts\\switch_to_local.ps1 -Vps user@1.2.3.4 [-Key C:\\path\\to\\id_rsa]" -ForegroundColor Yellow
    Write-Host "Или установите переменную окружения: `$env:FAILOVER_VPS_HOST = 'user@1.2.3.4'"
    exit 1
}

$argsList = @("$ScriptDir\failover.py", "--to", "local", "--vps", $Vps)
if ($Key) { $argsList += @("--key", $Key) }
if ($RemoteDir) { $argsList += @("--remote-dir", $RemoteDir) }
if ($Force) { $argsList += "--force" }

python @argsList
