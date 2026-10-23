# Run explicitly as Administrator when ready to register startup tasks.
param([string]$RepositoryRoot = (Split-Path $PSScriptRoot -Parent), [switch]$AtStartup)
$ErrorActionPreference = 'Stop'
$RepositoryRoot = (Resolve-Path $RepositoryRoot).Path
$Trigger = if ($AtStartup) { New-ScheduledTaskTrigger -AtStartup } else { New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) }
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
foreach ($Component in @('manager', 'plex')) {
    $Name = "Chamoxis-$Component"
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) { throw "$Name already exists; review it before replacing it." }
    $Script = Join-Path $RepositoryRoot "scripts\start-$Component.ps1"
    $Arguments = '-NoProfile -NonInteractive -File "' + $Script + '" -RepositoryRoot "' + $RepositoryRoot + '"'
    $Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments -WorkingDirectory $RepositoryRoot
    if ($AtStartup) {
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -User 'SYSTEM' -RunLevel Highest | Out-Null
    } else {
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings | Out-Null
    }
}
Write-Host 'Startup tasks registered. Existing external applications remain monitored only.'
