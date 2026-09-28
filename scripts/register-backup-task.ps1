param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [string]$Distro = 'Ubuntu-24.04',
    [string]$At = '04:30',
    [string]$TaskName = 'HomeServer Backup Pull'
)
$ErrorActionPreference = 'Stop'
$resolvedEnv = (Resolve-Path -LiteralPath $EnvFile).Path
$pull = (Resolve-Path (Join-Path $PSScriptRoot 'pull-backup.ps1')).Path
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -File `"$pull`" -EnvFile `"$resolvedEnv`" -Distro `"$Distro`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
