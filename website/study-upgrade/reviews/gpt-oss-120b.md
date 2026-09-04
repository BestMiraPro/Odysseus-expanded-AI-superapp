# Review – Odysseus Study Module Upgrade Plan (by gpt-oss-120b)

**Date:** 2026-06-29
**Scope:** Study feature only (`routes/study_routes.py`, `src/*study*.py`, `src/fsrs.py`, `static/js/study.js`).

---

## 1. SOUNDNESS – “app logs everything but learns from nothing”

The claim is **well‑supported**:
- `StudyReview` and `StudyAttempt` tables are defined in `core/database.py` and are **append‑only logs** (see the model definitions in the backend‑routes doc, e.g. `StudyReview` fields `rating`, `state_before`, `interval_days` etc.).
- The scheduler (`src/fsrs.py`) **never reads** those logs: the `schedule()` function always receives `w=DEFAULT_W` (see `src/fsrs.py:83‑37` and `src/fsrs.py:141`).
- The plan generator (`src/study_plan.py`) uses only static importance/mastery numbers supplied by the UI; there is no hook that re‑weights the plan based on actual FSRS stability or attempt accuracy (see the “Plan ↔ scheduler disconnect” note in `02‑core‑logic.md` §2.4).
- Confidence is stored (`StudyAttempt.confidence`) and used **only** to map to an FSRS rating (`src/study_ai.py:395‑414`), never surfaced to the learner.
Thus the headline finding is accurate and strongly evidenced by the code and the documentation.

---

## 2. SEQUENCING – 6‑phase order

| Phase | Core purpose | Dependencies observed |
|-------|--------------|-----------------------|
| **0 Stabilize & Secure** | durability of ratings, security hardening, golden‑value tests | *Must* run before any behavioural change; otherwise data loss or security regression would affect later phases. |
| **1 Close the data loop** | per‑user FSRS optimisation (1.1) and confidence‑calibration feedback (1.2) | Relies on Phase 0 (durable logs) and on the existing `rating_from_outcome` mapping (already present). |
| **2 Retrieval‑practice hardening** | confidence‑weighted two‑stage testing, typed‑recall, elaborative interrogation | Needs Phase 1 to have reliable logs and (optionally) the Easy‑gate calibration flag. |
| **3 Insight & Habit** | dashboards, focus‑session linkage, non‑coercive motivation | Depends on Phase 2 delivering richer practice data; also needs Phase 0 stability. |
| **4 Scheduler fidelity & architecture** | FSRS‑5/6, semantic interleaving, modularisation | Can be done after Phase 3 because it introduces breaking changes (new parameters, flag‑gated behaviour). |
| **5 Optional** | pretesting, worked‑examples, delayed feedback | Purely additive; can be deferred.

**Assessment:** The order is logical. The only subtle omission is that the **FSRS optimiser (Phase 1.1)** needs a **minimum‑review threshold** before it can produce a stable `w`; this threshold should be enforced in Phase 1 and documented, otherwise Phase 4.1 (migrate to FSRS‑5/6) could be attempted with insufficient data. Apart from that, dependencies are correctly captured.

---

## 3. RISKS & FEASIBILITY

| Risk | Where it originates | Impact | Mitigation |
|------|--------------------|--------|------------|
| **Rating loss / optimistic UI** | `static/js/study.js` – `rateCard()` advances UI before the POST succeeds (`study.js:1224‑1244`). | Silent loss of learning data, undermines Phase 0 goal. | Phase 0 must replace the optimistic flow with a *await‑then‑advance* or a retry‑queue (as already described). |
| **Path‑traversal & missing auth** | `_resolve_uploaded_file()` in `routes/study_routes.py` (line 1070‑1110) and inconsistent owner checks (see `01‑backend‑routes.md §5`). | Potential file‑system escape, privilege escalation. | Add canonicalisation, explicit `owner` validation, and unit tests. |
| **Over‑confident Easy‑gate** | `rating_from_outcome` trusts `confidence == "sure"` for Easy (`src/study_ai.py:405, 413`). | Learners who over‑report certainty get inflated intervals, hurting retention. | Phase 1.2 should make the gate optional and add a calibration‑threshold check before auto‑downgrading. |
| **FSRS‑5/6 migration breakage** | New short‑term state and interval‑fuzz will change due dates for many cards (`src/fsrs.py` learning branch). | Users may see large schedule jumps; old `preview_intervals` become inaccurate. | Flag‑gate the new model, provide a reversible migration script, and require golden‑value tests (Phase 4.1). |
| **Monolithic JS file** | `static/js/study.js` is 2 119 LOC, making testability hard. | Future changes can cause regressions unnoticed. | Phase 4.3 (module split) mitigates this; unit tests for pure helpers (e.g., `preview_intervals`) should be added. |
| **Missing `study_vision` tests** | No unit tests for any function in `src/study_vision.py`. | Refactors could break PDF rendering without detection. | Phase 0 → P7 (add golden tests) addresses this low‑effort risk. |
| **No per‑topic dynamic runway** | `src/study_plan.py` uses static offsets (`BASE_OFFSETS = [1,3,7,14,30]`). | Plan cannot adapt to a learner who’s already mastered a topic. | Phase 1.3 (plan ↔ FSRS state) solves the disconnect. |

Overall feasibility is high; most work items are pure‑Python or straightforward frontend changes. The only potentially **under‑estimated** effort is the per‑user FSRS optimiser (requires a gradient‑or‑Bayesian fit and a DB migration), but the plan already allocates a Large (L) size for it.

---

## 4. GAPS (missing but important)
1. **Accessibility** – no ARIA roles, focus‑traps, or visible focus rings; rating buttons rely on colour alone. This violates WCAG 2.1 AA.
2. **Privacy‑by‑design** – confidence‑accuracy calibration graphs could expose sensitive performance data; no explicit consent or data‑export control.
3. **Performance** – N+1 queries in several routes (`study_routes.py` lines 1070‑1110) and no pagination for large decks; could cause latency spikes.
4. **Offline resilience** – no client‑side queue for ratings or focus completions; a lost network currently loses data.
5. **Fuzz / load‑balancing** – FSRS schedules many cards to the same day; no interval jitter, leading to review cliffs.
6. **Per‑card stability decay (FSRS‑6)** – noted as a future improvement but not addressed in any current phase besides the optional Phase 4.1.
7. **Bulk operations** – no bulk suspend/delete for cards or questions, limiting maintainability for large subjects.

---

## 5. TWO OPEN JUDGMENT CALLS

### (a) Phase 4.1 – migrate to FSRS‑5/6 now, or stay on personalised FSRS‑4.5?
**Recommendation:** **Stay on personalised FSRS‑4.5 for now and only migrate to FSRS‑5/6 after the per‑user optimiser is in place.**
- The optimiser will already personalise the 17 `w` parameters, delivering the largest retention gain (≈ 18 % per Zhao 2024). Adding the short‑term component of FSRS‑5/6 on top of a generic 4.5 model yields diminishing returns and introduces schedule volatility (pile‑ups, interval‑fuzz) that would be hard to test without a stable baseline.
- FSRS‑5/6 also requires a storage change (short‑term state) and a flag‑gate; releasing it without the optimiser would risk a disruptive schedule change for users with little history.
- Therefore, **defer** the migration to a later optional sub‑phase (Phase 4.1) after Phase 1.1 is shipped and validated.

### (b) Phase 1.2 – auto‑downgrade ‘Easy’ ratings for over‑confident users, or only SHOW the calibration curve?
**Recommendation:** **Show the calibration curve **and** keep the rating unchanged, with an optional *opt‑in* Easy‑gate toggle.**
- Evidence: Confidence‑weighted two‑stage testing (d≈ 0.72) is most effective when learners *see* the mismatch between confidence and accuracy (the calibration graph). Forcing an automatic downgrade can be perceived as punitive and may reduce motivation.
- The existing `rating_from_outcome` mapping already respects confidence for Easy; adding a *soft* gate (e.g., downgrade Easy→Good only when recent “sure” accuracy < 70 %) can be toggled per‑user. This satisfies the research goal (calibration) while preserving learner agency.
- Implementation is low‑effort (add a flag check in `rating_from_outcome` and a UI component in the Today tab).

---

## 6. VERDICT
**Approve with changes**.

### Highest‑leverage change to make now
**Implement Phase 0 → 0.1 (durable review ratings) and Phase 0 → 0.2 (path‑traversal & auth hardening).**
- This directly protects the data‑log that the rest of the upgrade plan depends on, eliminates a concrete security issue, and unlocks the feasibility of all later phases.
- The change can be done behind a feature flag, has clear unit‑testable boundaries, and yields an immediate safety win.

All other phases are sound; the two judgment calls have been resolved as above. The plan proceeds as written, with the added emphasis on the Phase 0 durability work and the calibration‑graph UI rather than automatic Easy‑downgrade.

---

*End of review.*