# A3 — Server-Side Idempotency for Durable Ratings

**Phase:** 0.1 (Durable ratings + idempotency)
**Tier:** A (blocking, ≈4h)
**Baseline:** `study-upgrade-baseline` (`1da3b4f2df82b9844e29247ca9fadae089bacfda`)
**Provenance:** `00-FINAL-build-plan.md` §3 (A3); `debate/round2-glm.md` §3 (A3) + §2 (C-REBUT-4)
**Status:** Spec — DO NOT edit application source from this file; this is a blueprint for the build crew.

---

## 1. Problem statement (why this is a defect, not a feature)

`StudyReview` and `StudyAttempt` are append-only logs whose `id` is a fresh `uuid.uuid4()` per insert
(`core/database.py:1691` and `:1795`), with **no UNIQUE constraint on any natural key**. The two
write sites that produce these rows — `review_card` (`routes/study_routes.py:1744`) and
`attempt_question` (`routes/study_routes.py:2716`) — also mutate the parent FSRS card/question state
in the same transaction. A network retry (the 0.1 client retry queue) therefore hits the handler a
second time with the same logical action but produces:

1. a **second log row** (double-counted in stats/calibration), and
2. **a second `fsrs.schedule()` call** that recomputes the card state from the *already-updated*
   state — see `routes/study_routes.py:1753` (card site) and `:2780` (question site).

The second effect is the dangerous one. The `fsrs.schedule()` call reads `card.state`,
`card.stability`, `card.difficulty`, `card.reps`, `card.lapses` from the row that the *first*
successful call already advanced. Re-scheduling against the post-review state does **not** reproduce
the original interval — it computes a *third* interval on top of the double count. For a `Good`
(=3) rating on a review-state card, the learner's intended interval is one step; the retried handler
delivers two steps plus drift. The card's `due`/`stability`/`reps`/`lapses` are silently corrupted,
and there is no audit trail because the `StudyReview.id` is fresh.

**Behavioral contract (the part CODEX R4 under-specified and this spec locks):** on a duplicate
key the handler must **short-circuit BEFORE `fsrs.schedule()`** and return the *prior* result. It
must **not** recompute the card state. Dedupe-on-insert alone (rejecting the second `StudyReview`
row but still calling `schedule()`) leaves the card corrupted — the row is clean, the FSRS state is
wrong. The short-circuit must happen before the schedule call, not after.

---

## 2. Schema change

### 2.1 `StudyReview` — `core/database.py:1688`

Add a nullable `idempotency_key` column and a UNIQUE index on it.

**Current (`core/database.py:1688-1702`):**
```python
class StudyReview(TimestampMixin, Base):
    """Append-only log of every card review (stats, streaks, calibration)."""
    __tablename__ = "study_reviews"

    id            = Column(String, primary_key=True, index=True)
    owner         = Column(String, nullable=True, index=True)
    card_id       = Column(String, index=True, nullable=False)
    deck_id       = Column(String, index=True, nullable=True)
    rating        = Column(Integer, nullable=False)   # 1=Again 2=Hard 3=Good 4=Easy
    state_before  = Column(String, nullable=True)
    interval_days = Column(Integer, default=0)
    duration_ms   = Column(Integer, nullable=True)
    reviewed_at   = Column(DateTime, default=utcnow_naive, index=True)
```

**Spec — add one column after `reviewed_at` (line ~1702):**
```python
    reviewed_at   = Column(DateTime, default=utcnow_naive, index=True)
    idempotency_key = Column(String, nullable=True, index=True)  # client-supplied; UNIQUE via migration index
```

> The SQLAlchemy model declares `index=True` (a plain B-tree). The **UNIQUE** constraint is created
> by the migration in §5 so the column is addable on existing tables (SQLite cannot add a column
> with a fresh UNIQUE constraint inline in `ALTER TABLE` without a table rebuild; the
> `CREATE UNIQUE INDEX IF NOT EXISTS` form is used instead, matching the codebase's existing
> migration idiom — see `core/database.py:811`, `:888`, `:913`).

### 2.2 `StudyAttempt` — `core/database.py:1789`

Add the same nullable `idempotency_key` column + UNIQUE index.

**Current (`core/database.py:1789-1811`):**
```python
class StudyAttempt(TimestampMixin, Base):
    """Append-only log of practice-question attempts (stats + calibration)."""
    __tablename__ = "study_attempts"

    id           = Column(String, primary_key=True, index=True)
    owner        = Column(String, nullable=True, index=True)
    question_id  = Column(String, index=True, nullable=False)
    deck_id      = Column(String, index=True, nullable=True)
    qtype        = Column(String, nullable=True)
    answer       = Column(Text, nullable=True)
    correct      = Column(Boolean, nullable=True)
    score        = Column(Integer, nullable=True)
    rating       = Column(Integer, nullable=True)
    confidence   = Column(String, nullable=True)      # "sure" | "unsure" | "guess"
    hints_used   = Column(Integer, default=0)
    grading      = Column(Text, nullable=True)
    duration_ms  = Column(Integer, nullable=True)
    attempted_at = Column(DateTime, default=utcnow_naive, index=True)
```

**Spec — add one column after `attempted_at` (line ~1811):**
```python
    attempted_at   = Column(DateTime, default=utcnow_naive, index=True)
    idempotency_key = Column(String, nullable=True, index=True)  # client-supplied; UNIQUE via migration index
```

### 2.3 Why UNIQUE, not just a lookup column

The idempotency check is a `SELECT ... WHERE idempotency_key = ?` under concurrency. With a plain
index, two near-simultaneous retries can both miss the lookup and both insert. A UNIQUE index makes
the second `INSERT` (or `db.flush()`) raise `IntegrityError`, which the handler catches and converts
to the "return prior result" path. This is the correctness backstop behind the optimistic
short-circuit in §4. The UNIQUE constraint applies only to non-NULL keys — old rows with `NULL`
(see §5 back-compat) are exempt, because SQLite UNIQUE indexes treat multiple NULLs as distinct.

---

## 3. Client key-generation strategy (pairs with Phase 0.1 retry queue)

The client (Phase 0.1's durable retry queue in `static/js/study.js`) generates the
`idempotency_key`. The contract:

1. **Stable per logical action.** The key identifies *the user's intent to rate this card once*,
   not *this HTTP request*. It must be generated **once** when the action is enqueued and reused
   for every retry of that same action.
2. **Survives retries.** The key is stored alongside the queued payload (the queue is durable
   across reloads/reconnects per 0.1) and re-sent verbatim on each retry. The client MUST NOT
   regenerate the key on retry — a fresh key per request defeats dedupe.
3. **Scope.** Key uniqueness is per-row-table. A `review_card` action and an `attempt_question`
   action may share a key namespace or not, but a single key must not be reused across two different
   logical actions. Recommended form: `<action>:<entity_id>:<nonce>` where `nonce` is a
   monotonically increasing per-session counter or a `crypto.randomUUID()`.
4. **Format.** Opaque string, ≤ 128 chars, URL-safe. Example: `rv:<card_id>:01J...`.
5. **Backward compatibility.** The key is OPTIONAL on the wire. Old clients (pre-0.1) send no key;
   the server treats `None` as "no idempotency, current behavior" (§4, §6).

**For `review_card`** (`static/js/study.js:1458` `rateCard`): the key is minted when the rating is
enqueued (just before the `jpost` at `study.js:1480`), stored in the queue payload, and sent as the
new `idempotency_key` field on the `ReviewIn` body.

**For `attempt_question`** (`static/js/study.js:1728` submit handler): the key is minted when the
submit is enqueued (just before the `jpost` at `study.js:1730`), sent as the new `idempotency_key`
field on the `AttemptIn` body.

---

## 4. Server behavioral contract — call site 1: `review_card`

**Site:** `routes/study_routes.py:1744-1772` (`def review_card`).

### 4.1 Current control flow (frozen baseline)

```python
@router.post("/cards/{card_id}/review")
def review_card(request: Request, card_id: str, body: ReviewIn):
    user = _owner(request)
    if body.rating not in (1, 2, 3, 4):
        raise HTTPException(400, "rating must be 1-4")
    db = SessionLocal()
    try:
        card = _get_card(db, card_id, user)                      # :1748
        deck = _get_deck(db, card.deck_id, user)
        state_before = card.state or "new"
        result = fsrs.schedule(                                  # :1753  ← MUST NOT reach here on retry
            _card_fsrs_dict(card), body.rating,
            desired_retention=_flt(deck.retention, 0.9),
        )
        card.state = result["state"]                            # :1762 mutates card
        card.stability = str(result["stability"])
        card.difficulty = str(result["difficulty"])
        card.due = _to_naive_utc(result["due"])
        card.last_review = _to_naive_utc(result["last_review"])
        card.reps = result["reps"]
        card.lapses = result["lapses"]
        db.add(StudyReview(                                     # :1762 inserts log row
            id=str(uuid.uuid4()), owner=user, card_id=card.id,
            deck_id=card.deck_id, rating=body.rating,
            state_before=state_before,
            interval_days=result["interval_days"],
            duration_ms=body.duration_ms,
            reviewed_at=_utcnow_naive(),
        ))
        db.commit()
        return {"card": _card_to_dict(card), "interval_days": result["interval_days"]}
    finally:
        db.close()
```

### 4.2 Required control flow (post-A3)

The idempotency check must run **after** ownership (`_get_card`/`_get_deck`) but **before**
`fsrs.schedule()`. Pseudocode:

```python
@router.post("/cards/{card_id}/review")
def review_card(request: Request, card_id: str, body: ReviewIn):
    user = _owner(request)
    if body.rating not in (1, 2, 3, 4):
        raise HTTPException(400, "rating must be 1-4")
    db = SessionLocal()
    try:
        card = _get_card(db, card_id, user)                      # ownership + 404
        deck = _get_deck(db, card.deck_id, user)

        # ── IDEMPOTENCY SHORT-CIRCUIT (before fsrs.schedule) ──────────────
        if body.idempotency_key:
            prior = db.query(StudyReview).filter(
                StudyReview.idempotency_key == body.idempotency_key
            ).first()
            if prior is not None:
                # Return the PRIOR result. Do NOT call fsrs.schedule.
                # Do NOT touch card.state/stability/difficulty/due/reps/lapses.
                # Re-derive the card dict from current card state (which is
                # correct — it was advanced by the first, successful call).
                return {
                    "card": _card_to_dict(card),
                    "interval_days": prior.interval_days,
                }
        # ─────────────────────────────────────────────────────────────────

        state_before = card.state or "new"
        result = fsrs.schedule(
            _card_fsrs_dict(card), body.rating,
            desired_retention=_flt(deck.retention, 0.9),
        )
        card.state = result["state"]
        card.stability = str(result["stability"])
        card.difficulty = str(result["difficulty"])
        card.due = _to_naive_utc(result["due"])
        card.last_review = _to_naive_utc(result["last_review"])
        card.reps = result["reps"]
        card.lapses = result["lapses"]
        try:
            db.add(StudyReview(
                id=str(uuid.uuid4()), owner=user, card_id=card.id,
                deck_id=card.deck_id, rating=body.rating,
                state_before=state_before,
                interval_days=result["interval_days"],
                duration_ms=body.duration_ms,
                reviewed_at=_utcnow_naive(),
                idempotency_key=body.idempotency_key,           # NEW
            ))
            db.commit()
        except IntegrityError:                                  # UNIQUE race backstop
            db.rollback()
            prior = db.query(StudyReview).filter(
                StudyReview.idempotency_key == body.idempotency_key
            ).first()
            # card was NOT mutated by us (we rolled back before flush completed
            # the row insert; the schedule result was computed but the card
            # mutation was rolled back too — so re-read card is the prior state).
            # NOTE: see §4.4 for the card-mutation ordering invariant.
            return {
                "card": _card_to_dict(card),
                "interval_days": prior.interval_days if prior else result["interval_days"],
            }
        return {"card": _card_to_dict(card), "interval_days": result["interval_days"]}
    finally:
        db.close()
```

### 4.3 The third-wrong-interval corruption (what the short-circuit prevents)

If the handler dedupes only the `StudyReview` row but still calls `fsrs.schedule()` on retry:

- **First call (succeeds):** `schedule(card, rating)` reads `{state: "review", stability: S0,
  reps: R0, ...}` and returns `result1` with `interval_days = I1`. The handler writes
  `card.stability = S1`, `card.reps = R0+1`, `card.due = now + I1`, and inserts `StudyReview(id=uuid1,
  interval_days=I1)`.
- **Retry (same logical action, same `card_id`):** `schedule(card, rating)` now reads
  `{state: "review", stability: S1, reps: R0+1, ...}` — the **post-first-call** state. FSRS
  extrapolates from `S1`, producing `interval_days = I2 ≠ I1` (typically larger, because stability
  grew). The handler overwrites `card.stability = S2`, `card.due = now + I2`, and — if deduped only
  on the row — either inserts a second `StudyReview(id=uuid2, interval_days=I2)` or, worse, is
  *blocked* from inserting but has **already mutated the card**.

Net effect: the card has been advanced **twice** for one user rating. The learner meant to schedule
the card once; they got two scheduling steps. The `due` date is wrong, `stability` is inflated,
`reps` is double-counted, and the calibration/stats log either double-counts the review or
disagrees with the card state. The short-circuit in §4.2 prevents this by **never calling
`schedule()` on a duplicate key** — the retry returns `prior.interval_days` and the card is untouched.

### 4.4 Card-mutation ordering invariant (for the IntegrityError backstop)

The optimistic path mutates `card.state`/`stability`/`...` **before** `db.add(StudyReview)`. If the
`StudyReview` insert hits the UNIQUE constraint (concurrent retry race), `db.rollback()` reverts the
card mutation *as well* (it's all in one transaction). After rollback, the in-memory `card` object
is stale; the handler must re-read or refresh it before building the response. The pseudocode above
returns `_card_to_dict(card)` after rollback — the build crew must verify whether SQLAlchemy's
session rollback expunges `card` and re-fetches it, or whether an explicit `db.refresh(card)` /
`db.expire_all()` is needed. **Acceptance test §7.5 covers this race explicitly.**

---

## 5. Server behavioral contract — call site 2: `attempt_question`

**Site:** `routes/study_routes.py:2716-2807` (`async def attempt_question`).

This handler is **more complex** than `review_card` because it has two DB sessions: a first
`SessionLocal()` (`:2721`) to load the question, then MCQ-check/AI-grade work with **no DB open**,
then a **second** `SessionLocal()` (`:2778`) for the `fsrs.schedule()` + `StudyAttempt` insert.
The idempotency check must guard the *second* session — that's where `schedule()` and the log row
live.

### 5.1 Required control flow (post-A3)

```python
@router.post("/questions/{question_id}/attempt")
async def attempt_question(request: Request, question_id: str, body: AttemptIn):
    user = _owner(request)
    # ── First session: load question, do MCQ check / AI grading (UNCHANGED) ─
    db = SessionLocal()
    try:
        row = _get_question(db, question_id, user)
        qtype = row.qtype
        options = json.loads(row.options) if row.options else []
        question_text = row.question
        reference = row.reference or ""
        correct_index = row.correct_index
    finally:
        db.close()

    # ... MCQ local check OR AI open-grading (UNCHANGED) ...
    # ... confidence = body.confidence if body.confidence in ("sure","unsure","guess") else None
    # ... rating = rating_from_outcome(qtype, correct=correct, score=score,
    #                                  hints_used=body.hints_used or 0, confidence=confidence)

    # ── Second session: schedule + log, with idempotency guard ────────────
    db = SessionLocal()
    try:
        row = _get_question(db, question_id, user)               # re-read

        # ── IDEMPOTENCY SHORT-CIRCUIT (before fsrs.schedule) ──────────────
        if body.idempotency_key:
            prior = db.query(StudyAttempt).filter(
                StudyAttempt.idempotency_key == body.idempotency_key
            ).first()
            if prior is not None:
                # Return the PRIOR result. Do NOT call fsrs.schedule.
                # Do NOT touch row.state/stability/difficulty/due/reps/lapses.
                return {
                    "qtype": prior.qtype,
                    "correct": prior.correct,
                    "score": prior.score,
                    "grading": json.loads(prior.grading) if prior.grading else None,
                    "correct_index": correct_index if prior.qtype == "mcq" else None,
                    "reference": reference,
                    "rating": prior.rating,
                    "interval_days": None,  # log row does not store interval_days today
                    "next_due": _iso(row.due),
                }
        # ─────────────────────────────────────────────────────────────────

        result = fsrs.schedule({                                 # :2780 ← MUST NOT reach here on retry
            "state": row.state or "new",
            "stability": _flt(row.stability),
            "difficulty": _flt(row.fsrs_difficulty),
            "last_review": row.last_review,
            "reps": row.reps or 0,
            "lapses": row.lapses or 0,
        }, rating)
        row.state = result["state"]
        row.stability = str(result["stability"])
        row.fsrs_difficulty = str(result["difficulty"])
        row.due = _to_naive_utc(result["due"])
        row.last_review = _to_naive_utc(result["last_review"])
        row.reps = result["reps"]
        row.lapses = result["lapses"]
        try:
            db.add(StudyAttempt(
                id=str(uuid.uuid4()), owner=user, question_id=row.id,
                deck_id=row.deck_id, qtype=qtype, answer=answer_text[:4000],
                correct=correct, score=score, rating=rating,
                confidence=confidence, hints_used=body.hints_used or 0,
                grading=json.dumps(grading) if grading else None,
                duration_ms=body.duration_ms, attempted_at=_utcnow_naive(),
                idempotency_key=body.idempotency_key,           # NEW
            ))
            db.commit()
        except IntegrityError:                                  # UNIQUE race backstop
            db.rollback()
            prior = db.query(StudyAttempt).filter(
                StudyAttempt.idempotency_key == body.idempotency_key
            ).first()
            return {
                "qtype": prior.qtype if prior else qtype,
                "correct": prior.correct if prior else correct,
                "score": prior.score if prior else score,
                "grading": (json.loads(prior.grading) if prior and prior.grading else grading),
                "correct_index": correct_index if (prior or qtype) == "mcq" else None,
                "reference": reference,
                "rating": prior.rating if prior else rating,
                "interval_days": None,
                "next_due": _iso(row.due),
            }
        return {
            "qtype": qtype,
            "correct": correct,
            "score": score,
            "grading": grading,
            "correct_index": correct_index if qtype == "mcq" else None,
            "reference": reference,
            "rating": rating,
            "interval_days": result["interval_days"],
            "next_due": _iso(row.due),
        }
    finally:
        db.close()
```

> **Note on `interval_days`:** `StudyAttempt` does **not** store `interval_days` today (only
> `rating`, `correct`, `score`, etc. — see `core/database.py:1789-1811`). The retry short-circuit
> therefore cannot return the prior `interval_days` from the log row; it returns `None` for that
> field (the client's `result` is already in `p.result` from the first call and is not re-rendered
> from the retry response — see `study.js:1736` `p.log.push(...)` only on success). If the build
> crew wants the retry response to be byte-identical to the first response, add
> `interval_days = Column(Integer, nullable=True)` to `StudyAttempt` in §2.2. **This is optional
> and not required by the ACs in §7** — flag it in the PR description either way.

### 5.2 The corruption here is identical in kind to §4.3

A retried `attempt_question` that still calls `schedule()` recomputes the question's FSRS state
from the post-first-call `row.state`/`row.stability`/`row.reps`, producing a second (wrong)
interval and double-advancing the question. The short-circuit prevents it.

---

## 6. Backward compatibility — old clients with no key

`ReviewIn` (`routes/study_routes.py:132`) and `AttemptIn` (`:216`) gain an optional field:

```python
class ReviewIn(BaseModel):
    rating: int  # 1=Again 2=Hard 3=Good 4=Easy
    duration_ms: Optional[int] = None
    idempotency_key: Optional[str] = None      # NEW — None = legacy client, no dedupe

class AttemptIn(BaseModel):
    choice_index: Optional[int] = None
    answer: Optional[str] = None
    confidence: Optional[str] = None
    hints_used: int = 0
    duration_ms: Optional[int] = None
    idempotency_key: Optional[str] = None      # NEW — None = legacy client, no dedupe
```

**Behavior when `body.idempotency_key is None`:** the short-circuit block is skipped entirely
(`if body.idempotency_key:` is false), and the handler proceeds exactly as the frozen baseline.
Old clients, and any client that forgets to send the key, get current (non-idempotent) behavior.
There is **no penalty** for omitting the key — only a benefit (dedupe) for sending one. This is the
zero-risk migration path: the schema change adds a nullable column, the handler change adds a
no-op-when-None branch.

---

## 7. Migration plan

Follow the codebase's existing migration idiom (see `core/database.py:730-770`
`_migrate_add_study_summary_columns`, `:880-895` `_migrate_add_model_endpoint_owner_column`,
`:910-920` `_migrate_add_provider_auth_id_column` for the exact pattern: `PRAGMA table_info`,
`ALTER TABLE ... ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`, wrapped in try/except with
`logging.getLogger(__name__).warning(...)` on failure).

Add **one** new migration function (idempotent — safe to run multiple times) and call it from
`init_db()` (`core/database.py:1945` body, append after the existing `_migrate_*` calls):

```python
def _migrate_add_study_idempotency_keys():
    """Add nullable idempotency_key + UNIQUE index to study_reviews and study_attempts."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        for tbl in ("study_reviews", "study_attempts"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            if "idempotency_key" not in cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN idempotency_key VARCHAR")
            # UNIQUE index treats multiple NULLs as distinct in SQLite — old rows
            # with NULL idempotency_key (all pre-migration rows) are unaffected.
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{tbl}_idempotency_key "
                f"ON {tbl} (idempotency_key)"
            )
        conn.commit()
        logging.getLogger(__name__).info("Migrated: added idempotency_key column + UNIQUE index to study_reviews, study_attempts")
    except Exception as e:
        logging.getLogger(__name__).warning(f"study idempotency_key migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
```

And in `init_db()`:
```python
    ...
    _migrate_backfill_task_folders()
    _migrate_add_study_idempotency_keys()      # NEW — append at end of _migrate_* list
```

**Why UNIQUE index, not UNIQUE constraint:** SQLite cannot add a column with a UNIQUE constraint
inline (`ALTER TABLE ... ADD COLUMN x TEXT UNIQUE` fails if the table has existing rows, because
UNIQUE columns must be non-null or have a default — see SQLite docs). The
`CREATE UNIQUE INDEX IF NOT EXISTS` form is idempotent, works on existing tables, and treats
multiple NULLs as distinct (so the millions of legacy rows with `NULL idempotency_key` don't
violate the index). This matches `core/database.py:811`, `:888`, `:913` exactly.

**Foreign-key note:** `core/database.py:1940-1942` warns that `foreign_keys` is globally enabled
and schema changes must not violate FK constraints. This migration only **adds** a nullable column
and an index — no FK impact, no table rebuild, no `foreign_keys` toggle needed.

---

## 8. Acceptance tests

Tests go in `tests/test_study_idempotency.py` (new file). Use the existing test DB pattern
(`SessionLocal`, in-memory or temp SQLite). Each test below is a pass/fail criterion.

### 8.1 Schema migration
- **AC-M1:** After `init_db()` on a fresh DB, `PRAGMA table_info(study_reviews)` includes
  `idempotency_key` and `PRAGMA index_list(study_reviews)` shows a unique index on it. Same for
  `study_attempts`.
- **AC-M2:** Running `init_db()` twice (idempotency) does not error and does not duplicate the
  index (`CREATE UNIQUE INDEX IF NOT EXISTS`).
- **AC-M3:** On a pre-existing DB with rows in `study_reviews` (all `idempotency_key = NULL`), the
  migration succeeds, no rows are modified, and inserting a second NULL-`idempotency_key` row does
  not raise (multiple NULLs allowed).

### 8.2 `review_card` idempotency
- **AC-R1 (happy path, key sent once):** POST `/cards/{id}/review` with
  `idempotency_key="rv:test:1"` returns `{card, interval_days}` and inserts exactly one
  `StudyReview` row with that key. `card.reps` increases by 1.
- **AC-R2 (retry, same key):** POST the same `idempotency_key` again with the same body. The
  handler returns the **same `interval_days`** as AC-R1, inserts **zero** new `StudyReview` rows,
  and `card.reps`/`card.stability`/`card.due` are **unchanged** from AC-R1 (verified by reading the
  row before and after the retry). This proves `fsrs.schedule()` was NOT called on retry.
- **AC-R3 (no key = legacy):** POST with no `idempotency_key` (None). Two identical POSTs produce
  two `StudyReview` rows and advance the card twice — i.e. legacy (non-idempotent) behavior
  preserved.
- **AC-R4 (different keys = different actions):** POST with `idempotency_key="rv:test:1"` then
  `idempotency_key="rv:test:2"`. Both succeed, two rows inserted, card advanced twice. Different
  keys are different actions.

### 8.3 `attempt_question` idempotency
- **AC-Q1 (happy path, key sent once):** POST `/questions/{id}/attempt` with
  `idempotency_key="att:test:1"` returns the result dict and inserts exactly one `StudyAttempt`.
- **AC-Q2 (retry, same key):** Same key, same body. Returns the prior result (same `correct`,
  `score`, `rating`), inserts zero new rows, `row.reps`/`row.stability`/`row.due` unchanged. Proves
  `fsrs.schedule()` was NOT called on retry.
- **AC-Q3 (no key = legacy):** No key → two POSTs produce two rows and advance the question twice.
- **AC-Q4 (open-ended grading):** Repeat AC-Q2 with an open-ended question (exercises the AI-grade
  path + second session). The retry must short-circuit without re-calling the LLM grader — i.e. the
  second call's response time is near-zero and the grading payload equals the first. (Mock the LLM
  in the test.)

### 8.4 Concurrency / race
- **AC-C1 (UNIQUE backstop):** Two concurrent POSTs with the same `idempotency_key` (e.g. via
  `threading` or `asyncio.gather`). Exactly one wins the insert; the other raises
  `IntegrityError`, the handler catches it, rolls back, and returns the prior result. The card is
  advanced exactly once. (This is the backstop for the optimistic-check-then-insert race window.)
- **AC-C2 (card not corrupted on race):** After AC-C1, `card.reps` == `initial_reps + 1` (not +2),
  `card.due` reflects exactly one `schedule()` call, and there is exactly one `StudyReview` row.

### 8.5 Backward compatibility
- **AC-B1:** An old-client request body (no `idempotency_key` field at all) parses successfully
  into `ReviewIn`/`AttemptIn` (Pydantic with a default of `None`) and the handler behaves as the
  frozen baseline.

---

## 9. Out of scope / non-goals

- **Client retry queue implementation** (Phase 0.1) — this spec specifies the *key contract* the
  queue must meet (§3) but not the queue itself.
- **Per-user FSRS `w` threading** (Phase 1.1) — `fsrs.schedule()` at `:1753` and `:2780` omits
  `w=` today; A3 does not change that. The idempotency branch is upstream of `schedule()`, so A6
  threading is independent and composes cleanly.
- **`study_service.py` extraction** (Phase 0.6 / A4) — the short-circuit block lives inside the
  handlers as written; when A4 extracts `_get_card`/`_get_question` to a service, the idempotency
  check stays in the route handler (it returns an HTTP response and belongs at the edge).
- **Storing `interval_days` on `StudyAttempt`** — optional (see §5.1 note); not required by ACs.

---

## 10. Build checklist (for the build crew)

1. `core/database.py:1702` — add `idempotency_key` column to `StudyReview`.
2. `core/database.py:1811` — add `idempotency_key` column to `StudyAttempt`.
3. `core/database.py` (new function, near `:880`) — `_migrate_add_study_idempotency_keys()`.
4. `core/database.py` `init_db()` body (~`:1990`) — call the new migration.
5. `routes/study_routes.py:132` — add `idempotency_key: Optional[str] = None` to `ReviewIn`.
6. `routes/study_routes.py:216` — add `idempotency_key: Optional[str] = None` to `AttemptIn`.
7. `routes/study_routes.py:1744` (`review_card`) — insert the §4.2 short-circuit before
   `fsrs.schedule()` at `:1753`, and wrap the `db.add(StudyReview(...))`+`db.commit()` in a
   `try/except IntegrityError` rollback-backstop.
8. `routes/study_routes.py:2716` (`attempt_question`) — insert the §5.1 short-circuit in the
   second session (before `fsrs.schedule()` at `:2780`), same IntegrityError backstop.
9. `tests/test_study_idempotency.py` (new) — ACs in §8.
10. **Do NOT** modify `static/js/study.js` in this task — the client key generation is Phase 0.1's
    scope; this spec only defines the contract (§3) the client must meet.
