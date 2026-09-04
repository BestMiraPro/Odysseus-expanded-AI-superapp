<#
.SYNOPSIS
  Deploy Odysseus from a clean branch build - independent of whichever branch is
  checked out in your working tree. Defaults to `dev` (the combined branch with
  Study + Omnigent + mainline).

.DESCRIPTION
  Avoids the file-clobbering hot-patch trap: always builds the image from a
  dedicated build worktree checked out to origin/<Branch>, then recreates ONLY
  the `odysseus` container from that image - while keeping your real data.

  Build source and runtime data are decoupled: it builds with the project
  directory pointed at the build worktree (so `build: .` is the branch's source),
  but runs with --project-directory pointed at your canonical repo (so ./data,
  ./logs and .env resolve to your real DB and settings).

.PARAMETER Branch       Branch to build & deploy (default: dev).
.PARAMETER DataRepo     Repo dir whose ./data holds your real DB. Auto-detected
                        as git's PRIMARY worktree if omitted.
.PARAMETER BuildTree    Dedicated build worktree (detached HEAD on origin/<Branch>).
.PARAMETER CodeTools    Bind-mount your checkout at /app/code and point the Study
                        agent's code tools at it (docker-compose.code.yml), so
                        its edits land in a real file instead of the image's
                        baked copy. Python edits still need this restart.
.PARAMETER HealthTimeoutSec  Seconds to wait for /api/health after the container
                        is recreated (default 300). Startup grew past the old
                        60s ceiling, which made good deploys report failure.
.PARAMETER DryRun       Print the resolved compose config and stop.

.EXAMPLE
  pwsh ./deploy.ps1                 # build & deploy origin/dev onto your data
  pwsh ./deploy.ps1 -Branch main    # deploy a different branch
  pwsh ./deploy.ps1 -DryRun         # show resolved paths only, change nothing
  pwsh ./deploy.ps1 -CodeTools      # deploy with the Study agent's code tools on your checkout
#>
[CmdletBinding()]
param(
  [string]$Branch    = 'dev',
  [string]$DataRepo,
  [string]$BuildTree = (Join-Path $env:USERPROFILE '.odysseus-deploy'),
  [string]$Project   = 'odysseus',
  [string]$Service   = 'odysseus',
  [switch]$CodeTools,
  [int]$HealthTimeoutSec = 300,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$a) {
  Write-Host "    $exe $($a -join ' ')" -ForegroundColor DarkGray
  & $exe @a; if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($a -join ' ')" }
}

# --- 1. Build worktree on origin/<Branch> (detached, so it never clashes with a
#        branch checked out in your working tree) -------------------------------
$seed = if ($DataRepo) { $DataRepo } else { (Get-Location).Path }
if (Test-Path (Join-Path $BuildTree '.git')) {
  Step "Updating build worktree -> origin/$Branch"
  Run git @('-C', $BuildTree, 'fetch', 'origin', $Branch)
  Run git @('-C', $BuildTree, 'checkout', '--detach', "origin/$Branch")
  Run git @('-C', $BuildTree, 'reset', '--hard', "origin/$Branch")
} else {
  Step "Creating build worktree at $BuildTree on origin/$Branch"
  Run git @('-C', $seed, 'fetch', 'origin', $Branch)
  Run git @('-C', $seed, 'worktree', 'add', '--detach', $BuildTree, "origin/$Branch")
}

# --- 2. Resolve canonical data repo (git's PRIMARY worktree) --------------------
if (-not $DataRepo) {
  $first = (& git -C $BuildTree worktree list --porcelain |
            Select-String '^worktree ' | Select-Object -First 1).ToString()
  $DataRepo = $first -replace '^worktree\s+', ''
}
$DataRepo = (Resolve-Path $DataRepo).Path
if (-not (Test-Path (Join-Path $DataRepo 'data'))) {
  throw "No 'data' folder under $DataRepo - pass -DataRepo with your live deployment dir."
}
$composeFile = Join-Path $BuildTree 'docker-compose.yml'
# The overlay only adds a volume + env var, so it applies to the runtime
# invocations (project directory = your real repo), not to the image build.
$runFiles = @('-f', $composeFile)
if ($CodeTools) {
  $codeOverlay = Join-Path $BuildTree 'docker-compose.code.yml'
  if (-not (Test-Path $codeOverlay)) { throw "Missing $codeOverlay - is the branch too old?" }
  $runFiles += @('-f', $codeOverlay)
}
Step "Branch     : $Branch @ $(git -C $BuildTree rev-parse --short HEAD)"
Step "Data/DB    : $DataRepo"
Step "Build src  : $BuildTree"
if ($CodeTools) { Step "Code tools : ON - agent edits $DataRepo via /app/code" }

if ($DryRun) {
  Step "Resolved compose config (project-directory = $DataRepo):"
  Run docker (@('compose', '-p', $Project, '--project-directory', $DataRepo) + $runFiles + @('config'))
  Write-Host "Dry run only - nothing built or recreated." -ForegroundColor Yellow
  return
}

# --- 3. Build image from the branch (project dir = build worktree) --------------
Step "Building image '$Project-$Service' from $Branch ..."
Run docker @('compose', '-p', $Project, '-f', $composeFile, 'build', $Service)

# --- 4. Recreate the container on your real data --------------------------------
Step "Recreating '$Service' on your data ..."
Run docker (@('compose', '-p', $Project, '--project-directory', $DataRepo) +
            $runFiles + @('up', '-d', '--no-build', $Service))

# --- 5. Verify ------------------------------------------------------------------
Step "Waiting for health ..."
$port = if ($env:APP_PORT) { $env:APP_PORT } else { '7000' }
# Wall-clock budget rather than a loop count: the old 20 x 3s gave ~60s, which
# the image outgrew once the upstream sync pulled in heavier startup work
# (FastEmbed model load, vector store warm-up). The deploy then reported
# failure for a container that was in fact coming up fine.
$sw = [Diagnostics.Stopwatch]::StartNew()
$ok = $false
while ($sw.Elapsed.TotalSeconds -lt $HealthTimeoutSec) {
  try {
    $resp = Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
  Start-Sleep -Seconds 3
}
$took = [int]$sw.Elapsed.TotalSeconds
if ($ok) {
  Write-Host "Deployed. Health OK on :$port after ${took}s. Source = $Branch @ $(git -C $BuildTree rev-parse --short HEAD)." -ForegroundColor Green
} else {
  throw "Container recreated but /api/health did not return 200 within ${took}s. Raise -HealthTimeoutSec, or check: docker logs $Project-$Service-1"
}
