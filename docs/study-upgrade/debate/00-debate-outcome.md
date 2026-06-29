# Debate Outcome — Ratified Change Set

**Format:** 3-round, 2-agent debate. Seats: **Agent CODEX** = kimi-k2-7-code (Codex unavailable in roster; user-approved fallback), **Agent GLM** = glm-5-2.
**Settled inputs (not relitigated):** (a) stay on personalized FSRS-4.5; (b) calibration curve only, no auto-downgrade.

## Result
- **Round 1:** independent proposals (`round1-codex.md`, `round1-glm.md`).
- **Round 2:** critique + revise (`round2-codex.md`, `round2-glm.md`).
- **Round 3 vote (merit-bound):** **CODEX → GLM** and **GLM → GLM**. **Unanimous 2–0 for GLM's Round-2 proposal.** No tie; no merged round required. Orchestrator ratifies.

## Why GLM won (both agents agreed)
GLM's R2 is the more actionable build guide: it resolved the anchor dispute with hard git evidence, specified the idempotency **short-circuit before `fsrs.schedule()`**, made service-layer extraction **contract-tests-first**, kept the KaTeX finding specific, and correctly reframed `done_blocks` reset as a UX AC. CODEX voted against its own proposal, conceding these points; the calibration disagreement effectively converged (numeric slider first, then the persistent curve on the numeric signal — the existing per-session 3-bin readback stays).

## Carry-overs folded into the winner
- CODEX's explicit "**commit the working tree, record the SHA, anchor against it**" meta-rule → already GLM A1.
- CODEX's Phase-0 decomposition order → already GLM §4 roadmap.
- CODEX's `sure`≠Easy-gate **confounding rationale** (`study_ai.py:507–528`) → adopted as the justification for numeric-first calibration.

## The ratified change set (GLM R2, `round2-glm.md`)
**Tier A (blocking, ~26h):** A1 re-anchor vs a committed SHA · A2 re-scope Phase 0.2 (drop already-fixed path-traversal; keep AI rate-limiting on 11 endpoints; specific KaTeX test) · A3 idempotency that short-circuits before `schedule()` · A4 thin `study_service.py` + endpoint contract tests first · A5 numeric slider (0.3) → persistent calibration curve (1.2) · A6 gate per-user FSRS fit at ≥400 reviews + exclude learning-step rows · A7 interval fuzz ±25% pulled forward · A8 golden-value FSRS + `study_vision` tests.
**Tier B (~10h):** B1 `study_stats.py` + `/api/study/stats` + composite indexes · B2 promote pretesting to Phase 2.
**Tier C:** C1 `done_blocks` preservation UX AC · C2 a11y/privacy/perf · C3 modularization re-estimated L+.
**Tier D (drop/defer):** FSRS-5/6 → Phase 5 · auto-downgrade Easy → dropped · semantic interleaving stays Phase 4.

Full detail and the revised sequenced roadmap (Phase 0.1–0.6 → 5) are in `round2-glm.md` §3–§4.
