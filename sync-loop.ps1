# Hidden background loop:
#   every 30 min : config / skills / memories sync   (-Mode both)
#   session sync : every 6 h normally, but RETRIES every tick until it succeeds
#                  (so a network blip or a closed-Hermes window self-heals)
# Started at logon by the Startup-folder launcher (HermesSync.cmd).
# To stop: kill this powershell process and/or delete the launcher from Startup.
$Script           = Join-Path $PSScriptRoot 'sync-hermes.ps1'
$IntervalSec      = 1800
$SessionsEveryMin = 360

$lastSessions = $null
while ($true) {
  try { & $Script -Mode both } catch { }

  $run = $false
  if (-not $lastSessions) { $run = $true }
  elseif (((Get-Date) - $lastSessions).TotalMinutes -ge $SessionsEveryMin) { $run = $true }
  if ($run) {
    & $Script -Mode sessions
    if ($LASTEXITCODE -eq 0) {
      $lastSessions = Get-Date
    } else {
      Write-Host ("session sync incomplete (rc " + $LASTEXITCODE + "); retrying next tick")
    }
  }

  Start-Sleep -Seconds $IntervalSec
}
