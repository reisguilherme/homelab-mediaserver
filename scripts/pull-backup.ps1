param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [string]$Distro = 'Ubuntu-24.04'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backup = Join-Path $root 'backup'
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$log = Join-Path $backup 'pull.log'
$wslScript = '/mnt/' + $root.Substring(0, 1).ToLowerInvariant() + ($root.Substring(2) -replace '\\', '/') + '/scripts/pull-backup-wsl.sh'
$resolvedEnv = (Resolve-Path -LiteralPath $EnvFile).Path
if ($resolvedEnv -notmatch '^[A-Za-z]:\\') { throw 'EnvFile must be a local drive path accessible in WSL.' }
$wslEnv = '/mnt/' + $resolvedEnv.Substring(0, 1).ToLowerInvariant() + ($resolvedEnv.Substring(2) -replace '\\', '/')
& wsl.exe -d $Distro -- bash $wslScript --env-file $wslEnv *>> $log
if ($LASTEXITCODE -ne 0) {
    throw "Backup pull failed (exit $LASTEXITCODE). See $log"
}
