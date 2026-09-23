$ErrorActionPreference = 'Stop'
$pull = (Resolve-Path (Join-Path $PSScriptRoot 'pull-backup.ps1')).Path
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -File `"$pull`""
$trigger = New-ScheduledTaskTrigger -Daily -At '04:30'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'HomeServer Backup Pull' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
