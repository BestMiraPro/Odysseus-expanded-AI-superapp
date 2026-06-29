# Review: Odysseus Study Module Upgrade Plan

Reviewer: `kimi-k2-7-code`  
Date: 2026-06-29  
Scope: `docs/study-upgrade/00-improvement-plan.md` and its supporting files (`01-backend-routes.md`, `02-core-logic.md`, `03-frontend.md`, `04-research-study-methods.md`), spot-checked against source.

---

## 1. SOUNDNESS — Is the headline finding correct?

**Agree, with minor nuance.**

The plan’s central claim — "the app logs everything but learns from nothing" — is **factually correct** and well-supported by the code.

- `StudyReview` and `StudyAttempt` are append-only logs (`core/database.py:1688–1698`, `:1789–1804`) capturing rating, state_before, interval_days, duration, score, confidence, hints_used, and timing.
- `fsrs.schedule()` accepts a custom `w` vector (`src/fsrs.py:141`) but every production call in `routes/study_routes.py` passes only `desired_retention`, never `w` (`routes/study_routes.py:1755` for cards, `:2780` for questions).
- `src/study_plan.py:_priority()` (`:25–29`) computes topic priority from static `importance × mastery` at plan-generation time. There is no evidence of re-generation from FSRS state or attempt accuracy.
- Confidence data exists in `StudyAttempt.confidence` (`core/database.py:1802`) and feeds `rating_from_outcome` (`src/study_ai.py:401–414`), but it is not read back for calibration feedback.

**Minor nuance:** the plan sometimes equates "FSRS parameter personalization" with "learning." The app *does* use per-card stability/difficulty computed from the global `w` to schedule each card — it is not entirely static. But the claim is correct in the intended sense: **the scheduler is globally parameterized, and no parameters are ever fitted to the user’s own history.**

The evidence in the plan (`02-core-logic.md §1.4`, `§2.4`, `§4.2#7`, `§5.2#7`) is accurate and well anchored.

---

## 2. SEQUENCING — Is the 6-phase order right?

**Mostly right, but with two important caveats.**

| Finding | Verdict |
|---------|---------|
| 0 → 1 → 2 → 3 → 4 → 5 | Sound in principle |
| Phase 0 durability | Hard prerequisite — correct |
| Phase 1 data loop | Highest leverage — correct |
| Phase 2 retrieval features before dashboard | Reasonable, but dashboard calibrations depend on 2.1 |
| Phase 4.3 modularization | Should finish **with** Phase 0/1, not left to Phase 4 |

**Caveat 1 — Modularization is enabling work, not a Phase 4 fidelity add-on.** `routes/study_routes.py` is 3,553 lines with 53 route handlers and highly duplicated `_owner(request)` / `SessionLocal()` / `_get_*` patterns. Splitting it is high-risk, but delaying it means Phases 1–3 will continue to be implemented in the monolith, increasing the migration surface and regression risk. Phase 4.3 should be promoted to **Phase 0b** (split routes into a package) and **Phase 2b** (split `study.js`). The plan’s own dependency graph hints at this tension (Phase 4.3 is shown branching opportunistically from Phase 0).

**Caveat 2 — The data-loop prerequisite is under-specified.** Phase 1.1 (per-user parameter optimization), 1.2 (calibration), and 1.3 (plan↔FSRS connection) all require a clean stats pipeline. There is no explicit pre-phase for a `src/study_stats.py` module + `GET /api/study/stats` refactor. Phase 3.1 (dashboard) then depends on that same pipeline. A shared "analytics substrate" should be Phase 0.5.

**Caveat 3 — Phase 4.1 FSRS-5/6 truly belongs late.** It requires re-fitting the per-user params (`w` vector has different semantics/length in FSRS-5/6) and updating golden tests. Keeping it after data-loop validation is correct.

---

## 3. RISKS & FEASIBILITY

| Risk | Assessment |
|------|------------|
| **Per-user FSRS optimizer (1.1)** | **Underestimated effort.** Fitting 17 parameters requires a robust loss function, held-out validation, and careful handling of review logs that include early-state reviews (state="new"/"learning") with near-zero elapsed time. The plan mentions "gradient/Bayesian fit à la fsrs4anki" but does not acknowledge that an optimizer also needs a *clipper* for reviews with failed local state (`StudyReview.state_before` is logged, so filterable) and a minimum-history threshold. Acceptability criterion "With ≥N reviews" is vague; N should be specified (likely ≥100–200 reviews for 17 params). |
| **Durability / offline queue (0.1)** | **Correct and feasible.** The actual code at `static/js/study.js:1458–1479` advances `r.idx` and calls `renderReview()` *before* the network request, confirming the bug. The fix is straightforward: move `renderReview()` after network success or implement a `localStorage` queue. |
| **Path traversal (0.2)** | **Plan oversimplifies the fix.** `_resolve_uploaded_file()` already uses `os.path.basename()` and a realpath prefix check (`routes/study_routes.py:1070–1111`), but the function also walks the upload tree and trusts an `uploads.json` `stored` path (line 1090). A symlink in the date-based tree could still bypass `_confined()` because `os.walk(..., followlinks=False)` only skips *directory* symlinks; file symlinks are followed. The plan’s AC says "`../` and absolute-path inputs are rejected," but the real failure mode is a symlinked file under `UPLOAD_DIR`. Add: resolve the canonical path via `Path(...).resolve()` and verify it is a regular file under root. |
| **Owner checks (0.2)** | **Partially correct.** Helpers `_get_deck`, `_get_card`, `_get_exam`, etc. (`routes/study_routes.py:1480–1497`) do verify ownership, returning 404 if mismatch. However, `get_current_user` can return `None` when auth is disabled, and in that case the code admits all owners (`user is not None and deck.owner != user`). This is intentional for single-user/local mode, but the plan should note it is an accepted architectural choice, not a gap to "fix." |
| **AI endpoint rate limiting (0.2)** | **Valid gap.** `src/rate_limiter.py` exists and is used in `routes/auth_routes.py`, but not in `study_routes.py`. All LLM endpoints (`/ai/*`, `/extract`, `/notes`, `/overview`, `/reformat`, `/materials/:id/notes`, etc.) currently lack per-user budgets. |
| **Sync DB in async handlers** | **Not addressed.** Every route opens a synchronous `SessionLocal()` inside `async def` handlers, blocking the event loop. This is flagged in `01-backend-routes.md` but absent from the improvement plan’s phases. It should be at least a Phase 4 item, not left out. |
| **Same-day learning step semantics** | **Potential regression if not preserved.** Current `fsrs.schedule()` uses fixed 5m/12m delays in learning/relearning (`src/fsrs.py:196–201`). Replacing them with FSRS-5 short-term modeling would change button previews and daily due counts. The plan correctly says "behind a flag," but acceptance criteria should include A/B verification that retention does not drop for existing users. |
| **Acceptance criteria weakness** | Several AC are too soft. Examples: 1.1 "lowers log-loss vs DEFAULT_W" (by how much? for what coverage?), 1.2 "overconfident users see the gap" (which metric?), 4.3 "files materially smaller" (define: average LOC per route module <250?). |

---

## 4. GAPS — What does the plan omit?

| Area | Gap | Suggested action |
|------|-----|----------------|
| **Privacy / local-first promise** | The plan says "No data leaves the box" for optimizer/stats, but does not explicitly address what happens if cloud LLMs are used for the `/explain-further` or `/ask` routes (these may send question text + source material). Given the plan is for a self-hosted AI workspace, a privacy impact note should be added: "LLM calls for coaching/explanation send question snippets to the configured endpoint; personalization math stays local." | Add explicit statement to §5. |
| **Data integrity / idempotency** | Phase 0.1 AC says "no duplicate submissions," but there is no mention of server-side idempotency keys for `/review` and `/attempt`. If the client retries a queued request after a timeout, the same review could be logged twice. | Add idempotency-key column or dedup on `(card_id, reviewed_at rounded to second, rating, duration_ms)`. |
| **Backup / export / migration of study data** | Users of a self-hosted study tool should be able to export their review/attempt history and re-import it. The plan omits this. | Add Phase 5 item: per-deck JSON export of cards + questions + logs, import with log replay. |
| **Sync DB issue** | As noted above, no phase addresses the blocking `SessionLocal()` calls inside async handlers. | Add Phase 4.4: async DB or background queue for study stats. |
| **Accessibility testing** | The plan lists ARIA/focus fixes but no acceptance test method. | Add AC: keyboard-only walkthrough of Practice and Review passes; run aXe or similar. |
| **Load testing / pile-ups** | FSRS-4.5 with no fuzz creates same-day due piles. The plan’s mitigation is Phase 4.1 fuzz, but there is no measurement of current pile-up severity. | Add quick diagnostic: query `COUNT(*) GROUP BY due_day` on a seeded account. |
| **Question/circuit breaker for overfitted `w`** | The optimizer could produce pathological `w` (e.g., negative growth). No mention of parameter-clamp checks vs plausible ranges. | Add acceptance criterion: fitted `w` must keep monotonicity properties and not exceed published ±3σ bounds. |

---

## 5. TWO OPEN JUDGMENT CALLS

### (a) FSRS-5/6 migration now vs. stay on personalized FSRS-4.5?

**Recommendation: Stay on personalized FSRS-4.5 for now; do not migrate to FSRS-5/6 in this upgrade window.**

**Reasoning:**
1. **Lemma first.** Phase 1.1 (per-user `w` optimization) is the single biggest scheduling improvement and is already data-ready. A personalized FSRS-4.5 will outperform a globally parameterized FSRS-5/6 for most active users. The marginal gain from FSRS-5/6 over *personalized* 4.5 is much smaller than the gain from personalization itself.
2. **Risk order.** FSRS-5/6 changes the state space (short-term memory component, per-card decay) and likely the parameter vector length. That forces a full re-fit of every user’s parameters, a migration of stored card/question state, and new golden tests. Doing that *before* validating the optimizer pipeline invites correlated bugs.
3. **User-visible value.** The short-term modeling mainly improves same-day learning steps (currently fixed 5m/12m). That is a subtle UX improvement, not a headline feature. The dashboard/calibration loop (Phases 1–3) has far higher perceived value.
4. **Future optionality.** Phase 4.1 can remain in the plan as a Phase-4 flagged experiment, but I would defer shipping it to a later cycle where the optimizer is mature and golden tests are stable.

**Conclusion:** invest the Phase 4.1 effort into **making the FSRS-4.5 optimizer excellent** rather than into a model migration.

### (b) Auto-downgrade Easy→Good for overconfident users, or only show the calibration curve?

**Recommendation: Show the calibration curve first; auto-downgrade only as an opt-in per-deck flag, and only after a clear statistical threshold.**

**Reasoning:**
1. **Overconfidence is real.** The current mapping trusts user-reported `confidence == "sure"` for Easy (`src/study_ai.py:401–405`). A user who is poorly calibrated can handshake themselves into artificially long intervals, leading to avoidable lapses.
2. **Auto-downgrade is risky UX.** Changing the user’s self-reported rating behind the scenes without transparency breaks the FSRS contract: the user pressed "sure/Easy" and the system silently mapped it to "Good." This can cause confusion, distrust, and loss of control. It is also hard to explain cleanly in the UI.
3. **Metacognition is trainable.** The literature (Son & Simon, 2012) suggests that *showing* calibration improves judgment faster than *enforcing* it. A calibration curve is a learning intervention, not just a diagnostic.
4. **When auto-downgrade is acceptable.** If implemented, it should be: (a) **opt-in**, ideally at deck creation ("Auto-correct my overconfidence"); (b) **thresholded**, e.g., only when the user’s rolling 30-attempt "sure" accuracy is below 70%; (c) **visible**, with an inline note: "You said ‘sure,’ but your recent accuracy is 62% — this was logged as Good." Without that transparency, the feature is patronizing and brittle.

**Conclusion:** ship Phase 1.2 as **calibration visualization only, with an off-by-default "Easy calibration guard" flag**. If analytics show the guard materially improves retention rates, default it to on for *new* decks only.

---

## 6. VERDICT

**Approve with changes.** The plan is pedagogically sound, technically grounded, and correctly identifies the highest-leverage theme (data-loop closing). However, it underestimates the optimizer, underweights modularization as enabling work, and lacks detail on privacy, idempotency, and async DB issues.

**The single highest-leverage change I would make:**

> **Insert Phase 0.5: build the shared analytics substrate (`src/study_stats.py` + `GET /api/study/stats` + indexed queries) before any personalization, dashboard, or calibration features.**

This substrate is the prerequisite for Phase 1.1 (optimizer), Phase 1.2 (calibration), Phase 1.3 (plan reweighting), and Phase 3.1 (dashboard). Building it first avoids each workstream inventing its own brittle SQL, defines clean acceptance tests for the data loop, and de-risks every downstream phase. Without it, the plan risks implementing four different `SELECT ... GROUP BY` analyses that disagree.

**Secondary high-leverage change:** move Phase 4.3 (route/frontend modularization) earlier — split `routes/study_routes.py` into a `routes/study/` package as Phase 0.3 and `static/js/study.js` as Phase 2.5. That makes every subsequent feature cheaper and testable.

---

## File:line evidence summary

- `core/database.py:1688–1698` — `StudyReview` append-only log.
- `core/database.py:1789–1804` — `StudyAttempt` append-only log.
- `src/fsrs.py:141` — `schedule()` accepts custom `w`; production calls pass only `desired_retention`.
- `routes/study_routes.py:1755` — card review uses global `DEFAULT_W`.
- `routes/study_routes.py:2773–2780` — question attempt uses global `DEFAULT_W`.
- `src/study_ai.py:401–414` — `rating_from_outcome` maps confidence to Easy.
- `static/js/study.js:1458–1479` — `rateCard` advances UI before network success.
- `routes/study_routes.py:1070–1111` — `_resolve_uploaded_file` path resolution and confinement check.
- `routes/study_routes.py:1480–1497` — ownership helpers already exist.
- `src/study_plan.py:25–29` — static `_priority` based on user-input mastery.
- `src/study_vision.py:104–108` / `:176–180` — `batch_pages` and `text_layer_is_thin` have no unit tests.

