# Round 3 — THE VOTE (Agent GLM)

**Voter:** Agent GLM (glm-5-2), voting on merit.
**Proposals:** `round2-glm.md` (mine) vs `round2-codex.md` (CODEX's).

---

## VOTE: GLM

---

## (2) The decisive reason

By Round 2, the two proposals had **converged on almost everything** — including the contested calibration sequencing. Both R2 proposals put the numeric-confidence slider in Phase 0.3 and the persistent calibration curve on the *numeric* signal in Phase 1.2. My R2 A5 explicitly says "persistent calibration curve from `StudyAttempt` on the *numeric* signal"; CODEX's R2 R3 says the same. So CODEX's strongest point (calibration confounding) — while analytically correct — does **not differentiate the two proposals as build guides**, because both already adopt its conclusion.

Where the proposals *do* differ, GLM's R2 is the stronger build guide on every actionability dimension that matters to a builder:

1. **Anchor error-prevention.** GLM R2 §0 contains the rigorous `git` three-copy analysis (HEAD `:1035` vs working-tree `:1070` vs worktree `:1035`; `ASK_COACH_SYSTEM` absent at HEAD). This is the single most important error-prevention insight in the debate: a builder reading it will not be misled. CODEX R2 §0 acknowledges GLM caught this but is derivative and less precise.
2. **Idempotency AC.** GLM R2 A3 specifies the *behavioral* requirement (short-circuit *before* `fsrs.schedule()`, return the prior result) — CODEX R2 R4 *conceded and adopted* this from GLM.
3. **Rate-limiting actionability.** GLM R2 A2 enumerates the 11 AI endpoints with verified line numbers (`:1779, :1815, :1872, :2166, :2224, :2276, :2310, :2892, :2924, :3043, :3093`) and the reusable pattern from `auth_routes.py:90-132`. CODEX R2 R2 says "AI rate-limiting" without the list — a builder must re-derive it.
4. **Golden-test evidence.** GLM R2 A8 cites the `test_fsrs_scheduler.py` docstring (`:8-11`) that explicitly says it pins structural properties *not* exact floats — proving a `DEFAULT_W` refit wouldn't be caught — and names the `study_vision` helpers (`text_layer_is_thin :176`, `batch_pages :104`) with zero coverage. CODEX R2 mentions golden tests but less specifically.
5. **Per-session calibration evidence.** GLM R2 discovered that the working tree *already has* a 3-bin calibration readback (`study.js:1779-1801`: `sureWrong`/`guessRight`), which sharpens the debate — the gap is the *persistent* curve, not the *concept*. CODEX R2 doesn't mention this.

A build guide's primary job is to prevent builders from doing the wrong thing. GLM R2 is more precise, more actionable, and more error-preventive on the dimensions where the proposals differ.

---

## (3) Rebuttal of CODEX's single strongest point

CODEX's strongest point is the **calibration confounding argument** (R2 §2.1): `rating_from_outcome` at `study_ai.py:517` assigns Easy *only* when `confidence == "sure"`, so the "sure" confidence bin is structurally entangled with the Easy-rating boundary. A 3-bin calibration curve's top bin is therefore not a clean confidence signal — it's confounded with the scheduler's rating decision.

**I concede the confounding is real** — I verified it at `study_ai.py:516-525`:
- MCQ: `return 4 if confidence == "sure" else 3`
- Open: `return 4 if (s >= 95 and confidence == "sure") else 3`

"Sure" is both a confidence bin and the Easy gate. This is a genuine structural defect of the 3-bin signal, deeper than mere coarseness.

**My rebuttal:** the confounding does not change the *build instruction*, because (a) my R2 already adopted numeric-first (Phase 0.3 slider → Phase 1.2 curve on numeric), so both proposals ship the same sequencing; (b) the confounding does not corrupt the *display* path — the existing per-session readback (`study.js:1779`) computes `sureWrong = confidence === 'sure' && !(correct)`, which references confidence × correctness, **not** the FSRS rating, so the displayed calibration is computationally independent of the rating decision; (c) the confounding only *bites* if you act on the 3-bin curve to alter ratings — which the settled "curve only, no auto-downgrade" decision already precludes; and (d) the actually-decisive sub-argument (retroactive redefinition of the curve's x-axis on numeric migration) was independently raised by me in R1 E2 (the pinned enum→numeric mapping), so CODEX's confounding insight adds analytical depth but not a new *conclusion*.

Net: CODEX's confounding reasoning is the more rigorous *justification* for numeric-first, but since both R2 proposals already adopt numeric-first, it doesn't make CODEX's build guide *different* from mine on this point. The differentiators are the other dimensions, where GLM R2 is stronger.

---

## (4) The one thing from the LOSING proposal I'd still fold into the winner

**Fold CODEX's calibration confounding *rationale text* into the plan's Phase 0.3/1.2 justification.** My R2 adopted numeric-first but argued it on the "retroactive redefinition" ground; CODEX's `sure`=Easy-gate entanglement is the *stronger* reason and should be written into the plan as the primary justification so a future reader doesn't relitigate it. Concretely: add to Phase 0.3's "why": *"the existing 3-bin `confidence` enum is confounded with the FSRS Easy-rating boundary (`study_ai.py:517`: Easy requires `confidence=='sure'`), so a calibration curve built on it would entangle confidence calibration with rating behavior; the numeric slider breaks this coupling."*

Secondarily, fold in CODEX's slightly more nuanced `done_blocks` UX handling (R2 §2.2: warn user, best-effort preserve only when new blocks match old `date:idx` keys, otherwise reset) — it's a cleaner UX design than my "preserve or prompt" binary.
