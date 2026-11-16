param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$LinuxUser
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$config = Join-Path $env:USERPROFILE '.wslconfig'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$directory = Join-Path $env:TEMP "CDS-WSL-Memory-$stamp"
New-Item -ItemType Directory -Path $directory -ErrorAction Stop | Out-Null
if (Test-Path -LiteralPath $config) {
    $bytes = [System.IO.File]::ReadAllBytes($config)
    if (@($bytes | Where-Object { $_ -gt 127 }).Count -gt 0) {
        throw 'Existing config has non-ASCII encoding; leave it unchanged for manual review.'
    }
    Copy-Item -LiteralPath $config -Destination (Join-Path $directory 'wslconfig.backup')
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'wsl-memory-restart.ps1') -Destination $directory
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'wsl-runners-start.sh') -Destination $directory
Add-Type @'
using System.Runtime.InteropServices;
public static class WslMemoryIni {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool WritePrivateProfileString(string section, string key, string value, string path);
}
'@
if (-not [WslMemoryIni]::WritePrivateProfileString('wsl2', 'memory', '10GB', $config)) {
    throw 'Unable to update the WSL memory setting.'
}
if (-not ((Get-Content -LiteralPath $config) -match '^\s*memory\s*=\s*10GB\s*$')) {
    throw 'WSL memory setting verification failed.'
}
$bootstrap = Join-Path $directory 'wsl-runners-start.sh'
$linuxBootstrap = (& "$env:WINDIR\System32\wsl.exe" -d $Distro -u $LinuxUser --exec wslpath -u $bootstrap | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxBootstrap.StartsWith('/mnt/')) {
    throw 'Unable to resolve the copied Linux startup script.'
}
$helper = Join-Path $directory 'wsl-memory-restart.ps1'
$command = "& '$($helper.Replace("'", "''"))' -Distro '$Distro' -LinuxUser '$LinuxUser' -Bootstrap '$($linuxBootstrap.Replace("'", "''"))'"
$encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
Remove-Item Env:RUNNER_TRACKING_ID -ErrorAction SilentlyContinue
$process = Start-Process -FilePath "$PSHOME\powershell.exe" -WindowStyle Hidden -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) -PassThru
[pscustomobject]@{memory_limit='10GB'; restart_delay_seconds=90; helper_pid=$process.Id; backup_directory=$directory} | ConvertTo-Json -Compress
