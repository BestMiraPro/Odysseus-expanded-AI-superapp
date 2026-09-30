#!/usr/bin/env bash
# Install or update Odysseus on this machine from the `main` branch and start it.
#
#   no clone yet -> clones the repo, creates .env from .env.example
#   clone exists -> fetches and fast-forwards to origin/$BRANCH
#                   (refuses if you have uncommitted changes)
#
# Then `docker compose up -d --build` (keeps ./data: DB, memories, documents)
# and waits for /api/health. Plain `docker compose` from the repo folder, same
# as the README Quick Start, so it updates the stack you already run.
#
#   ./deploy-main.sh                       # from a clone
#   curl -fsSL https://raw.githubusercontent.com/BestMiraPro/Odysseus-expanded-AI-superapp/main/deploy-main.sh | bash
#
# Env overrides: DIR (repo location), BRANCH (default main),
# HEALTH_TIMEOUT (seconds, default 600).
set -euo pipefail

REPO_URL="https://github.com/BestMiraPro/Odysseus-expanded-AI-superapp.git"
BRANCH="${BRANCH:-main}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-600}"

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. Prerequisites
command -v git >/dev/null || die "git is not installed."
command -v docker >/dev/null || die "docker is not installed."
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start it and run this again."
docker compose version >/dev/null 2>&1 || die "'docker compose' is not available. Update Docker."

# --- 1. Locate or clone the repo
if [ -z "${DIR:-}" ]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/docker-compose.yml" ] && [ -d "$here/.git" ]; then
    DIR="$here"
  else
    DIR="$HOME/Odysseus-expanded-AI-superapp"
  fi
fi

if [ ! -d "$DIR/.git" ]; then
  if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "$DIR exists but is not a git clone. Move it aside or set DIR=..."
  fi
  step "Cloning $BRANCH into $DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
else
  step "Updating $DIR to origin/$BRANCH"
  dirty="$(git -C "$DIR" status --porcelain --untracked-files=no)"
  [ -z "$dirty" ] || die "uncommitted changes in $DIR; commit or stash them first:
$dirty"
  git -C "$DIR" fetch origin "$BRANCH"
  if [ "$(git -C "$DIR" rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
    if git -C "$DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
      git -C "$DIR" checkout "$BRANCH"
    else
      git -C "$DIR" checkout -b "$BRANCH" "origin/$BRANCH"
    fi
  fi
  # Fast-forward only: never rewrites or merges local work.
  git -C "$DIR" merge --ff-only "origin/$BRANCH"
fi

# --- 2. First-run files
[ -f "$DIR/.env" ] || { step "Creating .env from .env.example"; cp "$DIR/.env.example" "$DIR/.env"; }
mkdir -p "$DIR/data" "$DIR/logs"
first_run=0; [ -f "$DIR/data/app.db" ] || first_run=1

# --- 3. Build and start
rev="$(git -C "$DIR" rev-parse --short HEAD)"
step "Building and starting $BRANCH @ $rev (first build takes a while)"
(cd "$DIR" && docker compose up -d --build)

# --- 4. Wait for health
port="$(sed -n 's/^[[:space:]]*APP_PORT[[:space:]]*=[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$DIR/.env" | head -n1)"
port="${port:-7000}"
step "Waiting for http://localhost:$port/api/health ..."
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
until curl -fsS -o /dev/null "http://127.0.0.1:$port/api/health" 2>/dev/null; do
  [ "$(date +%s)" -lt "$deadline" ] || die "containers started but /api/health did not answer within ${HEALTH_TIMEOUT}s. Check: cd \"$DIR\" && docker compose logs odysseus --tail 100"
  sleep 3
done

printf '\n\033[32mOdysseus %s @ %s is running: http://localhost:%s\033[0m\n' "$BRANCH" "$rev" "$port"
if [ "$first_run" = 1 ]; then
  printf '\033[33mFirst start - your generated admin password:\033[0m\n'
  (cd "$DIR" && docker compose logs odysseus 2>/dev/null | grep -A1 "Initial admin user" || true)
  printf "\033[33mLog in as 'admin', then change it under Settings -> Account.\033[0m\n"
fi
