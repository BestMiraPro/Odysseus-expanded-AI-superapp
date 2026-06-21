# Deploying Odysseus cleanly (build-from-`main`)

The app is usually hot-patched by copying files into the running container. That's
fine for quick fixes on **one** branch, but the moment two branches exist
(e.g. `main` = study work, `dev` = a new feature) and you deploy by copying
whatever is checked out, the container becomes an inconsistent mix whose breakage
only shows up on the **next restart** (the running process masks it in memory).

`deploy-main.ps1` removes that trap: it always **builds the image from a dedicated
`main` worktree** and recreates only the `odysseus` container from that image,
**reusing your real data** — without touching your editing checkout.

## Usage

```powershell
# Deploy exactly what's on origin/main (fetch + reset the build tree first):
pwsh .\deploy-main.ps1 -Pull

# Deploy the build tree as-is (no fetch):
pwsh .\deploy-main.ps1

# Just show the resolved paths (build context + ./data mount), change nothing:
pwsh .\deploy-main.ps1 -DryRun
```

The script lives on `main`, so the canonical copy is always at
`<build-worktree>\deploy-main.ps1` after a `-Pull`.

## How it keeps your data

`docker-compose.yml` mounts `./data` and `./logs` **relative to the project
directory**. The script:

- **Builds** with the project directory = the `main` build worktree, so the build
  context (`build: .`) is `main`'s source.
- **Runs** with `--project-directory` = your canonical repo
  (`C:\Users\Dinis Mira~\odysseus` by default, auto-detected as git's primary
  worktree), so `./data`, `./logs`, and `.env` resolve to your **real** DB and
  settings, and `--no-build` reuses the image just built.

So the *build source* and the *runtime data* are decoupled: deploys no longer
depend on which branch is checked out.

## Promoting a feature

When a feature on `dev` (or any branch) is ready, **merge it into `main`** and
deploy with `-Pull`. Don't run features by copying files into the live container —
that's the workflow that caused the drift this script exists to prevent.

## Notes

- First build is a full image build (minutes); later builds reuse layer cache.
- Only the `odysseus` service is recreated; `chromadb`/`searxng`/`ntfy` keep
  running.
- A bash port is trivial if you deploy from Linux/macOS — the same two
  `docker compose` invocations apply.
