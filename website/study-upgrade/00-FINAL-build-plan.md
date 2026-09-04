# Study App Upgrade — FINAL Build Plan (Ratified)

**Status:** Ready for owner approval → then build.
**Supersedes:** `00-improvement-plan.md` (v1 draft — stale anchors, do not build from it).
**Provenance:** 4 code/research analyses → 6-model review panel (unanimous "approve with changes") → 3-round, 2-agent debate (**CODEX=kimi-k2-7-code vs GLM=glm-5-2**), ratified **2–0 for GLM's Round-2 proposal**. Full detail in `debate/round2-glm.md` and `debate/00-debate-outcome.md`.

---

## 0. Settled decisions (locked — not relitigated)

- **(a) Spaced repetition:** stay on **personalized FSRS-4.5** (per-user `w`); FSRS-5/6 deferred to Phase 5. *(6/6 panel + debate unanimous.)*
- **(b) Calibration:** **show the calibration curve only — no silent Easy→Good auto-downgrade.** *(6/6 panel + debate unanimous; the 3-point scale is too coarse to auto-correct, and FSRS lapse dynamics self-correct.)*

## 1. Headline thesis (validated at code level by every reviewer)

The Study module is already excellent and genuinely science-grounded (faithful FSRS-4.5, evidence-based plan generator, solid AI pipeline). The highest-leverage upgrade is **not new features — it's closing the data loop: the app logs everything (`StudyReview`, `StudyAttempt`) but learns from nothing.** Reading that data back enables (1) per-user FSRS optimization, (2) calibration feedback, and (3) a plan that adapts to real mastery — all with **zero new user behavior required.**

---

## 2. ⚠️ Build pre-requisite — anchoring meta-rule (A1)

The plan was written against the **dirty working tree**, which has tracked uncommitted changes (`git diff --stat`: **588 insertions(+), 44 deletions(-)** across 7 files) plus untracked study-upgrade/source files, and is a moving target. There are **three divergent copies** of the code (old worktree, committed HEAD `da4c6b7`, and the working tree). Key proof: **`ASK_COACH_SYSTEM` does not exist at HEAD** — it's uncommitted in-progress work.

**Phase 0.0 completed baseline freeze:**
- **Frozen baseline SHA:** `1da3b4f2df82b9844e29247ca9fadae089bacfda`
- **Tag:** `study-upgrade-baseline`
- **Freeze method:** temporary-index commit object made from the dirty working tree and untracked files, tagged without moving `dev` or cleaning the working tree.

**Therefore, all build anchors must use the frozen baseline:**
1. Use `study-upgrade-baseline` / `1da3b4f2df82b9844e29247ca9fadae089bacfda` as the Phase 0 baseline.
2. Re-anchor every `file:line` reference against that SHA using the verified table below.
- Do **not** anchor "against HEAD" (HEAD is behind the plan's own references).
- Do **not** treat the review's `:1035` as authoritative — that's the HEAD line; builders work the working tree (`:1070`).

**Verified anchor table** (frozen tree `study-upgrade-baseline`):

| Symbol | Frozen-tree line | Notes |
|---|---:|---|
| `rateCard` | `static/js/study.js:1458` | `async function rateCard(rating)` |
| `/attempt` submit | `static/js/study.js:1728` | handler starts near `:1720` |
| `/focus/{id}/finish` submit | `static/js/study.js:2110` | `finishFocus` starts `:2101` |
| `_resolve_uploaded_file` | `routes/study_routes.py:1070-1107` | `basename` + `realpath` + `_confined()` |
| `rating_from_outcome` | `src/study_ai.py:507` | imported by `study_routes.py` |
| `ASK_COACH_SYSTEM` | `src/study_ai.py:614` | appended to at `:814` |
| `_priority` | `src/study_plan.py:31` | stable priority helper |
| `mix` (interleaving) | `src/study_plan.py:217` | index rotation |
| `fsrs.schedule` card site | `routes/study_routes.py:1753` | omits `w=` |
| `fsrs.schedule` question site | `routes/study_routes.py:2780` | omits `w=` |
| `DEFAULT_W` | `src/fsrs.py:33` | FSRS-4.5 default weights |
| `LEARN_AGAIN/HARD_MIN` | `src/fsrs.py:46-47` | used at `:196`, `:199`, `:210`, `:240` |
| `StudyReview` schema | `core/database.py:1688` | `StudyReview.id` starts `:1692` |
| `StudyAttempt` schema | `core/database.py:1789` | attempt row model |
| `StudyAttempt.confidence` | `core/database.py:1802` | `"sure" \| "unsure" \| "guess"`, nullable |
| AI endpoint: `/ai/generate-cards` | `routes/study_routes.py:1779` | `@router.post` |
| AI endpoint: `/ai/quiz` | `routes/study_routes.py:1815` | `@router.post` |
| AI endpoint: `/ai/grade` | `routes/study_routes.py:1872` | `@router.post` |
| AI endpoint: `/materials/{id}/reextract-text` | `routes/study_routes.py:2166` | `@router.post` |
| AI endpoint: `/materials/{id}/notes` | `routes/study_routes.py:2224` | `@router.post` |
| AI endpoint: `/decks/{id}/overview` | `routes/study_routes.py:2276` | `@router.post` |
| AI endpoint: `/materials/{id}/extract` | `routes/study_routes.py:2310` | `@router.post` |
| AI endpoint: `/questions/{id}/explain` | `routes/study_routes.py:2892` | `@router.post` |
| AI endpoint: `/questions/{id}/explain-further` | `routes/study_routes.py:2924` | `@router.post` |
| AI endpoint: `/cards/{id}/explain-further` | `routes/study_routes.py:3043` | `@router.post` |
| AI endpoint: `/overview` | `routes/study_routes.py:3093` | `@router.get` |

---

## 3. Ratified change set

### Tier A — MUST FIX before/at build start (blocking, ≈26h)

- **A1 — Re-anchor vs a committed SHA** *(S, ~1h)* — see §2 above.
- **A2 — Re-scope Phase 0.2 security** *(S, ~2h)* — **DROP** path-traversal (already fixed: `basename`+`realpath`+`_confined()` at `study_routes.py:1070-1111`; convert to a regression test). **KEEP** AI rate-limiting: add `RateLimiter` (pattern from `auth_routes.py:90-132`) to the **11 AI endpoints** (list in `round2-glm.md §3 A2`). **ADD** a specific KaTeX/LaTeX sanitization test (`\href{javascript:...}` / `\text{<script>}` must not execute; `markdown.js:727` reinserts KaTeX HTML *after* the sanitizer) — ranked below rate-limiting, above generic a11y.
- **A3 — Server-side idempotency for durable ratings** *(M, ~4h)* — nullable `idempotency_key` + UNIQUE on `StudyReview` (`core/database.py:1688`) and `StudyAttempt` (`:1789`). **Behavioral AC:** on duplicate key, **short-circuit *before* `fsrs.schedule()`** and return the prior result — never recompute card state (a retried `review_card` at `:1743-1772` would otherwise produce a third wrong interval). Pairs with the client retry queue (0.1).
- **A4 — Thin `study_service.py` + endpoint contract tests FIRST** *(M, ~6h)* — extract the nested ownership closures (`_get_deck/_get_card/_get_question/...` at `:1480-1492, 2073-2079`) to a module-level service taking `db` explicitly. **Write endpoint contract tests for the 53 `@router` handlers BEFORE extraction** (there are zero study route tests today) — this is the regression net. Full `routes/study/` package split stays Phase 4.
- **A5 — Numeric confidence slider (0.3) THEN persistent calibration curve (1.2)** *(M, ~6h)* — slider 0–100 with **pinned enum→number mapping** (`sure→85, unsure→55, guess→25`, documented + tested); migrate `StudyAttempt.confidence` to numeric (back-compat). Build the persistent curve on the **numeric** signal. Existing per-session 3-bin readback (`study.js:1792-1801`) stays as-is.
- **A6 — Gate per-user FSRS fitting** *(S, ~2h)* — require **≥400 reviews** before fitting `w`; warm-start fallback to `DEFAULT_W`. Determinism AC (same logs ⇒ same `w`), owner-scope AC. **Exclude `interval_days=0` learning-step rows** (`fsrs.py:46-47`) from the optimizer training set — they're fixed re-drill delays, not stability.
- **A7 — Pull interval fuzz (±25%) forward to Phase 2** *(S, ~2h)* — seeded, flag-gated jitter in `_next_interval_days` (`fsrs.py:132-135`), review-state cards only.
- **A8 — Golden-value FSRS + `study_vision` tests** *(S, ~3h)* — exact-float golden tests vs the FSRS-4.5 reference (current tests pin only structural properties), plus `study_vision.text_layer_is_thin`/`batch_pages` coverage.

### Tier B — High-value, not blocking (≈10h)
- **B1 — `study_stats.py` + `GET /api/study/stats` + composite indexes** *(M, ~4h)* — shared analytics substrate; composite `(owner, reviewed_at)`/`(owner, attempted_at)` indexes; fixes the N+1 at `study_routes.py:923`.
- **B2 — Promote pretesting to Phase 2.5** *(S, ~3h)* — queue-ordering over existing questions, no new schema (d≈0.35).

### Tier C — Adopt with re-scoping (≈6h)
- **C1 — `done_blocks` preservation UX AC** *(S, ~1h)* — on plan regeneration, migrate completion (`date:idx` match) or prompt before reset (`:1999` currently resets intentionally; Phase 1.3 makes regen frequent).
- **C2 — a11y / privacy / perf** — fold into Phase 4.2 / 1.1.
- **C3 — Re-estimate full modularization as L+** (3,553 + 2,119 lines, 53 handlers).

### Tier D — Drop / defer
- FSRS-5/6 → Phase 5 · auto-downgrade Easy → **dropped** · semantic interleaving → stays Phase 4.

---

## 4. Sequenced roadmap

```
Phase 0  Stabilize & secure                                              ✅ DONE
  0.0  Commit working tree + record SHA + re-anchor (A1)   ← FIRST       ✅
  0.1  Durable ratings + idempotency (A3: short-circuit before schedule) ✅
  0.2  Security: drop path-traversal; AI rate-limiting + KaTeX test (A2) ✅
  0.3  Numeric confidence slider + pinned mapping (A5)                   ✅
  0.4  Golden-value FSRS + study_vision tests (A8)                       ✅
  0.5  study_stats.py + /api/study/stats + composite indexes (B1)        ✅
  0.6  Thin study_service.py + endpoint contract tests FIRST (A4)        ✅
Phase 1  Close the data loop                                            ✅ DONE
  1.1  Per-user w (≥400 threshold, warm-start, determinism) (A6)         ✅
  1.2  Persistent calibration curve on numeric signal (A5)               ✅
  1.3  Plan↔FSRS mastery + done_blocks preservation UX AC (C1)           ✅
Phase 2  Retrieval hardening                                            ✅ DONE
  2.1 fuzz ±25% (A7) ✅ · 2.2 elaborative-interrogation/JOL ✅ ·
  2.3 pretesting (B2) ✅ · 2.4 adaptive selection ✅ ·
  2.5 typed-recall/wrong-MCQ gate ✅
Phase 3  Insight & habit                                                ✅ DONE
  3.1 dashboard (study_stats) ✅ · 3.2 Focus↔Plan link ✅ ·
  3.3 non-coercive motivation ✅
Phase 4  Architecture                                                  ✅ DONE
  4.1 semantic interleaving ✅ · 4.2 full split + a11y (C2/C3) ✅ ·
  4.3 async FSRS optimization (optional) ✅
Phase 5  Optional                                                       ✅ DONE
  delayed feedback ✅ · cross-subject search ✅ ·
  (FSRS-5/6 · faded worked examples · bulk ops — deferred)
```

> **Status:** All planned phases complete. The full study + FSRS test suite
> (324 passed, 1 skipped) is green. The monolithic `routes/study_routes.py`
> has been split into the `routes/study/` package (Phase 4.2).

**Effort:** Tier A ≈ 26h · Tier A+B ≈ 33h.

---

## 5. Approval gate

Approve this plan to authorize the build crew. Suggested dispatch: **claude-code / codex-class workers** for Phase 0 (durable ratings, idempotency, service extraction, tests), with API models for research-flavored sub-tasks. Phase 0.0 (commit + SHA + re-anchor) runs first and unblocks everything else.
