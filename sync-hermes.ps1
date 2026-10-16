<#
  sync-hermes.ps1 -- Two-way Hermes sync using a private GitHub repo as the middleman.

  Synced (relative to HERMES_HOME):
    config.yaml, SOUL.md, channel_directory.json
    skills/ , memories/ , sessions/
  Secrets (encrypted before sync):
    .env , auth.json  ->  secrets/*.enc
  Session store (encrypted before sync):
    state.db snapshot ->  snapshots/<label>.db.enc   (branch: sessions-snapshot)

  Usage:
    powershell -NoProfile -ExecutionPolicy Bypass -File sync-hermes.ps1 -Mode push
    powershell -NoProfile -ExecutionPolicy Bypass -File sync-hermes.ps1 -Mode pull
    powershell -NoProfile -ExecutionPolicy Bypass -File sync-hermes.ps1 -Mode both
    powershell -NoProfile -ExecutionPolicy Bypass -File sync-hermes.ps1 -Mode sessions

  Passphrase: -Passphrase "xxx"  or env var HERMES_SYNC_PASSPHRASE
#>
[CmdletBinding()]
param(
  [ValidateSet('push','pull','both','sessions')][string]$Mode = 'both',
  [string]$Passphrase = $env:HERMES_SYNC_PASSPHRASE,
  [string]$Remote    = $env:HERMES_SYNC_REMOTE,
  [string]$Branch    = 'main',
  [string]$GitUser   = $env:HERMES_SYNC_GIT_USER,
  [string]$GitEmail  = $env:HERMES_SYNC_GIT_EMAIL
)

if (-not $Remote) {
  Write-Host "ERROR: no repo configured. Pass -Remote <git-url> or set the HERMES_SYNC_REMOTE env var."
  exit 1
}

$ErrorActionPreference = 'Continue'

# ---------------- paths ----------------
if ($env:HERMES_HOME) { $HermesHome = $env:HERMES_HOME } else { $HermesHome = Join-Path $env:LOCALAPPDATA 'hermes' }
$SyncRoot = Join-Path $env:LOCALAPPDATA 'hermes\sync'
$RepoDir  = Join-Path $SyncRoot 'repo'
$LogFile  = Join-Path $SyncRoot 'sync.log'

# ---------------- git transport (local proxy, auto-detected) ----------------
# A local Clash/V2Ray-style proxy is common, and on some networks it is REQUIRED to reach
# github.com. It must never be baked into git's global config: a stopped proxy would then
# wedge every git call with "Could not connect to server" even after it is switched off.
# Instead we probe the usual local ports once per run and inject -c http.proxy=... only
# while one actually accepts connections; otherwise we connect directly. Starting or
# stopping the proxy therefore needs no configuration change - the next run adapts.
function Test-LocalPort([int]$Port) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne(300)) { return $false }
    $client.EndConnect($async)
    return $true
  } catch { return $false } finally { $client.Close() }
}

function Get-ProxyCandidates {
  $ports = New-Object System.Collections.Generic.List[int]
  try {
    $reg = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($reg -and $reg.ProxyServer -match '(\d+)\s*$') { [void]$ports.Add([int]$Matches[1]) }
  } catch { }
  foreach ($p in @(7897, 7890, 7891, 10809, 10808, 1080, 8080, 8889, 20171)) {
    if (-not $ports.Contains($p)) { [void]$ports.Add($p) }
  }
  return $ports
}

$ProxyUrl = $null
foreach ($port in (Get-ProxyCandidates)) {
  if (Test-LocalPort $port) { $ProxyUrl = "http://127.0.0.1:$port"; break }
}
# cap the connect phase so an unreachable remote fails in seconds instead of minutes
$GitNetArgs = @('-c', 'http.connectTimeout=10')
if ($ProxyUrl) { $GitNetArgs += @('-c', "http.proxy=$ProxyUrl", '-c', "https.proxy=$ProxyUrl") }

# One quick reachability probe per run: an offline round (or a proxy that is not running)
# must not burn three 21-second connect timeouts before giving up.
function Test-RemoteReachable {
  $target = 'github.com'
  $port = 443
  if ($ProxyUrl) {
    $u = [System.Uri]$ProxyUrl
    $target = $u.Host
    $port = $u.Port
  }
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $async = $client.BeginConnect($target, $port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne(4000)) { return $false }
    $client.EndConnect($async)
    return $true
  } catch { return $false } finally { $client.Close() }
}

# ---------------- manifest ----------------
$IncludeFiles = @('config.yaml','SOUL.md','channel_directory.json')
$IncludeDirs  = @('skills','memories','sessions')
$SecretFiles  = @('.env','auth.json')

function Log([string]$m) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
  Write-Host $line
  try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}

# ---------------- AES-256-CBC + PBKDF2-SHA256 (100k) ----------------
function Protect-File([string]$InPath,[string]$OutPath,[string]$Pass) {
  $salt = New-Object byte[] 16
  $iv   = New-Object byte[] 16
  $rng  = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($salt); $rng.GetBytes($iv)
  $kdf  = [System.Security.Cryptography.Rfc2898DeriveBytes]::new($Pass,$salt,100000,[System.Security.Cryptography.HashAlgorithmName]::SHA256)
  $key  = $kdf.GetBytes(32)
  $aes  = [System.Security.Cryptography.Aes]::Create()
  $aes.Key = $key; $aes.IV = $iv; $aes.Mode = 'CBC'; $aes.Padding = 'PKCS7'
  $data = [System.IO.File]::ReadAllBytes($InPath)
  $enc  = $aes.CreateEncryptor().TransformFinalBlock($data,0,$data.Length)
  $out  = New-Object byte[] ($salt.Length + $iv.Length + $enc.Length)
  [Array]::Copy($salt,0,$out,0,16)
  [Array]::Copy($iv,0,$out,16,16)
  [Array]::Copy($enc,0,$out,32,$enc.Length)
  [System.IO.File]::WriteAllBytes($OutPath,$out)
}

function Unprotect-File([string]$InPath,[string]$OutPath,[string]$Pass) {
  $raw  = [System.IO.File]::ReadAllBytes($InPath)
  if ($raw.Length -lt 33) { throw "corrupt encrypted file: $InPath" }
  $salt = [byte[]]$raw[0..15]
  $iv   = [byte[]]$raw[16..31]
  $enc  = [byte[]]$raw[32..($raw.Length-1)]
  $kdf  = [System.Security.Cryptography.Rfc2898DeriveBytes]::new($Pass,$salt,100000,[System.Security.Cryptography.HashAlgorithmName]::SHA256)
  $key  = $kdf.GetBytes(32)
  $aes  = [System.Security.Cryptography.Aes]::Create()
  $aes.Key = $key; $aes.IV = $iv; $aes.Mode = 'CBC'; $aes.Padding = 'PKCS7'
  $dec  = $aes.CreateDecryptor().TransformFinalBlock($enc,0,$enc.Length)
  [System.IO.File]::WriteAllBytes($OutPath,$dec)
}

# ---------------- git helpers ----------------
function GitRun {
  param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)
  $all = @($GitNetArgs) + $GitArgs
  $out = & git.exe -C $RepoDir @all 2>&1
  $code = $LASTEXITCODE
  return [pscustomobject]@{ code = $code; out = ($out -join "`n") }
}

function Ensure-Repo {
  if (Test-Path (Join-Path $RepoDir '.git')) { return }
  New-Item -ItemType Directory -Force -Path $RepoDir | Out-Null
  Write-Host "first run: cloning $Remote ..."
  & git.exe clone --branch $Branch $Remote $RepoDir 2>&1 | Write-Host
  if (-not (Test-Path (Join-Path $RepoDir '.git'))) {
    Write-Host "clone failed or empty remote: local init instead ..."
    & git.exe -C $RepoDir init -b $Branch 2>&1 | Write-Host
    & git.exe -C $RepoDir remote add origin $Remote 2>&1 | Write-Host
  }
  & git.exe -C $RepoDir config user.name  $GitUser  | Out-Null
  & git.exe -C $RepoDir config user.email $GitEmail | Out-Null
  if (-not (Test-Path (Join-Path $RepoDir '.git'))) { throw "failed to initialise repo at $RepoDir (is git installed?)" }
}

function Copy-Tree([string]$Src,[string]$Dst) {
  New-Item -ItemType Directory -Force -Path $Dst | Out-Null
  $rc = Start-Process -FilePath 'robocopy' -ArgumentList @($Src,$Dst,'/E','/XF','*.lock','*.db-shm','*.db-wal','/NFL','/NDL','/NJH','/NJS','/NP','/R:1','/W:1') -Wait -PassThru -WindowStyle Hidden
  if ($rc.ExitCode -ge 8) { throw "robocopy failed (exit $($rc.ExitCode)): $Src -> $Dst" }
}

# ---------------- local -> repo ----------------
function Stage-Payload {
  $payload = Join-Path $RepoDir 'payload'
  if (Test-Path $payload) { Remove-Item -Recurse -Force $payload }
  New-Item -ItemType Directory -Force -Path $payload | Out-Null

  foreach ($f in $IncludeFiles) {
    $src = Join-Path $HermesHome $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $payload $f) -Force }
  }
  foreach ($d in $IncludeDirs) {
    $src = Join-Path $HermesHome $d
    if (Test-Path $src) { Copy-Tree $src (Join-Path $payload $d) }
  }
  if ($Passphrase) {
    $sec = Join-Path $RepoDir 'secrets'
    New-Item -ItemType Directory -Force -Path $sec | Out-Null
    foreach ($s in $SecretFiles) {
      $src = Join-Path $HermesHome $s
      if (Test-Path $src) { Protect-File $src (Join-Path $sec ("$s.enc")) $Passphrase; Log "encrypted+staged $s" }
    }
  } else {
    Log "no passphrase: skipping .env / auth.json"
  }
  Log "staged local content into payload/"
}

# ---------------- repo -> local ----------------
function Restore-Payload {
  $payload = Join-Path $RepoDir 'payload'
  if (-not (Test-Path $payload)) { Log "no payload/ in repo, skip restore"; return }
  foreach ($f in $IncludeFiles) {
    $src = Join-Path $payload $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $HermesHome $f) -Force }
  }
  foreach ($d in $IncludeDirs) {
    $src = Join-Path $payload $d
    if (Test-Path $src) { Copy-Tree $src (Join-Path $HermesHome $d) }
  }
  $sec = Join-Path $RepoDir 'secrets'
  if ((Test-Path $sec) -and $Passphrase) {
    foreach ($s in $SecretFiles) {
      $enc = Join-Path $sec ("$s.enc")
      if (Test-Path $enc) { Unprotect-File $enc (Join-Path $HermesHome $s) $Passphrase; Log "decrypted+restored $s" }
    }
  }
  Log "restored payload/ into $HermesHome"
}

# ---------------- push / pull ----------------
function Do-Push {
  if (-not (Test-RemoteReachable)) { Log "remote unreachable (proxy down / offline): skipping push"; return }
  Ensure-Repo
  Stage-Payload
  GitRun 'add' '-A' | Out-Null
  $msg = "sync from " + $env:COMPUTERNAME + " at " + (Get-Date -Format 's')
  GitRun 'commit' '-m' $msg | Out-Null
  $rb = GitRun 'pull' '--rebase' '--autostash' 'origin' $Branch
  if ($rb.code -ne 0) { GitRun 'rebase' '--abort' | Out-Null; Log "rebase failed, aborted (will try direct push)" }
  $p = GitRun 'push' 'origin' $Branch
  if ($p.code -ne 0) { Log ("push failed: " + $p.out); throw "push failed" }
  Log "push ok"
}

function Do-Pull {
  if (-not (Test-RemoteReachable)) { Log "remote unreachable (proxy down / offline): skipping pull"; return }
  Ensure-Repo
  $pl = GitRun 'pull' '--no-rebase' '-X' 'theirs' 'origin' $Branch
  if ($pl.code -ne 0) { Log ("pull note: " + $pl.out) }
  Restore-Payload
  Log "pull done"
}

function Find-Python {
  $cands = New-Object System.Collections.Generic.List[string]
  $c = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($c) { $cands.Add($c.Source) }
  foreach ($r in @((Join-Path $env:LOCALAPPDATA 'hermes\tools'), (Join-Path $env:LOCALAPPDATA 'hermes\installs'))) {
    if (Test-Path $r) {
      Get-ChildItem -Path $r -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
        ForEach-Object { $cands.Add($_.FullName) }
    }
  }
  foreach ($p in $cands) {
    if ($p -like '*\WindowsApps\*') { continue }   # Store alias stub, not a real interpreter
    # transient/staging copies (Hermes unpacks tools into .reclaim-* dirs that may vanish)
    if ($p -like '*\.reclaim-*' -or $p -like '*\Temp\*' -or $p -like '*\tmp\*') { continue }
    $v = & $p -c "import sys; print(sys.version_info[0])" 2>&1
    if ($LASTEXITCODE -eq 0 -and (($v -join '') -match '^\d')) { return $p }
  }
  return $null
}

# Sessions live in state.db (SQLite). Sync = snapshot + additive merge.
# Snapshots travel on a dedicated single-commit branch that is force-pushed, so the
# git repo never grows. Each machine owns snapshots/<label>.db.enc and absorbs all
# *other* machines' snapshots (additive, tagged by source).
# The snapshot holds RAW conversation text, so it is AES-encrypted before it is
# published - the same treatment .env / auth.json get. A plaintext snapshots/*.db
# left behind by an older peer is still read (legacy), but never produced.
$SnapRepo   = Join-Path $SyncRoot 'snaprepo'
$SnapBranch = 'sessions-snapshot'
# label for THIS machine's snapshot file; peers use it as the import tag.
# override with the HERMES_SYNC_LABEL env var (e.g. 笔记本 / 台式机).
$SnapLabel  = if ($env:HERMES_SYNC_LABEL) { $env:HERMES_SYNC_LABEL } else { $env:COMPUTERNAME }

function Ensure-SnapRepo {
  if (Test-Path (Join-Path $SnapRepo '.git')) { return }
  New-Item -ItemType Directory -Force -Path $SnapRepo | Out-Null
  & git.exe @GitNetArgs clone --depth 1 --branch $SnapBranch $Remote $SnapRepo 2>&1 | Out-Null
  if (-not (Test-Path (Join-Path $SnapRepo '.git'))) {
    Remove-Item -Recurse -Force $SnapRepo -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $SnapRepo | Out-Null
    & git.exe -C $SnapRepo init -b $SnapBranch 2>&1 | Out-Null
    & git.exe -C $SnapRepo remote add origin $Remote 2>&1 | Out-Null
  }
  & git.exe -C $SnapRepo config user.name  $GitUser  | Out-Null
  & git.exe -C $SnapRepo config user.email $GitEmail | Out-Null
}

function Update-SnapRepo {
  Ensure-SnapRepo
  & git.exe -C $SnapRepo @GitNetArgs fetch --depth 1 origin $SnapBranch 2>&1 | Out-Null
  & git.exe -C $SnapRepo reset --hard FETCH_HEAD 2>&1 | Out-Null
}

function Publish-Snapshot([string]$LocalSnap) {
  if (-not $Passphrase) {
    Log "no passphrase: refusing to publish the session snapshot in the clear"
    return $false
  }
  if (-not (Test-RemoteReachable)) {
    Log "remote unreachable (proxy down / offline): skipping snapshot publish"
    return $false
  }
  Update-SnapRepo
  $dir = Join-Path $SnapRepo 'snapshots'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  # retire this machine's plaintext snapshot - and any SQLite sidecars an older
  # version may have committed; the snapshot must only ever travel encrypted
  foreach ($suffix in @('.db', '.db-shm', '.db-wal')) {
    $legacy = Join-Path $dir ($SnapLabel + $suffix)
    if (Test-Path $legacy) { Remove-Item -Force $legacy }
  }
  Protect-File $LocalSnap (Join-Path $dir ($SnapLabel + '.db.enc')) $Passphrase
  & git.exe -C $SnapRepo add -A | Out-Null
  $msg = "session snapshot from {0} at {1}" -f $env:COMPUTERNAME, (Get-Date -Format 's')
  & git.exe -C $SnapRepo commit -m $msg | Out-Null
  $ok = $false
  for ($i = 1; $i -le 3; $i++) {
    $r = & git.exe -C $SnapRepo @GitNetArgs push --force origin $SnapBranch 2>&1
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Log ("snapshot push attempt $i failed: " + ($r -join ' '))
    Start-Sleep -Seconds 5
  }
  if (-not $ok) { Log "snapshot publish failed (network?)"; return $false }
  Log ("sessions: published encrypted snapshot as snapshots/" + $SnapLabel + ".db.enc")
  return $true
}

function Get-RemoteSnapshots {
  Update-SnapRepo
  $d = Join-Path $SnapRepo 'snapshots'
  if (-not (Test-Path $d)) { return @() }
  $out = New-Object System.Collections.Generic.List[object]
  foreach ($f in @(Get-ChildItem -Path $d -File -ErrorAction SilentlyContinue)) {
    if ($f.Name -eq ($SnapLabel + '.db.enc') -or $f.Name -eq ($SnapLabel + '.db')) { continue }
    if ($f.Name -like '*.db.enc') {
      $out.Add([pscustomobject]@{
        Label = $f.Name.Substring(0, $f.Name.Length - 7); Enc = $true; Path = $f.FullName })
    } elseif ($f.Name -like '*.db') {
      $out.Add([pscustomobject]@{
        Label = $f.BaseName; Enc = $false; Path = $f.FullName })
    }
  }
  return $out
}

function Do-Sessions {
  $py = Find-Python
  if (-not $py) { Log "python not found: session sync unavailable"; return }
  $tool = Join-Path $SyncRoot 'state_db_tool.py'
  $localSnap = Join-Path $SyncRoot 'state.db.snapshot'
  Log ("using python: " + $py)
  $sout = & $py $tool snapshot --out $localSnap 2>&1
  if ($LASTEXITCODE -ne 0) { Log ("snapshot failed (rc $LASTEXITCODE): " + ($sout -join ' ')); exit 1 }
  if ($sout) { Log (($sout -join ' ').Trim()) }
  if (-not (Publish-Snapshot $localSnap)) { Log "sessions: publish failed"; exit 1 }
  $others = @(Get-RemoteSnapshots)
  if ($others.Count -eq 0) { Log "sessions: no snapshots from other machines yet"; Log "sessions: done"; return }
  $work = Join-Path $SyncRoot 'tmp'
  New-Item -ItemType Directory -Force -Path $work | Out-Null
  foreach ($o in $others) {
    $plain = $o.Path
    if ($o.Enc) {
      if (-not $Passphrase) { Log ("skip " + $o.Label + ": snapshot is encrypted but no passphrase is set"); continue }
      $plain = Join-Path $work ($o.Label + '.db')
      try { Unprotect-File $o.Path $plain $Passphrase }
      catch { Log ("decrypt failed for " + $o.Label + ": " + $_); continue }
    }
    $mout = & $py $tool merge --snapshot $plain --tag $o.Label 2>&1
    Log ("merge " + $o.Label + " (rc $LASTEXITCODE): " + (($mout -join ' ').Trim()))
    if ($o.Enc) { Remove-Item -Force $plain -ErrorAction SilentlyContinue }
  }
  Log "sessions: done"
}

# ---------------- main ----------------
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue) -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "ERROR: git not found in PATH. Install Git for Windows (https://git-scm.com/download/win) and reopen the terminal."
  exit 1
}
New-Item -ItemType Directory -Force -Path $SyncRoot | Out-Null
Log "=== start $Mode (HERMES_HOME=$HermesHome) ==="
if ($ProxyUrl) { Log ("git transport: local proxy " + $ProxyUrl + " (auto-detected)") }
else { Log "git transport: direct (no local proxy listening)" }
switch ($Mode) {
  'push' { Do-Push }
  'pull' { Do-Pull }
  'both' { Do-Push; Do-Pull }
  'sessions' { Do-Sessions }
}
Log "=== end ==="
