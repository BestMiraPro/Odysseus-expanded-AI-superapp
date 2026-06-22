# Deploying Odysseus cleanly (build-from-branch)

The app is usually hot-patched by copying files into the running container. That's
fine for quick fixes on **one** branch, but the moment two branches diverge (e.g.
`main` = Study work, `dev` = a new feature) and you deploy by copying whatever is
checked out, the container becomes an inconsistent mix whose breakage only shows
up on the **next restart**.

`deploy.ps1` removes that trap: it always **builds the image from a dedicated
build worktree checked out to `origin/<Branch>`** and recreates only the
`odysseus` container from that image, **reusing your real data** — without
touching your editing checkout.

## Usage

```powershell
# Build & deploy origin/dev (the combined branch: Study + Omnigent + mainline):
pwsh .\deploy.ps1

# Deploy a different branch:
pwsh .\deploy.ps1 -Branch main

# Show the resolved paths (build context + ./data mount), change nothing:
pwsh .\deploy.ps1 -DryRun
```

`dev` is the default and the canonical "everything" branch. Push your work to
`dev`, then `deploy.ps1` rebuilds from it.

## How it keeps your data

`docker-compose.yml` mounts `./data` and `./logs` **relative to the project
directory**. The script:

- **Builds** with the project directory = the build worktree (detached on
  `origin/<Branch>`), so the build context (`build: .`) is that branch's source.
- **Runs** with `--project-directory` = your canonical repo (auto-detected as
  git's primary worktree), so `./data`, `./logs`, and `.env` resolve to your
  **real** DB and settings, and `--no-build` reuses the image just built.

The build worktree uses a **detached HEAD**, so it never clashes with the same
branch being checked out in your working tree.

## Notes

- First build is a full image build (minutes); later builds reuse layer cache.
- Only the `odysseus` service is recreated; `chromadb`/`searxng`/`ntfy` keep
  running, and your data is untouched.
- A bash port is trivial (the same two `docker compose` invocations).
