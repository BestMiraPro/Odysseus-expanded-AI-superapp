# Odysseus Study Module — Upgrade Plan

**Status:** DRAFT — awaiting owner approval
**Date:** 2026-06-29
**Author:** Crew orchestrator (synthesis of 4 parallel analyses)
**Scope:** The Study feature only (`routes/study_routes.py`, `src/study_*.py`, `src/fsrs.py`, `static/js/study.js`, study UI in `static/index.html`/`app.js`).

> This document is the single source of truth for the upgrade. Once approved, it becomes the build guide for implementation agents. Supporting evidence lives in the sibling files:
> - `01-backend-routes.md` — HTTP/orchestration layer audit
> - `02-core-logic.md` — FSRS + plan generator + AI pipeline audit (with file:line anchors)
> - `03-frontend.md` — UI/UX + client API audit
> - `04-research-study-methods.md` — learning-science technique catalog + effect sizes

---

## 1. Executive summary

The Study module is **already one of the most pedagogically serious self-hosted study tools in existence.** It implements a faithful, dependency-free **FSRS-4.5** scheduler, a **deterministic evidence-based plan generator** (retrieval practice, expanding spacing, interleaving, priority = importance × gap, mock exams with a JOL cue, sleep-protected taper, honest cram triage), and a **well-engineered AI pipeline** (multi-layer JSON repair, anti-answer-leak guards, Socratic-during / didactic-after coaching, principled outcome→FSRS rating mapping). It is genuinely grounded in the literature — not gamified fluff.

The biggest opportunity is therefore **not** adding more raw features — it is **closing the loop on data the app already collects but never reads back.** Every review and attempt is logged (`StudyReview`, `StudyAttempt`) with rating, confidence, hints, and timing — yet nothing uses that history to (a) personalize the FSRS parameters, (b) show the learner their confidence-vs-accuracy calibration, or (c) make the static study plan adapt to real mastery. These three "read-back" upgrades are the highest leverage available and require **zero new user behavior.**

Alongside that, there is a tier of correctness/robustness fixes (durable review ratings, path-traversal hardening, route modularization) and a tier of evidence-backed feature additions (confidence-weighted two-stage testing, elaborative-interrogation prompts, FSRS-5/6 fidelity, a real progress dashboard).

**The single highest-leverage theme:** *the app measures everything and learns from nothing yet. Make it learn from its own logs.*

---

## 2. Current-state scorecard

### 2.1 What is strong (preserve, do not regress)
| Area | Verdict |
|------|---------|
| FSRS-4.5 correctness | Faithful to spec; all four equations verified vs published model (`02-core-logic.md §1.3`) |
| Study-plan pedagogy | Excellent — encodes 8 distinct learning-science principles correctly (`§2.2`) |
| AI JSON robustness | Best-in-class multi-stage repair recovers partial LLM output (`§3.2`) |
| Outcome→FSRS rating map | Principled: hints⇒Hard, confidence gates Easy (`§3.4`) |
| Anti-answer-leak | Three-layer defense (regex + extraction prompt + audit pass) |
| Coaching split | Socratic mid-attempt, didactic post-attempt — pedagogically correct |
| Closed-book discipline | Consult/hint during a live question is penalized; free after submit |
| Test coverage of `study_ai.py` / `study_plan.py` | Strong (~55 + ~18 tests) |

### 2.2 Headline weaknesses
| # | Weakness | Source |
|---|----------|--------|
| W1 | **Logs are write-only** — FSRS never personalizes; calibration never surfaced; plan never adapts | `02 §1.4, §4.2, §5.2#7` |
| W2 | **Review ratings are not durable** — UI advances before the network call; failure = silent data loss, no retry/offline queue | `03 §2.4, §4.5#1` |
| W3 | **Path-traversal risk** in `_resolve_uploaded_file()`; inconsistent owner checks; AI endpoints unthrottled | `01 §5` |
| W4 | **Monoliths** — `study_routes.py` (3,553 lines) and `study.js` (2,119 lines) are single files; hard to maintain/test | `01 §5`, `03 §4.4` |
| W5 | **No progress dashboard** — no retention curve, per-topic accuracy, calibration graph, or due forecast | `03 §1.7, §5` |
| W6 | **MCQ allows recognition over recall**; confidence is a coarse 3-word scale | `03 §3.3` |
| W7 | **Plan ↔ scheduler disconnect** — two systems that never share state | `02 §2.4, §4.2#9` |
| W8 | **FSRS-4.5 only** — no FSRS-5/6 short-term component, no interval fuzz (same-stability pile-ups) | `02 §1.6` |
| W9 | **Native `prompt()` for card edits**; weak a11y; model selector hidden on mobile; Focus sessions not linked to Plan | `03 §4` |
| W10 | **Test gaps** — `study_vision.py` has zero unit tests; no golden-value FSRS tests | `02 §5.1` |

### 2.3 Research coverage matrix (what evidence-based methods are/aren't covered)
From `04-research-study-methods.md`, mapped to this app:

| Technique (effect size) | Coverage | Action |
|---|---|---|
| Retrieval practice (d≈0.77) | **Strong** | Keep; add typed-recall option |
| Distributed/spaced practice (d≈0.66) | **Strong** (static) | Make cadence adapt to FSRS stability |
| FSRS scheduling | **Strong** (4.5, global params) | Personalize params; move toward 5/6 |
| Interleaving (d≈0.44) | **Partial** (rotation) | Semantic/related-topic clustering |
| Desirable difficulty | **Strong** | Extend to adaptive question selection |
| Metacognition / calibration (d≈0.47) | **Partial** (captured, not shown) | Surface calibration curve; JOL prompts |
| Confidence-weighted two-stage testing (d≈0.72) | **Missing** | Add — top-ranked addition |
| Elaborative interrogation (d≈0.68) | **Missing** | Add "Why?" prompts to coaching |
| Self-explanation (d≈0.55) | **Partial** | Structured reflection prompt |
| Feedback timing | **Partial** | Optional delayed feedback for high-confidence errors |
| Generation effect / pretesting (d≈0.35) | **Missing** | Pretest-before-study mode |
| Worked examples / dual coding | **Missing** | Lower priority; faded worked examples for procedural content |

---

## 3. Guiding principles for this upgrade
1. **Read back the logs.** The biggest wins use data already collected — no new user burden.
2. **Don't break the science.** Every change must preserve closed-book discipline, the rating semantics, and the taper. Add golden tests before touching FSRS.
3. **Evidence over gamification.** Motivation features must be effort-based and non-coercive (flex streaks, implementation intentions) — never punitive.
4. **Ship in vertical slices.** Each phase is independently shippable and reversible behind a flag.
5. **Modularize as you touch.** No big-bang rewrite; split files opportunistically when implementing a slice in that area.

---

## 4. Phased roadmap

Each item lists: **what**, **where** (file anchors), **acceptance criteria (AC)**, and a **suggested implementation worker** (model best-fit). Effort is T-shirt size.

### PHASE 0 — Stabilize & secure (must land first) — *correctness, no behavior change*

**0.1 Make review ratings durable (W2)** — `S`
- *What:* Don't advance the card UI until `/cards/{id}/review` succeeds; on failure, queue the rating in `localStorage` and retry with backoff. Same for `/questions/{id}/attempt` and `/focus/{id}/finish`.
- *Where:* `static/js/study.js:1224–1244` (`rateCard`), `:1511` (attempt), `:2053` (focus finish).
- *AC:* Killing the network mid-session loses zero ratings; queued ratings flush on reconnect; no duplicate submissions.
- *Worker:* `kimi-k2-7-code` (frontend).

**0.2 Security hardening (W3)** — `M`
- *What:* Fix path traversal in `_resolve_uploaded_file()` (canonicalize + confine to uploads root); audit every study endpoint for the `_owner` check; add rate limiting to AI endpoints (`/ai/*`, `/extract`, `/notes`, `/overview`).
- *Where:* `routes/study_routes.py:1070–1110`, all `@router` handlers, `src/rate_limiter.py`.
- *AC:* `../` and absolute-path inputs are rejected by a test; every mutating endpoint verifies ownership; AI endpoints return 429 past a per-user budget.
- *Worker:* `qwen3-coder-480b-a35b-instruct` + a `security-review` skill pass.

**0.3 Request de-duplication / loading locks (W2)** — `S`
- *What:* Disable submit buttons + guard against double-fire for Create subject, Extract, Generate-plan, Generate-cards.
- *Where:* `static/js/study.js` (relevant handlers).
- *AC:* Double-clicking never creates duplicates.
- *Worker:* `kimi-k2-7-code`.

**0.4 Golden-value & missing tests (W10)** — `S`
- *What:* Add FSRS golden tests vs the published FSRS-4.5 reference for a few fixture cards; add unit tests for `study_vision.text_layer_is_thin` and `batch_pages`.
- *Where:* `tests/test_fsrs_scheduler.py`, new `tests/test_study_vision.py`.
- *AC:* `DEFAULT_W` drift is caught by a failing test; `study_vision` pure helpers covered.
- *Worker:* `deepseek-v3-1` or `qwen3-235b-a22b-instruct-2507`.

### PHASE 1 — Close the data loop (highest leverage) — *the core of this upgrade*

**1.1 FSRS per-user parameter optimization (W1, P0 in `02 §6`)** — `L`
- *What:* New module `src/fsrs_optimize.py` that fits the 17 `w` parameters per user from `StudyReview`+`StudyAttempt` history (minimize predicted-vs-actual retention error, à la the `fsrs4anki` optimizer). Store fitted `w` (new `StudyUserParams` table or column); pass to `schedule(..., w=user_w)`. Provide a safe **warm-start fallback** for users with too little history (keep `DEFAULT_W`, optionally stratified).
- *Where:* new `src/fsrs_optimize.py`; thread `w` from `routes/study_routes.py:1755` and `:2780` (plumbing already accepts `w`, `fsrs.py:141`).
- *AC:* With ≥N reviews, a nightly/opt-in job produces a `w` that lowers log-loss vs `DEFAULT_W` on held-out reviews; scheduler uses it; pure optimizer is unit-tested on synthetic logs.
- *Worker:* `deepseek-v4-pro` (algorithm) — author the optimizer; `qwen3-coder-480b` wires it.

**1.2 Calibration feedback loop (W1, W6; research #1, d≈0.72)** — `M`
- *What:* New pure helper `src/study_stats.py` computing the confidence-vs-accuracy curve from `StudyAttempt`. Surface it in a stats view. Optionally gate `rating_from_outcome`'s Easy path: downgrade Easy→Good when the user's recent "sure" accuracy is below threshold (pass optional `calibration` arg).
- *Where:* `src/study_ai.py:rating_from_outcome` (`§3.4`), new `src/study_stats.py`, new `GET /api/study/calibration`.
- *AC:* Calibration curve renders; overconfident users see the gap; Easy-gate behavior is unit-tested and flag-controlled.
- *Worker:* `deepseek-v4-pro` (stats logic) + `kimi-k2-7-code` (UI).

**1.3 Connect the study plan to FSRS state (W7, P2 in `02 §6`)** — `M`
- *What:* `generate_plan` accepts each topic's FSRS-derived mastery (mean stability / recent accuracy) and re-weights `_priority` + time allocation from *actual* mastery instead of the static 1–5 bucket. Offer "regenerate plan from current mastery."
- *Where:* `src/study_plan.py:_priority` (`:25–29`) and `generate_plan` signature; route passes aggregated FSRS state.
- *AC:* A topic mastered via reviews loses plan time on regeneration; deterministic given the same inputs (tests updated).
- *Worker:* `glm-5-2` or `deepseek-v4-pro`.

### PHASE 2 — Retrieval-practice hardening (evidence-backed features)

**2.1 Confidence-weighted two-stage testing (research top pick)** — `M`
- *What:* Replace 3-word confidence with a numeric slider (0–100) captured *before* feedback; show calibration over time; this feeds 1.2.
- *Where:* `static/js/study.js` practice player (`:1312–1565`); `StudyAttempt.confidence` becomes numeric (migration, keep back-compat).
- *AC:* Confidence stored as number; calibration graph uses it; old enum values migrated.
- *Worker:* `kimi-k2-7-code` + backend migration by `qwen3-coder-480b`.

**2.2 Typed-recall mode for cards; force "type the answer" on wrong MCQ (W6)** — `M`
- *What:* Optional mode requiring the user to type a keyword before revealing a card's back; after a wrong MCQ, require typing the correct answer/rationale before advancing (strengthens memory, kills recognition-only).
- *Where:* `static/js/study.js` card + practice players.
- *AC:* Mode is opt-in per deck; wrong-MCQ gate works; closed-book preserved.
- *Worker:* `kimi-k2-7-code`.

**2.3 Elaborative interrogation + JOL prompts (research d≈0.68 / 0.47, P4 in `02 §6`)** — `S`
- *What:* Add a "Why does this follow?" branch to `ASK_COACH_SYSTEM`; add a pre-feedback JOL prompt that elicits confidence mid-attempt.
- *Where:* `src/study_ai.py:ASK_COACH_SYSTEM` (`:495–505`).
- *AC:* Prompt fires mid-attempt without leaking answers; covered by a prompt-shape test.
- *Worker:* `deepseek-v4-pro` (prompt design).

**2.4 Adaptive question selection / desirable difficulty (P3 in `02 §6`)** — `M`
- *What:* When building the practice queue, prefer the next item whose predicted `retrievability` is in the ~0.7–0.85 "effortful but retrievable" band, not strictly next-due. Add a pure, testable selector.
- *Where:* new pure selector helper driven by `src/fsrs.py:retrievability`; wired in the queue route.
- *AC:* Selector unit-tested; queue composition shifts toward the target band; flag-controlled.
- *Worker:* `deepseek-v4-pro` + `qwen3-coder-480b`.

### PHASE 3 — Insight & habit (dashboard + motivation, non-coercive)

**3.1 Real progress dashboard (W5)** — `L`
- *What:* New Stats view: retention curve, due forecast (use `preview`/`retrievability`), per-topic accuracy & mastery trend, calibration graph, focus-time trend, projected exam-readiness.
- *Where:* `static/js/study.js` (new tab/module), backed by `src/study_stats.py` + `GET /api/study/stats` extensions.
- *AC:* All charts render from real data on a seeded account; mobile-friendly.
- *Worker:* `kimi-k2-7-code` (UI) + `glm-5-2` (stats endpoints).

**3.2 Link Focus sessions to Plan blocks / decks (W9)** — `S`
- *What:* On focus start, ask "what are you working on?" and offer today's plan blocks; auto-log completed minutes against the block.
- *Where:* `static/js/study.js` Focus (`:1980`), `StudyFocusSession` gains `deck_id`/`block_id`.
- *AC:* Completed focus time attributes to plan blocks; Today tab reflects it.
- *Worker:* `kimi-k2-7-code` + small backend change.

**3.3 Non-coercive motivation (research §4)** — `S`
- *What:* Effort-based streak with weekly **flex days**; implementation-intention prompt at plan generation ("If Tue 7pm, then review X"); spacing reminders tied to next optimal review window.
- *Where:* `static/js/study.js` Today; `src/study_plan.py` (intention cue strings); notifications via existing channels.
- *AC:* Streak survives one missed day/week; reminders fire at the right window; nothing punitive.
- *Worker:* `glm-5-2`.

### PHASE 4 — Scheduler fidelity & architecture (longer horizon)

**4.1 FSRS-5/6 short-term component + interval fuzz (W8, P6 in `02 §6`)** — `L`
- *What:* Add a short-term memory state so Hard/Again on learning cards yield stability-meaningful sub-day intervals (not fixed 5/12 min); add ±≤25% fuzz to avoid same-stability pile-ups. Re-run optimizer (1.1) for the new model.
- *Where:* `src/fsrs.py:schedule` learning branch (`:159–167`), review branch (`:205–213`), `_next_interval_days` (`:133–135`).
- *AC:* Golden tests updated to FSRS-5/6 reference; pile-ups reduced in a simulation; behind a flag with migration of stored state.
- *Worker:* `deepseek-v4-pro`.

**4.2 Semantic interleaving (P5 in `02 §6`)** — `M`
- *What:* Replace index-rotation `mix` with relatedness-based grouping (topic embeddings via existing embedding stack / topic graph) so interleaved blocks pair related topics.
- *Where:* `src/study_plan.py:191–203`; topic-affinity input from `src/embeddings.py`.
- *AC:* Interleaved blocks group related topics; deterministic given embeddings; tests added.
- *Worker:* `glm-5-2`.

**4.3 Modularization & quality (W4, W9)** — `L`
- *What:* Split `routes/study_routes.py` into a `routes/study/` package by concern (decks, cards, queue/review, ai, materials, exams, focus, stats) with a thin service layer; split `static/js/study.js` into `study/{api,cards,practice,plan,focus,stats}.js`. Replace native `prompt()` edits with inline forms; add ARIA roles/focus trap/visible focus; un-hide model selector on mobile; bigger touch targets; keyboard shortcuts for Practice (1–n select, Enter submit, H hint, C consult, N next). Add DB indexes; fix N+1s; standardize error handling.
- *AC:* No behavior regressions (full test suite green + `verify` skill run); a11y audit passes basics; files materially smaller.
- *Worker:* `qwen3-coder-480b` (backend split) + `kimi-k2-7-code` (frontend split) + `simplify`/`code-review` skills.

### PHASE 5 (optional / lower priority)
- Generation-effect **pretesting** mode (present unsolved problems before study). — research d≈0.35
- **Faded worked examples** for procedural content (where SR is a poor fit). 
- Delayed feedback for high-confidence errors.
- Bulk operations + global cross-subject search; offline service-worker queue.

---

## 5. Cross-cutting requirements
- **Feature flags:** every behavioral change (1.1, 1.2 Easy-gate, 2.4, 4.1) ships behind a per-user/per-deck flag, default off until validated.
- **Migrations:** additive and reversible; `StudyUserParams` table (1.1), numeric `confidence` (2.1), `StudyFocusSession.deck_id` (3.2), FSRS-5/6 state (4.1). Provide backfill + rollback.
- **Testing gate:** no phase merges without (a) unit tests for new pure logic, (b) the existing suite green, (c) the `verify` skill run against a seeded study account.
- **No data leaves the box:** optimizer and stats run locally — preserves the local-first/privacy-first promise. No new external calls beyond existing LLM endpoints.
- **Preserve science invariants (regression guards):** closed-book penalty intact; hints⇒Hard; taper present; never schedule on/after exam day; anti-answer-leak audit still runs.

---

## 6. Sequencing & dependencies
```
Phase 0 (stabilize/secure) ─┬─> Phase 1 (data loop) ─┬─> Phase 2 (retrieval features)
                            │                         └─> Phase 3 (dashboard/habit)
                            └─> Phase 4.3 (modularize, opportunistic) 
Phase 1.1 (optimizer) ──> Phase 4.1 (FSRS-5/6 re-fit)
Phase 1.2 (calibration) ──> Phase 2.1 (numeric confidence) ──> Phase 3.1 (dashboard calibration chart)
```
Recommended order: **0 → 1 → 2 → 3 → 4 → 5.** Phase 0 is a hard prerequisite (durability + security). Phase 1 unlocks the differentiated value. Phases 2–3 are user-facing payoff. Phase 4 is fidelity/maintainability.

## 7. Suggested worker assignments (for the build crew)
| Workstream | Lead model | Why |
|---|---|---|
| FSRS optimizer / FSRS-5/6 / stats algorithms | `deepseek-v4-pro` | Strong math/algorithm reasoning |
| Backend routes / migrations / security | `qwen3-coder-480b-a35b-instruct` | Large-context coding |
| Frontend (durability, dashboard, practice UX) | `kimi-k2-7-code` | Coding, JS |
| Plan generator / interleaving / motivation | `glm-5-2` | Balanced reasoning + code |
| Tests / golden values | `qwen3-235b-a22b-instruct-2507` | Thorough, careful |
| Quality passes | `code-review`, `simplify`, `security-review`, `verify` skills | Built-in |

## 8. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Per-user FSRS fit overfits on sparse logs | Minimum-review threshold + warm-start fallback to `DEFAULT_W`; held-out validation |
| Easy-gate frustrates well-calibrated users | Flag-controlled; only triggers below an accuracy threshold; transparent UI explanation |
| FSRS-5/6 migration corrupts schedules | Behind flag; migrate state with reversible backfill; golden tests first |
| Refactor regressions | Opportunistic, slice-by-slice; full suite + `verify` gate per merge |
| Scope creep into the rest of Odysseus | Scope locked to study files listed in §0 |

## 9. Definition of done (for the whole upgrade)
- Phases 0–3 shipped and on by default; Phase 4 shipped behind validated flags.
- Zero rating data loss under network failure; security tests pass.
- A new user can see a calibration curve and an adapting plan after ~2 weeks of use.
- FSRS personalization measurably beats global params on held-out reviews for active users.
- Full test suite green; `verify` skill confirms the practice/review/plan/dashboard flows in the running app.

---

## 10. Approval gate
**This plan is awaiting your approval.** On approval, the build crew executes Phase 0 first. Please confirm or adjust:
1. **Scope** — Study module only, as listed. ✅/✏️
2. **Priority order** — 0 → 1 → 2 → 3 → 4 → 5 (data-loop-first). ✅/✏️
3. **Two judgment calls to confirm:**
   - (a) **FSRS-5/6 migration** (Phase 4.1) — do it, or stay on personalized 4.5? (4.5 + per-user params already captures most of the gain.)
   - (b) **Easy-gate via calibration** (Phase 1.2) — auto-downgrade Easy→Good for overconfident users, or surface the calibration curve only and leave ratings untouched?
4. Anything to **add/drop** from Phase 5.
