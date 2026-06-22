from types import SimpleNamespace

from routes.study_routes import _same_or_later_study_part, _same_study_question


def _q(text, number=None):
    return SimpleNamespace(question=text, number=number)


def test_duplicate_prompt_is_not_a_real_prerequisite():
    text = (
        "Consider the following problem: P: opt f(x,y)=x-y subject to "
        "x >= 0, y >= 0, y >= x, x+y <= 2. Which proposition is FALSE?"
    )

    assert _same_study_question(_q(text), _q(text))


def test_distinct_parts_are_not_treated_as_duplicates():
    setup = "A company designs a food product with Yummy Score."

    assert not _same_study_question(
        _q(f"{setup} Formalize P and identify the decision variables."),
        _q(f"{setup} Write the Karush-Kuhn-Tucker conditions of P."),
    )


def test_numbered_part_rejects_same_and_later_labels_as_previous_context():
    current = _q("Write the KKT conditions.", "16b")

    assert _same_or_later_study_part(current, _q("Duplicate b.", "16b"))
    assert _same_or_later_study_part(current, _q("Future c.", "16c"))
    assert not _same_or_later_study_part(current, _q("Previous a.", "16a"))
