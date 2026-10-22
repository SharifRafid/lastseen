# lastseen installer for Windows.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/sharifrafid/lastseen/main/install.ps1 | iex"
# Re-run it any time to upgrade.
$ErrorActionPreference = 'Stop'

$ref = if ($env:LASTSEEN_REF) { $env:LASTSEEN_REF } else { 'main' }
$source = "https://github.com/sharifrafid/lastseen/archive/refs/heads/$ref.zip"
if ($ref -match '^v[0-9]') { $source = "https://github.com/sharifrafid/lastseen/archive/refs/tags/$ref.zip" }
$port = if ($env:LASTSEEN_PORT) { $env:LASTSEEN_PORT } else { '8765' }

Write-Host 'This will install lastseen, a private location history for your Apple devices.'
Write-Host '  1. Install uv (a small Python tool manager) if you do not have it'
Write-Host '  2. Install lastseen with its own Python, in your user account (no admin needed)'
Write-Host '  3. Start lastseen so you can finish setup in your browser'
Write-Host ''

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing uv...'
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv was installed but cannot be found. Open a new PowerShell window and run this installer again.'
    }
}

# Upgrading: stop the running lastseen first, so its files can be replaced.
$old = Get-Command lastseen -ErrorAction SilentlyContinue
if ($old) {
    Write-Host 'Stopping the running lastseen...'
    try { & $old.Source stop *> $null } catch {}
}

Write-Host 'Installing lastseen...'
uv tool install --force --python 3.12 $source
if ($LASTEXITCODE -ne 0) { throw 'Installing lastseen failed (see the messages above).' }
uv tool update-shell | Out-Null

$binDir = (uv tool dir --bin).Trim()
$env:Path = "$binDir;$env:Path"
$exe = Join-Path $binDir 'lastseen.exe'
if (-not (Test-Path $exe)) { $exe = 'lastseen' }

$autostart = $false
try { & $exe service status *> $null; $autostart = ($LASTEXITCODE -eq 0) } catch {}
if ($autostart) {
    # Autostart is on: start the background instance with the new version.
    try { & $exe service install *> $null } catch {}
} else {
    Start-Process -FilePath $exe -WindowStyle Hidden
}

$running = $false
for ($i = 0; $i -lt 20 -and -not $running; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/api/health" | Out-Null
        $running = $true
    } catch { Start-Sleep -Seconds 1 }
}

$dataDir = if ($env:LASTSEEN_DATA_DIR) { $env:LASTSEEN_DATA_DIR } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'lastseen' } else { Join-Path $env:USERPROFILE 'AppData\Local\lastseen' }
Write-Host ''
Write-Host 'lastseen is installed.'
if ($running) {
    Write-Host "Finish setup in your browser: http://127.0.0.1:$port"
    Write-Host '(It should open by itself. Later, just run: lastseen)'
} else {
    Write-Host "lastseen didn't start. Open a new PowerShell window and run: lastseen"
    Write-Host 'It shows what went wrong.'
}
Write-Host ''
Write-Host "Open a new terminal if the 'lastseen' command is not found yet."
Write-Host 'To stop it:     lastseen stop'
Write-Host 'To uninstall:   lastseen service uninstall; lastseen stop; uv tool uninstall lastseen'
Write-Host "Your history and settings live in: $dataDir"
