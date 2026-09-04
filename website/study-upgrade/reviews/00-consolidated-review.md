# Consolidated Re-Evaluation of the Study Upgrade Plan

**Panel:** 6 independent model reviews of `docs/study-upgrade/00-improvement-plan.md`.
**Requested models:** kimi-k2-7-code, deepseek-v4-pro, glm-5-2, nemotron-ultra, + 2 orchestrator picks (qwen3-235b-thinking, gpt-oss-120b).
**Substitutions (with reason):**
- **nemotron-ultra → minimax-m2-5.** Nemotron Ultra failed deterministically on two independent sessions with `400 BadRequestError: Expecting ',' delimiter` — it emits malformed JSON in tool-call arguments in this deployment and cannot drive the file tools. Not transient.
- **deepseek-v4-pro → deepseek-v4-flash** (per user instruction "do v4 flash then if pro doesn't work"). V4-pro failed twice with empty-error gateway faults.

## Verdict tally
| Model | Verdict |
|---|---|
| kimi-k2-7-code | Approve with changes |
| glm-5-2 | Approve with changes |
| qwen3-235b-thinking | Approve with critical changes |
| gpt-oss-120b | Approve with changes |
| minimax-m2-5 *(nemotron sub)* | Approve with changes |
| deepseek-v4-flash *(v4-pro sub)* | **Needs rework as a build guide** |

**Unanimous (6/6) on both open judgment calls:**
- **(a) FSRS-5/6:** stay on **personalized FSRS-4.5**; defer 5/6 (move Phase 4.1 → Phase 5, revisit after per-user `w` runs 2–3 months).
- **(b) Easy auto-downgrade:** **show the calibration curve only**; do NOT silently downgrade. At most an opt-in toggle, and only after the numeric-confidence slider exists (the 3-point scale is too coarse to auto-correct).

## Consensus required changes (before handing to build crew)
1. **Re-anchor every `file:line` reference.** Confirmed stale by glm, deepseek-flash, kimi AND orchestrator's own check: `rateCard` is at `study.js:1458` (not 1224–1244); `_resolve_uploaded_file` at `study_routes.py:1035` (not 1070); `fsrs.py` constants off ~9 lines; `rating_from_outcome` at `study_ai.py:507` (not 395–414). The analysis was written against a drifted revision. **This is the #1 fix** — a build guide with wrong anchors will misdirect agents.
2. **Re-scope Phase 0.2 security.** Path-traversal is **already fixed** (realpath root + basename + `_confined()` + `followlinks=False`). Drop that item; keep the **AI-endpoint rate-limiting** half — confirmed genuinely unthrottled (0 references to the existing `rate_limiter.py` in `study_routes.py`).
3. **Fix the calibration sequencing.** Phase 1.2 (calibration feedback) needs the numeric-confidence signal from Phase 2.1. Either **move 2.1 before 1.2**, or **merge "compute calibration" (1.2) + "display calibration" (3.1) into one vertical slice** (minimax's highest-leverage idea; qwen's critical-path fix).
4. **Add server-side idempotency** for the durable-rating retry queue (Phase 0.1), or replays will duplicate `StudyReview` rows and corrupt the Phase 1.1 optimizer training set (glm; implied by gpt-oss).
5. **Gate per-user FSRS fitting** on a **minimum-review threshold** before personalizing `w` (gpt-oss, minimax, deepseek-flash).
6. **Caveat new-card personalization:** the 5/12-min learning-step intervals are fixed delays, NOT memory-stability metrics — do not feed them to the optimizer as stability (qwen, glm).

## High-value additions (recommended, not blocking)
- **Phase 0.5 — shared analytics substrate** (`study_stats.py` + `GET /api/study/stats`) as the common prerequisite for optimizer, calibration, plan-reweighting and dashboard, so each workstream doesn't reimplement brittle SQL (kimi).
- **Pull interval fuzz (±25%) forward** as a cheap partial-5/6 gain decoupled from full migration (glm).
- **Promote pretesting** (d≈0.35, trivial to implement) out of Phase 5 (qwen).
- Re-estimate **modularization** effort upward; start incremental refactor earlier (qwen, kimi).
- Add: **accessibility (ARIA/focus)**, **privacy controls for calibration data**, **LaTeX input sanitization**, and a **performance/regression test suite** (gpt-oss, minimax). NB: `owner` columns are already indexed (deepseek-flash), so DB-indexing need is partial.

## Bottom line
The plan's **strategy is validated unanimously** — "logs everything, learns from nothing" is correct, the closing-the-data-loop thesis holds, and the phase *intent* order is sound. But it is **not yet a faithful build guide**: stale anchors, one stale security item, and the 1.2↔2.1 ordering must be fixed first. With items 1–6 applied, this moves from "needs rework" to "approve."
