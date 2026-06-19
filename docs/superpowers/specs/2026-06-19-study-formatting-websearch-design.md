# Study: row layout, faithful formatting, web-theory fallback — design

Date: 2026-06-19
Build order: A (layout) → B/C (rendering) → D (web fallback + settings).

## A. Material row fits

The category `<select>` is inside the filename span, which has
`overflow:hidden; text-overflow:ellipsis`, so a long filename clips the
dropdown. Fix: move the select out of that span; make `.study-row` wrap
(`flex-wrap`) with the filename+meta truncating on the left and the
dropdown + action buttons in a wrapping group. Nothing overflows; the
dropdown is always visible.

## B/C. Faithful markdown + LaTeX rendering

KaTeX is loaded app-wide; `mdToHtml` renders markdown + KaTeX. Today the
question stems, MCQ options, references, hints, grading feedback, card
front/back and the question-bank list render as plain escaped text, so
formatting and formulas are lost.

- Render all AI-written study text via `mdToHtml` (KaTeX): practice question
  stem, options (inline), reference, hints, grading feedback/followup,
  explanations; cards front/back; question-bank list. Notes/overview/
  explain-further already render markdown.
- Options render inline (a one-line option must not become a block).
- A shared instruction block added to the extract / author / notes / grade /
  hint / explain / explain-further / overview prompts: transcribe faithfully,
  preserve the source's structure (bold, lists, sub/superscripts, tables),
  and write all math as well-formed LaTeX (`$…$` inline, `$$…$$` display).

## D. Web-theory fallback + country/school

### Settings
- `country` — per-user, in Odysseus general Settings (free-text, optional).
- `study_school` — per-user, in the Study app (free-text, optional).

### Escalation (last-resort web search)
When Consult (locate) / Explain-further find no relevant local material:
1. search the **theory** materials;
2. if nothing relevant, search **all** materials (incl. exercises/solutions);
3. only if still nothing → **web search, automatically**.

### Localized web step
- An LLM builds the search query from the exact concept + school + country and
  picks the language from the **school** (else **country**, else English).
- Run `comprehensive_web_search(query, return_sources=True)` → (text, sources).
- An LLM writes the theory from the results **in that language**, clearly
  labelled "From the web — no matching course material", with clickable source
  links (open in a new tab).
- Consult shows the web sources as openable links; Explain-further cites them
  in its footer.

## Out of scope (v1)
- Caching web-theory results (regenerated each time).
- A hardcoded country→language table (the LLM infers language).
