# Round 1 — Agent CODEX Proposal

**Model:** `kimi-k2-7-code` (Agent CODEX, code-first seat)  
**Date:** 2026-06-29  
**Scope:** Concrete edits to `docs/study-upgrade/00-improvement-plan.md` before it becomes a build guide.

---

## 1. Executive position

The unanimous strategy is right: **close the data loop first**. But the current plan is not yet a faithful build guide — its file anchors are stale, one security item is already fixed, and the calibration sequence is backwards. As CODEX, I will not relitigate the two fixed inputs (stay on FSRS-4.5; show calibration, do not silently downgrade Easy). My proposal: apply the six consensus fixes, then harden the plan with code-grounded changes the panel identified: a shared analytics substrate, forward-pulled interval fuzz, numeric-confidence as a true Phase-0 signal, promoted pretesting, earlier modularization touch-points, and explicit accessibility/privacy/LaTeX-sanitization/regression-suite gates.

---

## 2. Fixed inputs — one-line dissents only

| Consensus decision | CODEX dissent |
|---|---|
| (a) Stay on personalized FSRS-4.5, defer FSRS-5/6 | **Agreed.** I would only add: keep an experimental `w` plumbing path in v1 so the *fuzzer* can be tested on a 5/6 model later without another refactor. (No behavior change.) |
| (b) Calibration: curve only, no auto-downgrade | **Agreed, strongly.** Any future Easy-gate must be opt-in and require numeric confidence; write that into the plan. |

---

## 3. Consensus required changes (apply exactly)

### R1. Re-anchor every file:line reference in the plan
**What:** Replace stale anchors with current-source locations verified against `HEAD`.
- `rateCard`: `static/js/study.js:1458–1479`
- practice submit `/questions/{id}/attempt`: `static/js/study.js:1720–1744`
- focus finish `/focus/{id}/finish`: `static/js/study.js:2102–2113`
- `_resolve_uploaded_file`: `routes/study_routes.py:1070–1111`
- `rating_from_outcome`: `src/study_ai.py:507–528`
- `ASK_COACH_SYSTEM`: `src/study_ai.py:614` (search needed; line may drift)
- `_priority`: `src/study_plan.py:31`
- FSRS `schedule` / learning steps: `src/fsrs.py:196–201`
- `StudyReview` log: `core/database.py:1688–1698`
- `StudyAttempt` log: `core/database.py:1789–1802`

**Why:** Verified stale by `kimi`, `glm`, and orchestrator spot-checks. A build guide with wrong anchors sends implementation agents to the wrong code.
**Risk:** Low. Pure docs change; requires one pass with `grep -n`.
**Effort:** XS.

### R2. Re-scope Phase 0.2 security: drop path-traversal, keep AI rate-limiting + owner audit
**What:** Remove the "fix path traversal" item. Keep: (1) audit every study endpoint for `_owner` / `_get_*` ownership helpers, and (2) add per-user rate limits to all LLM routes (`/ai/*`, `/extract`, `/notes`, `/overview`, `/reformat`, `/materials/:id/notes`, `/questions/:id/explain`, `/questions/:id/explain-further`, `/cards/:id/explain-further`).

**Why:** Source verification (`routes/study_routes.py:1070–1111`) shows `_resolve_uploaded_file` already uses `os.path.basename` + `os.path.realpath` + `_confined()` on all three resolution paths (index, direct, walk). `../` and absolute paths are already rejected. `grep` for `rate_limit` in `routes/study_routes.py` returns zero hits; the existing rate-limiter in `src/rate_limiter.py` is genuinely unused in study routes.
**Risk:** Low (drops no-op) to Medium (rate-limiting changes UX under heavy AI use).
**Effort:** S.

### R3. Fix calibration sequencing: numeric confidence before calibration feedback
**What:** Re-sequence so Phase 2.1 (numeric confidence slider) becomes **Phase 0.3**, the pre-requisite to both calibration display and any optional Easy-gate. Merge the old "compute calibration" (1.2) and "display calibration" (3.1) into **Phase 1.2 Calibration loop** as one vertical slice: compute curve → surface in dashboard/Today.

**Why:** `StudyAttempt.confidence` is currently a 3-bin string (`sure`/`unsure`/`guess`, `core/database.py:1802`). Building a calibration curve on three coarse bins is statistically weak and would be invalidated by a later numeric migration. Numeric confidence should be captured first so every downstream curve, gate, and dashboard chart uses the same signal.
**Risk:** Medium — requires DB migration, UI slider, and back-compat mapping for historical enums.
**Effort:** M.

### R4. Add server-side idempotency to the durable-rating retry queue
**What:** In Phase 0.1, specify that `/cards/{id}/review`, `/questions/{id}/attempt`, and `/focus/{id}/finish` must accept an optional `idempotency_key` and dedupe on `(idempotency_key)` or a deterministic hash of `(resource_id, client_timestamp, payload)`. Update the AC: "a retried submission creates exactly one row." Phase 0.1 also says queue in `localStorage` and retry with backoff; without server dedupe, retries duplicate `StudyReview` rows and corrupt the Phase 1.1 optimizer training set.

**Why:** `rateCard` (`study.js:1458`) advances `r.idx` and re-renders *before* `await jpost`. The proposed retry queue will therefore double-submit on network flakes unless the server rejects duplicates.
**Risk:** Low. Adds a column/hash and a uniqueness check.
**Effort:** S.

### R5. Gate per-user FSRS fitting on a minimum-review threshold
**What:** In Phase 1.1, require `≥ 400 reviews + attempts per user` before using a fitted `w`; below threshold, fall back to `DEFAULT_W` (warm-start). Evaluate held-out log-loss only when N ≥ 400.

**Why:** Fitting 17 parameters (`src/fsrs.py:DEFAULT_W`) on sparse logs overfits. Verified by `glm`, `gpt-oss`, and `minimax`. Current production passes no `w` at all (`routes/study_routes.py:1755`, `:2780`), so fallback behavior is identical to today.
**Risk:** Low — protects new users from pathological curves.
**Effort:** XS in plan; S in implementation.

### R6. Caveat that 5/12-min learning-step intervals are not stability metrics
**What:** In Phase 1.1 optimizer AC and Phase 4.1 notes, explicitly state: "fixed 5m/12m learning/relearning delays (`src/fsrs.py:196–201`) are operational delays, not FSRS stability predictions. Exclude state=`learning`/`relearning` rows with `interval_days=0` from optimizer training and retention calibration, or treat them separately."

**Why:** `fsrs.py` hardcodes `LEARN_AGAIN_MIN = 5` and `LEARN_HARD_MIN = 12`. The optimizer must not interpret these as evidence of memory stability.
**Risk:** Low. Correctness guard.
**Effort:** XS.

---

## 4. High-value additions — CODEX prioritization

### A1. Phase 0.5 — Shared analytics substrate (`src/study_stats.py` + `GET /api/study/stats`)
**What:** Create a single pure-ish `src/study_stats.py` module and one stats endpoint that aggregates `StudyReview`, `StudyAttempt`, and current FSRS state. Expose reusable functions used by: optimizer (Phase 1.1), calibration curve (Phase 1.2), plan re-weighting (Phase 1.3), dashboard (Phase 3.1), and AI rate-limiting telemetry.

**Why:** Found by `kimi` as the single highest-leverage missing prerequisite. Without it, each workstream will write its own brittle SQL and disagree on definitions.
**Risk:** Medium — becomes a dependency for many phases; requires stable API contract.
**Effort:** M.

### A2. Pull interval fuzz (±25%) forward to Phase 2
**What:** Add `±25%` deterministic jitter to `_next_interval_days` in `src/fsrs.py:133–135`, keyed by card/question ID so fuzz is stable per item. Ship behind flag.

**Why:** `glm` and panel note this is a 20-line, zero-model-change mitigation for same-stability pile-ups. Verified: `preview_intervals` currently returns exact intervals, and `schedule` uses `round()` with no jitter.
**Risk:** Low — does not change memory model, only spreads due dates.
**Effort:** S.

### A3. Promote pretesting to Phase 2
**What:** Add a "Pretest first" mode when generating a deck or importing material: surface 3–5 unsolved questions before the learner studies the source, then re-test after study. Exposes `StudyAttempt` rows with `pretest=true`.

**Why:** `qwen` flags this as trivial to implement and supported by generation-effect research (d≈0.35). Currently buried in Phase 5.
**Risk:** Low — entirely additive, flag-gated.
**Effort:** S.

### A4. Promote modularization from Phase 4.3 into Phase 0/1 touch-points
**What:** Do not wait for a monolithic Phase 4.3. During Phase 0, extract a thin `src/study_service.py` layer covering deck/card/question ownership, writes, and transactions. During Phase 1, split the first stats/AI callers out of `routes/study_routes.py`. Leave the full `routes/study/` package and `static/js/study/` split as Phase 4.2 continuation.

**Why:** `kimi`, `glm`, and `qwen` all note that Phase 1.1/1.3 wiring must happen inside the 3,553-line `routes/study_routes.py` and 2,119-line `static/js/study.js` monolith. Incremental extraction reduces regression surface and makes later tests possible.
**Risk:** Medium — any refactor can introduce regressions.
**Effort:** L (cumulative), but amortized and gated by existing tests.

### A5. Accessibility, privacy/LaTeX sanitization, and regression-suite gates
**What:** Add explicit Phase-0/Phase-3 acceptance criteria:
- **Accessibility:** keyboard-only Practice/Review walkthrough passes; visible focus; ARIA on rating/confidence buttons; model selector visible on mobile.
- **Privacy:** `StudyUserParams` and calibration curves are owner-scoped; per-deck export of cards + questions + logs; no cross-user stats read.
- **LaTeX sanitization:** every AI endpoint that parses student text or renders Markdown must sanitize `$...$` and `\(...\)` to prevent injection/rendering bugs; add a golden test.
- **Regression suite:** before any Phase merges, run (a) `test_fsrs_scheduler.py` + new golden-value tests, (b) `verify` skill against a seeded study account, (c) endpoint contract tests for all mutating study routes.

**Why:** `gpt-oss` and `minimax` raised these. The current test suite has no golden FSRS value checks, no `study_vision` tests, and no route-level contract tests.
**Risk:** Low to Medium — mostly testing and docs.
**Effort:** M.

---

## 5. Things CODEX explicitly DROPS or DOWNRANKS

| Panel/plan item | CODEX verdict | Rationale |
|---|---|---|
| FSRS-5/6 migration (Phase 4.1) | **Drop from Phase 4; move to Phase 5/spike** | Fixed input. Must be decoupled from fuzz, which can stand alone. |
| Auto-downgrade Easy → Good | **Drop** | Fixed input. Optional Easy-gate may be revisited only after numeric confidence and opt-in design. |
| Path-traversal "fix" in Phase 0.2 | **Drop** | Already fixed in source. Replaced with rate-limiting + owner audit. |
| Full Phase 4.3 modularization | **Downrank to incremental Phase 0/1 service-layer split + Phase 4 continuation** | A big-bang split is XL risk; touch-point extraction pre-optimizer is safer. |
| Typed-recall mode / wrong-MCQ gate (Phase 2.2) | **Keep but downrank below numeric confidence, pretesting, and fuzz** | High value for recall, but depends on practice-player refactor; defer to Phase 2.2 after 0.3/2.1 land. |
| Semantic interleaving (Phase 4.2) | **Keep in Phase 4** | Requires embeddings graph; not on the critical path. |

---

## 6. Re-sequenced roadmap (CODEX)

```
Phase 0  Stabilize & secure
  0.1  Durable review/attempt/focus ratings + idempotency keys
  0.2  Security: drop path-traversal; AI endpoint rate limiting + owner audit
  0.3  Numeric confidence slider (was Phase 2.1) — captures the signal first
  0.4  Golden-value & missing tests + regression-suite gate
  0.5  Shared analytics substrate (study_stats.py + /api/study/stats)
  0.6  Thin service-layer extraction from study_routes.py monolith

Phase 1  Close the data loop
  1.1  Per-user FSRS parameter fitting (gated by ≥400 reviews, warm-start fallback)
  1.2  Calibration loop: compute + display curve (merged)
  1.3  Connect study plan to FSRS mastery

Phase 2  Retrieval-practice hardening
  2.1  Interval fuzz ±25% (pulled forward; behind flag)
  2.2  Elaborative-interrogation + JOL prompts
  2.3  Pretesting mode (promoted from Phase 5)
  2.4  Adaptive question selection / desirable difficulty
  2.5  Typed-recall / wrong-MCQ gate (lower priority)

Phase 3  Insight & habit
  3.1  Real progress dashboard (uses shared substrate)
  3.2  Link Focus sessions to Plan blocks / decks
  3.3  Non-coercive motivation (flex streaks, implementation intentions)

Phase 4  Scheduler fidelity & architecture
  4.1  Semantic interleaving
  4.2  Complete routes/study/ + static/js/study/ modularization + a11y/privacy/LaTeX gates
  4.3  Async DB / background queue for stats (optional)

Phase 5  Optional / future
  FSRS-5/6 short-term migration
  Faded worked examples
  Delayed feedback for high-confidence errors
  Bulk operations + offline service-worker queue
```

---

## 7. Risk register (CODEX additions)

| Risk | Mitigation |
|---|---|
| Optimizer overfits on sparse logs | Hard threshold (≥400 reviews), held-out validation, warm-start fallback. |
| Retry queue duplicates rows | Server-side idempotency keys; deterministic dedupe. |
| Numeric confidence migration mis-maps historical enums | Backfill with explicit mapping (`sure→80`, `unsure→60`, `guess→30`) and flag to exclude pre-migration attempts from curve. |
| Interval fuzz changes scheduling determinism | Seed fuzz from card ID; golden tests compare with fuzz disabled. |
| Service-layer refactor breaks routes | Add endpoint contract tests *before* splitting file. |
| AI rate-limiting frustrates legitimate use | Per-user daily budget with burst bucket, generous defaults, clear error messages. |
| LaTeX/MathJax in prompts causes injection | Normalize `$...$` / `\(...\)` in coach/explain inputs; test with known math markup. |

---

## 8. File:line evidence summary (verified by CODEX)

| Finding | Verified location |
|---|---|
| `rateCard` advances UI before network | `static/js/study.js:1458–1479` |
| Practice submit handler | `static/js/study.js:1720–1744` |
| Focus finish handler | `static/js/study.js:2102–2113` |
| `_resolve_uploaded_file` already confined | `routes/study_routes.py:1070–1111` |
| No `rate_limit` usage in study routes | `grep` returned 0 |
| `rating_from_outcome` maps confidence to Easy | `src/study_ai.py:507–528` |
| `_priority` uses static mastery | `src/study_plan.py:31` |
| FSRS learning/relearning steps fixed 5/12 min | `src/fsrs.py:196–201` and constants `LEARN_*_MIN` |
| `StudyReview` / `StudyAttempt` append-only logs | `core/database.py:1688–1698` / `:1789–1802` |
| `w` not passed in production schedule calls | `routes/study_routes.py:1755`, `:2780` |

---

## 9. Worker assignments (updated)

| Workstream | Lead | Support | Reasoning |
|---|---|---|---|
| Durable ratings + idempotency + numeric confidence | `kimi-k2-7-code` | — | Frontend + backend contract. |
| AI rate-limiting + owner audit | `qwen3-coder-480b` | `security-review` skill | Large-context coding + security. |
| Shared analytics substrate + optimizer | `deepseek-v4-pro` | `qwen3-235b-a22b` | Math + careful wiring. |
| Plan↔FSRS connection + pretesting | `glm-5-2` | — | Plan generator logic. |
| Dashboard + frontend modularization | `kimi-k2-7-code` | `simplify` skill | JS/UI. |
| Golden tests + regression suite | `qwen3-235b-a22b-instruct-2507` | — | Thorough testing. |
| Final quality passes | `code-review`, `security-review`, `verify` skills | — | Built-in gates. |

---

**CODEX verdict:** Apply R1–R6, insert A1–A5, drop/downrank as listed, and re-sequence as shown. The plan then becomes a faithful build guide with correct anchors, a corrected calibration dependency chain, durable data from the start, and a shared substrate that makes every later phase cheaper.
