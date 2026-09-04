# Study: practise by chapter or by theme — 2026-09-04

Two independent ways to slice a subject's question bank, so a session can be
"work through Chapter 3 of the workbook" or "drill integration wherever it
appears" instead of only "everything, shuffled".

## Why

`Exercises_Workbook.pdf` holds 165 questions. Practising it means practising all
165 in FSRS order; there is no way to sit down and work through one chapter.

Nothing in the schema can express a chapter today:

- `topic` is far too granular — 88 distinct topics across those 165 questions,
  roughly 1.9 questions each.
- `number` is a flat sequence (`1`…`165`) with no chapter prefix.
- `source_page` is recorded on only 315 of 792 questions.

So chapters have to come out of the document. Themes are a second, different
question — what a question is *about* rather than where it sits — and are worth
answering across the whole subject, not one document at a time.

## The two axes

They are deliberately not the same control twice:

| | Chapters | Themes |
|---|---|---|
| Scope | One document | The whole subject |
| Source | The document's own headings | Clustering the `topic` labels |
| Order | Meaningful (1, 2, 3 …) | None; sorted by size |
| Answers | "Work through Chapter 3 of the workbook" | "Drill integration across every past paper" |

## Data model

Nullable columns, added by idempotent migration. Nothing existing changes
meaning; absent values read as "not grouped".

| Column | Table | Type | Meaning |
|---|---|---|---|
| `chapter` | `study_questions` | TEXT | Heading text, e.g. `3 — Joint distributions` |
| `chapter_index` | `study_questions` | INTEGER | Ordinal, so chapters sort correctly |
| `theme` | `study_questions` | TEXT | Coarse subject-wide label, e.g. `Integration` |
| `chapter_count` | `study_materials` | INTEGER | 0/1 = not split; ≥2 = offer chapters |

`chapter_count` on the material is the cheap test for "does this document have
multiple chapters" — the picker never scans questions to decide whether to draw
a chapter row.

## Deriving chapters

During extraction the model already walks the document, so it additionally
records the heading each question sits under and that heading's ordinal.

For questions already extracted, `run_detect_chapters(user, material_id)` reads
only the document's headings and maps existing questions onto them using
`number` and `source_page`. It writes `chapter_count` on the material.

**The single-chapter rule:** if detection finds fewer than two distinct
headings, it writes `chapter_count = 1`, leaves every question's `chapter` null,
and the document is never split. A one-chapter exam paper must keep looking
exactly as it does today.

Materials with fewer than 8 questions are skipped without an AI call — below
that a split leaves chapters too small to practise, and the whole-material
Practice button already covers them.

## Deriving themes

`run_cluster_themes(user, deck_id)` collects the distinct `topic` strings for a
subject, asks the model to group them into 6–10 coarse themes, and writes the
resulting theme onto every question carrying those topics.

One pass per subject and no PDF reading, which is what makes it cheap enough to
re-run. Re-running re-clusters cleanly because it only ever rewrites `theme`.

## Practising a slice

`practice_queue_payload` gains `chapter` and `theme` beside the existing
`deck_id` / `material_id` / `topics`. They are scope filters and nothing more, so
due-first-then-new ordering, cross-subject interleaving, the topic fallback and
mock mode all keep working unchanged inside a chapter or a theme.

Spaced repetition still applies within a slice, and the slice does not change
the order: the user's `study_order` preference decides it exactly as for an
unscoped session (default sinks already-seen questions behind unseen ones;
`review` puts due retrievals first).

## API

`GET /api/study/decks/{deck_id}/groupings` returns both axes with counts in one
call:

```json
{
  "chapters": [
    { "material_id": "…", "material": "Exercises_Workbook.pdf",
      "chapters": [ { "label": "1 — Probability basics", "index": 1, "count": 14 } ] }
  ],
  "themes": [ { "name": "Integration", "count": 41, "materials": 4 } ]
}
```

Materials with `chapter_count < 2` are omitted from `chapters` entirely, so an
empty list is the signal to hide the whole section.

`GET /api/study/practice/queue` accepts `chapter` and `theme`.

## UI

The Practice tab keeps its front page (cross-subject **Practice everything**
plus the subject list). Choosing a subject opens a chooser for that subject
rather than starting immediately:

```
PRACTICE — Principles of Microeconomics

[ Everything · 48 due · 95 total ]        ← unchanged behaviour

BY CHAPTER                                 ← only if some material has ≥2 chapters
  Exercises_Workbook.pdf
    1 — Probability basics      14 q   [Practice]
    2 — Random variables        24 q   [Practice]
    3 — Joint distributions     23 q   [Practice]
  Calculus S1 Exam 1.pdf        single chapter — practise the whole paper

BY THEME                                   ← only if themes have been grouped
  Integration           41 q · 4 documents  [Practice]
  Joint distributions   23 q · 2 documents  [Practice]
```

Each section is hidden entirely when it would be empty, so a subject made only
of one-chapter exam papers looks exactly as it does now. The per-material
**Practice** buttons in Subjects stay as a shortcut.

The session header shows the chosen slice as a scope chip, the way the existing
material and topic scopes already do.

## Backfill

Both actions join the existing **Tidy bank** panel in the subject view and the
agent's `maintain_bank` tool, so there is no new maintenance surface:

- **Detect chapters** — per material
- **Group themes** — per subject

Cost is one AI pass per multi-chapter material plus one per subject.

## Testing

Service-level, following `tests/test_study_review_followups.py`:

- chapter and theme scoping in `practice_queue_payload`, including that a slice
  still returns due questions before new ones
- the single-chapter rule: fewer than two headings ⇒ `chapter_count = 1`, no
  question gets a `chapter`, and the picker omits the material
- detection and clustering with the LLM stubbed, so the suite stays offline
- idempotency: re-running either action rewrites rather than duplicating
- the groupings endpoint omits empty sections
- frontend: `node --check`, plus a structural assertion that the chapter section
  is absent when no material has chapters — the same class of guard that caught
  the settings-panel regression

## Out of scope

- Chapter-scoped mock exams (mocks stay whole-scope for now)
- Editing chapter or theme labels by hand
- Chapters for flashcards or notes — this is about questions, whether they came
  from an exercise document or a theory one
