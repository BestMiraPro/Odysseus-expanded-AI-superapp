# Review of `00-improvement-plan.md` — Study Module Upgrade

**Reviewer:** glm-5-2 (author of `02-core-logic.md`, reviewing independently)
**Date:** 2026-06-29
**Method:** Read all five sibling docs in full, then spot-checked the actual source files the plan anchors against (`src/fsrs.py`, `src/study_plan.py`, `src/study_ai.py`, `routes/study_routes.py`, `static/js/study.js`, `src/rate_limiter.py`, `core/database.py`, `tests/`).

**Verdict (short form):** **Approve with changes.** The strategic thesis and phase order are sound and well-motivated; the plan is implementable. But several *file:line anchors are wrong*, the **single highest-profile security finding is stale**, and a few acceptance criteria are too weak to actually guard the invariants the plan claims to protect. These are fixable without re-architecting the plan.

---

## 1. SOUNDNESS — "logs everything but learns from nothing"

**Agree — and the evidence is stronger than the plan states.** I verified every leg of the claim against source:

- **No optimizer exists.** `ls src/fsrs_optimize.py src/study_stats.py` → both "No such file or directory". Confirmed absent.
- **`w` is never threaded per-user.** The card-review call at `routes/study_routes.py:1753` passes only `desired_retention=_flt(deck.retention, 0.9)` — it does **not** pass `w=`. Every `fsrs.*` function defaults to `w=DEFAULT_W` (`fsrs.py:141`). So the plan's claim "plumbing already accepts `w`" is true at the *function signature* level but **false at the call sites** — Phase 1.1 needs real wiring work, not just a new optimizer module. The plan undersells this slightly ("thread `w` from `routes/study_routes.py:1755` and `:2780`") by implying it's a one-liner; the second call site (`:2780`, question attempt) also omits `w=`, and the deck's per-deck retention is the only personalization knob today.
- **Calibration is captured, not surfaced.** `StudyAttempt.confidence` is a `String` column (`core/database.py:1802`, values `sure|unsure|guess`); `rating_from_outcome` consumes it for rating, but `grep -l study_vision tests/`-style search shows nothing reads it back for a curve. Correct.
- **Plan is static.** `_priority = importance * (6 - mastery)` at `study_plan.py:31` uses the user-supplied 1–5 `mastery` bucket; nothing injects FSRS stability. Correct.

So the headline is not just supported — it's *understated* on the wiring effort. One caveat for intellectual honesty: the app is not *literally* learn-from-nothing — FSRS itself adapts per-card (stability/difficulty drift) and per-deck (desired retention). The accurate framing is "**the 17 global memory-model parameters never personalize, and the logs never feed back into the plan or into a calibration view.**" The plan's phrasing is rhetorically fine but a reviewer of the implementation will push back on "learns from nothing" literally. Recommend softening to "logs everything but never optimizes the memory model or closes the calibration loop."

## 2. SEQUENCING

**The 0 → 1 → 2 → 3 → 4 → 5 order is right, with two caveats.**

- **Phase 0 before 1 is correct and non-negotiable.** Phase 1.1 fits `w` from `StudyReview`/`StudyAttempt`; if ratings are silently dropped (W2, verified: `rateCard` at `study.js:1458` does `r.idx += 1; renderReview()` **before** `await jpost`, and on catch only `toast()`s — no rollback, no queue, no retry), the optimizer trains on a systematically censored sample (lost ratings are disproportionately the *last* rating of a flaky session, not random). That bias would silently corrupt the fit. So 0.1 (durable ratings) is a genuine *statistical* prerequisite for 1.1, not just a nicety — the plan should say so explicitly rather than just "hard prerequisite (durability + security)."
- **0.4 (golden tests) is correctly placed *before* 1.1.** You cannot safely refit `DEFAULT_W` without golden-value guards. Verified: `tests/test_fsrs_scheduler.py` (179 lines) pins structural properties only, deliberately not exact floats (`:8-11`). No golden-value tests exist today. Good call.
- **Dependency the plan *under*-states:** Phase 1.2's "Easy-gate via calibration" (the contested item 5b) depends on a *reliable* calibration curve, which depends on a *numeric* confidence, which is Phase 2.1. The dependency arrow `1.2 ──> 2.1` in §6 is drawn backwards relative to the *behavioral* dependency: the Easy-gate reads "recent 'sure' accuracy," but `sure|unsure|guess` is a 3-bin signal — auto-downgrading on 3-bin data is noisy. If 5b is chosen as "auto-downgrade," it should land **after** 2.1, not in Phase 1. The plan puts the behavior in 1.2 and the better signal in 2.1; that ordering is only safe if 5b is chosen as "show only."
- **Item I'd re-prioritize:** Phase 4.3 (modularize the 3,553-line `study_routes.py` and 2,119-line `study.js`) is parked at the end as "opportunistic," but Phase 1.1's `w` threading touches the *exact same two call sites* (`:1753`, `:2780`) buried in that monolith, and 1.3's plan↔FSRS wiring threads state through `generate_plan`'s caller in the same file. Doing 4.3 *incrementally* before 1.x would lower the risk of every later phase. I'd move a *thin* service-layer extraction (just decks/cards/queue/review) into Phase 0 as 0.5, not defer all of 4.3. The plan's "modularize as you touch" principle (§3.5) is correct in spirit but the roadmap doesn't actually schedule the touch.

## 3. RISKS & FEASIBILITY

### 3.1 Stale / inaccurate findings (must fix)

- **W3 "Path-traversal risk in `_resolve_uploaded_file()`" is STALE.** I read `routes/study_routes.py:1070-1110`: the function already (a) strips to `os.path.basename(file_id)`, (b) resolves with `os.path.realpath`, and (c) confines via a `_confined()` check (`os.path.realpath(p).startswith(root + os.sep)`) on *all three* resolution paths (index lookup, legacy direct join, and the `os.walk` tree). `../` and absolute-path inputs are already rejected. Phase 0.2 should **drop** the path-traversal bullet and keep only the owner-check audit + rate-limiting. As written, 0.2's AC ("`../` and absolute-path inputs are rejected by a test") will *trivially pass today* because the fix already exists — making the AC useless as a regression guard unless the AC is reworded to "assert the existing confinement holds" (a characterization test, not a bug fix). This is the most important correction in this review: the plan's headline security item is a no-op.
- **Rate-limiting claim is correct.** `src/rate_limiter.py` exists (a `RateLimiter` class) but `grep -n "rate_limit\|RateLimit" routes/study_routes.py` returns **zero hits** — AI endpoints (`/ai/*`, `/extract`, `/notes`, `/overview`) are genuinely unthrottled. So the *rate-limiting* half of 0.2 is valid and worth doing; just drop the path-traversal half.

### 3.2 Wrong file:line anchors (must fix before handing to builders)

The plan and `02-core-logic.md` carry several anchors that don't match the current source. A builder following them literally will edit the wrong code:

| Plan / 02-doc anchor | Claimed location | Actual location |
|---|---|---|
| `rating_from_outcome` | `study_ai.py:395-414` (02 §3.4) | `study_ai.py:507` |
| `ASK_COACH_SYSTEM` | `study_ai.py:495-505` (plan 2.3) | `study_ai.py:614` |
| `rateCard` (durable ratings) | `study.js:1224-1244` (plan 0.1) | `study.js:1458` |
| `/questions/{id}/attempt` submit | `study.js:1511` (plan 0.1) | `study.js:1728` |
| `/focus/{id}/finish` submit | `study.js:2053` (plan 0.1) | `study.js:2110` |
| `mix` (interleaving) | `study_plan.py:191-203` (02 §2.2) | `study_plan.py:216-217` |
| `_priority` | `study_plan.py:25-29` (plan 1.3) | `study_plan.py:31` |

These drift because the docs were written against a slightly earlier source. They're cosmetic for a *review* but **dangerous for a build guide** — Phase 0.1's entire AC ("Killing the network mid-session loses zero ratings") points at the wrong function. Recommend a one-pass re-anchoring against `HEAD` before approval.

### 3.3 Acceptance criteria that are too weak

- **0.1 durability AC** — "no duplicate submissions" is asserted but the implementation sketch (queue in `localStorage`, retry with backoff) has no AC for **idempotency on the server side**. If the client retries a `/cards/{id}/review` after a partial success, does the route double-count `reps`/`lapses`? The AC needs: "a retried review is idempotent — server dedupes by (card_id, reviewed_at-window) or returns the prior result." Without this, durability *creates* a new data-integrity bug (duplicate reviews polluting the optimizer's training set in 1.1).
- **1.1 optimizer AC** — "lowers log-loss vs `DEFAULT_W` on held-out reviews" is the right shape but **missing the minimum-N threshold**. The §8 mitigation mentions "minimum-review threshold + warm-start fallback," but the AC doesn't enforce it. A user with 12 reviews will *always* "lower log-loss" on held-out by overfitting; the AC should require "≥ N (e.g. 400) reviews *and* held-out improvement *with the fallback path exercised* for users below N." Also missing: a **determinism** AC (same logs ⇒ same `w`, bit-for-bit) since the optimizer feeds the deterministic plan generator.
- **2.1 numeric confidence migration** — AC says "old enum values migrated" but doesn't specify the **migration mapping** (`sure→?`, `unsure→?`, `guess→?`). Mapping `sure→90`, `unsure→60`, `guess→30` (common choice) silently changes every historical calibration point's meaning. The AC should pin the mapping or require that historical calibration be computed only on post-migration attempts.
- **4.3 modularization AC** — "no behavior regressions (full test suite green + `verify` skill run)" is necessary but **insufficient for a 3.5k-line route split**: there's no test coverage of the route layer today commensurate with its size. The AC should require an **endpoint-level contract test** (request→response shape per route) be added *before* the split, so the split has a regression net. Otherwise "full suite green" is a near-vacuous guard for this item.

### 3.4 Items that could break existing behavior

- **1.3 plan regeneration from FSRS mastery** changes `generate_plan`'s signature and the determinism tests (`test_deterministic_for_same_inputs`) will break by construction — the plan acknowledges tests must be updated but lists it as an AC, not a risk. Flag it: regenerating a *user's existing, partially-completed* plan (`StudyExam.done_blocks`) can desync the `done_blocks` set from the new block list. The plan has no AC for **preserving completion state across regeneration**. Real risk of erasing the user's progress.
- **4.1 FSRS-5/6 migration** stores new state fields; the plan's "behind a flag with migration of stored state" is right, but the **rollback path** is underspecified. FSRS-5/6 adds a short-term memory state that 4.5 doesn't have; rolling *back* from 5/6 to 4.5 drops that state irreversibly. AC should require a tested downgrade, not just an additive upgrade.

## 4. GAPS

1. **Privacy of learner data in the optimizer (omitted).** §5 says "no data leaves the box" for the optimizer — good. But 1.1 fits 17 parameters from `StudyReview`+`StudyAttempt`, which is the most sensitive aggregate a user has (it reveals what they find hard, their study cadence, lapse patterns). The plan never specifies **where the fitted `w` is stored and who can read it**. For a multi-user self-hosted box (Odysseus supports `owner` scoping throughout), a `StudyUserParams` table must be owner-scoped, and the nightly fit job must never write another user's `w`. Add an explicit owner-scope AC to 1.1.
2. **No concurrency/idempotency story for the rating endpoint.** Phase 0.1 is client-side durability only; the server `/cards/{id}/review` has no idempotency key, no optimistic-concurrency guard. With retries now in play (0.1) this becomes a correctness gap, not just a robustness one (see 3.3).
3. **No transaction boundary for multi-write operations.** `01-backend-routes.md §5` flags "missing transaction handling" and "partial failure handling." The plan's Phase 0 doesn't address this; Phase 1.1's optimizer write (`StudyUserParams` + schedule using new `w`) and 1.3's plan regeneration are both multi-step writes where a partial failure leaves inconsistent state. Add a Phase 0.5 or fold into 0.1: "all mutating study endpoints run in a single transaction; optimizer job writes atomically."
4. **Testing of the optimizer itself is thin in the AC.** 1.1 says "pure optimizer is unit-tested on synthetic logs" but there's no AC for a **property test** (e.g., "on logs generated by `DEFAULT_W`, the recovered `w` is within ε of `DEFAULT_W`") — the only credible cheap test that the optimizer actually works rather than overfits. A log-loss AC alone can pass on a degenerate optimizer.
5. **Performance of the fit job is unaddressed.** Fitting 17 params by gradient/Bayesian on a user with 10k+ reviews is not free; the plan calls it "nightly/opt-in" with no AC for runtime, memory, or that it doesn't lock the DB for the user. On SQLite (Odysseus's stack) a long write transaction blocks everything. Add a "fit completes in < T on a 5k-review fixture without holding a write lock" AC.
6. **No N+1 / index work in Phase 1 even though 1.1 and 3.1 both scan `StudyReview`/`StudyAttempt` heavily.** Indexes are deferred to 4.3. The calibration curve (1.2) and the optimizer (1.1) will table-scan these append-only logs on every stats render; indexes on `(owner, reviewed_at)` and `(owner, attempted_at)` should be a Phase 0 or 1 prerequisite, not Phase 4.

## 5. THE TWO OPEN JUDGMENT CALLS

### (a) Phase 4.1 — migrate to FSRS-5/6 now, or stay on personalized FSRS-4.5?

**Recommendation: stay on personalized FSRS-4.5; defer 5/6 to a post-Phase-3 spike, not Phase 4.**

Reasoning:
- **Marginal value is small *after* 1.1.** The two real gains from FSRS-5/6 are (i) a short-term memory state for sub-day learning intervals and (ii) per-card stability decay. (i) only matters during the *learning* phase (fixed 5/12-min delays today), which is a small fraction of a mature user's review volume; (ii) is a refinement that, per the research note's own citation, is "critical for conceptual knowledge where 90% retention is unsustainable" — a population the app doesn't specifically target. Once 1.1 personalizes the 17 `w` per user, the *global* fit gap that 5/6 mostly closes is already largely closed *for that user*. The plan itself says "4.5 + per-user params already captures most of the gain" — I agree, and that sentence should be the decision, not an open question.
- **Cost is disproportionate to a Phase-4 placement.** FSRS-5/6 adds state that 4.5 doesn't model, requires re-running the optimizer (1.1) against a different objective, re-goldening tests against a new reference, and a reversible state migration with a tested *downgrade*. That's a real research project, not an "L" task. Doing it before Phase 3's dashboard means the dashboard's "retention curve" AC would have to be re-validated against the new model.
- **Fuzz (the cheap half of 4.1) should be decoupled and pulled forward.** Interval fuzz (±25%) is a 20-line, zero-model-change fix for same-stability pile-ups, independently valuable, and has nothing to do with FSRS-5/6. The plan bundles them; **split 4.1 into "4.1a fuzz (do in Phase 2, trivial)" and "4.1b short-term model (defer indefinitely / spike)."** This recovers most of W8's user-visible benefit (pile-ups) at a fraction of the risk.

**Net:** personalized 4.5 is the right stopping point for this upgrade cycle. Revisit 5/6 only if post-1.1 held-out log-loss analysis shows the global fit is still the binding constraint (it almost certainly won't be).

### (b) Phase 1.2 — auto-downgrade "Easy"→"Good" for overconfident users, or show the calibration curve only?

**Recommendation: show the calibration curve only; do NOT auto-downgrade ratings.**

Reasoning:
- **It fights the app's own rating semantics.** `02-core-logic.md §3.4` (which I wrote) is explicit that "the FSRS rating is a report of retrieval quality, not a reward." Auto-downgrading a user's *reported* Easy to Good silently *reinterprets their report* based on an aggregate statistic — that's the scheduler second-guessing the learner's JOL. It muddies the very signal (self-reported confidence) that 1.2 is trying to *improve*. If the user learns that saying "sure" gets silently demoted, they'll game it (say "unsure" to keep Easy), which *corrupts* the calibration data — the exact opposite of 1.2's goal.
- **The signal is too coarse to act on automatically (today).** `confidence` is `sure|unsure|guess` (`core/database.py:1802`). "Recent 'sure' accuracy below threshold" on a 3-bin signal is noisy; the threshold is arbitrary; and the windowing isn't specified. Phase 2.1 *replaces* this with a 0–100 numeric slider — so building auto-downgrade on the 3-bin signal in Phase 1, then replacing the signal in Phase 2, is building a behavioral gate on a known-ephemeral input. If auto-downgrade is ever to ship, it should be re-evaluated *after* 2.1, on numeric data, and behind the same flag — not committed to now.
- **Calibration training works by *showing* the gap, not hiding it.** The literature the plan cites (metacognition d≈0.47) is about *feedback* to the metacognitive monitor, not about the system overriding the monitor. Forcing the downgrade removes the learner's chance to self-correct on the next rating, which is where the learning happens.
- **FSRS already absorbs the error gracefully.** As `02 §3.4` notes, an overconfident "sure"→Easy→too-long-interval is *self-correcting*: the card lapses, difficulty rises, and the next interval shortens. The scheduler has a built-in remediation; adding a *second* remediation (auto-downgrade) on top double-penalizes and risks over-correction.
- **The flag-controlled caveat in the plan isn't enough.** Even off-by-default, a shipped auto-downgrade becomes a feature someone has to reason about, test, and document. Keeping it *unbuilt* keeps the calibration loop *pure* (capture → display → learner adjusts), which is the cleaner pedagogical design and matches the research note.

**Net:** ship 1.2 as "compute + surface the calibration curve only." Leave `rating_from_outcome` untouched. If, after 2.1's numeric confidence ships and a few months of data show persistent overconfidence *that FSRS's lapse dynamics aren't absorbing*, revisit — but default to the non-coercive principle the plan already states (§3.3).

## 6. VERDICT & HIGHEST-LEVERAGE CHANGE

**Approve with changes.** The strategic thesis (close the data loop) is correct and well-supported; the phase order is right; the worker assignments are sensible. The plan is buildable. But it should not be handed to implementation agents as-is for three reasons: (1) the path-traversal finding is stale and will waste a builder's time + give false security assurance; (2) several file:line anchors are wrong and will send editors to the wrong code; (3) the durability AC lacks server-side idempotency, which would let Phase 0.1 *introduce* the data-integrity bug (duplicate reviews) that Phase 1.1 then trains on.

**Single highest-leverage change:** before approval, re-anchor every file:line citation against `HEAD` *and* re-scope Phase 0.2 to drop the path-traversal claim (it's already fixed at `study_routes.py:1070-1110`) — keeping only the AI-endpoint rate limiting and owner-check audit. A build crew that trusts stale anchors and a stale security finding will both mis-edit source and ship a "security fix" that changes nothing, while the *real* Phase-0 prerequisite (server-side idempotency for `/review`, so the new client retry queue in 0.1 doesn't duplicate the optimizer's training data) goes unlisted. Fix the anchors and the AC; the rest of the plan stands.

---

### Summary of spot-check evidence (for traceability)

| Claim in plan | Verified? | Evidence |
|---|---|---|
| No `fsrs_optimize.py` / `study_stats.py` | ✅ | `ls` → both absent |
| `schedule` call at `:1753` doesn't pass `w=` | ✅ | `sed -n 1753,1757p` shows only `desired_retention=` |
| `rate_limiter.py` exists but unused in study routes | ✅ | `grep` in `study_routes.py` → 0 hits; file present |
| `_resolve_uploaded_file` has path-traversal risk (W3) | ❌ **STALE** | `:1070-1110` already does `basename` + `realpath` + `_confined()` on all 3 paths |
| `study_vision` has zero unit tests | ✅ | `grep -l study_vision tests/` → none |
| `rateCard` advances UI before awaiting network (W2) | ✅ | `study.js:1458` — `r.idx += 1; renderReview()` before `await jpost` |
| `rating_from_outcome` at `study_ai.py:395-414` | ❌ wrong anchor | actual `:507` |
| `ASK_COACH_SYSTEM` at `:495-505` | ❌ wrong anchor | actual `:614` |
| `rateCard` at `study.js:1224-1244` | ❌ wrong anchor | actual `:1458` |
| `/attempt` at `study.js:1511` | ❌ wrong anchor | actual `:1728` |
| `/focus/.../finish` at `study.js:2053` | ❌ wrong anchor | actual `:2110` |
| `mix` at `study_plan.py:191-203` | ❌ wrong anchor | actual `:216-217` |
| `_priority` at `study_plan.py:25-29` | ❌ wrong anchor | actual `:31` |
| `confidence` is 3-bin string `sure|unsure|guess` | ✅ | `core/database.py:1802`; validation `study_routes.py:2772` |
| `study_routes.py` is 3,553 lines / `study.js` 2,119 | ✅ | `wc -l` confirms |
