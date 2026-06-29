# Round 2 — Agent GLM Revised Final Change Proposal

**Author:** Agent GLM (glm-5-2). Round 1: `round1-glm.md`. Opponent: Agent CODEX (`round1-codex.md`).
**Date:** 2026-06-29
**What changed from R1:** I verified the anchor dispute with `git` (§1 — decisive), conceded two CODEX points (modularization timing, numeric-confidence-as-Phase-0), refined my calibration position with newly-discovered evidence (a per-session calibration view *already exists* in the working tree), and downgraded one of my own R1 findings (`done_blocks`) after finding the reset is *intentional*.

**Settled inputs (not relitigated):** stay on personalized FSRS-4.5; calibration curve only, no auto-downgrade.

---

## §0 — THE ANCHOR DISPUTE, RESOLVED WITH GIT EVIDENCE

This is the contested point #1 and it has a hard, verified answer. I ran:

```
git rev-parse --show-toplevel    → /mnt/c/Users/Dinis Mira~/odysseus
git ls-files | grep study_routes.py  → routes/study_routes.py   (tracked, canonical)
git ls-files | grep study.js         → static/js/study.js        (tracked, canonical)
git rev-parse --short HEAD           → da4c6b7   (branch: dev)
git status --short routes/study_routes.py static/js/study.js  →  M (BOTH MODIFIED, uncommitted)
```

**There are THREE distinct copies of this code, and they disagree:**

| Copy | `_resolve_uploaded_file` | `rateCard` | `rating_from_outcome` | `ASK_COACH_SYSTEM` | Tracked? |
|---|---|---|---|---|---|
| `.claude/worktrees/condescending-stonebraker-.../` (old worktree) | `:1035` | `:1419` | `:502` | **absent** | NO |
| `git show HEAD:` (committed `da4c6b7`) | `:1035` | `:1419` | `:502` | **absent** | YES (committed) |
| **Working tree (on-disk, `M` modified)** | **`:1070`** | **`:1458`** | **`:507`** | **`:614`** | YES (dirty) |

The working tree has **242 uncommitted insertions** (`git diff --stat`: +175 in `study_routes.py`, +78 in `study.js`) — active in-progress feature work (a new `AskIn` model, `ASK_COACH_SYSTEM`/`ASK_TUTOR_SYSTEM` imports, `_question_to_dict` gains `source_page`/`original`). `ASK_COACH_SYSTEM` **does not exist at HEAD** — it's part of the uncommitted work.

**Resolution:**
- **The consolidated review's `:1035` is the HEAD/committed line — it is NOT wrong, it was reading committed state.** My R1 claim that the review's correction was "itself wrong" was too strong. The review read HEAD (or the matching worktree); CODEX and I read the dirty working tree.
- **The authoritative anchor for builders is the working tree** (`:1070`, `:1458`, `:507`, `:614`), because that's what's on disk and what the plan was written against (the plan references `ASK_COACH_SYSTEM` at `:495-505`, which only exists in the working tree).
- **BUT the working tree is a moving target.** The #1 fix must therefore be stronger than "re-anchor against HEAD": it must be **"commit the current working tree (or a snapshot of it) and re-anchor against that specific SHA, then state the SHA in the plan."** Anchoring against a dirty tree is fragile — the next `git add` shifts every line.

**Authoritative anchor table (working tree @ time of writing, branch `dev`):**

| Symbol | Line | Verified by |
|---|---|---|
| `rateCard` | `static/js/study.js:1458` | `grep` + read |
| `/attempt` submit | `static/js/study.js:1728` (handler `:1720`) | read `:1720-1744` |
| `/focus/{id}/finish` submit | `static/js/study.js:2110` | `grep` + read |
| `_resolve_uploaded_file` | `routes/study_routes.py:1070` (ends `:1111`) | read `:1030-1120` |
| `rating_from_outcome` | `src/study_ai.py:507` | `grep` + read |
| `ASK_COACH_SYSTEM` | `src/study_ai.py:614` | `grep` + read |
| `_priority` | `src/study_plan.py:31` | `grep` (stable at HEAD too) |
| `mix` (interleaving) | `src/study_plan.py:216-217` | read |
| `fsrs.schedule` card site | `routes/study_routes.py:1753` (omits `w=`) | read `:1750-1758` |
| `fsrs.schedule` question site | `routes/study_routes.py:2780` (omits `w=`) | read `:2778-2790` |
| `DEFAULT_W` | `src/fsrs.py:33` | read |
| `LEARN_AGAIN/HARD_MIN` | `src/fsrs.py:46-47`; used `:196,199,210` | read |
| `StudyReview` schema | `core/database.py:1688` | read |
| `StudyAttempt.confidence` | `core/database.py:1802` (`sure|unsure|guess`, nullable) | read |

**Action for the plan:** A1 must say "snapshot the working tree to a commit, record the SHA, and re-anchor against it" — not just "re-anchor against HEAD" (HEAD is behind the plan's own references).

---

## §1 — Critique of CODEX's R1: where CODEX is stronger (CONCEDE)

### C-CONCEDE-1: Modularization — CODEX's incremental `study_service.py` in Phase 0/1 is better than my "keep in Phase 4"
**CODEX R1 (A4):** Extract a thin `src/study_service.py` (deck/card/question ownership, writes, transactions) during Phase 0; split first stats/AI callers during Phase 1; leave the full `routes/study/` package as Phase 4 continuation.
**My R1:** Kept modularization in Phase 4 with only a "thin 0.6 slice" added.
**Verdict: CODEX is right; I concede and adopt.** Evidence:
- `routes/study_routes.py` is 3,553 lines with **53 `@router` handlers** and **zero** study-specific route tests (verified: `ls tests/ | grep study` → 5 files, all unit tests of pure helpers: `study_ai_helpers`, `study_material_classify`, `study_plan_generator`, `study_prereq_duplicates`, `study_source`; none hit a route).
- The `_get_*` ownership helpers (`_get_deck` `:1480`, `_get_card` `:1486`, `_get_question` `:2079`) are **nested closures inside `register()`** (verified: 4-space indent under register body). This is the natural seam for extraction: they already take `db` as a param, so lifting them to a module-level `study_service.get_deck(db, deck_id, user)` is mechanical.
- Phase 1.1 (`w` threading at `:1753, :2780`) and my idempotency item (A3) both edit `review_card` (`:1743`) and `attempt_question` (`:2715`) inside this monolith. Doing those edits *first* and splitting *later* means the split must preserve them blind. CODEX's "extract the service layer *before* wiring" order is lower-risk.
- **My refinement to CODEX's proposal:** the extraction must be gated by **endpoint contract tests written first** (CODEX agrees, A5/R7). Without contract tests, "extract then wire" is a refactor with no regression net — and the 53 handlers have none today. So: Phase 0.6 = contract tests + thin `study_service.py` (ownership + transaction helpers only); Phase 1 wires through it; Phase 4.3 finishes the split. I fold this in as **A4-revised** below.

### C-CONCEDE-2: Numeric-confidence slider placement — CODEX's "Phase 0.3, before calibration" is more defensible than my "3-bin now, slider later"
**CODEX R1 (R3):** Move numeric confidence to Phase 0.3 as the prerequisite to calibration.
**My R1:** Build the calibration curve on the 3-bin signal now; slider (2.1) refines the x-axis later.
**Verdict: Partial concede.** CODEX's argument — "building a curve on 3 coarse bins is statistically weak and invalidated by a later numeric migration" — is sound for the *persistent* curve. I refine rather than fully concede, because of **new evidence**:
- The working tree **already has a per-session calibration readback** on the 3-bin signal: `study.js:1792-1801` renders "sure but wrong" / "lucky guesses" chips and a "Calibration alarms — sure but wrong" section, computed from the in-memory `p.log`. So 3-bin calibration *display already exists* for the session scope; the gap CODEX and I are both solving is the **persistent, cross-session curve from `StudyAttempt`**.
- For the *persistent* curve, CODEX is right: a 3-bin persistent curve would be re-rendered with different x-values after the numeric migration (the mapping ambiguity I flagged in R1's E2), undermining its credibility. Better to land the numeric slider first so the persistent curve is born on the right signal.
- **Revised position:** Phase 0.3 = numeric confidence slider (with pinned enum→number mapping, my E2). Phase 1.2 = persistent calibration curve from `StudyAttempt` on the *numeric* signal, merged with the dashboard display (the 1.2+3.1 merge from my R1 A4). The existing per-session 3-bin readback stays as-is (it's session-scoped and doesn't need migration). This is close to CODEX's R3 + my R1 A4 merged.

### C-CONCEDE-3: CODEX's re-sequenced roadmap is cleaner
**CODEX R1 §6:** Phase 0 gains 0.3 (numeric confidence) and 0.5 (study_stats) and 0.6 (service layer); Phase 2 absorbs fuzz + pretesting.
**Verdict: Adopt CODEX's skeleton** with my A4-revised (contract tests before service extraction) and my E1/E2 refinements. CODEX's ordering is better than my R1 because it front-loads the *signal* and *substrate* that Phase 1 depends on.

---

## §2 — Critique of CODEX's R1: where CODEX is weaker or wrong (REBUT)

### C-REBUT-1: CODEX's anchor table mixes HEAD and working-tree lines (silently)
CODEX's R1 §8 lists `_resolve_uploaded_file` at `routes/study_routes.py:1070-1111` (working tree) — correct for the working tree, but CODEX also lists `rateCard` at `1458-1479` (working tree) while the consolidated review said `1035` (HEAD). CODEX didn't notice the discrepancy or its source (uncommitted working-tree changes). **This matters:** if a builder checks out a fresh clone at HEAD `da4c6b7`, CODEX's `:1070` is wrong by 35 lines (HEAD is `:1035`); `ASK_COACH_SYSTEM` at `:614` **doesn't exist at HEAD at all**. CODEX's R1 §9 worker-assignment table and risk register inherit this silent ambiguity.
**My fix (already in §0):** the re-anchor item must specify *commit the working tree, record the SHA, anchor against it*. CODEX's "verified against HEAD" claim is false for `ASK_COACH_SYSTEM` (absent at HEAD). This is a real defect in CODEX's proposal — it would mislead a builder who reads "HEAD" literally.

### C-REBUT-2: CODEX downrates the LaTeX/KaTeX sanitization finding below its evidence
**CODEX R1 (A5):** Lumps LaTeX sanitization into a generic "accessibility/privacy/LaTeX/regression" bucket as a Phase-0/3 AC.
**My R1 (B3):** Specific finding — `markdown.js:721-722` reinserts sanitized HTML, then `:726-727` reinserts KaTeX HTML *after* the sanitizer, so `\href`/`\text{}` macros bypass `sanitizeAllowedHtml`. Verified again this round (working tree): `katex.renderToString(..., {throwOnError:false})` at `:597,607,616,625` with no `strict`/`trust` override; study content flows `mdToHtml → innerHTML` at `study.js:924,939,978,1025`.
**Rebut:** This is a concrete, localized, testable bug class (KaTeX output bypasses the sanitizer by ordering), not a vague "sanitize LaTeX" AC. CODEX's bundling loses the specificity. Severity is **lower than idempotency/rate-limiting** (study AI content is locally generated, not arbitrary web input), but it's a real XSS-adjacent path. **My revised priority:** a small, specific Phase-0.2 sub-item: "add a test that `\href{javascript:alert(1)}{x}` and `\text{<script>}` in study markdown don't execute; if they do, re-run `sanitizeAllowedHtml` over KaTeX output or set KaTeX `strict`." Keep it ranked *below* rate-limiting/idempotency but *above* generic a11y. CODEX under-specifies it; I keep it specific.

### C-REBUT-3: CODEX omits the `done_blocks` regeneration behavior entirely
**CODEX R1:** No mention of `StudyExam.done_blocks` desync on plan regeneration.
**My R1 (E1):** Flagged "regeneration erases the user's progress" as a risk.
**New evidence this round:** `study_routes.py:1999` already does `exam.done_blocks = json.dumps([])  # plan changed; reset checkmarks` — the reset is **intentional** (with a comment). So my R1 framing ("silent risk") was partly wrong; it's a deliberate behavior. **Rebut to CODEX:** the omission still matters, because Phase 1.3 (inject FSRS mastery into `generate_plan`) makes regeneration *more frequent and more valuable*, so "regenerate wipes all checkmarks" becomes more painful. The plan should add a Phase 1.3 AC: "regeneration either (a) migrates completion state by matching old→new blocks on `date:idx` where the block still exists, or (b) keeps the reset but prompts the user ('regenerating will clear your checkmarks — continue?')." This is a UX AC, not a data-integrity one. CODEX missing it is a gap; my R1 overstated it; the refined version is the right size.

### C-REBUT-4: CODEX's idempotency spec is thinner than the evidence warrants
**CODEX R1 (R4):** "accept an optional `idempotency_key` and dedupe on `(idempotency_key)` or a deterministic hash."
**My R1 (A3):** Same, plus the specific failure: a retried `review_card` recomputes `fsrs.schedule()` from the *already-updated* card state (`study_routes.py:1762-1769` does `card.reps = result["reps"]` + `db.add(StudyReview(...))` in one txn), so a retry produces a *third* wrong interval on top of the double count. Verified again this round: `StudyReview.id` is fresh `uuid.uuid4()` (`core/database.py:1691`) with no UNIQUE on any natural key; same for `StudyAttempt` (`:1795`).
**Rebut:** CODEX's spec is right in shape but under-specifies the dedup *behavior*: on a duplicate key, the server must **return the prior result without re-applying `fsrs.schedule`** — otherwise "dedupe the row but still recompute the card state" leaves the card corrupted. The idempotency branch must short-circuit *before* `schedule()`, not just reject the duplicate insert. I keep my A3 with this explicit behavioral AC; CODEX's R4 should adopt it.

---

## §3 — REVISED FINAL CHANGE PROPOSAL (incorporating concessions + rebuttals)

### Tier A — MUST FIX before builders (blocking, ~22h)

**A1. Re-anchor against a committed SHA, not "HEAD"** — *S, ~1h*
- Commit the working tree (or `git stash` a snapshot), record the SHA, state it in the plan. Re-anchor all `file:line` refs against that SHA using the §0 table. Do **not** say "against HEAD" — HEAD (`da4c6b7`) is behind the plan's own references (`ASK_COACH_SYSTEM` is absent at HEAD). Do **not** use the consolidated review's `:1035` as the authoritative line — it's the HEAD line, but builders work against the working tree (`:1070`).

**A2. Re-scope Phase 0.2** — *S, ~2h*
- Drop path-traversal (already fixed: `basename`+`realpath`+`_confined()` at `study_routes.py:1070-1111`, all 3 paths). Keep AI rate-limiting: add `RateLimiter` (pattern from `auth_routes.py:90-132`) to the 11 AI endpoints (verified: `/ai/generate-cards` `:1779`, `/ai/quiz` `:1815`, `/ai/grade` `:1872`, `/materials/{id}/reextract-text` `:2166`, `/materials/{id}/notes` POST `:2224`, `/decks/{id}/overview` POST `:2276`, `/materials/{id}/extract` `:2310`, `/questions/{id}/explain` `:2892`, `/questions/{id}/explain-further` `:2924`, `/cards/{id}/explain-further` `:3043`, `/overview` `:3093`). Reword the path-traversal AC to a characterization/regression test.
- **Add (from B3):** a specific LaTeX/KaTeX sub-item — test that `\href{javascript:...}` / `\text{<script>}` in study markdown don't execute; if they do, fix by re-running `sanitizeAllowedHtml` over KaTeX output (post-`markdown.js:727`) or setting KaTeX `strict`. Ranked below rate-limiting, above generic a11y.

**A3. Server-side idempotency for durable ratings** — *M, ~4h*
- Add nullable `idempotency_key` + UNIQUE index to `StudyReview` (`core/database.py:1688`) and `StudyAttempt` (`:1789`). Client (0.1's queue) generates the key. **Server behavioral AC (refined from R1, rebutting CODEX R4):** on duplicate key, short-circuit *before* `fsrs.schedule()` and return the prior result — do NOT recompute the card state (a retried `review_card` at `:1743-1772` recomputes from the already-updated card, producing a third wrong interval). Old clients without a key fall back to current behavior.

**A4-revised. Phase 0.6 — thin `study_service.py` + endpoint contract tests** — *M, ~6h* (CONCEDED to CODEX)
- Extract `_get_deck`/`_get_card`/`_get_question`/`_get_material`/`_get_exam` (nested closures at `routes/study_routes.py:1480-1492,2073-2079`) to a module-level `src/study_service.py` taking `db` explicitly. **Prerequisite (my refinement):** write endpoint contract tests (request→response shape) for the 53 `@router` handlers *before* extraction — there are zero study route tests today (verified). This is the regression net that makes the extraction safe. Full `routes/study/` package split stays in Phase 4.3.

**A5. Calibration sequencing — numeric slider (0.3) THEN persistent curve (1.2)** — *M, re-seq* (PARTIAL CONCEDE to CODEX)
- Phase 0.3: numeric confidence slider (0–100) with **pinned enum→number mapping** (my E2: `sure→85, unsure→55, guess→25`, documented + tested). `StudyAttempt.confidence` migration to numeric, back-compat. Phase 1.2: persistent calibration curve from `StudyAttempt` on the *numeric* signal, merged with dashboard display (the 1.2+3.1 merge). The existing per-session 3-bin readback (`study.js:1792-1801`) stays as-is — it's session-scoped and needs no migration.

**A6. Gate per-user FSRS fitting** — *S, ~2h*
- Phase 1.1 AC: ≥400 reviews before fitting `w`; warm-start fallback to `DEFAULT_W` (today both call sites `:1753, :2780` omit `w=`, so fallback = current behavior). Add determinism AC (same logs ⇒ same `w`) and owner-scope AC (`StudyUserParams` owner-scoped). Caveat: 5/12-min learning steps (`fsrs.py:46-47,196,199,210`) are fixed re-drill delays, NOT stability — exclude `interval_days=0` learning rows from the optimizer training set.

**A7. Pull interval fuzz (±25%) forward** — *S, ~2h*
- Split Phase 4.1 into 4.1a (fuzz → Phase 2) and 4.1b (FSRS-5/6 → Phase 5). Seeded, flag-gated jitter in `_next_interval_days` (`fsrs.py:132-135`) for review-state cards only (not learning steps). Verified: zero fuzz today.

**A8. Golden-value + `study_vision` tests** — *S, ~3h*
- Phase 0.4: exact-float golden tests vs FSRS-4.5 reference (current `test_fsrs_scheduler.py` deliberately pins structural properties only — its docstring says so; a `DEFAULT_W` refit wouldn't be caught). Add `study_vision.text_layer_is_thin` (`:176`) and `batch_pages` (`:104`) tests (verified: zero coverage today).

### Tier B — HIGH-VALUE, not blocking (~10h)

**B1. Phase 0.5 — `study_stats.py` + `GET /api/study/stats` + composite indexes** — *M, ~4h*
- Shared analytics substrate (CODEX A1, my R1 B1). Composite indexes `(owner, reviewed_at)` / `(owner, attempted_at)` — individual columns are indexed (verified `core/database.py:1691,1703,1800,1810`) but the stats/optimizer queries are `(owner, time-window)`-shaped, so composites turn scans into range seeks. Fixes the N+1 at `study_routes.py:923` (`StudyAttempt.count()` inside a loop).

**B2. Promote pretesting to Phase 2** — *S, ~3h*
- Move from Phase 5 to Phase 2.5 (queue-ordering mode over existing questions; no new schema). d≈0.35, trivial.

### Tier C — ADOPT with re-scoping (~6h)

**C1. `done_blocks` preservation UX AC** — *S, ~1h* (refined from my R1 E1)
- Phase 1.3 AC: plan regeneration either migrates completion (match old→new `date:idx` blocks) or prompts before reset. Current behavior (`:1999`) resets intentionally — but 1.3 makes regeneration frequent, so the reset needs a UX guard. (CODEX omitted this; my R1 overstated it as data-integrity; refined to UX.)

**C2. Accessibility, privacy, regression suite** — fold into 4.3 / 1.1
- A11y (ARIA/focus/keyboard), privacy (owner-scope, already largely present — verified every `_get_*` checks `.owner != user`), perf AC for the fit job (SQLite blocks on long write txns). Fold into Phase 4.3 and 1.1.

**C3. Re-estimate modularization** — Phase 4.3 is L+, not L
- The full `routes/study/` + `static/js/study/` split (3,553 + 2,119 lines, 53 handlers) is realistically 2× an "L." The thin 0.6 slice (A4-revised) is the lower-risk start; 4.3 finishes it.

### Tier D — DROP/DOWNRANK
- **FSRS-5/6 (4.1b)** → Phase 5 (settled).
- **Auto-downgrade Easy** → drop (settled; 3-bin signal too coarse, FSRS lapse dynamics self-correct).
- **Semantic interleaving (4.2)** → stays Phase 4 (current is index-rotation `study_plan.py:216-217`; semantic needs embeddings stack).
- **Elaborative interrogation (2.3)** → keep, rank last in Phase 2.

---

## §4 — REVISED sequenced roadmap (CODEX skeleton + my refinements)

```
Phase 0  Stabilize & secure
  0.1  Durable ratings + idempotency keys (A3: short-circuit before schedule)
  0.2  Security: drop path-traversal; AI rate-limiting (11 endpoints) + KaTeX caveat (A2)
  0.3  Numeric confidence slider + pinned enum→number mapping (A5/E2)
  0.4  Golden-value FSRS tests + study_vision tests (A8)
  0.5  study_stats.py + /api/study/stats + composite indexes (B1)
  0.6  Thin study_service.py + endpoint contract tests FIRST (A4-revised) [CONCEDED to CODEX]

Phase 1  Close the data loop
  1.1  Per-user w (≥400 threshold, warm-start, determinism, owner-scope, 5/12 caveat) (A6)
  1.2  Calibration: persistent curve on numeric signal, merged with dashboard (A5)
  1.3  Plan↔FSRS mastery + done_blocks preservation UX AC (C1)

Phase 2  Retrieval hardening
  2.1  Interval fuzz ±25% (A7, pulled from 4.1a; seeded, flag-gated)
  2.2  Elaborative-interrogation + JOL prompts (re-anchored ASK_COACH_SYSTEM :614)
  2.3  Pretesting (B2, promoted from Phase 5)
  2.4  Adaptive question selection
  2.5  Typed-recall / wrong-MCQ gate

Phase 3  Insight & habit (calibration chart now in 1.2)
  3.1  Dashboard (uses study_stats.py)
  3.2  Focus ↔ Plan link
  3.3  Non-coercive motivation

Phase 4  Architecture
  4.1  Semantic interleaving (needs embeddings)
  4.2  Full routes/study/ + static/js/study/ split + a11y (C2/C3) — continuation of 0.6
  4.3  Async DB/background queue (optional)

Phase 5  Optional
  FSRS-5/6 (4.1b) · faded worked examples · delayed feedback · bulk ops
```

**New dependency arrows:**
```
0.3 numeric confidence ──> 1.2 calibration curve (numeric signal)
0.5 study_stats.py ─────> 1.1 optimizer, 1.2 calibration, 1.3 plan, 3.1 dashboard
0.6 study_service.py ───> 1.1 w-threading, A3 idempotency (edits same call sites)
0.6 contract tests ─────> 4.2 full split (regression net)
1.1 per-user w ─────────> 2.1 fuzz (golden tests updated for fuzz)
```

---

## §5 — Effort summary (revised)

| Item | Tier | Effort | Source |
|---|---|---|---|
| A1 re-anchor vs committed SHA | A | S (~1h) | R1, refined |
| A2 0.2 re-scope + KaTeX | A | S (~2h) | R1 + CODEX |
| A3 idempotency (short-circuit AC) | A | M (~4h) | R1, refined vs CODEX |
| A4-rev service layer + contract tests | A | M (~6h) | CONCEDED to CODEX |
| A5 numeric slider then curve | A | M (~6h) | partial concede to CODEX |
| A6 fit threshold/caveats | A | S (~2h) | consensus |
| A7 fuzz forward | A | S (~2h) | R1 + CODEX |
| A8 golden + vision tests | A | S (~3h) | R1 |
| B1 study_stats + indexes | B | M (~4h) | R1 + CODEX |
| B2 pretesting promote | B | S (~3h) | CODEX |
| C1 done_blocks UX AC | C | S (~1h) | R1 E1, refined |
| C2 a11y/privacy/perf (fold) | C | M | CODEX |
| C3 modularize re-estimate | C | L+ | R1 + CODEX |

Tier A ≈ 26h. Tier A+B ≈ 33h.

---

## §6 — Summary of R1→R2 changes

1. **Anchor dispute RESOLVED:** the consolidated review's `:1035` is the HEAD/committed line (not wrong); my `:1070` is the working-tree line. The working tree has 242 uncommitted insertions. **Authoritative for builders: working tree, but commit it first and record the SHA.** CODEX's anchors match the working tree but CODEX wrongly claimed "verified against HEAD" (`ASK_COACH_SYSTEM` is absent at HEAD).
2. **CONCEDED to CODEX:** (a) incremental `study_service.py` in Phase 0.6 is better than deferring modularization to Phase 4 — adopted with my "contract tests first" refinement; (b) numeric-confidence slider should precede the *persistent* calibration curve — adopted, with my pinned-mapping requirement; (c) CODEX's re-sequenced roadmap skeleton — adopted.
3. **REBUTTED vs CODEX:** (a) CODEX's "verified against HEAD" is false for `ASK_COACH_SYSTEM`; (b) CODEX under-specified idempotency (must short-circuit before `schedule()`, not just dedupe the row); (c) CODEX omitted `done_blocks` regeneration behavior entirely (I refined my own R1 claim — the reset is intentional at `:1999`, so it's a UX AC not data-integrity); (d) CODEX bundled the LaTeX finding into a generic AC, losing the specific `markdown.js:727`-after-`:722` ordering bug.
4. **NEW evidence:** a per-session 3-bin calibration readback *already exists* in the working tree (`study.js:1792-1801`), which sharpens the calibration debate — the gap is the *persistent* curve, not the *concept*.

---

*End of Round 2 — Agent GLM.*
