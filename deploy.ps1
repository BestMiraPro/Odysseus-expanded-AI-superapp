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
.PARAMETER DryRun       Print the resolved compose config and stop.

.EXAMPLE
  pwsh ./deploy.ps1                 # build & deploy origin/dev onto your data
  pwsh ./deploy.ps1 -Branch main    # deploy a different branch
  pwsh ./deploy.ps1 -DryRun         # show resolved paths only, change nothing
#>
[CmdletBinding()]
param(
  [string]$Branch    = 'dev',
  [string]$DataRepo,
  [string]$BuildTree = (Join-Path $env:USERPROFILE '.odysseus-deploy'),
  [string]$Project   = 'odysseus',
  [string]$Service   = 'odysseus',
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
Step "Branch     : $Branch @ $(git -C $BuildTree rev-parse --short HEAD)"
Step "Data/DB    : $DataRepo"
Step "Build src  : $BuildTree"

if ($DryRun) {
  Step "Resolved compose config (project-directory = $DataRepo):"
  Run docker @('compose', '-p', $Project, '--project-directory', $DataRepo, '-f', $composeFile, 'config')
  Write-Host "Dry run only - nothing built or recreated." -ForegroundColor Yellow
  return
}

# --- 3. Build image from the branch (project dir = build worktree) --------------
Step "Building image '$Project-$Service' from $Branch ..."
Run docker @('compose', '-p', $Project, '-f', $composeFile, 'build', $Service)

# --- 4. Recreate the container on your real data --------------------------------
Step "Recreating '$Service' on your data ..."
Run docker @('compose', '-p', $Project, '--project-directory', $DataRepo,
             '-f', $composeFile, 'up', '-d', '--no-build', $Service)

# --- 5. Verify ------------------------------------------------------------------
Step "Waiting for health ..."
$port = if ($env:APP_PORT) { $env:APP_PORT } else { '7000' }
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  try { if ((Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) { $ok = $true; break } }
  catch { Start-Sleep -Seconds 3 }
}
if ($ok) {
  Write-Host "Deployed. Health OK on :$port. Source = $Branch @ $(git -C $BuildTree rev-parse --short HEAD)." -ForegroundColor Green
} else {
  throw "Container recreated but /api/health did not return 200. Check: docker logs $Project-$Service-1"
}
