param(
    [Parameter(Mandatory)][string]$Distro,
    [Parameter(Mandatory)][string]$LinuxUser,
    [Parameter(Mandatory)][string]$Bootstrap
)
$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'restart.log'
Start-Transcript -LiteralPath $log -Force | Out-Null
try {
    Start-Sleep -Seconds 90
    $wsl = "$env:WINDIR\System32\wsl.exe"
    # The maintenance job itself must finish before the VM is stopped.
    $idle = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        & $wsl -d $Distro -u $LinuxUser --exec pgrep -x Runner.Worker 2>$null | Out-Null
        if ($LASTEXITCODE -eq 1) { $idle = $true; break }
        if ($LASTEXITCODE -ne 0) { throw 'Unable to check active runner jobs; restart deferred.' }
        Start-Sleep -Seconds 10
    }
    if (-not $idle) { throw 'A runner job is still active; restart deferred.' }
    & $wsl --shutdown
    if ($LASTEXITCODE -ne 0) { throw 'WSL shutdown failed.' }
    Start-Sleep -Seconds 10
    & $wsl -d $Distro -u $LinuxUser --exec /bin/bash $Bootstrap
    if ($LASTEXITCODE -ne 0) { throw 'Runner startup verification failed.' }
    Write-Output 'WSL restarted and both runner listeners started.'
} catch {
    Write-Output $_.Exception.Message
    throw
} finally {
    Stop-Transcript | Out-Null
}
