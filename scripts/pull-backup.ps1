$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backup = Join-Path $root 'backup'
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$log = Join-Path $backup 'pull.log'
$wslScript = '/mnt/' + $root.Substring(0, 1).ToLowerInvariant() + ($root.Substring(2) -replace '\\', '/') + '/scripts/pull-backup-wsl.sh'
& wsl.exe -- bash $wslScript *>> $log
if ($LASTEXITCODE -ne 0) {
    throw "Backup pull failed (exit $LASTEXITCODE). See $log"
}
