# Round 1 — Agent GLM Final Change Proposal

**Author:** Agent GLM (glm-5-2) — author of `02-core-logic.md` and the `glm-5-2.md` panel review; arguing freshly.
**Date:** 2026-06-29
**Stance:** Approve with changes — the "logs everything, learns from nothing" thesis is correct, but the plan is not yet a faithful build guide. Below is my concrete, prioritized, sequenced edit list. Every claim is verified against `HEAD` (anchors confirmed by me, not inherited from the panel — see §0, because the panel's own "fix" was itself partly stale).

**Settled inputs (6/6 panel, not relitigated):** stay on personalized FSRS-4.5, defer 5/6; calibration **curve only**, no auto-downgrade of Easy. I file one-line dissents in §9.

---

## §0 — A correction to the consolidated review BEFORE we start

The consolidated review's consensus fix #1 says: "`_resolve_uploaded_file` at `study_routes.py:1035` (not 1070)". **That correction is itself wrong.** I read `routes/study_routes.py:1030-1120`: `_resolve_uploaded_file` is at **line 1070** — the *original plan* was right; the panel's "correction" moved the anchor 35 lines in the wrong direction. A build crew following the consolidated review's "fix" will land on `_study_part_order_key` (a question-sorting helper), not the file resolver.

**Implication:** the re-anchoring task (consensus fix #1) must be done against `HEAD` by a single pass, and the panel's specific line numbers **cannot be trusted as a drop-in replacement** — they are themselves approximate. My verified anchor table (§1) is the one to use. This is the single most important meta-finding of Round 1: *the fix for stale anchors introduced new stale anchors.*

Verified anchors I will use throughout (all confirmed by reading source this round):

| Symbol | Plan/panel said | Actual (`HEAD`, verified) | Evidence |
|---|---|---|---|
| `rateCard` | plan `study.js:1224-1244`; panel `:1458` | **`static/js/study.js:1458`** | `grep` + read `:1455-1484` |
| `/attempt` POST (client) | plan `:1511`; panel `:1728` | **`static/js/study.js:1723`** (handler block starts `:1720`) | read `:1720-1744` |
| `/focus/{id}/finish` POST (client) | plan `:2053`; panel `:2110` | **`static/js/study.js:2110`** | `grep` + read `:2105-2113` |
| `_resolve_uploaded_file` | plan `:1070-1110`; panel `:1035` | **`routes/study_routes.py:1070`** | read `:1030-1120` |
| `rating_from_outcome` | `02` `:395-414`; plan `1.2` | **`src/study_ai.py:507`** | `grep` + read `:507-525` |
| `ASK_COACH_SYSTEM` | plan `2.3` `:495-505` | **`src/study_ai.py:614`** | `grep` |
| `_priority` | plan `1.3` `:25-29` | **`src/study_plan.py:31`** | `grep` + read `:31-35` |
| `mix` (interleaving) | `02` `:191-203` | **`src/study_plan.py:216-217`** | read `:205-225` |
| `fsrs.schedule` card site | plan `1.1` `:1755` | **`routes/study_routes.py:1753`** (omits `w=`) | read `:1750-1758` |
| `fsrs.schedule` question site | plan `1.1` `:2780` | **`routes/study_routes.py:2780`** (omits `w=`) | read `:2778-2790` |
| `DEFAULT_W` | plan `fsrs.py:141` | **`src/fsrs.py:33`** (the array); `schedule` sig at `:138-141` | read `:33, :138` |
| `LEARN_AGAIN/HARD_MIN` | (n/a) | **`src/fsrs.py:46-47`**; used `:196,199,210,240` | read |
| `confidence` enum | (n/a) | **`core/database.py:1802`**; validated `study_routes.py:2772` | read both |

---

## §1 — Tier A: MUST FIX before handing to builders (blocking)

These are correctness/faithfulness fixes. The plan is not a safe build guide without them.

### A1. Re-anchor EVERY `file:line` reference against `HEAD` — *S, ~1h*
- **What:** A single mechanical pass over `00-improvement-plan.md` replacing every stale anchor with the verified table in §0 above. Do **not** use the consolidated review's numbers as-is (see §0).
- **Why:** Phase 0.1's entire AC ("killing the network loses zero ratings") points at the wrong function (`:1224` vs `:1458`); Phase 0.2's security item points at `:1070` (plan, correct) while the panel "fix" sends builders to `:1035` (wrong). A build guide with wrong anchors misdirects agents to edit unrelated code.
- **Risk:** Low; pure doc edit. **But:** the risk of *not* doing it is high — a builder edits the wrong function and the real bug stays.
- **Effort:** S (~1h), one careful pass.

### A2. Re-scope Phase 0.2 — DROP path-traversal, KEEP AI rate-limiting — *S, ~2h*
- **What:** Delete the "Fix path traversal in `_resolve_uploaded_file()`" bullet from 0.2. Keep "audit every study endpoint for the `_owner` check" and "add rate limiting to AI endpoints." Reword the path-traversal AC from a bug-fix ("`../` rejected") to a **characterization test** ("assert the existing `basename`+`realpath`+`_confined()` confinement at `study_routes.py:1070-1110` continues to hold") so it becomes a regression guard, not a no-op.
- **Why:** Path-traversal is **already fixed.** I read `routes/study_routes.py:1070-1110`: it does `os.path.basename(file_id)` (`:1073`), `os.path.realpath(UPLOAD_DIR)` (`:1075`), and `_confined()` via `realpath(p).startswith(root + os.sep)` on **all three** resolution paths (index `:1091`, legacy direct `:1098`, walk `:1102` with `followlinks=False`). `../` and absolute-path inputs are rejected today. The AI-rate-limiting half is **genuinely absent**: `src/rate_limiter.py` exists (a `RateLimiter` class) and `auth_routes.py:90-132` uses the exact pattern (`_login_limiter = RateLimiter(max_requests=15, window_seconds=60); if not limiter.check(request.client.host): raise 429`), but `grep rate_limit|RateLimiter routes/study_routes.py` returns **zero hits**. The 11 AI-bearing endpoints to throttle (verified `@router` list): `/ai/generate-cards` `:1779`, `/ai/quiz` `:1815`, `/ai/grade` `:1872`, `/materials/{id}/reextract-text` `:2166`, `/materials/{id}/notes` POST `:2224`, `/decks/{id}/overview` POST `:2276`, `/materials/{id}/extract` `:2310`, `/questions/{id}/explain` `:2892`, `/questions/{id}/explain-further` `:2924`, `/cards/{id}/explain-further` `:3043`, `/overview` `:3093`.
- **Risk:** Low. Throttling by IP (the existing `RateLimiter.check(ip)` pattern) is the simplest correct approach; note it does not cover a single user behind a NAT sharing one IP, but that matches the existing auth-routes convention and is good enough for Phase 0.
- **Effort:** S (~2h): drop one bullet, add `from src.rate_limiter import RateLimiter` + a module-level `_ai_limiter = RateLimiter(max_requests=20, window_seconds=60)` and a `if not _ai_limiter.check(request.client.host): raise HTTPException(429,...)` guard on each AI route. Plus the characterization test.

### A3. Add SERVER-SIDE idempotency to the durable-rating queue (Phase 0.1) — *M, ~4h*
- **What:** Add an `idempotency_key` column (client-generated UUID) to `StudyReview` and `StudyAttempt`, with a UNIQUE constraint on `(owner, idempotency_key)`. The client (0.1's retry queue) generates the key at submit time and resends it on retries. Server: on conflict, return the previously-computed result instead of re-applying `fsrs.schedule`. This prevents a retried review from double-incrementing `reps`/`lapses` and double-inserting a log row.
- **Why:** This is the panel's consensus fix #4 and it's correct, but the reason is sharper than "duplicates." Verified: `routes/study_routes.py:1762-1769` (`review_card`) does `card.reps = result["reps"]; card.lapses = result["lapses"]` **and** `db.add(StudyReview(...))` in one transaction — so a retried POST recomputes `schedule()` from the *already-updated* card state, producing a *third* (wrong) interval on top of the double count. `StudyReview.id` is a fresh `uuid.uuid4()` per request (`core/database.py:1691`) with **no UNIQUE on any natural key** — confirmed by reading the schema at `core/database.py:1688-1703`. The same gap exists for `StudyAttempt` (`:1789-1810`, `id=str(uuid.uuid4())` at `study_routes.py:2795`, no natural-key constraint). **This is not just data noise — it corrupts the Phase 1.1 optimizer's training set with phantom reviews**, and because 0.1 *introduces* retries, 0.1 would *create* the bug that 1.1 then trains on. Idempotency must land *with* 0.1, not after.
- **Risk:** Medium. Migration must be additive (nullable column + unique index); old clients that don't send a key fall back to the current non-idempotent behavior (acceptable — only the new durable client retries). The conflict path needs to re-fetch and return the prior result without re-applying the schedule, which is a small but real logic change.
- **Effort:** M (~4h): schema migration (nullable `idempotency_key` + unique index), client key gen, server dedup branch in both `review_card` and `attempt_question`, plus tests for the retry-is-idempotent case.

### A4. Fix calibration sequencing — MERGE compute+display into one slice, BEFORE the numeric slider — *M, re-sequencing*
- **What:** The plan has calibration split across three phases: compute logic in 1.2, numeric-confidence slider in 2.1, calibration *chart* in 3.1. This is the panel's consensus fix #3. **My decision:** merge "compute the calibration curve" (1.2) and "display the calibration chart" (3.1) into a **single vertical slice in Phase 1**, and explicitly **do NOT block it on 2.1's numeric slider**. Render the curve on the existing 3-bin `confidence` signal (`sure|unsure|guess`) as a 3-point scatter/line; the numeric slider (2.1) later refines the x-axis granularity but is not a prerequisite for *showing* the learner their gap. Drop the "Easy-gate" from 1.2 entirely (settled input b).
- **Why:** The panel offered two options (move 2.1 before 1.2, OR merge 1.2+3.1). I take the merge because (i) 2.1 is a frontend migration with DB backfill and is a larger, riskier slice than drawing 3 dots; gating calibration on it delays the highest-leverage *visible* payoff behind a migration. (ii) A 3-point calibration curve is still pedagogically useful — "your 'sure' answers are 62% correct" is actionable today. (iii) The dependency the plan draws (`1.2 ──> 2.1`) is backwards for the *display*; it's only forwards for the *auto-downgrade* (which we've killed). Verified the signal: `StudyAttempt.confidence` is `String` `sure|unsure|guess` (`core/database.py:1802`), populated at `study_routes.py:2772`, consumed by `rating_from_outcome` for the Easy gate (`study_ai.py:517,525`) — but **nothing reads it back for a curve** (confirmed: no `study_stats.py` exists). So the curve is pure new logic over existing captured data — exactly the "read back the logs" thesis.
- **Risk:** Low-medium. The merge makes 1.2+3.1 a slightly larger slice (backend stats helper + a chart), but it removes a cross-phase dependency and ships the visible payoff earlier. The 3-bin→numeric migration in 2.1 must define its enum→number mapping explicitly (see B6) or historical points shift meaning.
- **Effort:** M (~6h): `study_stats.py` calibration function + `GET /api/study/calibration` + a 3-point chart in the stats view.

### A5. Gate per-user FSRS fitting on a minimum-review threshold — *S, part of 1.1 AC*
- **What:** Add to Phase 1.1's AC: "per-user `w` is only applied when the user has **≥ N reviews** (recommend N=400); below N, `schedule()` uses `DEFAULT_W` (warm-start fallback). The fallback path is exercised by a test." Add a **determinism** AC: "same logs ⇒ same `w`, bit-for-bit." Add an **owner-scope** AC: "`StudyUserParams` is owner-scoped; the fit job never writes or reads another user's `w`." Add the panel's consensus fix #6 caveat: "the 5/12-min learning-step intervals (`fsrs.py:196,199,210`, constants `LEARN_AGAIN_MIN=5`/`LEARN_HARD_MIN=12` at `:46-47`) are **fixed re-drill delays, not memory-stability metrics** — they are NOT fed to the optimizer as stability observations."
- **Why:** Consensus fixes #5 and #6. Verified the 5/12-min constants and their use: `src/fsrs.py:46-47` defines them with the comment *"Sub-day memory isn't modelled by FSRS-4.5, so these are pragmatic re-drill delays, not predictions"* (`:44-45`). They're applied via `timedelta(minutes=LEARN_AGAIN_MIN)` at `:196,210` and `LEARN_HARD_MIN` at `:199`. The codebase *already documents* that these are not stability — the optimizer just must not forget it. A user with 12 reviews will trivially "lower log-loss on held-out" by overfitting; the threshold prevents that. Owner-scoping matters because `StudyUserParams` (the fitted `w`) is the most sensitive aggregate on the box — it reveals what a user finds hard, their lapse patterns, study cadence.
- **Risk:** Low for the threshold/caveat (AC text + a guard in the fit job). The determinism AC is a real constraint on optimizer implementation (no stochastic init unless seeded).
- **Effort:** S (~2h) for the ACs + a `if review_count < N: return DEFAULT_W` guard + the caveat doc note. The optimizer itself is the L task in 1.1.

### A6. Pull interval fuzz (±25%) FORWARD as a standalone, decoupled from FSRS-5/6 — *S, ~2h*
- **What:** Split Phase 4.1 into **4.1a (fuzz, do now in Phase 2)** and **4.1b (FSRS-5/6 short-term model, defer to Phase 5)**. Fuzz: add `±≤25%` jitter to `_next_interval_days` output (`src/fsrs.py:132-135`) for *review-state* cards only (not learning steps), seeded per-card so the same card gets the same fuzz on regeneration (deterministic). Keep it behind a flag.
- **Why:** Verified there is **zero fuzz today** (`grep fuzz|jitter|random src/fsrs.py` → nothing; `_next_interval_days` at `:132-135` is `round(interval_for_retention(...))` clamped, no jitter). Same-stability cards pile up on the same due date. Fuzz is a ~20-line, zero-model-change fix that recovers most of W8's user-visible benefit (pile-ups) at a fraction of 4.1's risk. FSRS-5/6 (the short-term component) is the expensive half and is now deferred (settled input a) — so decoupling fuzz from 5/6 lets us ship the cheap win now without the migration.
- **Risk:** Low if seeded (deterministic) and flag-gated. Must NOT fuzz learning-step intervals (those are pedagogically meaningful fixed re-drills). Must update golden tests (A7) to allow fuzzed values or pin the seed.
- **Effort:** S (~2h): a seeded `random.Random(card_id_hash).uniform(0.75, 1.25)` multiplier in `_next_interval_days`, flag-gated, + tests.

### A7. Golden-value FSRS tests + `study_vision` tests (Phase 0.4) — *S, ~3h*
- **What:** As the plan says, but sharpen the AC: add golden-value tests that pin **exact float outputs** of `schedule()` for a few fixture cards against a published FSRS-4.5 reference (not just structural properties). Add unit tests for `study_vision.text_layer_is_thin` (`:176`) and `batch_pages` (`:104`).
- **Why:** Verified `tests/test_fsrs_scheduler.py` (179 lines) explicitly pins *structural properties only* — its docstring (`:8-11`) says *"pin the structural properties... rather than exact float values, so a parameter refit doesn't break the suite."* That's the right call for *structural* tests but means **a `DEFAULT_W` drift would not be caught** — and Phase 1.1 *refits* `w`. Without golden values, a bad optimizer output ships silently. `study_vision.py` exists (`src/study_vision.py`, 6755 bytes) with pure helpers `text_layer_is_thin` (`:176`) and `batch_pages` (`:104`); `grep -l study_vision tests/` returns nothing — zero coverage.
- **Risk:** Low. Golden tests must be authored against a trusted reference (the fsrs4anki optimizer's published values), not hand-computed.
- **Effort:** S (~3h).

---

## §2 — Tier B: HIGH-VALUE additions to adopt (not blocking, but should land)

### B1. Phase 0.5 — shared analytics substrate (`study_stats.py` + `GET /api/study/stats`) — *M, ~4h*
- **What:** Create `src/study_stats.py` as a pure module with the shared queries the optimizer (1.1), calibration (A4), plan-reweighting (1.3), and dashboard (3.1) all need: per-user review/attempt aggregation, per-topic accuracy, retention curve, due forecast. Expose via `GET /api/study/stats`. This is the panel's "Phase 0.5" addition (kimi's idea).
- **Why:** Without it, four workstreams each write their own SQL over `StudyReview`/`StudyAttempt`, which is exactly the N+1-prone pattern already present: `study_routes.py:923` does `db.query(StudyAttempt).filter(...).count()` **inside a loop over `rows`** — a classic N+1. The calibration (A4) and optimizer (1.1) will table-scan these append-only logs on every render unless there's a shared, indexed query path. Verified `owner` columns ARE already indexed (`core/database.py`: `StudyReview.owner` `:1691 index=True`, `StudyAttempt.owner` `:1800 index=True`, `StudyReview.reviewed_at` `:1703 index=True`, `StudyAttempt.attempted_at` `:1810 index=True`) — so deepseek's correction is right: DB-indexing need is partial, but **composite** indexes on `(owner, reviewed_at)` / `(owner, attempted_at)` are still missing and should be added here since the stats queries are always `(owner, time-window)`-shaped.
- **Risk:** Low. Pure additive module. The composite indexes are a cheap migration.
- **Effort:** M (~4h) for the module + endpoint + composite-index migration.

### B2. Promote pretesting out of Phase 5 — *S, ~3h*
- **What:** Move "generation-effect pretesting mode" (present unsolved problems *before* study) from Phase 5 into Phase 2 as **2.5**, ranked after the two-stage testing (2.1) and typed-recall (2.2) which are higher-leverage.
- **Why:** Panel consensus addition (qwen). d≈0.35 is the smallest effect in the catalog, but the implementation is trivial — it's a queue-ordering mode over existing questions (serve a topic's questions *before* its first-contact block), no new schema, no new AI calls. Verified the plan generator (`study_plan.py:generate_plan`) emits `first_contact` blocks and the practice queue is built server-side; a "pretest" mode just reorders. Low cost, real (if modest) gain, and it's evidence-based — belongs above Phase 5 filler like "faded worked examples."
- **Risk:** Low. Must preserve closed-book discipline (pretest is closed-book by definition).
- **Effort:** S (~3h): a queue-mode flag + a route that returns due-by-topic questions in pretest order.

### B3. LaTeX/KaTeX sanitization caveat — *S, ~1h investigation + small fix*
- **What:** Add a security note to Phase 0.2 (or a new 0.5 item): the markdown renderer reinserts KaTeX-rendered HTML **after** the HTML sanitizer pass, so LaTeX macros that emit raw HTML (`\href`, `\htmlEntity`, `\text{...}`) bypass the `sanitizeAllowedHtml` allowlist. Either (i) run the sanitizer over KaTeX output too, or (ii) confirm KaTeX's `strict`/`trust` defaults already neutralize this and document it. Add a test that `$\href{javascript:...}{x}$` and `\text{<script>}` don't execute.
- **Why:** This is a **NEW finding I verified this round**, not in the panel list. Read `static/js/markdown.js`: `sanitizeAllowedHtml` runs at `:722` (reinserting allowed-HTML placeholders), then `mathBlocks.forEach` reinserts KaTeX output at `:726-727` — **after** the sanitizer. KaTeX is called with `throwOnError:false` (`:597,607,616,625`) and no `strict`/`trust` option, so it uses defaults. Study content flows through this path: `_renderMarkdownInto` (`study.js:922`) and `_md` (`study.js:927`) call `mdToHtml` → `innerHTML` for AI notes, explanations, MCQ options, and references. Since study AI content is *locally generated* (not arbitrary web input), severity is **lower than a public-facing XSS**, but a malicious/leaked model prompt or pasted material could inject. Worth a caveat + a test, not a headline item.
- **Risk:** Low-medium. The fix (re-running sanitizer over KaTeX output, or setting KaTeX `strict:true`) could break legitimate math rendering — needs the 1h investigation first.
- **Effort:** S (~1h investigate + ~2h fix if needed). I rank this **below** the rate-limiter and idempotency but **above** the generic "add accessibility" item.

### B4. Add DB composite indexes for stats queries — *S, ~1h* (folded into B1)
- **What:** Add composite indexes `idx_study_reviews_owner_reviewed` on `(owner, reviewed_at)` and `idx_study_attempts_owner_attempted` on `(owner, attempted_at)` as part of Phase 0.5/B1.
- **Why:** Verified the individual columns are indexed (deepseek's correction stands), but the stats/optimizer queries are always `WHERE owner=? AND reviewed_at >= ?` shaped — a composite index turns a scan+filter into an index range seek. Without it, 1.1 and A4 table-scan append-only logs that grow unbounded.
- **Risk:** Low. Additive migration.
- **Effort:** S (~1h), folded into B1.

---

## §3 — Tier C: ADOPT with re-scoping (lower priority, refine not drop)

### C1. Re-estimate modularization (Phase 4.3) UPWARD and start a thin slice in Phase 0 — *L, re-scoping*
- **What:** The plan calls 4.3 an "L" and parks it at the end as "opportunistic." Re-estimate it **upward** (it's realistically 2× an "L" — a 3,553-line route file and 2,119-line JS file split is a multi-week effort) and pull a **thin** service-layer extraction (just `decks`/`cards`/`queue`/`review` routes) into Phase 0 as **0.6**, because 1.1's `w`-threading and A3's idempotency touch the *exact same two call sites* (`study_routes.py:1753, 2780`) buried in that monolith. **Require an endpoint-level contract test be added *before* the split** (request→response shape per route) so the split has a regression net — "full suite green" is near-vacuous for a route layer with no route-level tests today.
- **Why:** Verified `study_routes.py` is 3,553 lines, `study.js` is 2,119 lines (`wc -l`). Phase 1.1 and A3 both edit `review_card` (`:1743`) and `attempt_question` (`:2715`) inside this monolith; doing them *first* and splitting *later* means the split has to preserve those edits blind. Panel (qwen, kimi) flagged the underestimate. The contract-test requirement is my addition — without it, the split's AC ("no behavior regressions") is unenforceable.
- **Risk:** Medium. A bad split is worse than no split. The thin-slice-in-Phase-0 reduces risk by making the later full split incremental.
- **Effort:** L for the full 4.3; S-M for the 0.6 thin slice. Re-estimate the full item as "L+" or split into 4.3a (backend) + 4.3b (frontend).

### C2. Accessibility (ARIA/focus/keyboard) — *M, fold into 4.3*
- **What:** Keep the plan's a11y items (ARIA roles, focus trap, visible focus, keyboard shortcuts, un-hide mobile model selector, bigger touch targets) but **fold them into 4.3** rather than treating as a standalone phase. Verified `study.js` uses native `confirm()`/`prompt()` for card edits (the plan's W9) — these are a11y-hostile and should go inline as part of the frontend split.
- **Why:** Panel (gpt-oss, minimax) addition. Legitimate but not blocking the data-loop thesis.
- **Risk:** Low.
- **Effort:** M, folded into 4.3.

### C3. Privacy controls for calibration data — *S, fold into 1.1/A5*
- **What:** The owner-scope AC in A5 covers the core of this. Add: "the calibration view and the fitted `w` are visible only to the owning user; no cross-user aggregation."
- **Why:** Panel (gpt-oss, minimax) addition. Already largely covered by the `owner` scoping that exists throughout study routes (verified: every `_get_*` helper checks `s.owner != user`).
- **Risk:** Low.
- **Effort:** S, mostly AC text.

### C4. Regression/performance test suite — *M, ~4h*
- **What:** Add a performance AC to 1.1: "fit completes in < T on a 5k-review fixture without holding a write lock" (SQLite blocks on long write txns). Add an endpoint-level regression suite as part of C1's contract tests.
- **Why:** Panel (gpt-oss, minimax) addition. Fitting 17 params on 10k+ reviews is not free; on SQLite a long write txn blocks everything.
- **Risk:** Low.
- **Effort:** M (~4h).

---

## §4 — Tier D: DROP or DOWNRANK from the panel/plan list

### D1. DOWNRANK: Semantic interleaving (Phase 4.2) — keep in Phase 4
- **What:** Leave "semantic interleaving via topic embeddings" in Phase 4. Don't pull it forward.
- **Why:** Verified current interleaving is index-rotation: `study_plan.py:216-217` does `rot = study_days.index(d) % len(names); mix = [names[(rot+j) % len(names)] for j in range(k)]` — pure position rotation, no relatedness. The upgrade to embedding-based grouping requires the embeddings stack and a topic graph — a real dependency. The effect size (d≈0.44 for interleaving) is already *partially* captured by the existing rotation; the *semantic* refinement is a smaller marginal gain than the data-loop items. It belongs in Phase 4 with the other architecture work, not pulled into the high-leverage phases.
- **Risk:** N/A (no change, just don't promote).
- **Effort:** M, stays in Phase 4.

### D2. DROP from immediate scope: FSRS-5/6 short-term model (4.1b)
- **What:** Move 4.1b (the short-term memory state) fully to Phase 5 per the settled input. Fuzz (A6) is the only part pulled forward.
- **Why:** Settled 6/6. Confirmed the only FSRS-5/6-relevant code is the fixed 5/12-min learning steps (`fsrs.py:46-47,196,199,210`) which the codebase *already comments* are not stability metrics — so the gap is real but bounded to the learning phase, a small fraction of a mature user's volume. After 1.1 personalizes `w`, the global-fit gap 5/6 closes is already mostly closed *for that user*.

### D3. DOWNRANK: Elaborative interrogation + JOL prompts (Phase 2.3) — keep but it's the smallest Phase-2 item
- **What:** Keep 2.3 but rank it last in Phase 2 (after 2.1 two-stage, 2.2 typed-recall, 2.4 adaptive selection, B2 pretesting). Re-anchor `ASK_COACH_SYSTEM` from `:495-505` to `:614`.
- **Why:** d≈0.68 is large but the implementation is a prompt-string change (`study_ai.py:614`), so it's nearly free — but the *measurable* payoff is smaller than the retrieval-hardening items. Re-anchoring verified.

### D4. DROP the "5/12-min as stability" idea if it ever appears
- **What:** Already covered by A5's caveat, but worth calling out as an explicit DROP: never feed the learning-step delays to the optimizer as stability observations.
- **Why:** The code itself documents they're not stability (`fsrs.py:44-45`).

---

## §5 — NEW change the panel missed (beyond B3)

### E1. Add a "transaction boundary" requirement to Phase 0 — *S, AC text*
- **What:** Add to Phase 0 (or 0.1) the AC: "all mutating study endpoints run in a single DB transaction; the optimizer fit job writes `StudyUserParams` atomically; plan regeneration (`1.3`) writes the new plan + preserves `done_blocks` in one transaction." Specifically for 1.3: add an AC "regenerating a plan preserves the user's `done_blocks` completion state — checked-off blocks remain checked-off across regeneration."
- **Why:** NEW (from my own prior review, §3.4/§4.3, verified fresh). `study_routes.py:1762-1772` (`review_card`) and `:2780-2800` (`attempt_question`) each do `card.state=...; db.add(StudyReview(...)); db.commit()` — currently in one txn, good. But 1.1's optimizer write (`StudyUserParams` + applying new `w`) and 1.3's plan regeneration are multi-step writes where a partial failure leaves inconsistent state. The `done_blocks` desync risk is concrete: `StudyExam.done_blocks` (`core/database.py:1716`) is a JSON list of `"date:idx"` strings; regenerating the plan changes the block list, so the old `done_blocks` keys may no longer match — erasing the user's progress. No AC covers this today.
- **Risk:** Low (AC text + a preservation helper). The real risk is *not* adding it: a user regenerates their plan and loses all checkmarks.
- **Effort:** S (~2h) for the preservation logic in 1.3.

### E2. Pin the confidence enum→numeric migration mapping (Phase 2.1) — *S, AC text*
- **What:** Add to 2.1's AC: "the migration mapping is pinned: `sure→85`, `unsure→55`, `guess→25` (or whichever values are chosen) and documented; historical calibration is either computed only on post-migration attempts OR recomputed with the pinned mapping; the mapping is covered by a test."
- **Why:** NEW (from my prior review §3.3, verified). `StudyAttempt.confidence` is `sure|unsure|guess` (`core/database.py:1802`). 2.1 migrates to numeric 0-100. Without a pinned mapping, `sure→90` vs `sure→70` silently shifts every historical calibration point's x-coordinate — and A4's calibration curve would *show a different gap* before vs after migration, undermining its credibility. The plan says "old enum values migrated" but doesn't pin the mapping.
- **Risk:** Low (AC text + a test).
- **Effort:** S (~1h).

---

## §6 — Final sequenced edit list (what actually changes in the plan doc)

In order of application:

1. **[A1]** Mechanical re-anchor of all `file:line` refs using the §0 verified table. (Drop the consolidated review's `:1035` correction — it's wrong.)
2. **[A2]** Phase 0.2: delete path-traversal bullet; reword its AC to a characterization test; keep + detail the AI rate-limiting (list the 11 endpoints, show the `RateLimiter` pattern from `auth_routes.py:90-132`).
3. **[A3]** Phase 0.1: add `idempotency_key` to `StudyReview`/`StudyAttempt` (nullable + UNIQUE), client key gen, server dedup branch. Reword 0.1 AC to require idempotent retries.
4. **[A4]** Merge Phase 1.2 (compute) + Phase 3.1-calibration-chart (display) into one Phase-1 slice; render on 3-bin signal now; drop Easy-gate (settled b).
5. **[A5]** Phase 1.1 AC: add ≥400-review threshold + warm-start fallback + determinism + owner-scope + 5/12-min-not-stability caveat.
6. **[A6]** Split Phase 4.1 into 4.1a (fuzz, move to Phase 2) + 4.1b (short-term model, move to Phase 5). Implement 4.1a as seeded, flag-gated jitter in `_next_interval_days`.
7. **[A7]** Phase 0.4: sharpen AC to require *exact-float* golden values vs FSRS-4.5 reference; add `study_vision` helper tests.
8. **[B1]** Insert Phase 0.5: `study_stats.py` + `GET /api/study/stats` + composite indexes `(owner, reviewed_at)` / `(owner, attempted_at)`.
9. **[B2]** Move pretesting from Phase 5 → Phase 2.5.
10. **[B3]** Add LaTeX/KaTeX sanitization note + test to Phase 0.2 (investigate re-running sanitizer over KaTeX output post-`markdown.js:726`).
11. **[C1]** Re-estimate Phase 4.3 upward; insert Phase 0.6 thin service-layer extraction (decks/cards/queue/review) with a contract-test prerequisite.
12. **[C2/C3/C4]** Fold a11y, privacy ACs, and the perf AC into 4.3 / 1.1 respectively.
13. **[E1]** Add transaction-boundary + `done_blocks`-preservation AC to 1.3.
14. **[E2]** Pin confidence enum→numeric mapping in 2.1 AC.
15. **[D1-D4]** Downrank semantic interleaving (stays Phase 4); drop 4.1b to Phase 5; rank 2.3 last in Phase 2; explicit "never feed 5/12-min as stability" caveat.

**New dependency arrows (replaces §6 of the plan):**
```
Phase 0 (stabilize/secure/idempotent)
  ├─ 0.1 durable ratings + idempotency keys ─┐
  ├─ 0.2 rate-limiting + KaTeX caveat        │
  ├─ 0.4 golden tests                       │──> Phase 1
  ├─ 0.5 study_stats.py + indexes (NEW)      │
  └─ 0.6 thin route extraction (NEW)         ┘
Phase 1 (data loop)
  ├─ 1.1 per-user w (≥400 threshold) ──> 4.1a fuzz
  ├─ 1.2+3.1 calibration (MERGED, 3-bin now) ──> 2.1 numeric slider (refines x-axis)
  └─ 1.3 plan↔FSRS (preserve done_blocks) ──> 3.x
Phase 2 (retrieval hardening)
  ├─ 2.1 numeric confidence (pinned mapping)
  ├─ 2.2 typed recall
  ├─ 2.4 adaptive selection
  ├─ 2.5 pretesting (promoted from Phase 5)
  └─ 4.1a fuzz (pulled from 4.1)
Phase 3 (dashboard/habit) — minus the calibration chart (now in Phase 1)
Phase 4 (architecture) — 4.2 semantic interleave; 4.3 modularize (re-estimated) + a11y
Phase 5 (optional) — 4.1b FSRS-5/6; faded worked examples; delayed feedback; bulk ops
```

---

## §7 — Effort summary

| Item | Tier | Effort | Blocking? |
|---|---|---|---|
| A1 re-anchor | A | S (~1h) | YES |
| A2 0.2 re-scope | A | S (~2h) | YES |
| A3 idempotency | A | M (~4h) | YES |
| A4 calibration merge | A | M (~6h) | YES |
| A5 fit threshold/caveats | A | S (~2h) | YES |
| A6 fuzz forward | A | S (~2h) | YES |
| A7 golden tests | A | S (~3h) | YES |
| B1 study_stats.py | B | M (~4h) | no |
| B2 pretesting promote | B | S (~3h) | no |
| B3 KaTeX sanitization | B | S (~3h) | no |
| C1 modularize re-estimate + 0.6 | C | L+ | no |
| C2 a11y (into 4.3) | C | M | no |
| C3 privacy AC | C | S | no |
| C4 perf AC | C | M (~4h) | no |
| E1 txn/done_blocks | E (new) | S (~2h) | no |
| E2 pinned mapping | E (new) | S (~1h) | no |

Tier A total ≈ 20h (the "must fix before builders" gate). Tier A+B ≈ 30h.

---

## §8 — What I do NOT change (agreeing with the plan)

- **The strategic thesis** ("logs everything, learns from nothing") — verified and endorsed; if anything it's *understated* on wiring effort (both `schedule()` call sites omit `w=`, confirmed `:1753, :2780`).
- **The 0 → 1 → 2 → 3 → 4 → 5 phase intent** — sound; my changes re-sequence *within* phases, not the phase order.
- **The worker assignments** (§7 of the plan) — sensible.
- **Closed-book discipline, rating semantics, taper, anti-answer-leak** — preserved; A3 (idempotency) and A6 (fuzz) are the only scheduler-adjacent changes and both are guard-railed.

---

## §9 — One-line dissents on the settled inputs (filed per the rules, not relitigated)

- **(a) FSRS-5/6:** *Dissent:* I agree with deferral, but I'd go further and say 4.1b should be **spiked with a time-boxed experiment after 1.1 has 2-3 months of held-out log-loss data** before even *scheduling* it in Phase 5 — the decision should be data-driven by whether the global fit is still the binding constraint post-personalization, not calendar-driven.
- **(b) Easy auto-downgrade:** *No dissent.* Curve-only is correct; the 3-bin signal (`sure|unsure|guess`) is too coarse to act on automatically, and FSRS's lapse dynamics already self-correct overconfident Easy ratings. Auto-downgrade would double-penalize and corrupt the calibration signal.

---

*End of Round 1 proposal — Agent GLM.*
