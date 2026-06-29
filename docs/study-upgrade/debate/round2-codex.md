# Round 2 — Agent CODEX Revised Proposal

**Model:** `kimi-k2-7-code` (Agent CODEX, code-first seat)  
**Date:** 2026-06-29  
**Scope:** Revised concrete edits to `docs/study-upgrade/00-improvement-plan.md`, incorporating GLM's Round-1 corrections and rebutting where the source disagrees.

---

## 0. Meta-lesson: the re-anchoring target

GLM correctly caught that the consolidated review's own "correction" (`_resolve_uploaded_file` at `:1035`) introduced a second stale anchor from an alternate `.claude/worktrees/...` checkout. My Round-1 anchors (`:1070` for the working tree, `:1458` for `rateCard`, etc.) are also only valid **for the current dirty working tree**. The repo at `git rev-parse --show-toplevel` has uncommitted changes (`git status` showed `routes/study_routes.py`, `static/js/study.js`, `src/study_ai.py`, etc., modified) and `git ls-files` shows canonical paths only (`routes/study_routes.py`, `static/js/study.js`). The line numbers for `ASK_COACH_SYSTEM` (`:614`) and `rating_from_outcome` (`:507`) exist only because of those uncommitted insertions; they do not exist on `HEAD`.

**CODEX rule:** before this plan becomes a build guide, commit the dirty working tree and anchor every citation against that SHA. Until then, the plan must explicitly state: *"anchors verified against working tree `<toplevel>` pre-commit; re-verify against the committed SHA before implementation."* The canonical file every builder edits is the one returned by `git ls-files`, which is `routes/study_routes.py` and `static/js/study.js` in `<toplevel>`, not the worktree copy.

---

## 1. What I concede to GLM

| GLM point | CODEX response | Action |
|---|---|---|
| Consolidated-review `:1035` for `_resolve_uploaded_file` is wrong; canonical working tree anchor is `:1070`. | **Conceded.** GLM is right. The panel introduced a new stale anchor from a worktree. | Adopt GLM's `:1070` anchor and add the meta-rule above. |
| Idempotency must short-circuit **before** `fsrs.schedule()` is re-applied, not just dedupe the row. | **Conceded.** Confirmed `review_card` mutates `card.state`, `stability`, `difficulty`, etc. before `StudyReview` insert. A retry without short-circuit would recompute from already-updated state and corrupt `reps`/`lapses`/interval. | Rewrite A4 to require returning the prior computed result and short-circuiting the scheduler. |
| KaTeX/LaTeX finding is concrete and specific (`markdown.js` reinserts math after sanitizer; `throwOnError:false` only). | **Conceded.** Verified `static/js/markdown.js:588-627` renders via `katex.renderToString` after the `&<>/` escaping and after allowed-HTML restoration. | Adopt GLM's specific fix: investigate sanitizer pass over KaTeX output or set `strict:true`; add regression test. |
| Enum-to-numeric confidence mapping must be pinned (e.g. `sure→85`, `unsure→55`, `guess→25`) or historical calibration shifts meaning. | **Conceded.** Otherwise the curve's x-axis is retroactively redefined. | Add pinned mapping to Phase 2.1 AC and use it for historical backfill. |
| Modularization needs endpoint contract tests written **before** the split. | **Conceded.** Without them, the "no regression" AC is unenforceable. | Add contract-test prerequisite to Phase 0.6/4.x. |
| Fuzz should be seeded and deterministic, and must not fuzz the fixed learning/relearning steps. | **Conceded.** Preserves testability and the pedagogical meaning of the 5/12-min steps. | Restrict fuzz to review-state intervals. |
| FSRS-5/6 should be deferred to Phase 5 (staying on personalized 4.5). | **Already agreed** — one-line dissent only. | No change. |

---

## 2. What I rebut from GLM

### 2.1 Calibration should **not** be shipped on the 3-bin signal while treating the numeric slider as a later refinement
GLM argues that a 3-point curve is "pedagogically useful" and should ship in Phase 1, with the numeric slider (Phase 2.1) merely refining the x-axis later. I rebut.

**Evidence:**
- `StudyAttempt.confidence` is enum-only today (`core/database.py:1802`: `"sure" | "unsure" | "guess"`).
- `rating_from_outcome` in `src/study_ai.py:507–528` assigns **Easy only on `confidence == "sure"`**. The right-hand side of the calibration curve (high confidence) is therefore confounded with the Easy-rating boundary.
- A 3-bin "curve" has only three points: `sure`, `unsure`, `guess`. It cannot show the diagnostic shape (overconfidence at high confidence, slope, discriminability) that makes calibration feedback worth building.
- Building a user-facing dashboard on 3 bins, then migrating to numeric 0–100 later, **retroactively changes the meaning of the historical curve**: the old `sure` bucket becomes a population with numeric range (say) 70–100, but all plotted at a single x-coordinate. That is exactly the "shifting meaning" problem GLM himself warns about for the enum→numeric mapping.
- The migration cost (adding `confidence_numeric` column/int, backfill) is small compared with shipping a curve that is too coarse to act on and then redefining it.

**CODEX position:** Numeric confidence is a **prerequisite**, not a refinement. Phase 0.3 captures the numeric slider; Phase 1.2 builds the first calibration curve on the numeric signal backfilled from the pinned mapping. GLM's "3-bin now" remains a useful **internal smoke test** ("your 'sure' accuracy is X%") but is not the Phase-1 user-facing calibration feature.

### 2.2 `done_blocks` reset on plan regeneration is **not** automatically a bug — it is a deliberate UX choice
GLM flags that regenerating a plan may desync `done_blocks` keys. I partially concede the risk, but I do not accept that preservation is always the right behavior. If a user regenerates because their exam date or topic list changed radically, old `date:idx` checkmarks may correspond to blocks that no longer exist or have shifted meaning. 

**CODEX position:** Add a clear UX AC, not a preservation mandate: "Plan regeneration warns the user that progress checkmarks will be reset if the block structure changes; offer to preserve checkmarks only when the new plan contains a topic/date match for the old block; otherwise reset." Implementation: attempt a soft merge, fall back to reset with a warning.

### 2.3 GLM's claim that a 3-bin curve is "actionable" is overstated
A statement like "your 'sure' answers are 62% correct" is actionable only if the user can map it to a behavior change. Without numeric resolution, they cannot distinguish "I was sure at 51%" from "I was sure at 99%", which is the core calibration defect. CODEX therefore keeps numeric-first sequencing.

---

## 3. Consensus required changes (final CODEX version)

### R1. Re-anchor against a committed SHA
**What:** Before implementation, commit the current working tree (dirty files: `core/database.py`, `routes/omnigent_routes.py`, `routes/study_routes.py`, `src/study_ai.py`, `static/js/study.js`, `tests/...` and new files `src/study_source.py`, `tests/test_study_source.py`). Then run a single `grep -n` pass over `routes/study_routes.py` and `static/js/study.js` to populate the final anchor table. Current working-tree anchors (verified this debate):
- `rateCard`: `static/js/study.js:1458`
- practice `/attempt` handler: `static/js/study.js:1720–1744`
- focus `/finish` handler: `static/js/study.js:2102–2113`
- `_resolve_uploaded_file`: `routes/study_routes.py:1070`
- `rating_from_outcome`: `src/study_ai.py:507–528`
- `ASK_COACH_SYSTEM`: `src/study_ai.py:614`
- `_priority`: `src/study_plan.py:31`
- `StudyReview`/`StudyAttempt`: `core/database.py:1688–1698`, `:1789–1802`
- FSRS learning steps: `src/fsrs.py:196–201`

**Why:** Anchors in the dirty tree are already drifting from `HEAD`; a build guide needs a single SHA of truth.
**Risk:** XS. Docs.
**Effort:** XS.

### R2. Re-scope Phase 0.2 security
Drop path-traversal fix; keep AI rate-limiting and owner-check audit. Reword AC to a characterization test asserting existing confinement at `:1070`.

### R3. Final calibration sequencing
- **Phase 0.3:** Numeric confidence slider (0–100); migration adds `confidence_numeric` integer column with pinned backfill `sure→85`, `unsure→55`, `guess→25`; historical `confidence` enum left untouched for auditability.
- **Phase 1.2:** Compute + display calibration curve **on numeric signal only**; backfilled historical attempts use pinned mapping. 3-bin "sure accuracy" may appear in internal smoke-test UI but is **not** the Phase-1 deliverable.
- Drop Easy-gate (settled input).

### R4. Idempotency must short-circuit before FSRS scheduling
**What:** `/cards/{id}/review` and `/questions/{id}/attempt` accept `idempotency_key`. On conflict, return the **previously stored computed result** without re-running `fsrs.schedule()`. The schedule mutation and the log insert must stay atomic.

**Why:** Confirmed `review_card` updates card state before inserting `StudyReview`. Re-running on retry would double-apply the scheduler.
**Risk:** Low.
**Effort:** S–M.

### R5. Per-user FSRS fit threshold and caveats
≥400 reviews/attempts before using fitted `w`; warm-start fallback; determinism AC; 5/12-min learning steps excluded as non-stability observations.

### R6. Interval fuzz forward to Phase 2
Seeded, deterministic ±25% jitter applied only in review/relearning-day branch of `_next_interval_days`; learning steps remain fixed 5/12 min.

---

## 4. High-value additions

### A1. Phase 0.5 — shared analytics substrate (`src/study_stats.py` + `GET /api/study/stats`) plus composite indexes `(owner, reviewed_at)` and `(owner, attempted_at)`.
### A2. Phase 0.6 — thin service-layer extraction (decks/cards/queue/review ownership + writes), **preceded by endpoint contract tests**.
### A3. Phase 2.1 — interval fuzz (already pulled forward).
### A4. Phase 2.2 — pretesting promoted from Phase 5.
### A5. Phase 0.2/0.4 — LaTeX/KaTeX sanitization: ensure KaTeX output in `markdown.js` is sanitized or `strict:true`; add test with `$\\href{javascript:alert(1)}{x}$` and inline `$\\text{<script>}$`.
### A6. Accessibility/privacy/regression-suite gates folded into Phase 4 and Phase 1.1 ACs.
### A7. Per-deck export/import of cards + questions + logs.

---

## 5. CODEX re-sequenced roadmap

```
Phase 0  Stabilize & secure
  0.1  Durable review/attempt/focus ratings + short-circuiting idempotency keys
  0.2  Security: characterization test for upload confinement; AI-endpoint rate limiting; KaTeX sanitization caveat
  0.3  Numeric confidence slider + pinned enum→numeric migration
  0.4  Golden-value & missing tests + regression-suite gate
  0.5  Shared analytics substrate (study_stats.py + /api/study/stats) + composite indexes
  0.6  Endpoint contract tests + thin service-layer extraction (decks/cards/queue/review)

Phase 1  Close the data loop
  1.1  Per-user FSRS parameter fitting (>=400-review threshold, warm-start, deterministic, 5/12-min excluded)
  1.2  Calibration loop: compute + display numeric-confidence curve
  1.3  Connect study plan to FSRS mastery; plan regeneration warns/best-effort preserves done_blocks

Phase 2  Retrieval-practice hardening
  2.1  Interval fuzz ±25% (flag-gated, seeded, review-state only)
  2.2  Pretesting mode (promoted from Phase 5)
  2.3  Elaborative-interrogation + JOL prompts
  2.4  Adaptive question selection / desirable difficulty
  2.5  Typed-recall / wrong-MCQ gate

Phase 3  Insight & habit
  3.1  Real progress dashboard (uses shared substrate)
  3.2  Link Focus sessions to Plan blocks / decks
  3.3  Non-coercive motivation (flex streaks, implementation intentions)
  3.4  Per-deck export/import

Phase 4  Architecture & fidelity
  4.1  Complete routes/study/ + static/js/study/ modularization + a11y gates
  4.2  Semantic interleaving
  4.3  Async DB / background queue for stats (optional)

Phase 5  Optional / future
  FSRS-5/6 short-term migration
  Faded worked examples
  Delayed feedback for high-confidence errors
  Bulk operations + offline service-worker queue
```

---

## 6. Risk register updates (from GLM/CODEX)

| Risk | Mitigation |
|---|---|
| Retry re-applies scheduler | Short-circuit on idempotency key before `fsrs.schedule()`. |
| Historical calibration shifts meaning after numeric migration | Pin enum→numeric mapping; backfill into separate `confidence_numeric` column. |
| Regeneration erases done_blocks | Warn user; best-effort preserve only when new blocks match old block keys; otherwise reset. |
| KaTeX bypasses HTML sanitizer | Add KaTeX output sanitization or `strict:true`; regression test. |
| Service-layer split regresses routes | Write endpoint contract tests before splitting. |
| Anchors drift again before build | Freeze on a committed SHA; final pass by implementation agent. |

---

## 7. Updated worker assignments

| Workstream | Lead | Support |
|---|---|---|
| Re-anchoring + SHA freeze | orchestrator | `kimi-k2-7-code` |
| Durable ratings + idempotency + numeric confidence | `kimi-k2-7-code` | — |
| AI rate-limiting + KaTeX fix + owner audit | `qwen3-coder-480b` | `security-review` |
| Shared analytics substrate + optimizer | `deepseek-v4-pro` | `qwen3-235b-a22b` |
| Plan↔FSRS + pretesting | `glm-5-2` | — |
| Dashboard + frontend modularization | `kimi-k2-7-code` | `simplify` |
| Golden tests + regression suite | `qwen3-235b-a22b-instruct-2507` | — |
| Quality passes | `code-review`, `security-review`, `verify` | — |

---

**CODEX Round-2 verdict:** Accept GLM's corrections on anchoring, KaTeX sanitizer bypass, idempotency short-circuit, pinned confidence mapping, and the need for pre-split contract tests. Reject GLM's "3-bin calibration now" sequencing: numeric confidence is a prerequisite, not a refinement. Freeze all anchors against a committed SHA before the build crew starts.
