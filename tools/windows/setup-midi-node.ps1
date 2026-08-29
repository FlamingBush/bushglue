<#
.SYNOPSIS
  Set a Windows 11 laptop up as the MIDI node: keyboard -> MQTT -> the rig.

.DESCRIPTION
  Two jobs, because the laptop is meant to run lid-closed on battery in a
  playa-adjacent box:

  1. Power. Windows defaults fight this on three fronts -- closing the lid
     sleeps the machine, the Wi-Fi radio power-saves the link out from under
     a long-lived MQTT socket, and USB selective suspend can idle the
     keyboard off. All three are turned off here for both AC and battery.
  2. A scheduled task that starts bush-midi at logon and restarts it if it
     dies, with the two defaults that would otherwise defeat the whole point
     unchecked: "only on AC power" and "stop when switching to battery".

  Re-runnable: registering over an existing task replaces it.

.PARAMETER Broker
  MQTT broker address of the rig (odroid/orangepi). Required the first time;
  omitted, it keeps whatever is already in midi.env.

.PARAMETER MidiPort
  Case-insensitive substring of the MIDI input port name, e.g. "VI25".
  Empty means the first input port Windows offers.

.PARAMETER NoPower
  Register the task but leave power settings alone.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\windows\setup-midi-node.ps1 -Broker 192.168.1.50 -MidiPort VI25
#>
[CmdletBinding()]
param(
  [string]$Broker,
  [string]$MidiPort,
  [switch]$NoPower
)

$ErrorActionPreference = 'Stop'
$repo    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runner  = Join-Path $repo 'tools\windows\run-bush-midi.cmd'
$bushDir = Join-Path $env:LOCALAPPDATA 'bush'
$envFile = Join-Path $bushDir 'midi.env'
$taskName = 'bush-midi'

if (-not (Test-Path $runner)) { throw "missing $runner" }
New-Item -ItemType Directory -Force -Path $bushDir | Out-Null

# ── settings file ──────────────────────────────────────────────────────────
# Merge rather than overwrite, so re-running with just -MidiPort keeps the
# broker.
$settings = [ordered]@{}
if (Test-Path $envFile) {
  foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([^=\s]+)\s*=\s*(.*)$') { $settings[$matches[1]] = $matches[2] }
  }
}
if ($PSBoundParameters.ContainsKey('Broker'))   { $settings['BUSH_MQTT_BROKER'] = $Broker }
if ($PSBoundParameters.ContainsKey('MidiPort')) { $settings['MIDI_PORT'] = $MidiPort }
if (-not $settings['BUSH_MQTT_BROKER']) {
  throw "no broker set yet -- re-run with -Broker <rig address>"
}
($settings.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) |
  Set-Content -Encoding ASCII $envFile
Write-Host "settings -> $envFile"
$settings.GetEnumerator() | ForEach-Object { Write-Host "  $($_.Key)=$($_.Value)" }

# ── power ──────────────────────────────────────────────────────────────────
function Set-PowerValue {
  param($Sub, $Setting, $Value, $What)
  # Not every SKU exposes every setting; a missing one is not worth failing on.
  & powercfg /setacvalueindex SCHEME_CURRENT $Sub $Setting $Value 2>&1 | Out-Null
  & powercfg /setdcvalueindex SCHEME_CURRENT $Sub $Setting $Value 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Warning "could not set $What (exit $LASTEXITCODE)" }
  else { Write-Host "  $What" }
}

if (-not $NoPower) {
  Write-Host "power settings (current scheme, AC and battery):"
  # Lid close: do nothing. This is the one that makes closed-lid operation
  # work at all -- with it set, Modern Standby is never entered on lid close.
  Set-PowerValue SUB_BUTTONS LIDACTION 0 'lid close = do nothing'
  # Wi-Fi radio at maximum performance: power saving drops the association
  # during idle stretches, and an MQTT subscriber is idle by nature.
  Set-PowerValue 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 `
                 12bbebe6-58d6-4636-95bb-3217ef867c1a 0 'Wi-Fi power saving off'
  # USB selective suspend off: the keyboard must stay enumerated.
  Set-PowerValue 2a737441-1930-4402-8d77-b2bebba308a3 `
                 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 'USB selective suspend off'
  # Energy Saver kicking in on battery re-throttles the radio behind our back.
  Set-PowerValue SUB_ENERGYSAVER ESBATTTHRESHOLD 0 'auto Energy Saver off'

  foreach ($t in 'standby-timeout-ac','standby-timeout-dc',
                 'hibernate-timeout-ac','hibernate-timeout-dc') {
    & powercfg /change $t 0 2>&1 | Out-Null
  }
  Write-Host "  sleep and hibernate timeouts disabled"
  & powercfg /setactive SCHEME_CURRENT

  # The screen is off with the lid shut anyway; leaving the display timeout
  # alone is free battery.
  Write-Host "verify with: powercfg /q SCHEME_CURRENT SUB_BUTTONS LIDACTION"
}

# ── scheduled task ─────────────────────────────────────────────────────────
$action  = New-ScheduledTaskAction -Execute $runner
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -DontStopOnIdleEnd `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
  -Settings $taskSettings -Description 'MIDI keyboard -> bushglue MQTT' -Force | Out-Null

Write-Host ""
Write-Host "task '$taskName' registered (starts at logon, restarts on failure)."
Write-Host "  start now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "  stop:       Stop-ScheduledTask  -TaskName $taskName"
Write-Host "  log:        Get-Content -Wait '$bushDir\bush-midi.log'"
