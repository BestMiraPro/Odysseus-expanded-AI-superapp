# Review: Odysseus Study Module Upgrade Plan

**Reviewer:** MiniMax-M2.5 (substitute for Nemotron)  
**Date:** 2026-06-29  
**Scope:** Review of `docs/study-upgrade/00-improvement-plan.md` and its four supporting analysis files (`01-backend-routes.md`, `02-core-logic.md`, `03-frontend.md`, `04-research-study-methods.md`)

---

## 1. Soundness: Headline Finding Assessment

### Verdict: **Correct and well-supported**

The plan's central claim — "the app measures everything and learns from nothing yet" — is factually accurate and strongly evidenced.

**Supporting evidence from source verification:**

1. **FSRS per-user optimization (W1)** is explicitly absent. `src/fsrs.py:83,87,91,98,115,141,232` all default to `w=DEFAULT_W`. The 17-parameter vector is never personalized despite the data existing in `StudyReview` and `StudyAttempt` tables.

2. **Calibration feedback is write-only.** `rating_from_outcome` (`src/study_ai.py:507–530`) uses `confidence` to gate Easy ratings, but no code computes or surfaces the confidence-accuracy curve. The plan correctly notes this at `02 §4.2 #2`.

3. **Plan-scheduler disconnect verified.** `src/study_plan.py:25–29` computes priority as `importance * (6 - mastery)` using the static 1–5 bucket from exam configuration — it never reads FSRS stability or `StudyAttempt` outcomes. The dependency graph ends at generation time.

4. **The review logs are actively used only for streaks and history display** (`routes/study_routes.py:3128–3190`), not for optimization or calibration.

The supporting analyses are thorough: `02-core-logic.md` provides precise file:line anchors (e.g., `src/fsrs.py:141`, `src/study_ai.py:507`, `src/study_plan.py:25–29`) that this reviewer independently verified. The quantitative research backing from `04-research-study-methods.md` (effect sizes from Dunlosky et al., Zhao 2024) is appropriately cited.

**One nuance:** The headline is slightly hyperbolic — the app does "learn" in the narrow sense that FSRS adjusts intervals based on individual review outcomes (stability/difficulty update). But the plan's framing is correct: it does not *personalize the model parameters* or *surface metacognitive feedback* from its own logs. The distinction is well-taken.

---

## 2. Sequencing: Phase Order Assessment

### Verdict: **Sound with one dependency gap**

The proposed order **0 → 1 → 2 → 3 → 4 → 5** is generally correct, but the dependency graph in Section 6 has an implicit chain that deserves explicit callout:

**What the plan gets right:**
- Phase 0 (stabilize/secure) must come first — data durability (W2) and security (W3) are foundational.
- Phase 1 (data loop) unlocks all downstream personalization — it is correctly placed before Phase 2 (retrieval features that consume calibration data) and Phase 3 (dashboard that displays it).
- Phase 4 (scheduler fidelity) correctly depends on Phase 1.1 (optimizer), because FSRS-5/6 re-fitting requires per-user parameter optimization first.

**The dependency gap:**

The plan notes at `02 §6: P3` that adaptive question selection (choosing questions in the 0.7–0.85 retrievability band) depends on the `retrievability()` function, but it does not depend on Phase 1.1. However, **Phase 2.4 (adaptive question selection) should logically depend on Phase 1.2 (calibration feedback)** for a full desirable-difficulty loop: calibration tells you which confidence levels map to actual accuracy, which refines the retrievability targeting. The current sequencing treats these as independent, but they reinforce each other.

More critically: **Phase 3.1 (dashboard with calibration chart) depends on Phase 1.2** — the calibration curve data source. The dependency is implicit but not listed in the graph. Adding `Phase 1.2 → Phase 3.1` would make the sequencing explicit.

**Sequencing verdict:** The order is defensible and nearly optimal. The single highest-leverage sequencing fix is to add the Phase 1.2 → 3.1 dependency to the graph in Section 6, or to merge 1.2 and 3.1 as one vertical slice (compute and display calibration in the same phase). As-written, the risk is low because 1.2 and 3.1 are adjacent phases, but explicit is better than implicit.

---

## 3. Risks and Feasibility

### 3.1 Technical Risks

| Risk | Severity | Mitigation in Plan | Assessment |
|------|----------|--------------------|------------|
| **Per-user FSRS overfitting on sparse data** | Medium | Minimum-review threshold + warm-start fallback to `DEFAULT_W` | Adequate, but acceptance criteria (AC) in 1.1 do not specify the exact threshold (N) or validation method |
| **FSRS-5/6 migration corrupts schedules** | High | Behind flag; reversible backfill; golden tests first | Good — flag-controlled rollout is explicitly planned |
| **Easy-gate frustrates well-calibrated users** | Medium | Flag-controlled; threshold-based; transparent UI | Adequate, but AC does not specify what threshold triggers auto-downgrade |
| **Refactor regressions** | Medium | Slice-by-slice; full suite + `verify` skill gate | Standard — no additional risk beyond any refactor |

### 3.2 Effort Underestimates

- **Phase 1.1 (FSRS optimizer):** Rated `L` (large). This is accurate — fitting 17 parameters per user with gradient/Bayesian optimization and validating on held-out data is nontrivial. The plan correctly assigns `deepseek-v4-pro` (algorithm) plus `qwen3-coder-480b` (wiring).

- **Phase 4.3 (modularization):** Rated `L`. Splitting a 3,553-line route file and 2,119-line JS file into packages is inherently large. The plan correctly assigns two workers. However, **Phase 4.3 is listed as "opportunistic"** — it may slip. The plan should assign a specific target milestone for modularization, not treat it as purely opportunistic, because W4 (monoliths) affects long-term maintainability of all subsequent work.

- **Phase 3.1 (dashboard):** Rated `L`. The plan calls for retention curves, due forecasts, per-topic accuracy, calibration graphs, focus trends, and exam-readiness projections. This is a substantial UI build. Rating it `L` is appropriate, but the acceptance criteria ("all charts render from real data on a seeded account") could be more specific about which charts are required vs. deferred.

### 3.3 Behavior-Breaking Items

1. **Phase 2.1 (numeric confidence):** Changing `StudyAttempt.confidence` from enum (`sure|unsure|guess`) to numeric (0–100) is a breaking schema change. The plan notes a migration is needed, but the AC ("old enum values migrated") is vague — it should specify the migration logic (e.g., `sure→100`, `unsure→50`, `guess→20`).

2. **Phase 0.1 (durable ratings):** Changing the UI to block on network success fundamentally changes the user experience. Users who were accustomed to fast card progression may perceive latency. The AC ("Killing the network mid-session loses zero ratings") is correct but does not address the UX perception — a loading spinner or disabled state during the network call is implied but not specified.

3. **Phase 1.2 (Easy-gate):** If implemented as auto-downgrade, users who are correctly calibrated will see their Easy ratings silently become Good. This could be perceived as the system "not working." The flag-control is the right safeguard, but the plan should require a clear UI message ("Your Easy ratings have been adjusted for calibration") when the gate is active.

### 3.4 Acceptance Criteria Weaknesses

- **Phase 0.1:** "Killing the network mid-session loses zero ratings" is correct for the happy path but does not specify behavior when the queue is large (e.g., 50 ratings queued, network restores, does it batch or serialize?).
- **Phase 0.2:** AC says "AI endpoints return 429 past a per-user budget" but does not specify the budget window (per minute? per day?) or the quota size.
- **Phase 1.1:** "With ≥N reviews" — the value of N is not specified. FSRS optimization typically needs 50–100+ reviews to be meaningful.
- **Phase 1.2:** "overconfident users see the gap" — does not specify what "overconfident" means quantitatively (e.g., accuracy < 60% for "sure" responses in the last 30 attempts).

---

## 4. Gaps

### 4.1 Security

**Covered adequately:** Path traversal hardening (Phase 0.2) is explicitly addressed with canonicalization and confinement. Rate limiting for AI endpoints is flagged. Owner checks are audited.

**Gap:** No mention of **input sanitization for user-generated LaTeX** in card content. The JSON repair layer handles escaped LaTeX in LLM output (`src/study_ai.py:79–87`), but user-pasted LaTeX in card front/back fields goes directly to storage. A malicious user could craft LaTeX that, when rendered by the frontend's `mdToHtml()`, triggers arbitrary JavaScript (if KaTeX has vulnerabilities). The plan should add a security pass on user-submitted LaTeX/HTML content.

### 4.2 Data Integrity

**Covered:** Feature flags for every behavioral change; additive migrations; rollback provisions.

**Gap:** No mention of **database transaction boundaries** for the rating queue in Phase 0.1. If the queue in `localStorage` is flushed and the server fails partway through a batch, duplicates could occur. The AC says "no duplicate submissions" but does not specify idempotency keys.

### 4.3 Testing

**Covered:** Golden-value FSRS tests (Phase 0.4), `study_vision` unit tests (Phase 0.4), verify skill run.

**Gap:** **No performance/regression test suite specified.** The plan focuses on functional tests but does not mention:
- Load testing the queue endpoint with 10,000+ due cards
- Latency bounds for the calibration computation (Phase 1.2) as history grows
- Memory/leak verification for the 14-day focus chart (Phase 3.1) over long sessions

### 4.4 Privacy of Learner Data

**Covered:** The plan explicitly states "No data leaves the box" — optimizer and stats run locally.

**Gap:** No mention of **data export or deletion** for GDPR compliance. If a user wants to delete their study history, the plan does not specify a mechanism. This is a minor gap for a self-hosted tool but worth noting.

### 4.5 Performance

**Gap:** The plan does not explicitly address **indexing strategy** for the new queries introduced by Phase 1 (optimizer reads `StudyReview` + `StudyAttempt`; calibration reads the full attempt history). As users accumulate thousands of reviews, these queries could become slow. Adding DB indexes is mentioned as part of Phase 4.3 but not as a prerequisite for Phase 1.

---

## 5. The Two Judgment Calls

### 5a. Phase 4.1: FSRS-5/6 Migration — Recommendation: **DEFER (stay on personalized FSRS-4.5)**

**Reasoning:**

1. **Marginal value vs. implementation cost:** FSRS-5/6's short-term memory component addresses sub-day scheduling (the 5/12-minute fixed delays in learning state). The plan correctly notes at `02 §1.5` that FSRS-4.5 does not model sub-day decay, and FSRS-5/6 adds this. However, **personalized FSRS-4.5 (Phase 1.1) captures most of the scheduling efficiency gain** — the per-user `w` fit adjusts initial difficulty and stability growth curves, which is where most of the personalization payoff lives.

2. **FSRS-5/6 adds model complexity without proportional gain for this app's primary use case:** The app is primarily used for exam preparation with day-scale intervals. The sub-day component matters for heavy cramming (learning cards multiple times in one day), which is explicitly discouraged by the plan's taper and sleep-protection features. The 5/12-minute delays are a coarse proxy, but they are a *known* coarse proxy — the scheduler is not broken, just imprecise.

3. **Risk profile:** Migrating the scheduler requires:
   - Re-fitting the optimizer for the new model (new parameter count)
   - Migrating stored stability values (backward compatibility)
   - Golden tests against the new reference (not yet available in the open source)
   
   This is a high-risk, high-effort change that is better positioned as a Phase 5 (optional) or Phase 6 item after the data loop delivers measurable value.

4. **The plan's own evidence:** `02 §1.6` notes FSRS-5/6 is "a target" but the per-user optimization (P0) is ranked above it. The research note (`04 §2`) lists FSRS-6 as valuable but alongside per-user optimization — it does not claim FSRS-6 is strictly required.

**Recommendation:** Complete Phase 1.1 (per-user optimization) first, measure the retention improvement on held-out data, and re-evaluate FSRS-5/6 in a future cycle. The interval fuzz (also Phase 4.1) can be added independently of the short-term model — fuzz is a UI/queue improvement, not a memory model change, and it is lower risk.

---

### 5b. Phase 1.2: Easy-Gate — Recommendation: **SHOW CURVE ONLY (do not auto-downgrade)**

**Reasoning:**

1. **Transparency principle:** The plan's guiding principle #3 states "Evidence over gamification" and the study module's core value is "closed-book discipline" and "honest cram triage" (`02 §2.2`). Auto-downgrading a user's rating without explicit feedback violates this transparency. A user who selects "Easy" expects Easy — silently changing it to Good based on an algorithmic judgment is paternalistic and potentially frustrating.

2. **Calibration is the actual high-value feature:** The research note (`04 §1`) rates metacognitive calibration at d=0.47, and confidence-weighted two-stage testing at d=0.72. The calibration *curve itself* (Phase 1.2 + Phase 3.1) is the pedagogical intervention — surfacing the gap between confidence and accuracy trains metacognition. The auto-downgrade is a secondary effect that may be counterproductive for well-calibrated users.

3. **Risk of false positives:** The threshold for "overconfident" is not specified. If the system incorrectly identifies a well-calibrated user as overconfident (e.g., due to a recent run of hard questions), they receive systematically shorter intervals and may feel the scheduler is "broken." The flag-control helps, but even a flag requires users to understand why their ratings changed.

4. **Alternative path:** Show the calibration curve (required) and provide a **self-serve toggle** in the UI ("Automatically adjust Easy ratings to match my calibration") that the user can enable. This preserves autonomy while offering the intervention to those who want it. This is closer to how the plan handles other behavioral changes (feature flags).

5. **The plan already has the right infrastructure:** The calibration curve computation is essential for Phase 2.1 (numeric confidence) and Phase 3.1 (dashboard). Building the curve is non-negotiable; building the auto-downgrade is optional. The curve alone delivers the metacognitive training value; the auto-downgrade is a marginal optimization that risks user trust.

**Recommendation:** Implement the calibration curve and display it (Phase 1.2 + 3.1 merged into one vertical slice). Add the Easy-gate as a **user-enabled toggle**, not automatic behavior, and only after the curve is live and validated.

---

## 6. Verdict

### Overall: **Approve with changes**

The plan is rigorous, well-evidenced, and largely well-sequenced. The headline finding is correct, the data-loop-first philosophy is sound, and the research backing is appropriate. However, two specific changes would materially strengthen it:

### Required Changes (before approval):

1. **Specify acceptance criteria quantitatively:**
   - Phase 1.1: Define the minimum review threshold N (recommend: N ≥ 50)
   - Phase 1.2: Define "overconfident" threshold (recommend: "sure" accuracy < 60% over last 30 attempts)
   - Phase 0.2: Define rate-limit budget (recommend: e.g., 20 AI calls/minute per user)

2. **Add explicit Phase 1.2 → Phase 3.1 dependency** (or merge them into one slice)

3. **Add Phase 0.1 UX specification:** Loading state during network call (spinner or disabled buttons)

4. **Add migration specification for Phase 2.1:** Define enum-to-numeric mapping for `StudyAttempt.confidence`

### Recommended (non-blocking) Enhancements:

- Add DB indexing as a Phase 0 / Phase 1 prerequisite, not deferred to Phase 4.3
- Add a security pass for user-submitted LaTeX content
- Add performance/regression test references in the testing gate

### Single Highest-Leverage Change:

**Merge Phase 1.2 (calibration feedback loop) and Phase 3.1 (dashboard calibration chart) into a single vertical slice that computes and displays the calibration curve together.** This eliminates the implicit dependency, ensures the data pipeline is complete before UI work begins, and delivers the highest-ROI metacognitive feature (d=0.47–0.72) in one coherent pass. The Easy-gate auto-downgrade remains a future user-toggle, not a default behavior.

---

*This review was produced by MiniMax-M2-5 acting as a senior reviewer stress-testing the upgrade plan. All file:line anchors were independently verified against the source codebase.*