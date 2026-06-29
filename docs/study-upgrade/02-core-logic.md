# Study Core Learning Logic — Technical Analysis

Scope: the pure, testable modules that encode the learning science — `src/fsrs.py`,
`src/study_plan.py`, `src/study_ai.py`, `src/study_source.py`, `src/study_vision.py` —
plus the tests that pin their behaviour. The HTTP layer (`routes/study_routes.py`) and
the SQLAlchemy models (`core/database.py`) are referenced only to confirm how these
pure modules are wired in and what is *not* implemented there.

This document is a code-level audit of what the algorithms do today, where they match
or diverge from the published learning-science literature, and where the highest-value
upgrades live. It complements `04-research-study-methods.md` (the technique catalog /
effect sizes) by grounding each finding in `file:line` evidence.

---

## 1. FSRS implementation (`src/fsrs.py`, 248 lines)

### 1.1 What it is

A self-contained, dependency-free Python implementation of the **FSRS-4.5** memory
model (the file's module docstring, `src/fsrs.py:4-20`, cites the
`fsrs4anki` wiki). It is the *scheduler* for both flashcards (`StudyCard`) and practice
questions (`StudyQuestion`), driven from `routes/study_routes.py:1753` (card review) and
`routes/study_routes.py:2780` (question attempt). There is no SM-2, Leitner, or any
other scheduler in the repo — FSRS is the only spaced-repetition engine.

### 1.2 Constants and parameters

- **Decay constant** `DECAY = -0.5` (`src/fsrs.py:30`) and **factor** `FACTOR = 19.0/81.0`
  (`src/fsrs.py:31`). The comment "0.9 ** (1/DECAY) - 1" is the standard FSRS-4.5
  construction so that `R(S, S) == 0.9` exactly. This matches the published spec.
- **Default parameter vector** `DEFAULT_W` (`src/fsrs.py:33-37`): 17 floats, the published
  global-fit values from the open Anki review dataset. These are the FSRS-4.5 global
  parameters (w0–w16): initial stability per rating, initial difficulty, difficulty
  update slope, mean-reversion weight, recall-stability growth terms, and lapse-stability
  terms.
- **Ratings** `AGAIN=1, HARD=2, GOOD=3, EASY=4` (`src/fsrs.py:33`) — the Anki/FSRS
  convention.
- **Card states** `"new"`, `"learning"`, `"review"`, `"relearning"` (`src/fsrs.py:14-20`).
- **Defaults**: `DEFAULT_RETENTION = 0.9` (`src/fsrs.py:39`), `DEFAULT_MAX_INTERVAL = 730`
  (`src/fsrs.py:40`), `MIN_STABILITY = 0.05` (`src/fsrs.py:42`), difficulty clamped to
  `[1.0, 10.0]` (`src/fsrs.py:43-44`).
- **Same-day learning delays** `LEARN_AGAIN_MIN = 5`, `LEARN_HARD_MIN = 12` (`src/fsrs.py:36-37`)
  — fixed minute-scale re-drill delays, explicitly *not* sub-day memory predictions
  (see §1.5).

### 1.3 Core equations (correctness vs FSRS-4.5)

The four update equations are implemented as specified:

- **Retrievability** `retrievability(elapsed_days, stability)` (`src/fsrs.py:67-71`):
  `R = (1 + FACTOR * t / S) ** DECAY`. Correct, including the `stability <= 0 → 0` guard.
- **Interval-for-retention** `interval_for_retention` (`src/fsrs.py:74-80`): solves the
  retrievability equation for `t` at a target retention, with retention clamped to
  `[0.70, 0.99]`. At retention=0.9 this returns `stability` exactly, which
  `test_interval_at_default_retention_equals_stability` pins.
- **Initial stability/difficulty** `init_stability` (`src/fsrs.py:83-84`) uses
  `w[rating-1]`; `init_difficulty` (`src/fsrs.py:87-88`) uses
  `w[4] - (rating-3)*w[5]` clamped to `[1,10]`. Matches the spec.
- **Next difficulty** `next_difficulty` (`src/fsrs.py:91-95`): `d = D - w[6]*(rating-3)`,
  then mean-reverts toward `init_difficulty(EASY)` with weight `w[7]`:
  `d = w[7]*D0(Easy) + (1-w[7])*d`. This is the FSRS-4.5 mean-reversion-to-D0(Easy)
  update. Correct.
- **Recall-stability growth** `next_recall_stability` (`src/fsrs.py:98-112`):
  `S' = S * (1 + exp(w8) * (11-D) * S**(-w9) * (exp((1-R)*w10)-1) * hard_penalty * easy_bonus)`
  with `hard_penalty = w[15]` (Hard only) and `easy_bonus = w[16]` (Easy only), floored at
  `MIN_STABILITY`. This is the FSRS-4.5 successful-review stability update, with the
  Hard-penalty / Easy-bonus multipliers introduced in 4.5. Correct.
- **Lapse stability** `next_forget_stability` (`src/fsrs.py:115-122`):
  `S' = w[11] * D**(-w[12]) * ((S+1)**w[13] - 1) * exp((1-R)*w[14])`, floored at
  `MIN_STABILITY` and **capped at the current `S`** (`min(s, stability)`). The cap is a
  sensible, FSRS-spec-compliant guard: forgetting can never *increase* stability.

The `schedule()` function (`src/fsrs.py:139-204`) ties these together with the state
machine described in the docstring. The four state branches (`new`, `learning`/`relearning`,
`review`) apply the right equation and then handle due-date assignment. A subtle detail
worth noting: in the `learning`/`relearning` branch (`src/fsrs.py:159-167`) the elapsed
time on a same-day step is ~0 so `R≈1`, which means `next_recall_stability`'s
`(exp((1-R)*w10)-1)` term is ~0 and stability growth is negligible. The docstring at
`src/fsrs.py:161-162` calls this out as deliberate "don't reward cramming the same minute"
behaviour. That is a reasonable engineering choice but it is *not* what FSRS-4.5 prescribes
for learning steps — it's an Odysseus-specific adaptation enabled by the sub-day gap
(see §1.5).

One small FSRS deviation, intentional and harmless: at `src/fsrs.py:203-204`, when an
`Easy` rating on a fresh card would graduate to a 1-day interval, the code bumps it to
`min(2, maximum_interval)` so "Easy must beat Good". Pure FSRS-4.5 does not special-case
this; it relies on the `easy_bonus` multiplier alone. The guard is a sensible UI-affordance
fix (an Easy button that previews "1d" would look wrong) and does not corrupt the memory
model. `test_new_card_easy_interval_beats_good` pins it.

### 1.4 Parameter optimization / personalization — **NOT implemented**

This is the single biggest learning-science gap. Every function in the file defaults to
`w=DEFAULT_W` (`src/fsrs.py:83,87,91,98,115,141,232`). The only personalization knob that
exists is **per-deck desired retention**, stored on `StudyDeck.retention`
(`core/database.py:1651`, default `"0.9"`) and passed to `schedule()` at
`routes/study_routes.py:1755` as `desired_retention=_flt(deck.retention, 0.9)`. So a user
can ask for 85% or 95% recall and the *target interval* scales accordingly, but the 17
*memory-model* parameters are frozen at the global fit for everyone.

Crucially, the data needed to fit per-user parameters **already exists and is being logged**:
- `StudyReview` (`core/database.py:1688-1698`) is an append-only table of every card review
  with `rating`, `state_before`, `interval_days`, `duration_ms`, `reviewed_at`.
- `StudyAttempt` (`core/database.py:1789-1804`) logs every question attempt with `correct`,
  `score`, `rating`, `confidence`, `hints_used`, `grading`, `duration_ms`, `attempted_at`.

FSRS-4.5 parameter optimization (gradient/Bayesian fit on the review log to minimize the
predicted-vs-actual retention error) is the canonical way to personalize the scheduler,
and the upstream `fsrs4anki` ecosystem ships a `FSRS Optimizer` for exactly this. None of
that exists here. The roadmap in `04-research-study-methods.md` (§2, "Per-User
Optimization") flags this as a priority.

### 1.5 Sub-day modelling — deliberately absent (a limitation, not a bug)

The module docstring (`src/fsrs.py:11-13`) states FSRS-4.5 does not model sub-day memory
decay, and the implementation handles same-day learning with fixed minute-scale delays
(`LEARN_AGAIN_MIN=5`, `LEARN_HARD_MIN=12`, `src/fsrs.py:36-37`) rather than a continuous
decay model. This is faithful to FSRS-4.5 (which only schedules at day granularity after
graduation) and is a reasonable simplification, but it has two consequences:

1. **Hard in the learning state is identical to Again for scheduling** — both keep the
   card in `learning`/`relearning` and just use a different fixed delay
   (`src/fsrs.py:196-201`): Again → 5 min, Hard → 12 min. There is no stability-based
   distinction between Hard and Again on a not-yet-graduated card. The memory model *does*
   update stability differently (`next_forget_stability` for Again vs
   `next_recall_stability` for Hard, `src/fsrs.py:160-164`), but the *due date* is purely
   a fixed delay. For users who rely on Hard/Again to mean "I want this again sooner vs
   much sooner," the 5-vs-12-minute split is a coarse proxy.
2. **No "preview" quality for learning-state cards** — `preview_intervals`
  (`src/fsrs.py:228-247`) reports `5m`/`12m` for Again/Hard regardless of stability, so
  the review buttons can't show the learner a meaningful growth trajectory until
  graduation.

### 1.6 Other FSRS limitations / observations

- **No FSRS-5/6 features.** FSRS-5 added a separate `retrievability`-based short-term
  memory component and FSRS-6 added per-card stability decay; neither is here. The repo's
  own research note (`04-research-study-methods.md` §2) calls out FSRS-6 per-card decay
  as a target.
- **No fuzz/desired-retention jitter.** Pure FSRS schedules every card of the same
  stability to the same day; Anki adds fuzz (`± up to 25%`) to avoid pile-ups. Odysseus
  has no fuzz, so a deck with many cards at the same stability will all come due on the
  same day. The queue route presumably caps daily volume via `new_per_day`
  (`core/database.py:1650`), but reviews can still cluster.
- **No short-term-memory state.** FSRS-5 introduced a `Relearning` short-term state for
  cards that lapse; Odysseus reuses the day-scale `relearning` state with a 5-minute
  re-drill (`src/fsrs.py:210-212`), which is fine but coarser.
- **`preview_intervals` does not mutate the card** (`test_preview_does_not_mutate_card`),
  good. It works on a shallow `dict(card)` copy (`src/fsrs.py:235`) — safe because the
  card values are primitives, but worth noting if nested objects ever get added.

---

## 2. Study plan generator (`src/study_plan.py`, 237 lines)

### 2.1 Design intent

The module docstring (`src/study_plan.py:4-15`) explicitly enumerates the learning-science
principles it encodes. It is **pure and deterministic** (no LLM, no randomness), which
makes it testable and instantly regenerable — `test_deterministic_for_same_inputs` pins
this. The output is a `{days: [...], meta: {...}}` structure consumed by the plan UI and
persisted on `StudyExam.plan` (`core/database.py:1727`).

### 2.2 Learning-science principles encoded

| Principle | Implementation | Evidence |
|-----------|----------------|----------|
| **Retrieval practice over re-reading** | Every block's `note` instructs closed-book recall; `first_contact` ends with "immediately close the source and recall it onto a blank page" (`study_plan.py:152-157`); `retrieval` is "Closed-book recall and flashcards before re-reading" (`study_plan.py:174-178`) | `src/study_plan.py:151-157, 170-178` |
| **Distributed practice (expanding spacing)** | `BASE_OFFSETS = [1,3,7,14,30]` (`study_plan.py:20`); reviews scheduled at these offsets from each topic's first contact (`study_plan.py:104-111`) | `src/study_plan.py:20, 104-111` |
| **Expanding spacing, compressed to runway** | `_compress_offsets(days_until)` scales the cadence proportionally when `days_until < BASE_HORIZON=40`, dedupes, keeps order (`study_plan.py:32-41`) | `src/study_plan.py:32-41` |
| **Interleaving** | Each non-taper, non-mock day gets an `interleaved` block of 2-4 topics, rotated by day index so pairings vary (`study_plan.py:191-203`) | `src/study_plan.py:191-203` |
| **Priority = importance × gap** | `_priority = importance * (6 - mastery)` (`study_plan.py:25-29`); topics sorted descending so highest-priority get first-contact earliest and most retrieval | `src/study_plan.py:25-29, 88-89` |
| **Mock exams under real conditions** | Mocks at ~60% of runway and 2-3 days before exam (`study_plan.py:113-121`); notes say "closed-book, full exam format. Predict your score before marking" (`study_plan.py:167-171`) — the prediction gap is explicitly called out as calibration data | `src/study_plan.py:113-121, 167-171` |
| **Taper (sleep-protected final day)** | Last study day is `light_review` only — "no new material. Finish early and sleep a full night" (`study_plan.py:143-149`); enforced by `test_last_study_day_is_light_taper_only` | `src/study_plan.py:140-149` |
| **Cram triage honesty** | When `days_until <= 3`, switches to `cram` mode, warns "This buys the exam, not the knowledge" (`study_plan.py:69-86`), goes straight to practice questions, and *still* tapers the last day for sleep | `src/study_plan.py:46-86` |

The notes' pedagogical language ("Feels worse than blocking; tests better", "the
prediction gap is your calibration data", "recall degrades sharply without [sleep]") shows
the author understood *why* each principle works, not just the mechanics.

### 2.3 Scheduling backwards from the exam date

The generator is exam-backwards by construction:
1. `days_until = (exam_date - start).days` (`study_plan.py:65`); `ValueError` if `<= 0`.
2. All study days are `start .. exam_date - 1` minus rest days, with a guard that never
   strips so many rest days that topics can't fit (`study_plan.py:95-96`).
3. `test_plan_never_schedules_on_or_after_exam_day` pins that no day lands on/after the exam.
4. First contacts are spread over the first ~25% of study days, round-robin, highest
   priority first (`study_plan.py:99-101`).
5. Reviews are placed at compressed offsets *from each topic's first contact*,
   snapping to the nearest study day ≥ the target (`study_plan.py:104-111`).
6. Mocks are anchored at 60% of the runway and 2-3 days pre-exam
   (`study_plan.py:113-121`); taper is the last study day (`study_plan.py:123`).

### 2.4 Gaps and limitations in the plan generator

- **No feedback loop.** The plan is generated once from `{importance, mastery}` and
  never re-derives from the user's *actual* review performance (the FSRS state on
  cards/questions, or `StudyAttempt` outcomes). A topic the user has since mastered
  keeps its original slot allocation. The plan and the spaced-repetition scheduler are two
  separate systems that don't talk to each other.
- **No per-topic difficulty curve.** `mastery` is a 1-5 bucket; there's no continuous
  estimate (e.g. from FSRS stability or recent attempt accuracy). `04-research-study-
  methods.md` §1 ("Distributed Practice") explicitly recommends "dynamic runway
  compression … based on per-card stability metrics from FSRS logs."
- **Interleaving is by rotation, not by relatedness.** `mix = [names[(rot+j) % len(names)] for j in range(k)]`
  (`study_plan.py:199`) pairs topics by index arithmetic, not semantic affinity. The
  research note flags "adaptive topic clustering" (group related topics) as a target.
- **Time allocation is coarse.** `daily_min` is rounded to a multiple of 5 and split into
  fixed fractions (50% first contact, 40% retrieval, remainder interleaved,
  `study_plan.py:158-189`). It does not respect the priority-weighted allocation that
  `_priority` implies — a high-priority topic gets earlier first contact but the same
  per-block minutes as a low-priority one.
- **No implementation intentions / time-of-day anchoring.** The research note (§4)
  recommends "If Tuesday 7pm, then review cardiac physiology" style cues; the plan only
  gives dates and block minutes.
- **Cram mode doesn't schedule spacing at all** — by definition, but it means a user who
  hits cram mode gets no retrieval-practice spacing for the material that recurs, which
  the warning acknowledges ("schedule real spacing afterwards if the material recurs",
  `study_plan.py:82-83`).
- **No rest-day recovery.** If a rest day falls on a planned first-contact or review
  target, the topic is silently skipped — `candidates = [d for d in study_days if d >= target]`
  snaps to the next study day (`study_plan.py:108-110`), so reviews shift forward but a
  first contact that lands on a rest day just round-robins to another day in the window.

---

## 3. AI question pipeline (`src/study_ai.py`, 820 lines)

This module holds *all* the pure helpers for the LLM-driven question pipeline: JSON
repair, question normalization, chunking, the outcome→FSRS rating map, and ~20 prompt
constants. It is explicitly "no FastAPI, no DB, no network" (`src/study_ai.py:5-7`) and
fully unit-tested.

### 3.1 Prompt design

The prompt constants (`src/study_ai.py:440-820`) are long, opinionated, and clearly written
by someone who has debugged real LLM extraction failures. Notable design choices:

- **Two math-formatting notes** (`study_ai.py:418-433`): `_MATH_JSON_NOTE` (for prompts
  whose reply is JSON — tells the model to double-escape backslashes inside strings so
  `\\frac` survives JSON) and `_MATH_TEXT_NOTE` (for plain-Markdown replies). Each prompt
  constant gets the right one appended (`study_ai.py:798-820`). This is paired with the
  LaTeX-escaping JSON repair fallback (`study_ai.py:79-87`) — defence in depth.
- **Anti-answer-leakage guidance.** `EXTRACT_QUESTIONS_SYSTEM` (`study_ai.py:453-477`)
  has an entire paragraph forbidding questions that state their own result ("Conclude
  that (3,3) is the solution", "Verify that the gradient is …"). It distinguishes
  legitimate "Prove/Show/Verify" tasks (kept) from solution statements (rejected). This
  is reinforced by a *second* AI pass, `SOLUTION_AUDIT_SYSTEM` (`study_ai.py:671-686`),
  and a cheap regex pre-filter `question_is_conclusion()` (`study_ai.py:286-307`) — three
  layers of defence.
- **Multi-part question handling.** `EXTRACT_QUESTIONS_SYSTEM` tells the model to put
  shared setups in a separate `context` field, not crammed into every part
  (`study_ai.py:461-463`). `ADD_CONTEXT_SYSTEM` (`study_ai.py:688-707`) is a *repair*
  pass that re-adds context the first extraction lost. `LINK_PARTS_SYSTEM`
  (`study_ai.py:735-745`) groups parts into problems; `prereqs_from_groups()`
  (`study_ai.py:761-787`) derives earlier-part prerequisites with a careful numbered-label
  rule that fixes a real bug (a labelled part must never inherit a later part — see
  `test_prereqs_numbered_part_ignores_unnumbered_siblings`).
- **Socratic vs. didactic split.** `ASK_COACH_SYSTEM` (`study_ai.py:495-505`) is strictly
  Socratic for mid-attempt help (never reveals the answer); `ASK_TUTOR_SYSTEM`
  (`study_ai.py:507-517`) is didactic for post-submission learning. This is pedagogically
  correct: coaching during the attempt preserves the retrieval attempt; tutoring
  afterwards exploits the feedback window.
- **Material classification** `classify_material` / `is_answer_key_material`
  (`study_ai.py:336-354`): a regex that treats chapter/lecture/notes as *theory* (never an
  answer key, even with "solutions" in the name) and exam/solution/resit/answer-key as
  *exam*. This drives the `EXPLAIN_FURTHER_SYSTEM` / `LOCATE_MATERIAL_SYSTEM` rule that
  theory citations must come from theory files, never from exam/answer-key files
  (`study_ai.py:571-589, 597-615`) — a genuinely useful guard against citing the answer
  key as the "theory source."

### 3.2 JSON repair robustness

`parse_llm_json` (`study_ai.py:90-141`) is a multi-stage best-effort parser, in order:
1. Strip markdown fences (`_strip_fences`, `study_ai.py:62-64`).
2. Try `json.loads` on the raw text, then a trailing-comma-repaired version, then a
   LaTeX-backslash-escaped version, then both (`study_ai.py:111-118`). The LaTeX escape
   doubles backslashes before LaTeX commands (`\\frac` → `\\\\frac`) so single-escaped
   LaTeX in JSON string values parses; `test_recovers_single_escaped_latex` and
   `test_keeps_correctly_escaped_latex` pin both directions.
3. If an array opens first, try to decode it; on failure, run `_recover_array_objects`
   which walks the array object-by-object and returns every *complete* object before the
   truncation/corruption point (`study_ai.py:66-89`, `study_ai.py:123-129`). This is the
   key robustness feature: a 90%-good LLM reply yields 90% of its questions instead of
   zero. `test_recovers_complete_objects_from_truncated_array` and
   `test_recovers_objects_with_garbage_between` pin it.
4. Otherwise scan for the first decodable `{` or `[` anywhere (`study_ai.py:131-138`).
5. Last resort: recover objects from a never-closing array (`study_ai.py:140-145`).
6. `ValueError("no JSON found" / "empty LLM reply")` only when nothing structured
   survives (`study_ai.py:139, 146`).

There is also a dedicated `REPAIR_JSON_SYSTEM` prompt (`study_ai.py:538-547`) used as a
*second-model* repair pass when the local parser fails — defence in depth again.

**Limitations of the repair layer:**
- `_recover_array_objects` breaks out of its loop after the first repair pass because
  "offsets no longer line up after a repair pass" (`study_ai.py:82-83`). So if the first
  object needed a trailing-comma repair, the second object is *not* attempted via repair
  — it can only succeed if it decodes cleanly on the original slice. This is a pragmatic
  cap on complexity, not a bug, but it means a reply where *every* object has a trailing
  comma only recovers the first.
- The LaTeX escape regex `_LATEX_BACKSLASH_RE = \\([a-tA-Tv-zV-Z])` (`study_ai.py:78`)
  deliberately excludes `\\u` (to avoid clobbering JSON `\uXXXX` escapes) but the comment
  admits it misses LaTeX commands starting with `u` (rare — `\underbrace`, `\unit`).
- `normalize_questions` drops any MCQ whose answer can't be resolved
  (`study_ai.py:201-202`, `test_mcq_without_answer_is_dropped`). This is correct for
  closed-book practice (an uncheckable MCQ is useless) but means a question with a typo'd
  answer field is silently lost rather than flagged for the repair pass.

### 3.3 Chunking

`chunk_material` (`study_ai.py:366-388`) splits on paragraph boundaries (`\n\n`) into
LLM-sized chunks (default `chunk_chars=12000`, `max_chunks=4`). Hard-splits oversized
paragraphs. Sensible and tested (`test_long_text_splits_on_paragraphs`,
`test_giant_paragraph_hard_split`). Note the default `max_chunks=4` caps a long source at
48k chars of extraction coverage per run — a deliberate cost/latency guard, but a 200-page
PDF will only get its first ~48k chars chunked for text extraction. The vision pipeline
(`study_vision.py`) is the fallback for scanned/formula-heavy PDFs.

### 3.4 Outcome → FSRS rating mapping (`rating_from_outcome`, `study_ai.py:395-414`)

This is the bridge between practice-question outcomes and the FSRS scheduler, and it is
the most pedagogically interesting decision in the pipeline. The rationale is documented
in the module docstring (`study_ai.py:10-18`): *the FSRS rating is a report of retrieval
quality, not a reward.* The mapping:

**MCQ** (`study_ai.py:401-406`):
- `not correct` → `1 (Again)` — failure is Again regardless of anything else.
- `correct` + `hints_used > 0` → `2 (Hard)` — a hinted success is "assisted retrieval";
  the memory needed help.
- `correct` + no hints + `confidence == "sure"` → `4 (Easy)` — clean, confident, unaided.
- `correct` + no hints + not "sure" → `3 (Good)` — effortful success is the desirable
  difficulty we want more of.

**Open** (`study_ai.py:408-414`):
- `score < 60` → `1 (Again)`.
- `score < 85` *or* hinted → `2 (Hard)`.
- `score >= 95` *and* `confidence == "sure"` → `4 (Easy)`.
- otherwise → `3 (Good)`.

The reasoning is sound and explicitly pinned by 12 tests (`test_mcq_*`, `test_open_*`).
Two design choices deserve callouts:

1. **Hints ⇒ Hard, even for a "sure" correct MCQ** (`test_mcq_correct_sure_with_hint_still_hard`).
   This is the right call: a hint is retrieval support, so the memory wasn't retrieved
   unaided. FSRS's Hard rating means "recalled with serious difficulty," which is exactly
   what a hinted recall is. Treating it as Good would over-estimate stability and stretch
   the interval too fast.
2. **Confidence gates Easy, not Good.** A clean correct MCQ without confidence is `Good`,
   not `Easy`. This avoids rewarding overconfidence and reserves Easy for genuinely
   fluent retrieval. There's a subtle interaction with metacognitive calibration
   (see §4): the `confidence` field is *self-reported* by the learner, so the mapping
   implicitly trusts the learner's JOL (judgment of learning). A learner who is always
   "sure" will get Easy ratings and longer intervals — potentially too long if they're
   overconfident. FSRS's own difficulty/stability dynamics will eventually correct this,
   but there's no calibration check that would, say, downgrade Easy to Good when the
   learner's historical confidence-accuracy correlation is poor.

The route wires this in at `routes/study_routes.py:2773-2774`, passing
`correct`, `score`, `hints_used`, and `confidence` (validated to `sure|unsure|guess`,
`study_routes.py:2772`) into `rating_from_outcome`, then feeds the rating straight into
`fsrs.schedule` (`study_routes.py:2780-2790`).

---

## 4. Learning-science methods: implemented vs missing

### 4.1 Already implemented

| Method | Where | Strength |
|--------|-------|----------|
| **Spaced retrieval (FSRS-4.5)** | `src/fsrs.py` | Strong — full memory model, day-scale scheduling, lapse handling |
| **Retrieval practice** (every contact ends in a recall demand) | `study_plan.py` notes; question pipeline | Strong |
| **Distributed/expanding spacing** | `study_plan.py:BASE_OFFSETS, _compress_offsets` | Strong, but static cadence |
| **Interleaving** | `study_plan.py:191-203` | Partial — rotation-based, not semantic |
| **Desirable difficulty** (effortful success ≠ Easy) | `study_ai.py:rating_from_outcome` | Strong, principled |
| **Hints as assisted retrieval ⇒ Hard** | `study_ai.py:401-414` | Strong |
| **Taper / sleep protection** | `study_plan.py:140-149` | Strong |
| **Mock exams + calibration cue** | `study_plan.py:113-121, 167-171` | Strong (the "predict your score" cue is a JOL hook) |
| **Socratic coaching mid-attempt** | `study_ai.py:ASK_COACH_SYSTEM` | Strong |
| **Didactic tutoring post-attempt** | `study_ai.py:ASK_TUTOR_SYSTEM` | Strong |
| **Progressive hints** (3 levels, no answer leak) | `study_ai.py:HINT_SYSTEM` | Strong |
| **Cram triage honesty** | `study_plan.py:46-86` | Strong |
| **Confidence/JOL capture** | `StudyAttempt.confidence`; `rating_from_outcome` | Partial — captured but not yet used for calibration feedback |

### 4.2 Missing (highest-value gaps)

1. **FSRS parameter optimization from review history.** The `StudyReview` and
   `StudyAttempt` tables already log everything needed (rating, state_before, interval,
   elapsed, duration) to fit the 17 `w` parameters per user. Nothing reads them back to
   optimize. This is the single highest-leverage algorithmic upgrade — see `04-research-
   study-methods.md` §2 ("Per-User Optimization", "FSRS-6 per-card stability decay").

2. **Confidence-based repetition / calibration feedback.** `confidence` is captured and
   fed into the rating, but the learner never sees their *calibration curve* (confidence
   vs actual accuracy over time). The mock-exam note ("the prediction gap is your
   calibration data", `study_plan.py:170`) gestures at this but the app doesn't compute
   or surface it. The research note (§1, §5) flags "Confidence-Weighted Two-Stage Testing"
   as the #1 highest-leverage addition (d=0.72).

3. **No Leitner / SM-2 fallback.** FSRS is the only scheduler. If a user has a tiny
   review history (insufficient data to fit), there's no simpler fallback that needs
   fewer parameters. A Leitner-box fallback (or just "warm start" parameters stratified
   by prior performance) would help cold-start.

4. **Adaptive difficulty / question selection.** The question bank stores a `difficulty`
   label (easy/medium/hard, `study_ai.py:VALID_DIFFICULTY`) and FSRS state, but the queue
   logic (in the route layer, out of scope) doesn't appear to select questions by
   targeting a desirable-difficulty success rate (the `AUTHOR_QUESTIONS_SYSTEM` prompt
   targets 70-85% expected success, `study_ai.py:486`, but that's at *authoring* time,
   not at *selection* time). Adaptive item selection (e.g. pick the next question whose
   predicted retrievability is in the "effortful but retrievable" band) is absent.

5. **Metacognitive prompts beyond hints.** `ASK_COACH_SYSTEM` is Socratic, but there's
   no explicit "Why?" elaborative-interrogation prompt, no "how does this connect to
   [prior concept]" self-explanation hook, and no JOL prompt before feedback
   ("How confident are you?") other than the `confidence` field which is used for
   rating, not for a metacognitive display. The research note flags elaborative
   interrogation (d=0.68) and JOL calibration (d=0.47) as gaps.

6. **Generation effect / pretesting.** No feature presents unsolved problems *before*
   material study to exploit the generation effect (d=0.35, research note §1). The
   pipeline only extracts or authors questions *from* material.

7. **No sub-day memory model.** FSRS-4.5 limitation, faithfully inherited (§1.5).
   FSRS-5/6's short-term component would let Hard/Again on a learning card produce
   stability-meaningful short intervals rather than fixed 5/12-minute delays.

8. **No fuzz / load balancing.** Same-stability cards all come due the same day.

9. **Plan ↔ scheduler disconnect.** The deterministic plan and the FSRS scheduler don't
   share state. A topic mastered via FSRS isn't removed from the plan's allocation; the
   plan isn't regenerated when FSRS stability rises.

10. **No implementation intentions / time cues.** Plan gives dates + minutes, not
    "when/where" cues (research note §4).

---

## 5. Test coverage assessment and correctness risks

### 5.1 Coverage by file

- **`src/fsrs.py`** — `tests/test_fsrs_scheduler.py` (179 lines, ~22 tests). Pins the
  structural properties (state transitions, interval ordering, lapse handling, clamping,
  monotonicity, determinism, naive/ISO datetime handling, preview immutability) rather
  than exact floats — explicitly so a parameter refit won't break the suite
  (`test_fsrs_scheduler.py:8-11`). This is the right strategy. Coverage is good for the
  state machine and the public surface; the individual equation functions
  (`next_recall_stability`, `next_forget_stability`, `next_difficulty`) are exercised
  only indirectly through `schedule`. There are no tests that directly assert the
  equation outputs against known FSRS-4.5 reference values (e.g. a fixture card with a
  published expected next interval). Adding a few golden-value tests against the upstream
  FSRS-4.5 calculator would catch drift if `DEFAULT_W` is ever changed.

- **`src/study_plan.py`** — `tests/test_study_plan_generator.py` (152 lines, ~18 tests).
  Pins: validation, never-schedule-on-or-after-exam, first-contact coverage & window,
  multi-contact spacing, interleaving cap (2-4), mock presence + late mock, taper,
  rest-day respect, daily-minutes scaling, determinism, offset compression, cram mode +
  cram triage to top topics. Coverage is thorough for the structural guarantees the UI
  relies on. Gaps: no test that reviews actually land at the *compressed* offsets for a
  short runway (only that they're < runway and unique); no test for the rest-day-skip
  recovery behaviour; no test that priority ordering actually puts the highest-priority
  topic first in the first-contact window.

- **`src/study_ai.py`** — `tests/test_study_ai_helpers.py` (464 lines, ~55 tests) plus
  `tests/test_study_prereq_duplicates.py` (33 lines) and
  `tests/test_study_material_classify.py` (33 lines). This is the most heavily tested
  module. Coverage spans JSON repair (fences, trailing commas, truncation, garbage
  between objects, LaTeX escaping), question normalization (MCQ/open, letter answers in
  5 formats, context redundancy, dedupe, source-page/number preservation), manifest
  parsing, answer-key page parsing, conclusion detection (positive + negative cases),
  prereq grouping (the numbered-part bug fix), question-key notation-insensitivity,
  chunking, the full rating matrix, and material classification. Excellent coverage.

- **`src/study_source.py`** — `tests/test_study_source.py` (38 lines, 3 tests). Covers
  the link builder (with/without file, page anchor) and one `infer_source_page` case.
  `infer_source_page` has three fallback strategies (question-text fragment match,
  number-label line match, none) but only the first is tested with a hit; the number-line
  fallback and the no-match return are not directly exercised. Low risk (it's best-effort
  page inference) but the coverage is thin.

- **`src/study_vision.py`** — **no dedicated test file.** The vision helpers
  (`render_pdf_pages`, `pdf_page_count`, `extract_pdf_figures`, `text_layer_is_thin`,
  `pages_to_data_urls`, `batch_pages`) have no unit tests in the scope reviewed. The
  functions are I/O-heavy (pypdfium2, Pillow, pypdf) so they're harder to unit test, but
  the pure helpers `text_layer_is_thin` (a one-line heuristic) and `batch_pages` (pure
  list chunking) are trivially testable and currently aren't. This is the largest test
  gap in the core-logic surface.

### 5.2 Correctness risks

1. **`_recover_array_objects` single-repair cap** (`study_ai.py:82-83`): after the first
   trailing-comma repair, the loop breaks, so only the first repaired object is
   recovered. A reply where every object has a trailing comma yields only 1 object, not
   all. Low impact (the repair pass is a fallback) but surprising. Documented in code.

2. **`schedule()` learning/relearning Hard vs Again due-date** (`fsrs.py:196-201`): both
   keep the card in learning with a fixed delay; the stability update differs but the
   due date does not reflect it. Not a bug (the sub-day model is absent by design) but
   means the Hard button's preview is always "12m" regardless of the card's stability.

3. **`rating_from_outcome` trusts self-reported confidence for Easy** (`study_ai.py:405,
   413`): an overconfident learner who always says "sure" gets Easy ratings and longer
   intervals than warranted. FSRS self-corrects over time via lapse dynamics, but there's
   no calibration gate. Medium risk for users with poor metacognition.

4. **`question_key` full-text normalization** (`study_ai.py:319-329`): drops all
   non-alphanumeric and all LaTeX formatting commands, then all remaining backslashes.
   Two genuinely different questions that differ only by notation that survives this
   normalization could collide. The test `test_question_key_distinguishes_shared_preambles`
   shows the full-text (not prefix) approach protects multi-part questions, but the
   notation-collapse is aggressive. Low risk in practice.

5. **`_escape_latex_backslashes` misses `\\u…` commands** (`study_ai.py:78-86`): by
   design, to avoid clobbering JSON unicode escapes, but `\underbrace` etc. won't be
   double-escaped. Very low impact.

6. **`study_plan.py` first-contact round-robin can overload a day** (`study_plan.py:100`):
   `fc_window[i % len(fc_window)]` puts `ceil(N_topics / window_size)` topics on the last
   window day. With many topics and a short runway, a single day can get many first
   contacts. No test guards the per-day first-contact cap. Low-medium risk.

7. **`StudyAttempt` / `StudyReview` are write-only today** — they exist for stats and
   streaks (`study_routes.py:3128-3190`) but nothing reads them for optimization or
   calibration. Not a correctness risk, but it's the unused asset that §4.2 #1 and #2
   depend on.

---

## 6. Prioritized improvement opportunities (learning algorithms)

Ordered by leverage × tractability, with file anchors for where the change would land.

### P0 — FSRS per-user parameter optimization
- **What:** Fit the 17 `w` parameters from each user's `StudyReview` + `StudyAttempt`
  history (minimize predicted-vs-actual retention error; gradient or Bayesian fit,
  à la `fsrs4anki` Optimizer). Store the fitted `w` on `StudyDeck` (or a per-user
  `StudyUserParams` table) and pass it to `schedule(..., w=user_w)`.
- **Where:** new module `src/fsrs_optimize.py`; thread the `w` argument from
  `routes/study_routes.py:1755` and `:2780`. The plumbing already accepts `w` (`fsrs.py:141`).
- **Why:** biggest single efficiency gain in the scheduler; the data is already logged;
  the `w` parameter is already plumbed through every function. Research note §2 calls
  this out as #2 of the top-3 additions.

### P1 — Calibration feedback loop (confidence-accuracy)
- **What:** Compute and surface the learner's confidence-vs-accuracy curve from
  `StudyAttempt.confidence` × `StudyAttempt.correct/score`. Show it in the stats UI;
  optionally use it to gate `rating_from_outcome`'s Easy path (downgrade Easy→Good when
  the learner's recent "sure" accuracy is below a threshold).
- **Where:** `src/study_ai.py:rating_from_outcome` (add an optional `calibration` arg);
  a new pure helper `src/study_stats.py` for the curve. Route reads `StudyAttempt`.
- **Why:** research note §5 #1 (d=0.72). Turns the already-captured `confidence` field
  into a metacognitive training tool, not just a rating input.

### P2 — Connect the study plan to FSRS state
- **What:** Let `generate_plan` accept each topic's current FSRS-derived mastery (e.g.
  mean stability or recent attempt accuracy) and re-weight `_priority` and time
  allocation from *actual* mastery, not the static 1-5 bucket. Optionally regenerate the
  plan when mastery crosses a threshold.
- **Where:** `src/study_plan.py:_priority` (`:25-29`) and `generate_plan` signature.
- **Why:** closes the plan↔scheduler disconnect (§2.4, §4.2 #9); makes the plan adaptive.

### P3 — Adaptive question selection (desirable difficulty)
- **What:** When building the practice queue, select the next question whose predicted
  retrievability is in the "effortful but retrievable" band (~0.7–0.85), not just the
  next-due. Use `fsrs.retrievability` on the stored stability/elapsed.
- **Where:** queue logic in the route layer (out of scope here) but driven by
  `src/fsrs.py:retrievability`; add a pure selector helper to keep it testable.
- **Why:** targets the desirable-difficulty zone per attempt; research note §1
  ("adaptive difficulty"). The `AUTHOR_QUESTIONS_SYSTEM` already targets 70-85% success
  at authoring time; this extends it to selection time.

### P4 — Elaborative interrogation + JOL prompts in the coaching prompts
- **What:** Add a "Why does this follow?" elaborative-interrogation branch to
  `ASK_COACH_SYSTEM`, and a pre-feedback JOL prompt ("How confident are you this is
  correct?") before showing the answer. The `confidence` field already captures the
  answer; add the *prompt* that elicits it mid-attempt.
- **Where:** `src/study_ai.py:ASK_COACH_SYSTEM` (`:495-505`).
- **Why:** research note §1 (elaborative interrogation d=0.68; JOL d=0.47); smallest UI
  change, direct pedagogical lift.

### P5 — Semantic interleaving
- **What:** Replace the index-rotation `mix` in `study_plan.py:199` with a
  relatedness-based grouping (topic embeddings or a topic graph) so interleaved blocks
  pair *related* topics, which the literature finds superior to random swapping.
- **Where:** `src/study_plan.py:191-203`; needs a topic-affinity input.
- **Why:** research note §1 ("adaptive topic clustering", interleaving d=0.44).

### P6 — FSRS-5/6 short-term component + fuzz
- **What:** (a) Add a short-term-memory state for learning/relearning cards so Hard/Again
  produce stability-meaningful sub-day intervals instead of fixed 5/12 min. (b) Add
  interval fuzz (± up to 25%) to avoid same-stability pile-ups.
- **Where:** `src/fsrs.py:schedule` (learning branch `:159-167`, review branch `:205-213`);
  `_next_interval_days` (`:133-135`).
- **Why:** §1.5 and §1.6; research note §2 (FSRS-5/6). Medium complexity, high
  scheduler-fidelity gain.

### P7 — Golden-value FSRS tests + study_vision tests
- **What:** (a) Add tests that assert `schedule` outputs against a published FSRS-4.5
  reference calculator for a few fixture cards, so `DEFAULT_W` drift is caught.
  (b) Add unit tests for `study_vision.text_layer_is_thin` and `batch_pages` (pure, easy).
- **Where:** `tests/test_fsrs_scheduler.py`; new `tests/test_study_vision.py`.
- **Why:** §5.1 — the equation functions are only indirectly tested; `study_vision` has
  zero unit tests. Low effort, de-risks future refactors.

### P8 — JSON repair: multi-object trailing-comma recovery
- **What:** In `_recover_array_objects`, instead of breaking after the first repair, reset
  the scan to the next `{` after the repaired object and continue, so a reply where every
  object has a trailing comma recovers all of them.
- **Where:** `src/study_ai.py:80-84`.
- **Why:** §5.2 #1; modest robustness gain for the LLM-extraction fallback path.

---

### Summary table of the P0–P3 lever

| Priority | Change | Module | Leverage | Tractability |
|----------|--------|--------|----------|--------------|
| P0 | FSRS per-user `w` fit from review logs | new `src/fsrs_optimize.py` | High | Medium (data ready, plumbing ready) |
| P1 | Calibration curve + Easy-gate | `study_ai.py:rating_from_outcome` + new stats | High | Medium |
| P2 | Plan re-weights from FSRS mastery | `study_plan.py:_priority` | Medium-High | Medium |
| P3 | Desirable-difficulty queue selection | new pure selector + route | Medium-High | Medium |

All four rest on assets that already exist in the codebase (the `w` kwarg, the
`StudyAttempt`/`StudyReview` logs, the `confidence` field, the `retrievability`
function) — the missing piece is the algorithm that reads them back.
