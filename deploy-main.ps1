<#
.SYNOPSIS
  Deploy Odysseus from a clean `main` build — independent of whichever branch is
  checked out in your working tree.

.DESCRIPTION
  The mess this avoids: the app is normally hot-patched by copying files into the
  running container. With two branches in play (e.g. `main` = study, `dev` = a new
  feature), copying from whatever happens to be checked out leaves the container a
  half-and-half Frankenstein whose breakage only surfaces on the next restart.

  This script instead always builds the image from a dedicated `main` worktree and
  recreates ONLY the `odysseus` container from that image — while keeping your real
  data. Your editing checkout (which may be on `dev`) is never touched.

  How the data is preserved: docker-compose.yml mounts `./data` and `./logs`
  RELATIVE to the project directory. We build with the project directory pointed at
  the build worktree (so the build context is `main`'s source), but run with the
  project directory pointed at your canonical repo (so `./data` resolves to your
  real DB). Build source and runtime data are thus decoupled.

.PARAMETER DataRepo
  The repo dir whose ./data + ./logs hold your real DB & files (where you normally
  run docker compose). Defaults to git's PRIMARY worktree, auto-detected.

.PARAMETER BuildTree
  A dedicated `main` worktree used only as the clean build source.

.PARAMETER Pull
  Fetch origin and hard-reset the build worktree to origin/<Branch> before building
  (deploy exactly what's on the remote).

.PARAMETER DryRun
  Print the fully-resolved compose config (so you can confirm the build context and
  the ./data mount resolve where you expect) and stop — builds and recreates nothing.

.EXAMPLE
  pwsh ./deploy-main.ps1 -Pull
  # fetch origin/main, rebuild the image from it, recreate the container on your data.

.EXAMPLE
  pwsh ./deploy-main.ps1 -DryRun
  # show resolved paths only — safe, changes nothing.
#>
[CmdletBinding()]
param(
  [string]$DataRepo,
  [string]$BuildTree = (Join-Path $env:USERPROFILE '.odysseus-deploy-main'),
  [string]$Branch    = 'main',
  [string]$Project   = 'odysseus',
  [string]$Service   = 'odysseus',
  [switch]$Pull,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Step($msg)        { Write-Host "==> $msg" -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$a) {
  Write-Host "    $exe $($a -join ' ')" -ForegroundColor DarkGray
  & $exe @a
  if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($a -join ' ')" }
}

# --- 1. Ensure the build worktree exists and is on the target branch -----------
if (Test-Path (Join-Path $BuildTree '.git')) {
  Step "Build worktree: $BuildTree"
  if ($Pull) {
    Run git @('-C', $BuildTree, 'fetch', 'origin', $Branch)
    Run git @('-C', $BuildTree, 'reset', '--hard', "origin/$Branch")
  }
} else {
  Step "Creating build worktree at $BuildTree (branch $Branch)"
  # `git worktree add` works from any existing worktree; use DataRepo if known,
  # else the current dir.
  $from = if ($DataRepo) { $DataRepo } else { (Get-Location).Path }
  if ($Pull) { Run git @('-C', $from, 'fetch', 'origin', $Branch) }
  Run git @('-C', $from, 'worktree', 'add', $BuildTree, $Branch)
}

# --- 2. Resolve the canonical data repo (git's PRIMARY worktree) ---------------
if (-not $DataRepo) {
  $first = (& git -C $BuildTree worktree list --porcelain |
            Select-String '^worktree ' | Select-Object -First 1).ToString()
  $DataRepo = $first -replace '^worktree\s+', ''
}
$DataRepo = (Resolve-Path $DataRepo).Path
if (-not (Test-Path (Join-Path $DataRepo 'data'))) {
  throw "No 'data' folder under $DataRepo — that doesn't look like your live deployment dir. Pass -DataRepo explicitly."
}
$composeFile = Join-Path $BuildTree 'docker-compose.yml'
Step "Data/DB stays in: $DataRepo"
Step "Build source     : $BuildTree (HEAD $(git -C $BuildTree rev-parse --short HEAD))"

# --- 3. Dry run: show resolved config and stop ---------------------------------
if ($DryRun) {
  Step "Resolved compose config (project-directory = $DataRepo):"
  Run docker @('compose', '-p', $Project, '--project-directory', $DataRepo, '-f', $composeFile, 'config')
  Write-Host "Dry run only — nothing built or recreated." -ForegroundColor Yellow
  return
}

# --- 4. Build the image from `main` --------------------------------------------
# No --project-directory here: the project directory defaults to the compose
# file's dir (the build worktree), so `build: .` uses MAIN's source.
Step "Building image '$Project-$Service' from $Branch ..."
Run docker @('compose', '-p', $Project, '-f', $composeFile, 'build', $Service)

# --- 5. Recreate the container on your real data -------------------------------
# --project-directory = your canonical repo, so ./data + ./logs + .env resolve to
# your real files. --no-build reuses the image we just built.
Step "Recreating '$Service' container on your data ..."
Run docker @('compose', '-p', $Project, '--project-directory', $DataRepo,
             '-f', $composeFile, 'up', '-d', '--no-build', $Service)

# --- 6. Verify -----------------------------------------------------------------
Step "Waiting for health ..."
$port = if ($env:APP_PORT) { $env:APP_PORT } else { '7000' }
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { Start-Sleep -Seconds 3 }
}
if ($ok) {
  Write-Host "Deployed. Health OK on :$port. Study source = $Branch @ $(git -C $BuildTree rev-parse --short HEAD)." -ForegroundColor Green
} else {
  throw "Container recreated but /api/health did not return 200. Check: docker logs $Project-$Service-1"
}
