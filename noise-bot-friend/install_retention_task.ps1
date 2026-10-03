param(
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [string]$TaskName = 'YEGNoise-Cloverdale-Retention'
)
$ErrorActionPreference = 'Stop'
$configFile = (Resolve-Path -LiteralPath $ConfigPath).Path
$pythonFile = (Resolve-Path -LiteralPath $PythonPath).Path
$scriptFile = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'archive_retention.py')).Path
$quietPython = Join-Path (Split-Path -Parent $pythonFile) 'pythonw.exe'
if (Test-Path -LiteralPath $quietPython) { $pythonFile = $quietPython }
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '"{0}" --config "{1}" --apply' -f $scriptFile, $configFile
$action = New-ScheduledTaskAction -Execute $pythonFile -Argument $arguments -WorkingDirectory $PSScriptRoot
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$login = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($hourly,$login) -Principal $principal -Settings $settings -Description 'Verify station recordings on the hub, then free only verified old Pi copies.' -Force | Select-Object TaskName,State
