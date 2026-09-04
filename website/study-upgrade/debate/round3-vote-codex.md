# Round 3 — Agent CODEX Vote

## VOTE: GLM

## Decisive reason

GLM's Round-2 proposal is the stronger build guide. It resolves the anchor dispute correctly with git evidence: the consolidated review's `:1035` is the HEAD/committed line, my `:1070` is the dirty working tree; the plan must commit the working tree and anchor against that recorded SHA. CODEX R2 failed to recognize that the working tree itself (not just HEAD) is the moving target, and silently used working-tree anchors while claiming they were verified against HEAD — `ASK_COACH_SYSTEM` doesn't exist at HEAD at all. GLM also short-circuits idempotency *before* `fsrs.schedule()` with the exact behavioral AC, requires contract tests before service-layer extraction, and correctly reframes `done_blocks` reset as a UX AC (the code at `study_routes.py:1999` already resets intentionally). These are all concrete, source-grounded improvements over my own proposal.

## Rebuttal of GLM's strongest point (and why it fails)

GLM's strongest point is the calibration-sequence refinement: keep the existing per-session 3-bin "sure but wrong" display (`study.js:1792-1801`) as-is, but build the *persistent* cross-session calibration curve only after the numeric slider and pinned enum mapping land. I cannot rebut this — it is correct. I conceded it in R2 and GLM now states it even more precisely. My only residual position is that GLM's wording still allows a reader to think the 3-bin signal could temporarily drive the Phase-1 persistent curve; the plan should make explicit: "the persistent curve uses numeric confidence only; the existing session-scoped chips are unchanged."

## One thing from the LOSING proposal to fold into the winner

From CODEX R2: the explicit decomposition of Phase 0 into durable ratings → numeric confidence → stats substrate → service layer is cleaner than GLM's numbering, and the risk register should fold in the "anchors drift again before build" guard (freeze on committed SHA, final pass by implementation agent). Also, CODEX's worker assignments are slightly clearer on who owns the SHA-freeze pass.

---

FINAL VOTE: GLM
