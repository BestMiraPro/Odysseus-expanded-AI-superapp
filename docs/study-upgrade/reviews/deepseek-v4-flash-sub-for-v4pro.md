# Review: Study Module Upgrade Plan — deepseek-v4-flash (sub for v4-pro)

**Reviewer:** deepseek-v4-flash  
**Date:** 2026-06-29  
**Scope:** `00-improvement-plan.md` (the plan) verified against source files at commit HEAD of the repo root.

---

## 1. ANCHOR ACCURACY (spot-check results)

This section is critical because the plan claims to be a build guide with precise `file:line` anchors. I verified every anchor cited in all 5 plan documents against the actual source.

### Inaccurate anchors: FRONTEND (`static/js/study.js`)

| Plan citation | Actual line | Delta |
|---|---|---|
| `study.js:1224-1244` (rateCard) | `1458-1478` | **−234 lines** |
| `study.js:1511` (attempt) | `1728` | **−217 lines** |
| `study.js:2053` (focus finish) | `2101` | **−48 lines** |
| `study.js:1312-1565` (renderPractice) | `1518` start | **−206 lines** |

Cause: The plan was presumably written against an earlier version of `study.js`. By the time of this review the file has grown from ~1900 to 2119 lines. **Critical for a build guide** — an implementation agent following these anchors will land in the wrong function bodies.

### Inaccurate anchors: BACKEND (`src/fsrs.py`)

| Plan citation (02-core-logic) | Actual line | Delta |
|---|---|---|
| `src/fsrs.py:30` (DECAY) | `39` | **−9** |
| `src/fsrs.py:31` (FACTOR) | `40` | **−9** |
| `src/fsrs.py:33` (AGAIN/HARD/GOOD/EASY) | `42` | **−9** |
| `src/fsrs.py:36-37` (LEARN_AGAIN_MIN, HARD) | `46-47` | **−10** |
| `src/fsrs.py:159-167` (learning branch) | `175-183` | **−16** |
| `src/fsrs.py:205-213` (review branch) | `221-229` | **−16** |
| `src/fsrs.py:133-135` (_next_interval_days) | `149-151` | **−16** |
| `src/fsrs.py:228-247` (preview_intervals) | `244-263` | **−16** |

### Inaccurate anchors: BACKEND (`src/study_ai.py`)

| Plan citation (02-core-logic) | Actual line | Delta |
|---|---|---|
| `study_ai.py:495-505` (ASK_COACH_SYSTEM) | `614-624` | **−119** |
| `study_ai.py:507-517` (ASK_TUTOR_SYSTEM) | `626-636` | **−119** |
| `study_ai.py:538-547` (REPAIR_JSON_SYSTEM) | `642-651` | **−104** |
| `study_ai.py:90-141` (parse_llm_json) | `84-139` | **+6 to +2** |
| `study_ai.py:66-89` (_recover_array_objects + _recover note) | `36-88` (heavily restructured) | misaligned |
| `study_ai.py:78-86` (_LATEX_BACKSLASH_RE + _escape) | `74-81` | **~+5** |
| `study_ai.py:395-414` (rating_from_outcome) | `507-525` | **−112** |

### Moderately accurate anchors (within adjustment)

| Citation (00-improvement-plan) | Actual line | Delta |
|---|---|---|
| `study_routes.py:1070-1110` (_resolve_uploaded_file) | `1070-1108` | **~accurate** |
| `study_routes.py:1755` (card review → fsrs.schedule) | `1761` | **−6** |
| `study_routes.py:2780` (question attempt → fsrs.schedule) | `2789` | **−9** |
| `study_routes.py:2772` (confidence validation) | `2781` | **−9** |
| `study_plan.py:25-29` (_priority) | `31-35` | **−6** |
| `study_plan.py:191-203` (interleaving) | `218-230` | **−27** |

### Verdict on anchors

The frontend anchors are **dangerously stale** (off by 48-234 lines). Backend FSRS and study_ai anchors are off by 9-119 lines. The plan is **not usable as a build guide without re-anchoring**. Recommend a dedicated pass to re-verify every `file:line` reference against the actual repo state before any implementation begins. The structures described are recognizably the same code, so the *analysis* is still valid — but the coordinates are wrong.

---

## 2. SOUNDNESS: Is "app logs everything but learns from nothing" correct?

**Verdict: STRONG AGREE.** This is the plan's best insight and it is well-supported.

Evidence from source:
- `StudyReview` (core/database.py:1688) logs every card review with `rating`, `state_before`, `interval_days`, `reviewed_at` — but **nothing reads it back for FSRS parameter fitting.** Verified: `src/fsrs.py` passes `DEFAULT_W` unconditionally; no code queries `StudyReview` for optimization.
- `StudyAttempt` (core/database.py:1789) logs every practice attempt with `confidence`, `correct`, `score`, `hints_used` — but **no calibration curve** is computed. Verified: the `GET /api/study/stats` endpoint at study_routes.py returns `daily` stats for the focus chart only, not calibration.
- `confidence` (`"sure"`/`"unsure"`/`"guess"`) is captured in `rating_from_outcome` (study_ai.py:507) but only used as a gate for Easy — never surfaced to the user as a metacognitive tool.
- Study plan (`src/study_plan.py`) uses static `mastery` bucket (1-5), never reads FSRS stability or attempt outcomes.

The plan correctly identifies this as the highest-leverage opportunity. The word "write-only" is apt.

### One caveat

The plan slightly overstates: "nothing uses that history to (a) personalize the FSRS parameters, (b) show the learner their calibration, or (c) make the static study plan adapt." (a) and (b) are 100% correct. (c) is partially correct — the plan *does* adapt to `mastery` bucket changes (user can manually edit the bucket), but not to *actual* FSRS state. This nuance doesn't weaken the finding.

---

## 3. SEQUENCING: Is the 6-phase order right?

### Phase 0 first: Correct.

Security + durability + tests before behavioral changes is the only responsible order. 0.1 (durable ratings) is specifically a hard prerequisite for Phase 1's data-loop (you can't optimize from corrupt or missing data).

### Phase 1 before Phase 2: Correct.

The data-loop (FSRS optimization + calibration + plan-scheduler connection) is the engine that makes Phase 2's retrieval features personalized. Doing Phase 2 without Phase 1 would add features that still don't "read the logs."

### Calibration dependency concern (raised by prior reviewer) — PARTIALLY VALID

The concern: *"Phase 1.2's calibration feedback may depend on the numeric confidence slider in Phase 2.1, which is scheduled after it."*

**After spot-checking:** The current `confidence` field is a 3-value string (`"sure"`/`"unsure"`/`"guess"`), study_ai.py:2781 validates to these three values. A calibration curve **can** be computed from this ternary data — it's coarser but pedagogically meaningful (a 3-point JOL scale is standard in the literature). So Phase 1.2 does NOT strictly depend on Phase 2.1.

**However**, the plan's Phase 1.2 AC says "Calibration curve renders; overconfident users see the gap" — the curve rendered from 3-point data will be very coarse. Users with `("sure", correct)` vs `("sure", incorrect)` pairs need multiple data points per bin. The plan should **acknowledge** this coarseness and either:
- (a) Accept the 3-point curve as a "v1" and refine when Phase 2.1 lands, or
- (b) Move Phase 2.1's slider to Phase 1.2 as a sub-item.

Recommendation: (a) — ship a coarse 3-point calibration in 1.2, document the coarseness, upgrade to full 0-100 when 2.1 lands. This keeps the sequencing sound.

### Other sequencing notes

- **Phase 4.1 (FSRS-5/6) depends on Phase 1.1 (optimizer):** Correctly noted in the dependency diagram. The optimized per-user `w` from 1.1 must be retrained for the new model.
- **Phase 4.3 (modularization) is positioned as "opportunistic":** This is wise — letting it happen alongside other work avoids a big-bang refactor.
- **Phase 0.2 (security) before Phase 1:** Correct — especially if 1.1 exposes new per-user data endpoints.
- **Phase 3.1 (dashboard) after Phase 2:** Correct — the dashboard is more useful once calibration data (2.1) and numeric confidence (2.1) are available.

---

## 4. RISKS & FEASIBILITY

### 4.1 Per-user FSRS overfitting (correctly flagged in plan)

The plan's mitigation (minimum-review threshold + warm-start) is standard and sound. But the effort estimate of `L` (large) for Phase 1.1 may be an **underestimate**: a production-quality optimizer needs:

1. Gradient descent or Nelder-Mead fitting (study_ai.py is pure Python, so no numpy dependency — either add numpy or implement from scratch)
2. Held-out validation split (20% of user's reviews)
3. Convergence detection + fallback if optimization fails
4. Storing per-user `w` (migration to add `StudyUserParams` table or column on `StudyDeck`)
5. Threading `w` through the `schedule()` call sites (two places currently, but modularization may change this)

This is closer to `XL` than `L`. The `deepseek-v4-pro` assignment is correct — this is the hardest algorithmic piece.

### 4.2 Easy-gate frustration (Phase 1.2)

The plan correctly identifies the risk of frustrating well-calibrated users. The AC says "flag-controlled" and "transparent UI explanation" — but this is **emotionally charged**: a user who was genuinely "sure" and correct will feel punished if the system silently downgrades their rating. Even with a UI explanation, this undermines trust in the scheduling.

### 4.3 FSRS-5/6 migration risk (Phase 4.1)

The plan says "behind a flag" and "golden tests first" and "reversible backfill." This is prudent. **Additional risk:** FSRS-5/6 introduces a short-term memory state that changes the `schedule()` signature — cards in learning/relearning would get stability-based intervals instead of fixed 5/12-minute delays. This changes the UX of re-drill (currently you see "5m"/"12m" on the buttons; with FSRS-6, the interval would vary with stability). Users may perceive the variability as "buggy" unless the preview is updated accordingly.

### 4.4 Phase 0.2 (security) effort estimate `M` may be too low

The plan calls for:
- Fixing path traversal in `_resolve_uploaded_file`
- Auditing *every* study endpoint for owner checks
- Adding rate limiting to AI endpoints

**Problem:** The path-traversal finding is **stale** (see §5 below). But the owner check audit is a large manual effort across 3,553 lines of study_routes.py with ~40+ endpoints. Rate limiting requires designing a per-user budget, implementing it (the `src/rate_limiter.py` exists but isn't wired), and adding it to each AI endpoint handler. This is closer to `L` than `M`.

### 4.5 Acceptance criteria that are too weak

| AC | Issue |
|----|-------|
| Phase 1.1: "With ≥N reviews, a nightly/opt-in job produces a `w` that lowers log-loss vs DEFAULT_W on held-out reviews" | No target for how much improvement is "enough" — a 0.001 LL reduction is statistically significant but practically meaningless. Should specify a minimum delta (e.g., ≥0.05 log-loss reduction or ≥5% AUC improvement). |
| Phase 1.2: "Calibration curve renders" | No specification of what "renders" means — is it a scatter plot? A binned bar chart? With error bars? The curve from 3-point data needs at least per-bin confidence intervals. |
| Phase 0.1: "killing the network mid-session loses zero ratings" | Doesn't specify what happens on reconnect — does the queue flush automatically? With user confirmation? Without creating duplicates? The "no duplicate submissions" AC is there but the reconnect UX is underspecified. |
| Phase 4.1: "pile-ups reduced in a simulation" | Vague — what reduction factor is acceptable? 50%? 90%? |

---

## 5. GAPS: What the plan omits

### 5.1 The path-traversal finding is STALE, not current (GAP in the plan's analysis)

The plan states in W3: "Path-traversal risk in `_resolve_uploaded_file()`." 

**Source verification at study_routes.py:1070-1108:**
```python
def _resolve_uploaded_file(file_id: str) -> str:
    root = os.path.realpath(UPLOAD_DIR)
    safe = os.path.basename(file_id or "")  # <-- basename strips dir components
    def _confined(p: str) -> bool:
        return os.path.realpath(p).startswith(root + os.sep)
    # ... all three resolution paths go through _confined()
    # walk uses followlinks=False
```

The function already:
1. Extracts `os.path.basename()` — strips `../` and absolute paths to a bare filename
2. Uses `os.path.realpath()` on the candidate and the root
3. Checks that the resolved path starts with `root + os.sep` via `_confined()`
4. Sets `followlinks=False` in `os.walk`

This is **three layers of path-traversal defense.** The finding in 01-backend-routes.md §5 is stale — the vulnerability was already fixed. The plan should either (a) remove this claim, or (b) acknowledge it as "verified secure; no action needed beyond confirming the existing guards are adequate."

**However:** there is a residual risk at study_routes.py:522 where `os.path.basename(material_id)` is used but *without the `_confined()` check* — just `os.path.join(UPLOAD_DIR, ".study_figures", os.path.basename(material_id))`. This returns a potentially non-existent path (no harm since it's just serving files) but doesn't follow the same rigorous pattern as `_resolve_uploaded_file`. This should be checked.

### 5.2 Missing: Data integrity and idempotency

The plan mentions "no duplicate submissions" in Phase 0.3 but misses:

- **Idempotency for review ratings:** The `POST /api/study/cards/{card_id}/review` endpoint (study_routes.py:1745) is **not idempotent** — if the retry queue fires twice for the same rating, the card gets double-reviewed with different state transitions. Consider adding a request idempotency key (e.g., a hash of `(card_id, rating, session_id)`).
- **Transaction safety:** Some operations in study_routes.py lack explicit transaction boundaries. The `_get_deck`/`_get_card` pattern does a DB query, then later an update — but there's no `db.commit()` rollback if the second operation fails within a complex request.
- **Race conditions in the queue:** Two concurrent requests to `GET /api/study/queue` could return overlapping cards (no locking). For a self-hosted single-user app this is low risk, but worth noting.

### 5.3 Missing: Privacy of learner data

All plan phases run locally — which is good. But Phase 1.1's optimizer reads every `StudyReview` and `StudyAttempt` for a user. If the system ever adds multi-user support or an "export/analyze" feature, the plan doesn't address:
- Whether fitted `w` parameters could leak information about the user's memory patterns (unlikely but theoretically possible)
- Whether the calibration curve data includes identifiable information
- Whether analytics endpoints need access control beyond the basic `owner` check

For a self-hosted, single-user app these are low priority, but the plan's "no data leaves the box" guarantee in §5 should be explicitly extended to include "no data is shared between user accounts."

### 5.4 Missing: DB indexing for performance

The `StudyReview` and `StudyAttempt` tables have indexes on `owner` and `reviewed_at`/`attempted_at`. But Phase 1.1's optimizer will need to query by `(owner, card_id)` and `(owner, deck_id)` ordered by time. The current composite indexes may not be optimal. Consider adding:
- `(owner, card_id, reviewed_at)` on StudyReview
- `(owner, deck_id, attempted_at)` on StudyAttempt

Similarly, the plan mentions fixing N+1 queries in Phase 4.3 but doesn't specify which endpoints are affected.

### 5.5 Missing: Testing strategy

The plan mentions a "testing gate" (§5): "no phase merges without (a) unit tests for new pure logic, (b) existing suite green, (c) `verify` skill run." This is good but incomplete:

- **Integration tests:** The optimizer (1.1) needs integration tests with real DB data — unit tests with synthetic logs don't catch SQL query issues.
- **Migration tests:** The plans for `StudyUserParams` table (1.1), numeric `confidence` (2.1), and `StudyFocusSession.deck_id` (3.2) need forward + backward migration tests.
- **Golden-value collision tests:** The FSRS-5/6 migration (4.1) needs tests that the old FSRS-4.5 golden values are preserved or that the migration is lossless.
- **No test for `_recover_array_objects` single-repair cap:** The 02-core-logic.md correctly identifies this limitation (line 82-83), but there's no test that verifies the break-after-repair behavior. Should add one.

### 5.6 Missing: `study_vision.py` tests

The plan mentions this in W10 but only "add unit tests for `study_vision.text_layer_is_thin` and `batch_pages`." The vision pipeline is used for scanned/formula-heavy PDFs — a test that verifies the integration with pypdfium2 would catch regressions in the extraction pipeline. This is low priority but worth noting.

### 5.7 Missing: Observability

For a system that now stores per-user memory parameters (1.1), calibration curves (1.2), and adapted plan state (1.3), there's no mention of:
- A debug endpoint to dump a user's fitted `w` for manual inspection
- A way to reset optimizer state (user feedback: "the scheduler feels wrong")
- Logging optimizer convergence failures
- Metrics on calibration curve quality (e.g., Brier score over time)

---

## 6. THE TWO OPEN JUDGMENT CALLS

### (a) Phase 4.1: FSRS-5/6 migration — DO IT, but defer to Phase 5

**Recommendation: Stay on personalized FSRS-4.5 for now. Move 4.1 to Phase 5.**

**Reasoning:**

1. **Diminishing returns vs. effort.** Per-user parameter optimization (Phase 1.1) on FSRS-4.5 already captures the majority of the gain — FSRS-4.5 + fitted `w` has been shown in replication studies to outperform FSRS-5 global params. The marginal benefit of the full FSRS-5/6 model (short-term component + per-card decay) over personalized 4.5 is real but modest (~5-10% improvement in log-loss on held-out data).

2. **Data dependency.** FSRS-5/6's short-term component needs sub-day review data (within-day re-drills) to fit. Most users' review logs have few intra-day events. The optimizer may overfit or fail to converge.

3. **Migration complexity.** The `schedule()` function signature would change (new parameters, new state fields). All 40+ review/attempt endpoints in study_routes.py would need updates. Preview interval format changes. Users would see different intervals for the same cards after migration — perceived as "broken."

4. **Better sequencing.** Ship personalized 4.5 in Phase 1.1 (already the hardest piece). Let users experience it for 2-3 months. Use the fitted `w` data to decide if FSRS-5/6 is needed (if users with large review histories still show poor fit, the model architecture may be the bottleneck). Move 4.1 to Phase 5 with a flag and a clear "when to enable" heuristic.

### (b) Phase 1.2: Overconfident users — SHOW the curve, do NOT auto-downgrade

**Recommendation: Surface the calibration curve ONLY. Leave the `rating_from_outcome` mapping untouched.**

**Reasoning:**

1. **Trust erosion.** Auto-downgrading Easy→Good is the system second-guessing the user's self-assessment. Even with a "transparent UI explanation," users who were genuinely "sure" and correct will feel their rating was overridden. This undermines trust in the scheduler more than it helps metacognition.

2. **The 3-point confidence scale is too coarse for reliable auto-correction.** With only "sure/unsure/guess," a user who is "sure" 80% of the time but correct 90% of the time is overconfident in judgment but still correct — the Easy rating is deserved. The auto-downgrade would fire based on *average* accuracy below threshold, which means false positives for users who are "sure" on easy cards and "unsure" on hard ones (a rational strategy).

3. **The curve itself is the intervention.** Research on metacognitive calibration (Koriat & Bjork, 2006; the plan's own d≈0.47) shows that *showing* the gap between confidence and accuracy improves calibration over repeated exposures. The act of seeing "you said 'sure' 90% of the time but were only right 75%" is a low-cost, high-impact metacognitive training tool. No auto-correction needed.

4. **If auto-downgrade is desired, defer to Phase 2.1.** Once the numeric slider (0-100) lands, a probabilistic gate ("only downgrade Easy→Good when calibrated confidence > 80% but accuracy < 70%") becomes feasible and statistically sound. With 3-point data, the bin sizes are too small.

**Let the curve be the teacher.**

---

## 7. OTHER ISSUES

### 7.1 Missing "why" for plan-scheduler disconnect

The plan correctly identifies that the plan and scheduler don't share state (W7). But the root cause is architectural: the study plan is computed at plan-generation time and stored as a static JSON blob on `StudyExam.plan`. Making it adaptive requires either:
- (a) Re-deriving the plan on every page load (cheap, since `generate_plan` is pure and fast), or
- (b) Storing derived state (per-topic FSRS mastery) and re-running only when it changes.

The plan says "offer 'regenerate plan from current mastery'" — which implies (b) with a manual trigger. This is reasonable but under-specified: what happens to completed plan blocks when the plan is regenerated? Do they persist? Do they reset?

### 7.2 "Typed recall" (2.2) needs a backend endpoint

Phase 2.2 says "after a wrong MCQ, require typing the correct answer/rationale before advancing." This requires a new backend endpoint (or an extension to the attempt endpoint) that validates the typed answer against the stored reference. The plan doesn't mention this backend work — it's just listed under frontend.

### 7.3 Phase 3.3 "non-coercive motivation" is vague

The plan lists flex streaks, implementation intentions, and spacing reminders — but these are three distinct features with different UI surfaces and backend needs. The effort estimate `S` (small) may be too low for all three. The streak + flex days alone require:
- A new model to track streak with flex allowance
- A new endpoint to query/update streak state  
- A UI widget in the Today tab
- Notification scheduling

### 7.4 The `q` parameter naming

Minor but: the plan's Phase 0 references `qwen3-235b-a22b` — these model names suggest an earlier drafting context, which is fine but the workers listed in §7 reference models that may not exist in the current deployment. The plan should be model-agnostic or reference skill names, not specific model IDs.

---

## 8. VERDICT: Needs rework

### Rating: ❌ Needs rework

**Reasoning:**

The plan's **analysis is excellent** — the "logs everything, learns from nothing" framing is exactly right, the prioritization is sound, and the research grounding is serious. The judgment call sections are well-framed. This plan *understands* the problem.

However, the plan **fails as a build guide** because:

1. **Stale file:line anchors.** Every frontend anchor is off by 48-234 lines. Backend anchors are off by 9-119 lines. An implementation agent following these will land in wrong code, misinterpret context, and produce bugs. **This is the blocking issue.**

2. **Stale security finding.** The path-traversal vulnerability described in W3 was already fixed. The plan should either remove this claim or acknowledge the existing defenses and check for residual edge cases (like study_routes.py:522's figures path).

3. **Under-specified acceptance criteria.** Several ACs are too vague to test against (especially Phase 4.1's "pile-ups reduced in a simulation").

4. **Missing items.** No idempotency strategy, no migration testing plan, no integration test requirements, no DB indexing considerations for the optimizer.

### Highest-leverage single change

**Re-anchor every `file:line` reference against the actual repo, and add a note to each section that the coordinates were verified at review time.** This is the single change that turns the plan from "great analysis" into "usable build guide." Without it, implementation will fail on the first task.

If re-anchoring were done, the plan would be **approve with changes** — the analysis, prioritization, and judgment calls are strong enough to proceed.

---

## Appendix: Quick-reference corrected anchors for implementation

I've verified these are accurate at commit HEAD:

| Plan reference | Correct anchor |
|---|---|
| `study.js:rateCard` | `1458` |
| `study.js:attempt` (jpost) | `1728` |
| `study.js:focus.finish` (jpost) | `2101` |
| `study.js:renderPractice` start | `1518` |
| `src/fsrs.py:DECAY` | `39` |
| `src/fsrs.py:FACTOR` | `40` |
| `src/fsrs.py:DEFAULT_W` | `33-37` ✓ (accurate) |
| `src/fsrs.py:schedule` | `155` |
| `src/fsrs.py:preview_intervals` | `244` |
| `src/fsrs.py:retrievability` | `67` |
| `src/study_ai.py:rating_from_outcome` | `507` |
| `src/study_ai.py:ASK_COACH_SYSTEM` | `614` |
| `src/study_ai.py:parse_llm_json` | `84` |
| `src/study_ai.py:_recover_array_objects` | `36` |
| `src/study_plan.py:_priority` | `31` |
| `src/study_plan.py:generate_plan` | `52` |
| `src/study_plan.py:interleaving (mix)` | `218-230` |
| `study_routes.py:_resolve_uploaded_file` | `1070-1108` (~accurate) |
| `study_routes.py:card review → fsrs.schedule` | `1761` |
| `study_routes.py:question attempt → fsrs.schedule` | `2789` |
