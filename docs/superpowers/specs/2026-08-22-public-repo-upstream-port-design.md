# Design: Port Study + Omnigent onto current upstream, and publish the fork

**Date:** 2026-08-22
**Status:** Approved
**Scope:** Rebase 44 commits of local work onto the latest upstream Odysseus, verify it runs, then make the repository public as a CV-facing artifact.

---

## 1. Context

`BestMiraPro/Odysseus-expanded-AI-superapp` is a private fork of
[`pewdiepie-archdaemon/odysseus`](https://github.com/pewdiepie-archdaemon/odysseus)
(AGPL-3.0). It carries 44 commits of original work on top of an upstream
snapshot from 2026-06-12. Upstream `dev` has since advanced to 2026-08-20.

The commit range to be replayed, `9d7a3d6..dev`, holds **58 commits**: the 44
authored locally plus **14 by `pewdiepie-archdaemon`** that arrived through the
merge `1999025c Merge remote-tracking branch 'origin/dev' into study-into-dev`.
Those 14 are upstream's own June work and are already present in current
`upstream/dev` under different SHAs, so they are expected to drop out of the
rebase as empty commits.

### Local work being preserved

| Feature | Shape | Approx. size |
|---|---|---|
| **Study** — spaced repetition (FSRS), PDF/vision extraction, LaTeX rendering, multi-part question grouping, consult-materials, answer history | mostly new files | ~7,000 lines |
| **Omnigent** — agent bridge, native crew runs, per-model sub-agents, one-click launcher | mostly new files | ~1,000 lines |
| **Google sign-in** — OAuth login, long-lived sessions, `X-Forwarded-Proto` trust behind a tunnel | edits to existing files | ~150 lines |
| **`deploy.ps1`** — clean branch-build Docker deploy that decouples build source from runtime data | new file | ~120 lines |

Total delta `9d7a3d6..dev`: **62 files, +12,856 / −144**. Of that, **23 new
non-test files** carry over untouched; only **~1,500 lines** modify existing
upstream files. That ratio is what makes this port tractable.

Because the range includes upstream's 14 commits, that diff **overstates** the
local delta. Eleven of the files it touches carry no local change at all and are
dropped wholesale in Tier 4 rather than hand-merged.

---

## 2. Key discovery: upstream rewrote its history

`git merge-base dev upstream/dev` resolves to a commit only 16 from the root,
implying the fork diverged at the very beginning. It did not. Upstream
force-pushed a rewritten history, so shared commits have new SHAs.

Evidence — the fork point and upstream's equivalent commit have byte-identical trees:

```
ours   9d7a3d6  tree 5536ae71339c4f5b910c252657217f1867b5af15
theirs 6acc4277  tree 5536ae71339c4f5b910c252657217f1867b5af15
git diff --name-only 9d7a3d6 6acc4277  ->  0 files
```

**Consequence:** `git merge upstream/dev` would attempt to reconcile 1,192
"local" commits (most of which are actually upstream's own June work under new
SHAs) against 2,057 upstream commits, producing thousands of phantom conflicts
in code nobody here touched.

**Consequence, exploited:** because the trees are identical,
`git rebase --onto upstream/dev 9d7a3d6 dev` replays the 58 commits of the range
onto current upstream — the 44 local ones land, and the 14 upstream ones drop out
as empty. Conflicts arise only in files both sides edited, and the incremental
commit history is preserved.

### Approaches rejected

- **Merge upstream into the fork.** Rejected: the phantom-conflict explosion above.
- **Fresh upstream clone, drop files in as one commit.** Works, but collapses 44
  commits into one, discarding the development history that demonstrates process.

---

## 3. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Repo shape | Publish the fork with a **new README that leads with the local work**; upstream's README moves to `README-upstream.md` | A recruiter opening a 1,208-commit fork cannot otherwise tell which 44 commits are the author's |
| Port base | **`upstream/dev`** @ `b4d12932` | Matches the branch the fork is already based on, so the rebase has the analyzed shape |
| Verification bar | **Must actually run** on the live Docker deployment | Tests alone would not catch Study/Omnigent UI breakage |
| License | **Stays AGPL-3.0**, upstream attribution intact | Required by the license; `ACKNOWLEDGMENTS.md` already handles credit well |
| Sequence | **Port first, publish second** | Avoids publishing a 2-month-stale tree and then force-pushing a rebase over it |
| `Co-Authored-By: Claude` trailers (38 of 44 commits) | **Keep** | Stripping them to hide AI assistance would misrepresent the work; effective AI-assisted delivery of a 13k-line feature is a strength worth stating plainly in the README |
| `user.name` for new commits | **`Dinis Mira`**, set repo-local (global stays `BestMiraPro`) | Was `Study Upgrade Crew`. The email `dinas.m.mira@gmail.com` is already correct, so contributions link to the GitHub profile |

---

## 4. Goals / Non-goals

**Goals**

- Study, Omnigent, Google sign-in and `deploy.ps1` run on current upstream `dev`.
- Commit-by-commit history of the local work survives the port.
- Repository is public, AGPL-compliant, and legible as a portfolio piece.
- Public CI is green or disabled — not a wall of red X's.

**Non-goals**

- Extracting Study into a standalone repo (considered, deferred — it will not run
  without Odysseus internals).
- Contributing any of this upstream.
- Refactoring Study or Omnigent beyond what the port requires.
- Rebasing onto `upstream/main`; may be evaluated separately later.

---

## 5. Phase 1 — Port

Create `study-omnigent` from `upstream/dev`, replaying `9d7a3d6..dev` via
`git rebase --onto`. The existing `dev` branch is left untouched as a fallback
until the port passes verification.

Conflicts are resolved in ascending order of difficulty, so the easy tiers are
settled and committed before judgment-heavy files are touched.

### Tier 0 — no conflict possible (23 files)

`src/study_ai.py`, `src/study_plan.py`, `src/study_vision.py`, `src/fsrs.py`,
`routes/study_routes.py`, `static/js/study.js`, `src/omnigent_manager.py`,
`src/omnigent_native.py`, `routes/omnigent_routes.py`, `static/js/omnigent.js`,
`integrations/omnigent/**`, `deploy.ps1`, `DEPLOY.md`, and the `docs/superpowers/`
specs and plans. These do not exist upstream; they apply verbatim.

### Tier 1 — low (append-only or 8-line-or-smaller edits)

`.gitignore`, `requirements.txt`, `docker-compose.yml`, `Dockerfile`,
`docker/entrypoint.sh`, `core/auth.py`, `src/session_search.py`,
`static/js/modalManager.js`.

### Tier 2 — medium (additive, but upstream churned nearby)

| File | Local change | Upstream churn |
|---|---|---|
| `core/database.py` | +215 (new Study tables) | 610 |
| `static/style.css` | +625 (appended block) | 6,420 |
| `app.py` | +47 (blueprint registration) | 372 |
| `static/js/settings.js` | 44 | 1,633 |
| `src/document_processor.py` | 37 | 68 |
| `static/login.html` | +32 | 70 |
| `static/app.js` | +31 | 927 |
| `src/settings.py` | +10 | 81 |
| `routes/upload_routes.py` | 8 | 319 |

### Tier 3 — high (real merge judgment)

- **`static/index.html`** — local change is 168 lines inserting Study/Omnigent
  nav entries and panels; upstream rewrote 938. Hook points confirmed to still
  exist (`nav-item` / `data-panel` present in upstream `static/index.html`).
- **`routes/auth_routes.py`** — local +142 for Google sign-in against 177 lines
  of upstream churn. Confirmed upstream has **not** added app-level Google
  sign-in (its OAuth code is for Copilot, ChatGPT subscription, device flow and
  email), so this remains original work rather than a duplicate.
- **`src/llm_core.py`** — local 53 lines (`8f0b0f3` empty-content reasoning
  fallback) against upstream +1,920. Upstream `2fb3a316 Hide untagged reasoning
  dumps in chat` may supersede it; if upstream now handles empty `content` with
  a populated `reasoning` field, drop the local fix rather than force-fit it.

### Tier 4 — drop, taking upstream's current version (11 files)

Ten of these were touched **only** by the 14 upstream commits in the range and
carry no local change whatsoever:

`LICENSE`, `README.md`, `routes/api_token_routes.py`, `routes/codex_routes.py`,
`static/js/admin.js`, `static/js/cookbookServe.js`,
`integrations/claude/skills/odysseus/SKILL.md`,
`integrations/claude/skills/odysseus/scripts/odysseus_api.py`,
`integrations/codex/skills/odysseus/SKILL.md`,
`integrations/codex/scripts/odysseus_api.py`.

The eleventh, **`src/tool_implementations.py`**, carries a −3-line removal of the
`context_after` block in `do_search_chats` that came from the merge `79d6806`
rather than from a deliberate decision. Upstream deleted 4,103 lines from this
file in a module refactor, so the removal is moot either way.

`static/index.html` was touched by both sides and therefore stays in Tier 3.

Each drop is recorded in `docs/PORT-NOTES.md` with its reason.

---

## 6. Phase 2 — Verification ladder

Each rung must pass before the next is attempted. A failure sends work back to
the relevant tier in Phase 1 rather than forward.

1. **`pytest`** — the 30+ Study/Omnigent test files are the primary safety net.
   Upstream's own suite must also stay green; a regression there means the port
   damaged upstream behaviour.
2. **`docker compose build`** — catches `requirements.txt` and `Dockerfile`
   integration problems.
3. **Deploy** via `deploy.ps1` onto the existing data volume.
4. **Live smoke test:**
   - Study: upload material → extract → practice → consult → history
   - Omnigent: launch → crew run with an API model
   - Google sign-in end-to-end
   - One upstream feature untouched by this work (chat) to confirm no collateral damage

No success is claimed for any rung without the command output to back it.

---

## 7. Phase 3 — Publish

1. **`README.md` rewrite.** Leads with Study and Omnigent: what they do, why,
   architecture, screenshots, how to run. Then a clearly marked fork-attribution
   section pointing at upstream, and an explicit statement of AGPL-3.0
   obligations. Upstream's README is preserved as `README-upstream.md` and linked.
2. **Scrub local paths.** `C:\Users\Dinis Mira~\...` appears in
   `docs/superpowers/plans/`. Replace with repo-relative paths.
3. **License untouched.** `LICENSE` (AGPL-3.0) and `ACKNOWLEDGMENTS.md` stay as-is.
4. **CI triage.** Ten workflows begin running once public:
   `ci.yml`, `codeql.yml`, `container-scan.yml`, `container-trivy.yml`,
   `dependency-review.yml`, `docker-publish.yml`, `issue-description-check.yml`,
   `pr-description-check.yml`, `secret-scan.yml`, `workflow-security.yml`.
   `docker-publish.yml` will attempt registry pushes it has no credentials for.
   Each is either repointed to this namespace or disabled; a portfolio repo
   covered in failing checks is worse than one with no CI.
5. **Visibility flip is the author's action.** No `gh` CLI or GitHub credentials
   exist in this environment, and the change is outward-facing and effectively
   irreversible in terms of exposure. Exact steps are handed over instead.

### Publish-readiness scan (completed 2026-08-22)

- No secret patterns in the tracked working tree.
- No secret patterns introduced across the 44 local commits.
- No `.env`, `.pem`, `.p12`, `id_rsa`, `client_secret*`, `credentials.json`,
  database or PDF file added anywhere in all 1,208 commits.
- Google OAuth is entirely env-var driven (`GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`); no credentials in source.
- Local `dev` is identical to `origin/dev`; nothing unpushed.
- Only finding: local Windows paths in design docs (cosmetic, addressed above).

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| `static/index.html` conflict is unresolvable line-by-line | Re-apply the Study/Omnigent panel insertion by hand against upstream's new structure rather than fighting the 3-way merge |
| Upstream changed the Study module's DB or settings contracts | Caught at rung 1; Study tests are specific enough to localise the break |
| Rebase goes wrong midway | `dev` is untouched throughout; `git rebase --abort`, or reset `study-omnigent` and restart from a lower tier |
| Live deployment broken by a bad build | `deploy.ps1` builds from a detached worktree and only recreates the container; the data volume is not touched, and the previous image remains available for rollback |
| Publishing exposes something the scan missed | `secret-scan.yml` runs on the public repo as an independent check; the repo can be re-privatised, though exposure is not undone |

---

## 9. Success criteria

- `study-omnigent` sits directly on `upstream/dev` @ `b4d12932` with the local
  commits replayed and their authorship intact.
- Full test suite green; image builds; Study, Omnigent and Google sign-in all
  verified working in the running container.
- Every dropped change documented with the upstream commit that superseded it.
- Repository public, AGPL-3.0, upstream credited, README leading with the
  author's own work, CI not failing.
