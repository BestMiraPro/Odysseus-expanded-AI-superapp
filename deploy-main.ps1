<#
.SYNOPSIS
  Install or update Odysseus on this PC from the `main` branch and start it.

.DESCRIPTION
  One command for both a fresh PC and an existing install:

    * no clone yet  -> clones the repo, creates .env from .env.example
    * clone exists  -> fetches and fast-forwards to origin/<Branch>
                       (refuses if you have uncommitted changes)

  Then runs `docker compose up -d --build`, which rebuilds the app image and
  keeps everything in ./data (your DB, memories, documents), and waits for
  /api/health.

  It uses plain `docker compose` from the repo folder, the same as the README
  Quick Start, so it updates the stack you already run instead of starting a
  second one beside it.

  Works when piped from the web, where there are no parameters:
    irm https://raw.githubusercontent.com/BestMiraPro/Odysseus-expanded-AI-superapp/main/deploy-main.ps1 | iex

.PARAMETER Dir     Where the repo lives (default: this script's folder when run
                   from a clone, otherwise ~\Odysseus-expanded-AI-superapp).
.PARAMETER Branch  Branch to deploy (default: main).
.PARAMETER HealthTimeoutSec  Seconds to wait for /api/health (default 600; the
                   first build is slow).
#>
[CmdletBinding()]
param(
  [string]$Dir,
  [string]$Branch = 'main',
  [int]$HealthTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$RepoUrl = 'https://github.com/BestMiraPro/Odysseus-expanded-AI-superapp.git'
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$a) {
  Write-Host "    $exe $($a -join ' ')" -ForegroundColor DarkGray
  & $exe @a; if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($a -join ' ')" }
}

# --- 0. Prerequisites -----------------------------------------------------------
foreach ($tool in 'git', 'docker') {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    throw "$tool is not installed or not on PATH. Install Git and Docker Desktop first."
  }
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker is installed but not running. Start Docker Desktop and run this again." }
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { throw "'docker compose' is not available. Update Docker Desktop." }

# --- 1. Locate or clone the repo ------------------------------------------------
if (-not $Dir) {
  if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'docker-compose.yml')) -and
      (Test-Path (Join-Path $PSScriptRoot '.git'))) {
    $Dir = $PSScriptRoot
  } else {
    $Dir = Join-Path $HOME 'Odysseus-expanded-AI-superapp'
  }
}

if (-not (Test-Path (Join-Path $Dir '.git'))) {
  if ((Test-Path $Dir) -and (Get-ChildItem -Force $Dir | Select-Object -First 1)) {
    throw "$Dir exists but is not a git clone. Move it aside or pass -Dir."
  }
  Step "Cloning $Branch into $Dir"
  Run git @('clone', '--branch', $Branch, $RepoUrl, $Dir)
} else {
  Step "Updating $Dir to origin/$Branch"
  $dirty = & git -C $Dir status --porcelain --untracked-files=no
  if ($dirty) {
    throw "You have uncommitted changes in $Dir. Commit or stash them, then run this again:`n$dirty"
  }
  Run git @('-C', $Dir, 'fetch', 'origin', $Branch)
  $current = (& git -C $Dir rev-parse --abbrev-ref HEAD).Trim()
  if ($current -ne $Branch) {
    & git -C $Dir show-ref --verify --quiet "refs/heads/$Branch"
    if ($LASTEXITCODE -eq 0) { Run git @('-C', $Dir, 'checkout', $Branch) }
    else { Run git @('-C', $Dir, 'checkout', '-b', $Branch, "origin/$Branch") }
  }
  # Fast-forward only: never rewrites or merges local work.
  Run git @('-C', $Dir, 'merge', '--ff-only', "origin/$Branch")
}

# --- 2. First-run files ---------------------------------------------------------
$envFile = Join-Path $Dir '.env'
if (-not (Test-Path $envFile)) {
  Step "Creating .env from .env.example"
  Copy-Item (Join-Path $Dir '.env.example') $envFile
}
foreach ($d in 'data', 'logs') { New-Item -ItemType Directory -Force -Path (Join-Path $Dir $d) | Out-Null }
$firstRun = -not (Test-Path (Join-Path (Join-Path $Dir 'data') 'app.db'))

# --- 3. Build and start ---------------------------------------------------------
$rev = (& git -C $Dir rev-parse --short HEAD).Trim()
Step "Building and starting $Branch @ $rev (first build takes a while)"
Push-Location $Dir
try {
  Run docker @('compose', 'up', '-d', '--build')
} finally { Pop-Location }

# --- 4. Wait for health ---------------------------------------------------------
$port = '7000'
$portLine = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portLine) { $port = $portLine.Matches[0].Groups[1].Value }
Step "Waiting for http://localhost:$port/api/health ..."
$sw = [Diagnostics.Stopwatch]::StartNew()
$ok = $false
while ($sw.Elapsed.TotalSeconds -lt $HealthTimeoutSec) {
  try {
    $resp = Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
  Start-Sleep -Seconds 3
}
if (-not $ok) {
  throw "Containers started but /api/health did not answer within $HealthTimeoutSec s. Check: cd `"$Dir`"; docker compose logs odysseus --tail 100"
}

Write-Host ""
Write-Host "Odysseus $Branch @ $rev is running: http://localhost:$port" -ForegroundColor Green
if ($firstRun) {
  Write-Host "First start - your generated admin password:" -ForegroundColor Yellow
  Push-Location $Dir
  try {
    $logLines = @(& docker compose logs odysseus 2>$null)
    for ($i = 0; $i -lt $logLines.Count; $i++) {
      if ($logLines[$i] -match 'Initial admin user') {
        Write-Host $logLines[$i]
        if ($i + 1 -lt $logLines.Count) { Write-Host $logLines[$i + 1] }
      }
    }
  } finally { Pop-Location }
  Write-Host "Log in as 'admin', then change it under Settings -> Account." -ForegroundColor Yellow
}
