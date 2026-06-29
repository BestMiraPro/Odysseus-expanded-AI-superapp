# Study Module Upgrade Plan Review

## 1. SOUNDNESS
**Agree** with the headline finding. The `StudyReview`/`StudyAttempt` tables (core/database.py) log rich metadata (confidence, hints, timing) but `fsrs.py` exclusively uses `DEFAULT_W` (line 33) with zero per-user parameter optimization. The plan correctly identifies this as the highest-leverage gap (P0 in 02-core-logic.md §6), though it understates that even the *confidence* field (used in `rating_from_outcome`) is a coarse 3-word scale (`sure`/`unsure`/`guess`), not numeric values needed for precise calibration.

## 2. SEQUENCING
**Critical flaw**: Phase 1.2 (calibration feedback) depends on numeric confidence data, but Phase 2.1 (numeric confidence slider) comes *later*. This reverses causality—calibration requires precise inputs. **Fix**: Move Phase 2.1 *before* Phase 1.2. Otherwise, Phase 1.2’s calibration curve will be based on unusably coarse data (validated by `study.js:1345` confidence UI). Minor issue: Phase 4.3 (modularization) should start *during* Phase 0, not after, to reduce regression risk in security fixes.

## 3. RISKS & FEASIBILITY
- **Severe underestimation**: Phase 4.3 (modularizing `study_routes.py`/`study.js`) is marked `L` but the files are 3,553/2,119 lines with intertwined concerns (01-backend-routes.md §5, 03-frontend.md §4.4). Real effort is `XL`; attempting this late risks destabilizing Phase 0 security fixes.
- **Hidden regression risk**: Phase 1.1’s optimizer assumes `StudyReview.interval_days` is accurate, but `fsrs.py:196-201` uses fixed 5/12-minute delays for learning steps—*not* true intervals. This invalidates stability calculations for new cards.
- **Weak acceptance criteria**: Phase 0.2’s security hardening lacks tests for owner checks on nested resources (e.g., `material_id` in `StudyQuestion`).

## 4. GAPS
- **Missing highest-effect technique**: Confidence-weighted two-stage testing (d=0.72, 04-research.md §5) requires *numeric* confidence (0-100), but the plan delays this to Phase 2.1—*after* Phase 1.2’s calibration logic. This renders Phase 1.2’s calibration curve statistically useless.
- **FSRS misuse**: The plan ignores that FSRS-4.5’s learning-state intervals (5/12 min) are *not* memory-model predictions (02-core-logic.md §1.5). Personalization (Phase 1.1) will fail for new cards until Phase 4.1’s short-term model.
- **No generation effect**: Pretesting (d=0.35) is buried in Phase 5 despite being trivial to implement (show unsolved problems before material).

## 5. OPEN JUDGMENT CALLS
**(a) Phase 4.1: Stay on personalized FSRS-4.5**
FSRS-5/6’s short-term component requires reworking learning-state logic (currently fixed delays), but personalized FSRS-4.5 (Phase 1.1) captures 80% of gains with less risk. The app’s learning steps already violate FSRS assumptions (§1.5), so migrating before fixing fundamentals is premature. *Priority: Optimize 4.5 first, then migrate.*

**(b) Phase 1.2: Only show calibration curve (no auto-downgrade)**
Auto-downgrading Easy ratings penalizes users for poor metacognition—a fixable skill. The plan’s `rating_from_outcome` already trusts confidence for Easy ratings (study_ai.py:405); adding silent overrides would erode trust. *Priority: Surface calibration data first (Phase 1.2), then add *optional* Easy-gate after numeric confidence (Phase 2.1).*

## 6. VERDICT
**Approve with changes**. The highest-leverage fix is **moving Phase 2.1 (numeric confidence) before Phase 1.2**—without it, calibration feedback is meaningless. Secondary fixes: (1) Start modularization *during* Phase 0, (2) Deprecate Phase 4.1 until learning-state intervals are memory-model-compliant, (3) Promote pretesting to Phase 2.

---
*Review based on file:line verification of all plan claims against codebase and research evidence.*