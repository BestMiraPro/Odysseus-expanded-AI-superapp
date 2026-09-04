# Upstream Port + Public Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay the local Study / Omnigent / Google-auth work onto current upstream Odysseus `dev`, prove it runs on the live Docker deployment, then prepare the repository to be made public as a portfolio artifact.

**Architecture:** Upstream rewrote its history, so a merge is impossible but a *graft rebase* is exact — the fork point `9d7a3d6` and upstream's `6acc4277` have identical trees. The port runs in two stages: a **squash port** on a scratch branch resolves every conflict once and produces a known-good target tree, then the **real rebase** replays commit history with `git rerere` reusing those resolutions. Both must yield an identical tree — that equality is the correctness proof.

**Tech Stack:** git (`rebase --onto`, `rerere`), Python in Docker (pytest, 529 test files), Docker Compose, Flask, PowerShell (`deploy.ps1`).

---

## Critical context for the engineer

**You cannot run tests on the host.** Host Python 3.14 has pytest but no Flask and no app dependency. Everything runs inside a Docker image.

**The test suite is not green even on clean upstream.** `.github/workflows/ci.yml` marks pytest `continue-on-error: true`, citing known flaky / environment-dependent failures. **Task 0.3 records a baseline; every later comparison is against that baseline, never against zero.** "Tests pass" without a baseline comparison is a meaningless claim here.

**The commit range is 58 commits, not 44.** `9d7a3d6..dev` contains 44 commits by `BestMiraPro` plus **14 by `pewdiepie-archdaemon`** that arrived through the merge `1999025c Merge remote-tracking branch 'origin/dev' into study-into-dev`. Those 14 are upstream's own June work and are *already present* in current `upstream/dev` under different SHAs. Expect them to drop out of the rebase as empty commits. This is correct behaviour, not data loss.

**Because of that, `git diff 9d7a3d6..dev` overstates the local delta.** Eleven files in that diff are pure upstream work carrying no local change at all. Task 1.6 drops them wholesale. Do not hand-merge them.

**The odysseus stack is currently down** (`odysseus-odysseus-1  Exited (137) 2 weeks ago`). Exit 137 is an OOM kill — if it recurs in Phase 3, raise Docker's memory limit before blaming the port.

**Never touch `dev`.** It is the rollback. All work happens on new branches.

**Repo root:** `C:/Users/Dinis Mira~/odysseus/.claude/worktrees/thirsty-torvalds-842164`
All commands assume you are in that directory. Scratch artifacts go in `_scratch/port/`, which is already covered by `.gitignore`.

---

## File Structure

No new modules. The plan moves existing files onto a new base and adds release documentation.

**Created:**
- `README.md` — rewritten, leads with Study and Omnigent
- `README-upstream.md` — upstream's README, preserved verbatim
- `docs/PORT-NOTES.md` — baseline, dropped changes, and verification results (committed on `study-omnigent` in Phase 2)

**Resolved during the port:** 20 files across Tiers 1–3.
**Dropped during the port:** 11 files (Tier 4).
**Applied untouched:** 23 new files (Tier 0).

---

# Phase 0 — Safety net and baseline

### Task 0.1: Freeze state, enable conflict reuse, verify preconditions

**Files:** none (git metadata only)

- [ ] **Step 1: Create the scratch directory and tag the pre-port state**

```bash
mkdir -p _scratch/port
git tag pre-port-2026-08-22 dev
git tag -l 'pre-port*'
```

Expected: `pre-port-2026-08-22`

- [ ] **Step 2: Enable rerere so each conflict is resolved once, not 58 times**

```bash
git config rerere.enabled true
git config rerere.autoupdate true
git config --get rerere.enabled
```

Expected: `true`

Without this, the rebase re-asks you to resolve the same `static/style.css` conflict on every commit that touches it.

- [ ] **Step 3: Confirm the commit identity for new commits**

```bash
git config user.name; git config user.email
```

Expected: `Dinis Mira` and `dinas.m.mira@gmail.com`. If the name differs, set it repo-locally:

```bash
git config user.name "Dinis Mira"
```

- [ ] **Step 4: Confirm upstream is fetched and at the expected commit**

```bash
git fetch upstream dev
git log --oneline -1 upstream/dev
```

Expected: `b4d12932 fix(agent): drop the empty assistant turn from an approved-action replay (#6124)`

If the SHA differs, upstream has moved. Record the new SHA and substitute it everywhere `b4d12932` appears below.

- [ ] **Step 5: Re-confirm the graft point — this gates the entire plan**

```bash
git diff --name-only 9d7a3d6 6acc4277 | wc -l
```

Expected: `0`

**If this is not 0, stop and report it.** Everything downstream depends on these two commits having identical trees.

- [ ] **Step 6: Confirm the range composition matches this plan's assumptions**

```bash
git rev-list --count 9d7a3d6..dev
git shortlog -sne 9d7a3d6..dev
```

Expected: `58`, split `44 BestMiraPro` / `14 pewdiepie-archdaemon`.

---

### Task 0.2: Build the clean upstream baseline image

**Files:** none (Docker only)

- [ ] **Step 1: Create a detached worktree on clean upstream**

```bash
git worktree add --detach ../port-baseline upstream/dev
git -C ../port-baseline log --oneline -1
```

Expected: the same `b4d12932` line as Task 0.1.

- [ ] **Step 2: Build a baseline image, tagged so it cannot be confused with the ported one**

```bash
docker build -t odysseus-baseline:upstream-dev ../port-baseline
```

Expected: a successful build.

If clean upstream fails to build, the fault is upstream's, not yours. Record the error in `_scratch/port/PORT-NOTES.draft.md` and stop — porting onto a broken base wastes the whole effort.

---

### Task 0.3: Record the baseline test result

**Files:**
- Create: `_scratch/port/PORT-NOTES.draft.md` (gitignored; becomes `docs/PORT-NOTES.md` in Phase 2)

- [ ] **Step 1: Run the full suite on clean upstream**

```bash
docker run --rm odysseus-baseline:upstream-dev python -m pytest -q > _scratch/port/baseline.txt 2>&1; tail -30 _scratch/port/baseline.txt
```

Expected: a summary line such as `N failed, M passed, K skipped`. Failures here are expected and normal.

- [ ] **Step 2: Extract the exact set of failing node IDs**

```bash
grep -E '^(FAILED|ERROR)' _scratch/port/baseline.txt | sort > _scratch/port/baseline-failures.txt
wc -l < _scratch/port/baseline-failures.txt
cat _scratch/port/baseline-failures.txt
```

- [ ] **Step 3: Start the port notes**

Create `_scratch/port/PORT-NOTES.draft.md`, pasting the real numbers and real failure list from Steps 1–2:

```markdown
# Port notes — Study/Omnigent onto upstream dev

## Baseline: clean upstream/dev @ b4d12932

Command: `docker run --rm odysseus-baseline:upstream-dev python -m pytest -q`

Result: <paste the summary line from Step 1>

Pre-existing failures on clean upstream (NOT caused by this port):

```
<paste the contents of _scratch/port/baseline-failures.txt>
```

Any failure outside this list after the port is a regression introduced by the port.

## Dropped local changes

| Change | File | Reason |
|---|---|---|

## Skipped commits during rebase

| Commit | Subject | Reason |
|---|---|---|

## Verification results
```

Nothing is committed in this task — the draft is gitignored on purpose, because `port-scratch` and `study-omnigent` branch from different bases and a committed notes file would show up as a spurious tree difference in Task 2.2.

---

# Phase 1 — Squash port on a scratch branch

The goal of this phase is not history. It is to resolve every conflict once, carefully, and reach a tree you have verified. Phase 2 reuses these resolutions.

### Task 1.1: Create the scratch branch and stage the full delta

- [ ] **Step 1: Branch from clean upstream**

```bash
git checkout -b port-scratch upstream/dev
git log --oneline -1
```

Expected: `b4d12932 ...`

- [ ] **Step 2: Apply the delta with 3-way merge**

```bash
git diff 9d7a3d6..dev | git apply --3way --whitespace=nowarn; echo "apply exit: $?"
git status --short | head -40
```

A non-zero exit is expected — it means conflicts were left in the tree. `UU` marks conflicted files, `A` cleanly added, `M` cleanly modified.

- [ ] **Step 3: Record which files conflicted**

```bash
git diff --name-only --diff-filter=U | tee _scratch/port/conflicted.txt; wc -l < _scratch/port/conflicted.txt
```

Tasks 1.3–1.5 walk the tiers. Any tier-listed file absent from this list applied cleanly and needs no work.

---

### Task 1.2: Tier 0 — verify the 23 new files landed untouched

**Files:** `src/study_ai.py`, `src/study_plan.py`, `src/study_vision.py`, `src/fsrs.py`, `routes/study_routes.py`, `static/js/study.js`, `src/omnigent_manager.py`, `src/omnigent_native.py`, `routes/omnigent_routes.py`, `static/js/omnigent.js`, `integrations/omnigent/**`, `deploy.ps1`, `DEPLOY.md`, `docs/superpowers/**`

These do not exist upstream and cannot conflict. This task proves it.

- [ ] **Step 1: Confirm each new file is byte-identical to the original**

```bash
for f in src/study_ai.py src/study_plan.py src/study_vision.py src/fsrs.py \
         routes/study_routes.py static/js/study.js src/omnigent_manager.py \
         src/omnigent_native.py routes/omnigent_routes.py static/js/omnigent.js \
         integrations/omnigent/config.yaml integrations/omnigent/tools/odysseus_api.py \
         deploy.ps1 DEPLOY.md; do
  if git diff --quiet dev -- "$f"; then echo "OK   $f"; else echo "DIFF $f"; fi
done
```

Expected: `OK` on every line. Any `DIFF` means `git apply` mangled a new file — restore it directly with `git checkout dev -- <file>`.

- [ ] **Step 2: Syntax-check the new Python and JS**

```bash
docker run --rm -v "$(pwd):/w" -w /w odysseus-baseline:upstream-dev \
  python -m py_compile src/study_ai.py src/study_plan.py src/study_vision.py \
  src/fsrs.py routes/study_routes.py src/omnigent_manager.py \
  src/omnigent_native.py routes/omnigent_routes.py && echo "PY OK"
node --check static/js/study.js && node --check static/js/omnigent.js && echo "JS OK"
```

Expected: `PY OK` then `JS OK`.

---

### Task 1.3: Tier 1 — resolve the low-risk files

**Files (only those appearing in `_scratch/port/conflicted.txt`):** `.gitignore`, `requirements.txt`, `docker-compose.yml`, `Dockerfile`, `docker/entrypoint.sh`, `core/auth.py`, `src/session_search.py`, `static/js/modalManager.js`

Every local change here is additive or under 8 lines. Rule for the whole tier: **keep both sides.**

- [ ] **Step 1: For each conflicted file, read the authoritative local change**

```bash
git diff 9d7a3d6..dev -- <file>
```

- [ ] **Step 2: Keep upstream's lines and the local addition, deleting only the marker lines**

Remove `<<<<<<<`, `=======`, `>>>>>>>` and nothing else.

For `requirements.txt`, if a dependency you added is now pinned by upstream at a different version, **take upstream's pin** — it is tested against the rest of upstream's tree — and note the change in the draft notes.

- [ ] **Step 3: Verify no markers survive**

```bash
grep -rn '^<<<<<<<\|^>>>>>>>\|^=======$' --include='*.py' --include='*.js' --include='*.yml' --include='*.txt' --include='*.sh' . | grep -v '^./_scratch/' | head
```

Expected: no output.

- [ ] **Step 4: Validate the config files parse**

```bash
docker compose -f docker-compose.yml config > /dev/null && echo "COMPOSE OK"
bash -n docker/entrypoint.sh && echo "ENTRYPOINT OK"
```

Expected: `COMPOSE OK` then `ENTRYPOINT OK`.

- [ ] **Step 5: Stage the tier**

```bash
git add .gitignore requirements.txt docker-compose.yml Dockerfile docker/entrypoint.sh \
        core/auth.py src/session_search.py static/js/modalManager.js 2>/dev/null
git diff --name-only --diff-filter=U
```

---

### Task 1.4: Tier 2 — the additive-but-churned files

**Files:** `core/database.py`, `static/style.css`, `app.py`, `static/js/settings.js`, `src/document_processor.py`, `static/login.html`, `static/app.js`, `src/settings.py`, `routes/upload_routes.py`

Rule: **re-apply the local addition at the equivalent place in upstream's new structure**, rather than trusting the merge's guess at line placement.

- [ ] **Step 1: `core/database.py` — re-add the Study tables**

```bash
git diff 9d7a3d6..dev -- core/database.py
```

The change is +215 lines of new table definitions. Find where upstream defines its tables now and add yours there, in upstream's current style. Then check nothing was lost:

```bash
echo "ported:   $(grep -c 'CREATE TABLE' core/database.py)"
echo "upstream: $(git show upstream/dev:core/database.py | grep -c 'CREATE TABLE')"
```

The ported count must exceed upstream's by exactly the number of Study tables added — never be lower.

- [ ] **Step 2: `static/style.css` — re-append the local block**

The local change is a +625-line block at the end; upstream churned 6,420 lines elsewhere. Take upstream's whole file, then append:

```bash
git checkout upstream/dev -- static/style.css
git diff 9d7a3d6..dev -- static/style.css | grep '^+' | grep -v '^+++' | sed 's/^+//' >> static/style.css
tail -5 static/style.css
```

Then read the appended region and delete any selector upstream already defines.

- [ ] **Step 3: `app.py` — re-register the blueprints**

```bash
git diff 9d7a3d6..dev -- app.py
```

The change is +47 lines registering the Study and Omnigent blueprints. Add them where upstream registers blueprints now, then verify:

```bash
grep -n 'study_routes\|omnigent_routes' app.py
```

Expected: import lines and `register_blueprint` calls for both.

- [ ] **Step 4: Resolve the remaining Tier 2 files identically**

For each of `static/js/settings.js`, `src/document_processor.py`, `static/login.html`, `static/app.js`, `src/settings.py`, `routes/upload_routes.py`: read `git diff 9d7a3d6..dev -- <file>`, find the equivalent location in upstream's current file, apply the change there, remove the markers.

- [ ] **Step 5: Syntax gate**

```bash
docker run --rm -v "$(pwd):/w" -w /w odysseus-baseline:upstream-dev \
  python -m py_compile app.py core/database.py src/settings.py \
  src/document_processor.py routes/upload_routes.py && echo "PY OK"
node --check static/app.js && node --check static/js/settings.js && echo "JS OK"
```

Expected: `PY OK` then `JS OK`.

- [ ] **Step 6: Stage the tier**

```bash
git add core/database.py static/style.css app.py static/js/settings.js \
        src/document_processor.py static/login.html static/app.js \
        src/settings.py routes/upload_routes.py
git diff --name-only --diff-filter=U
```

---

### Task 1.5: Tier 3 — the three files needing real judgment

**Files:** `static/index.html`, `routes/auth_routes.py`, `src/llm_core.py`

- [ ] **Step 1: `static/index.html` — rebuild the panel insertion by hand**

Do not try to win the 3-way merge; upstream rewrote 938 lines. Start from upstream's file:

```bash
git checkout upstream/dev -- static/index.html
git diff 9d7a3d6..dev -- static/index.html
```

The diff shows nav entries and panel containers for Study and Omnigent. Note that part of this diff is upstream's own June work (this file was touched by both sides) — **you only need the Study and Omnigent additions**; ignore hunks that touch Settings/Add-Models markup, which upstream has since superseded.

Upstream still uses `nav-item` and `data-panel`, so the hook points exist. Add the two entries following the markup pattern of upstream's neighbouring panels, then verify:

```bash
grep -n 'study\|omnigent' static/index.html | head -20
```

Expected: a nav entry and panel container for each, plus `<script>` tags loading `js/study.js` and `js/omnigent.js`.

- [ ] **Step 2: `routes/auth_routes.py` — re-apply Google sign-in**

```bash
git diff 9d7a3d6..dev -- routes/auth_routes.py
```

The +142 lines add Google OAuth start/callback routes, long-lived sessions, and `X-Forwarded-Proto` trust. Upstream has **not** added app-level Google sign-in (its OAuth code covers Copilot, ChatGPT subscription, device flow and email only), so none of this is duplicated — all of it must survive.

Re-apply against upstream's current file, then confirm the env-var contract holds and no credential was inlined:

```bash
grep -n 'GOOGLE_CLIENT_ID\|GOOGLE_CLIENT_SECRET\|X-Forwarded-Proto' routes/auth_routes.py
grep -nE 'GOCSPX-|\.apps\.googleusercontent\.com' routes/auth_routes.py; echo "secret scan exit: $? (1 = clean)"
```

Expected: the first shows `os.getenv` reads; the second prints nothing and reports exit 1.

- [ ] **Step 3: `src/llm_core.py` — decide whether the local fix is still needed**

Commit `8f0b0f3 fix(llm): fall back to reasoning fields when content is empty` added 53 lines. Upstream added 1,920 and shipped `2fb3a316 Hide untagged reasoning dumps in chat`, which may already cover it.

```bash
git show 8f0b0f3 -- src/llm_core.py
git show 2fb3a316 --stat
git log --oneline upstream/dev --grep='reasoning' -i | head
```

Decision rule: read upstream's current handling of a response whose `content` is empty but whose `reasoning` field is populated.
- **If upstream now falls back to `reasoning`** → drop the local change, record it in the draft notes citing `2fb3a316`.
- **If it does not** → re-apply the fix. This is the exact failure mode seen with W&B-hosted Kimi and GLM models, and it will recur.

- [ ] **Step 4: Syntax gate**

```bash
docker run --rm -v "$(pwd):/w" -w /w odysseus-baseline:upstream-dev \
  python -m py_compile routes/auth_routes.py src/llm_core.py && echo "PY OK"
python -c "import html.parser; p=html.parser.HTMLParser(); p.feed(open('static/index.html',encoding='utf-8').read()); print('HTML PARSED')"
```

Expected: `PY OK` then `HTML PARSED`.

- [ ] **Step 5: Stage the tier**

```bash
git add static/index.html routes/auth_routes.py src/llm_core.py
git diff --name-only --diff-filter=U
```

---

### Task 1.6: Tier 4 — drop the eleven files carrying no local change

Ten of these were touched **only** by the 14 upstream commits in the range and contain no local work at all. The eleventh, `src/tool_implementations.py`, carries an accidental removal from the merge `79d6806` that upstream's own refactor has since made moot. All eleven take upstream's current version.

**Files:** `LICENSE`, `README.md`, `routes/api_token_routes.py`, `routes/codex_routes.py`, `static/js/admin.js`, `static/js/cookbookServe.js`, `integrations/claude/skills/odysseus/SKILL.md`, `integrations/claude/skills/odysseus/scripts/odysseus_api.py`, `integrations/codex/skills/odysseus/SKILL.md`, `integrations/codex/scripts/odysseus_api.py`, `src/tool_implementations.py`

- [ ] **Step 1: Confirm the ten are genuinely upstream-only before dropping them**

```bash
git log --perl-regexp --author='^((?!BestMiraPro).*)$' --format='' --name-only 9d7a3d6..dev | sort -u | grep -v '^$' > _scratch/port/upstream-only.txt
git log --author='dinas.m.mira' --format='' --name-only 9d7a3d6..dev | sort -u | grep -v '^$' > _scratch/port/mine.txt
comm -23 _scratch/port/upstream-only.txt _scratch/port/mine.txt
```

Expected: exactly the ten files listed above (`static/index.html` is correctly absent — both sides touched it, and it was handled in Task 1.5).

- [ ] **Step 2: Verify the `tool_implementations.py` removal is moot**

```bash
git diff 9d7a3d6..dev -- src/tool_implementations.py
grep -rn 'context_after' src/ routes/ | head
```

The local diff removes a `result.context_after` block from `do_search_chats`. If `context_after` no longer appears anywhere in `src/` or `routes/` — upstream deleted 4,103 lines from this file in a module refactor — the removal is already achieved and the change is moot.

If `context_after` **does** still exist elsewhere in upstream's tree, keep upstream's behaviour anyway: the removal was a merge accident, not a decision.

- [ ] **Step 3: Take upstream's version of all eleven**

```bash
git checkout upstream/dev -- \
  LICENSE README.md routes/api_token_routes.py routes/codex_routes.py \
  static/js/admin.js static/js/cookbookServe.js src/tool_implementations.py \
  integrations/claude/skills/odysseus/SKILL.md \
  integrations/claude/skills/odysseus/scripts/odysseus_api.py \
  integrations/codex/skills/odysseus/SKILL.md \
  integrations/codex/scripts/odysseus_api.py
git status --short | grep -E 'LICENSE|README|admin.js|codex_routes'
```

- [ ] **Step 4: Record the drops**

Add these rows to the "Dropped local changes" table in `_scratch/port/PORT-NOTES.draft.md`:

```markdown
| Change | File | Reason |
|---|---|---|
| all (10 files) | `LICENSE`, `README.md`, `routes/api_token_routes.py`, `routes/codex_routes.py`, `static/js/admin.js`, `static/js/cookbookServe.js`, `integrations/{claude,codex}/**` | Touched only by the 14 upstream commits in the range; already present in upstream/dev |
| removal of `context_after` block in `do_search_chats` | `src/tool_implementations.py` | Accidental clobber from merge 79d6806; upstream's module refactor removed the file section entirely |
```

- [ ] **Step 5: Confirm the tree is fully resolved**

```bash
echo "unresolved: $(git diff --name-only --diff-filter=U | wc -l)"
```

Expected: `unresolved: 0`

---

### Task 1.7: Verify and commit the scratch port

- [ ] **Step 1: Global conflict-marker sweep**

```bash
grep -rn '^<<<<<<<\|^>>>>>>>\|^=======$' --include='*.py' --include='*.js' --include='*.html' --include='*.css' --include='*.yml' --include='*.txt' --include='*.sh' . | grep -v '^./_scratch/' | head
```

Expected: no output.

- [ ] **Step 2: Commit the squashed port**

```bash
git add -A
git commit -m "port: Study + Omnigent + Google auth onto upstream dev b4d12932 (squashed)"
git log --oneline -1
```

- [ ] **Step 3: Build the ported image**

```bash
docker build -t odysseus-port:scratch .
```

A failure here is almost always `requirements.txt` — compare against upstream's pins.

- [ ] **Step 4: Run the Study and Omnigent tests first for fast signal**

```bash
docker run --rm odysseus-port:scratch python -m pytest -q \
  tests/test_study_ai_helpers.py tests/test_study_plan_generator.py \
  tests/test_study_material_classify.py tests/test_study_prereq_duplicates.py \
  tests/test_omnigent_native.py tests/test_omnigent_routes.py tests/test_omnigent_static.py
```

Expected: all pass. This code did not change, so a failure points at a broken integration point — blueprint registration, DB schema, or imports — not at feature logic.

- [ ] **Step 5: Run the full suite and diff against the baseline**

```bash
docker run --rm odysseus-port:scratch python -m pytest -q > _scratch/port/ported.txt 2>&1; tail -5 _scratch/port/ported.txt
grep -E '^(FAILED|ERROR)' _scratch/port/ported.txt | sort > _scratch/port/ported-failures.txt
echo "=== REGRESSIONS (fail now, passed on clean upstream) ==="
comm -13 _scratch/port/baseline-failures.txt _scratch/port/ported-failures.txt
echo "=== FIXED (failed on clean upstream, pass now) ==="
comm -23 _scratch/port/baseline-failures.txt _scratch/port/ported-failures.txt
```

**Gate: the REGRESSIONS list must be empty.** Anything in it is damage this port caused — return to the tier owning that file. Entries under FIXED are fine.

- [ ] **Step 6: Record the result in the draft notes**

Fill in "Verification results" in `_scratch/port/PORT-NOTES.draft.md` with the summary line and the empty regression list. No git commit — the draft stays gitignored until Phase 2.

---

# Phase 2 — Replay the real history

`port-scratch` holds a verified tree but only one commit. This phase reproduces that same tree with the commit history intact.

### Task 2.1: Graft-rebase onto upstream

- [ ] **Step 1: Create the real branch**

```bash
git checkout -b study-omnigent dev
git log --oneline -1
```

Expected: `da4c6b7 fix(omnigent): register crew + per-model workers as built-in agents`

- [ ] **Step 2: Start the graft rebase**

```bash
git rebase --onto upstream/dev 9d7a3d6 study-omnigent
```

`rerere` replays the Phase 1 resolutions, so most conflicts resolve themselves.

- [ ] **Step 3: Work through each stop**

```bash
git status --short | grep '^UU\|^AA'
git rerere status
```

If `rerere` already resolved it, the file has no markers — inspect it, `git add <file>`, then `git rebase --continue`. If it stopped on something new, resolve using the same tier rules from Phase 1.

**Expect roughly 14 commits to disappear.** Those are upstream's own commits from the range, already present in `upstream/dev`. Modern git drops them automatically as empty; if it stops and reports an empty commit:

```bash
git rebase --skip
```

Record each skip in the "Skipped commits" table of the draft notes.

**If the rebase becomes unsalvageable:**

```bash
git rebase --abort
git checkout port-scratch
```

`port-scratch` is a verified fallback with one commit instead of 44. Report that outcome honestly rather than forcing a bad rebase.

- [ ] **Step 4: Confirm the rebase landed on the right base**

```bash
git log --oneline -1 upstream/dev
git merge-base study-omnigent upstream/dev
git rev-list --count upstream/dev..study-omnigent
```

Expected: the merge-base equals `b4d12932`, and the count is around 44 (58 minus the ~14 dropped upstream commits).

- [ ] **Step 5: Confirm authorship survived**

```bash
git log --format='%an <%ae>' upstream/dev..study-omnigent | sort -u
```

Expected: `BestMiraPro <dinas.m.mira@gmail.com>` — rebase preserves the original author.

---

### Task 2.2: Prove the rebase is correct

This is the payoff for doing Phase 1 first.

- [ ] **Step 1: The two branches must produce an identical tree**

```bash
git diff --stat port-scratch study-omnigent
echo "differing files: $(git diff --name-only port-scratch study-omnigent | wc -l)"
```

Expected: `differing files: 0`

The draft notes live in gitignored `_scratch/`, so no exclusion is needed — this is a clean, total comparison.

**If it is not 0, the rebase diverged from the verified resolution.** Inspect each file with `git diff port-scratch study-omnigent -- <file>`, decide which side is right — usually `port-scratch`, since it was verified — and fix `study-omnigent` with a follow-up commit.

- [ ] **Step 2: Rebuild and re-run the suite on the real branch**

```bash
docker build -t odysseus-port:study-omnigent .
docker run --rm odysseus-port:study-omnigent python -m pytest -q > _scratch/port/final.txt 2>&1; tail -5 _scratch/port/final.txt
grep -E '^(FAILED|ERROR)' _scratch/port/final.txt | sort > _scratch/port/final-failures.txt
echo "=== REGRESSIONS vs baseline ==="
comm -13 _scratch/port/baseline-failures.txt _scratch/port/final-failures.txt
```

**Gate: empty regression list.**

- [ ] **Step 3: Promote the draft notes into the repo**

```bash
cp _scratch/port/PORT-NOTES.draft.md docs/PORT-NOTES.md
git add docs/PORT-NOTES.md
git commit -m "docs: port notes — baseline, dropped changes, tree-equality proof"
```

---

# Phase 3 — Live verification

Code-level correctness is not the bar. This phase proves it runs.

### Task 3.1: Bring the stack up on the ported branch

- [ ] **Step 1: Check the compose config resolves**

```bash
docker compose -p odysseus -f docker-compose.yml config > /dev/null && echo "CONFIG OK"
```

- [ ] **Step 2: Start the stack**

```bash
docker compose -p odysseus -f docker-compose.yml up -d --build odysseus
docker compose -p odysseus ps
```

The previous stack exited 137 (OOM) two weeks ago. If that recurs, raise Docker Desktop's memory allocation before assuming the port is at fault.

- [ ] **Step 3: Wait for health**

```bash
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7000/api/health || echo 000)
  echo "attempt $i: $code"
  [ "$code" = "200" ] && break
  sleep 3
done
```

Expected: `200`.

- [ ] **Step 4: If it did not come up, read the logs before changing anything**

```bash
docker compose -p odysseus logs --tail=120 odysseus
```

Import errors point at Task 1.4 Step 3 (blueprint registration). Database errors point at Task 1.4 Step 1 (Study tables).

---

### Task 3.2: Smoke-test the four things that must work

- [ ] **Step 1: Study end to end**

At `http://127.0.0.1:7000`: open the Study panel, upload a material file, confirm extraction produces questions, answer a practice question, use Consult, then confirm the History tab shows the answer.

- [ ] **Step 2: Omnigent**

Open the Omnigent panel, launch it, run a crew task against an API model. Use **GLM-5.1**, not GLM-5.2 — the latter returns 429 from a W&B account concurrency cap, which would be mistaken for a port failure.

- [ ] **Step 3: Google sign-in**

Sign out, then sign in with Google. This exercises the Task 1.5 Step 2 resolution including `X-Forwarded-Proto` handling.

- [ ] **Step 4: One untouched upstream feature**

Send a normal chat message and confirm a response streams. This catches collateral damage to upstream code — the failure mode `src/llm_core.py` is most likely to cause.

- [ ] **Step 5: Record the results**

Add a pass/fail line for each of Steps 1–4 to "Verification results" in `docs/PORT-NOTES.md`, then:

```bash
git add docs/PORT-NOTES.md
git commit -m "docs: live smoke-test results for the ported branch"
```

**Gate: all four pass before Phase 4.** If one fails, fix it and re-run Task 2.2 Step 2.

---

# Phase 4 — Prepare for public release

### Task 4.1: Scrub local machine paths

**Files:** `docs/superpowers/plans/*.md`

- [ ] **Step 1: Find every occurrence**

```bash
grep -rn 'C:\\Users\\Dinis Mira~' docs/ || echo "none found"
```

- [ ] **Step 2: Replace absolute local paths with repo-relative ones**

```bash
grep -rl 'C:\\Users\\Dinis Mira~\\odysseus' docs/ | while read -r f; do
  sed -i 's|C:\\Users\\Dinis Mira~\\odysseus\\|./|g; s|C:\\Users\\Dinis Mira~\\odysseus|.|g' "$f"
  echo "scrubbed $f"
done
grep -rn 'C:\\Users' docs/ || echo "clean"
```

Expected: `clean`.

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs: replace local machine paths with repo-relative paths"
```

---

### Task 4.2: Rewrite the README to lead with your work

**Files:**
- Create: `README-upstream.md`
- Modify: `README.md`

- [ ] **Step 1: Preserve upstream's README**

```bash
git mv README.md README-upstream.md
git commit -m "docs: preserve upstream README as README-upstream.md"
```

- [ ] **Step 2: Write the new `README.md`**

Order matters — the first screen must answer "what did this person build?":

1. **Title and one-line description.** Name the fork; state that it adds a spaced-repetition Study system and a multi-agent Omnigent bridge to Odysseus.
2. **What I built** — the four features, each with what it does and the engineering problem it solved: FSRS scheduling, PDF/vision extraction, LaTeX rendering, multi-part question grouping (~7,000 lines) for Study; agent bridge with per-model sub-agents and native crew runs (~1,000 lines) for Omnigent; Google OAuth with long-lived sessions and `X-Forwarded-Proto` trust behind a tunnel; `deploy.ps1` decoupling build source from runtime data.
3. **Screenshots or a short clip** of the Study panel — this matters more than prose for a portfolio reader.
4. **Architecture** — how Study plugs into Odysseus: `routes/study_routes.py` blueprint, `src/study_ai.py` extraction, `src/fsrs.py` scheduling, `src/study_vision.py` PDF handling, tables in `core/database.py`, `static/js/study.js` frontend.
5. **Running it** — Docker quick start, `deploy.ps1`, and the `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` env vars needed for Google sign-in.
6. **Development notes** — link `docs/superpowers/specs/` as the design docs written before each feature, and `docs/PORT-NOTES.md` as the record of porting onto upstream. State plainly that the work was built with AI assistance and that `Co-Authored-By` trailers are retained on the commits that used it.
7. **Fork attribution and license** — clearly marked: forked from [`pewdiepie-archdaemon/odysseus`](https://github.com/pewdiepie-archdaemon/odysseus), based on upstream `dev` @ `b4d12932`, licensed **AGPL-3.0** on the same terms as upstream, linking `LICENSE`, `ACKNOWLEDGMENTS.md` and `README-upstream.md`.

- [ ] **Step 3: Verify every internal link resolves**

```bash
grep -oE '\]\(([^)]+)\)' README.md | sed 's/](//; s/)//' | grep -v '^http' | while read -r p; do
  [ -e "${p%%#*}" ] && echo "OK   $p" || echo "DEAD $p"
done
```

Expected: no `DEAD` lines.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README leading with Study and Omnigent, with fork attribution"
```

---

### Task 4.3: Stop CI from failing publicly

**Files:** `.github/workflows/*.yml`

Ten workflows start running the moment the repo is public: `ci.yml`, `codeql.yml`, `container-scan.yml`, `container-trivy.yml`, `dependency-review.yml`, `docker-publish.yml`, `issue-description-check.yml`, `pr-description-check.yml`, `secret-scan.yml`, `workflow-security.yml`.

- [ ] **Step 1: Identify which need credentials or upstream-specific config**

```bash
grep -ln 'secrets\.' .github/workflows/*.yml
grep -n 'ghcr.io\|docker.io\|pewdiepie-archdaemon' .github/workflows/*.yml
```

- [ ] **Step 2: Disable the ones that cannot pass**

For each workflow from Step 1 that pushes to a registry or reads a secret you do not hold, replace its `on:` block with:

```yaml
on:
  workflow_dispatch:
```

`docker-publish.yml` is the certain case — it will attempt registry pushes with no credentials.

- [ ] **Step 3: Keep the ones that help**

Leave `ci.yml`, `codeql.yml` and `secret-scan.yml` enabled. `secret-scan.yml` independently rechecks the release scan, and CodeQL on a public repo is a genuine positive signal for a portfolio.

- [ ] **Step 4: Validate the YAML still parses**

```bash
for f in .github/workflows/*.yml; do
  python -c "import yaml,sys; yaml.safe_load(open(sys.argv[1],encoding='utf-8'))" "$f" && echo "OK  $f" || echo "BAD $f"
done
```

Expected: `OK` on every line.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/
git commit -m "ci: restrict credential-dependent workflows to manual dispatch"
```

---

### Task 4.4: Final release gate and handoff

- [ ] **Step 1: Re-run the secret scan on the final tree**

```bash
git grep -nIE '(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|GOCSPX-[A-Za-z0-9_-]{10,}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)' -- . \
  | grep -viE 'example|placeholder|your[-_]?|dummy|fake' | head
echo "scan complete"
```

Expected: nothing above `scan complete`.

- [ ] **Step 2: Confirm no sensitive file is tracked**

```bash
git ls-files | grep -iE '(^|/)\.env$|client_secret|credentials\.json|\.pem$|\.sqlite$' || echo "no sensitive tracked files"
git status --ignored --short | grep -E '^!!' | grep -iE '\.env|^!! data/|\.db$' | head
```

Expected: `no sensitive tracked files`. Anything from the second command is correctly ignored and will not be published — confirm it, do not delete it.

- [ ] **Step 3: Confirm the license and attribution are intact**

```bash
head -2 LICENSE
echo "README mentions upstream: $(grep -c 'pewdiepie-archdaemon' README.md)"
echo "ACKNOWLEDGMENTS intact:   $(grep -c 'pewdiepie-archdaemon\|opencode\|llmfit' ACKNOWLEDGMENTS.md)"
```

Expected: `GNU AFFERO GENERAL PUBLIC LICENSE`, and non-zero counts.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin study-omnigent
```

- [ ] **Step 5: Hand over the visibility flip**

The repo must be made public by its owner. This environment has no `gh` CLI and no GitHub credentials, and exposure is not reversible once indexed. Provide these steps rather than attempting it:

1. Merge or fast-forward `study-omnigent` into `dev`, or set it as the default branch, once satisfied.
2. GitHub → repo → **Settings** → **General** → **Danger Zone** → **Change visibility** → **Make public**.
3. Set the repo **description** and **topics** (`ai`, `spaced-repetition`, `flask`, `self-hosted`, `llm`, `agents`) — topics are how a portfolio repo gets found.
4. Confirm the **Actions** tab is green or empty, not red.
5. Confirm the fork attribution renders correctly on the public landing page.

- [ ] **Step 6: Final commit**

```bash
git add docs/PORT-NOTES.md
git commit -m "docs: final release gate results"
git push
```

---

## Rollback

At any point:

```bash
git checkout dev                    # untouched throughout
git checkout pre-port-2026-08-22    # the tagged pre-port state
```

To restore the previously running deployment:

```bash
docker compose -p odysseus -f docker-compose.yml up -d --no-build odysseus
```

The data volume is never modified by this plan.

## Cleanup after success

```bash
git worktree remove ../port-baseline
docker rmi odysseus-baseline:upstream-dev odysseus-port:scratch
rm -rf _scratch/port
```

Keep the `port-scratch` branch until the ported deployment has run for a while — it is the verified fallback.
