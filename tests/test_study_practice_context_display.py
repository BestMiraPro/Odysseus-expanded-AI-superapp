"""A practice question must not print its own answer, or its setup twice.

All three faults came off one screen, from one exercise extracted twice: once
whole (setup, table and both sub-parts in the question field, answer in
reference) and once split (setup in `context`, sub-parts in the question). The
part linker saw the shared setup and offered the whole version as a
prerequisite of the split one, so the panel showed:

  * the setup inside "Earlier in this problem" AND again under "Problem setup";
  * "Answer: (a) P(Disagree) = 0.30 ..." -- the answer to the question being
    asked, because the "earlier part" was the same exercise;
  * a table whose header had one more column than its rows, sliding every
    percentage one heading to the left.

The table repair is deliberately narrow, so most of these tests are about what
it must NOT touch.
"""

from __future__ import annotations

import pytest

from src.study_ai import repair_markdown_tables
from tests._study_js_harness import extract_const, needs_node, run_js

# The real stored context, verbatim, that rendered wrong.
BROKEN = """The results are presented in the following table.

| Program | Opinion | Agree | Disagree | Indifferent |
|---------|---------|-------|----------|-------------|
| Undergraduate | 20% | 10% | 10% |
| Master | 30% | 20% | 10% |"""


def _cells(md, line_index):
    rows = [l for l in md.split("\n") if "|" in l]
    return [c.strip() for c in rows[line_index].strip().strip("|").split("|")]


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

def test_the_redundant_dimension_header_is_dropped():
    """Markdown has one corner cell; a cross-tab arrives with two."""
    assert _cells(repair_markdown_tables(BROKEN), 0) == [
        "Program", "Agree", "Disagree", "Indifferent"]


def test_every_value_lands_under_its_own_heading():
    """The bug users saw: 20% of undergraduates filed under "Opinion"."""
    fixed = repair_markdown_tables(BROKEN)
    header = _cells(fixed, 0)
    undergrad = _cells(fixed, 2)
    assert dict(zip(header, undergrad)) == {
        "Program": "Undergraduate", "Agree": "20%",
        "Disagree": "10%", "Indifferent": "10%"}


def test_no_value_is_lost():
    for value in ("20%", "30%", "10%", "Undergraduate", "Master"):
        assert value in repair_markdown_tables(BROKEN)


def test_a_well_formed_table_is_left_alone():
    good = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    assert _cells(repair_markdown_tables(good), 0) == ["A", "B"]
    assert _cells(repair_markdown_tables(good), 2) == ["1", "2"]


def test_a_bigger_mismatch_is_not_guessed_at():
    """Two missing columns is ambiguous; dropping the wrong one corrupts data."""
    odd = "| A | B | C | D |\n|---|---|---|---|\n| 1 | 2 |"
    assert _cells(repair_markdown_tables(odd), 0) == ["A", "B", "C", "D"]


def test_short_rows_are_padded_not_truncated():
    odd = "| A | B | C | D |\n|---|---|---|---|\n| 1 | 2 |"
    assert _cells(repair_markdown_tables(odd), 2) == ["1", "2", "", ""]


def test_ragged_body_rows_are_left_alone():
    """Inconsistent rows mean the shape is not the cross-tab case."""
    ragged = "| A | B | C |\n|---|---|---|\n| 1 | 2 |\n| 3 | 4 | 5 |"
    assert _cells(repair_markdown_tables(ragged), 0) == ["A", "B", "C"]


@pytest.mark.parametrize("text", ["", "just prose", "a | b without a divider"])
def test_text_without_a_table_survives(text):
    assert repair_markdown_tables(text) == text


def test_prose_around_the_table_is_preserved():
    out = repair_markdown_tables(BROKEN)
    assert out.startswith("The results are presented in the following table.")


# --------------------------------------------------------------------------
# The prerequisite panel
# --------------------------------------------------------------------------

pytestmark_js = needs_node

# The threshold is a module-level const; run_js lifts functions only, so pull
# the real declaration in rather than restating its value here and letting the
# two drift.
PRELUDE = extract_const("_PREREQ_MIN_OVERLAP")

CURRENT = ("Suppose that a student was randomly chosen. What is the probability "
           "that this student answered Disagree in the following two cases? "
           "(a) Nothing more is known. (b) It is known to be a Master student.")


def _run(prereqs, question, context=""):
    import json
    return run_js(PRELUDE, "_normQ", "_prereqsToShow", epilogue=f"""
const out = _prereqsToShow({json.dumps(prereqs)}, {json.dumps({
        "question": question, "context": context})});
console.log(JSON.stringify({{ kept: out.map(p => p.question), n: out.length }}));
""")


@needs_node
def test_a_prerequisite_containing_the_whole_question_is_dropped():
    """It is the same exercise extracted twice -- its answer is this answer."""
    dup = {"question": "Setup paragraph and a table. " + CURRENT,
           "correct": "(a) 0.30 (b) 1/3"}
    assert _run([dup], CURRENT)["n"] == 0


@needs_node
def test_a_genuine_earlier_part_is_kept():
    """Shares the setup, asks something else -- the case the panel exists for."""
    part_a = {"question": "Setup paragraph. (a) How many students are there?"}
    assert _run([part_a], CURRENT)["n"] == 1


@needs_node
def test_formatting_differences_do_not_hide_a_duplicate():
    """One copy came from raw PDF text, the other from markdown."""
    dup = {"question": "| a | table |\n\n" + CURRENT.upper().replace(" ", "  ")}
    assert _run([dup], CURRENT)["n"] == 0


@needs_node
def test_a_short_question_does_not_match_by_coincidence():
    """"Compute the variance." appears inside plenty of longer questions."""
    short = "Compute the variance."
    longer = {"question": "Given the data above, compute the variance. Then plot it."}
    assert _run([longer], short)["n"] == 1


@needs_node
def test_shared_setup_is_stripped_from_a_kept_prerequisite():
    """Problem setup already shows it; repeating it wastes a screenful."""
    ctx = "The survey covered every student."
    part_a = {"question": ctx + " (a) How many responded?"}
    kept = _run([part_a], CURRENT, context=ctx)["kept"]
    assert kept and ctx not in kept[0]
    assert "(a) How many responded?" in kept[0]


@needs_node
def test_an_empty_prerequisite_is_dropped():
    """Nothing left after stripping the setup means nothing to show."""
    ctx = "The survey covered every student."
    assert _run([{"question": ctx}], CURRENT, context=ctx)["n"] == 0


@needs_node
def test_no_prerequisites_is_not_an_error():
    assert _run([], CURRENT)["n"] == 0
