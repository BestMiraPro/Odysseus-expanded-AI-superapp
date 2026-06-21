"""Unit tests for src/study_ai.py — JSON repair, question normalization,
chunking, and the outcome→FSRS rating mapping for the Study v2 pipeline."""
import json

import pytest

from src.study_ai import (
    canonical_qnum,
    chunk_material,
    dedupe_questions,
    missing_question_numbers,
    normalize_questions,
    parse_answer_key_pages,
    parse_llm_json,
    parse_question_manifest,
    prereqs_from_groups,
    question_is_conclusion,
    question_key,
    rating_from_outcome,
)


# --- parse_llm_json -----------------------------------------------------------

def test_parses_clean_json_array():
    assert parse_llm_json('[{"a": 1}]') == [{"a": 1}]


def test_strips_markdown_fences():
    raw = '```json\n[{"a": 1}]\n```'
    assert parse_llm_json(raw) == [{"a": 1}]


def test_ignores_prose_before_and_after():
    raw = 'Here are the questions:\n[{"a": 1}]\nHope this helps!'
    assert parse_llm_json(raw) == [{"a": 1}]


def test_repairs_trailing_commas():
    raw = '[{"a": 1,}, {"b": 2,},]'
    assert parse_llm_json(raw) == [{"a": 1}, {"b": 2}]


def test_recovers_complete_objects_from_truncated_array():
    raw = '[{"q": "one"}, {"q": "two"}, {"q": "thr'  # cut mid-object
    assert parse_llm_json(raw) == [{"q": "one"}, {"q": "two"}]


def test_recovers_objects_with_garbage_between():
    raw = '[{"q": "one"}, and also {"q": "two"}]'
    assert parse_llm_json(raw) == [{"q": "one"}, {"q": "two"}]


def test_single_object_reply():
    assert parse_llm_json('{"score": 80}') == {"score": 80}


def test_recovers_single_escaped_latex():
    # model wrote LaTeX with single backslashes (invalid JSON) -> recovered
    raw = '[{"q": "compute $\\sqrt{2}$ and $\\alpha + \\int_0^1 x$"}]'
    out = parse_llm_json(raw)
    assert out[0]["q"] == "compute $\\sqrt{2}$ and $\\alpha + \\int_0^1 x$"


def test_keeps_correctly_escaped_latex():
    raw = '[{"q": "$\\\\frac{a}{b}$"}]'   # properly double-escaped \\frac
    out = parse_llm_json(raw)
    assert out[0]["q"] == "$\\frac{a}{b}$"


def test_raises_on_no_json():
    with pytest.raises(ValueError):
        parse_llm_json("I cannot answer that.")


def test_raises_on_empty():
    with pytest.raises(ValueError):
        parse_llm_json("")


# --- normalize_questions ------------------------------------------------------

def _mcq(**kw):
    base = {"type": "mcq", "question": "Pick one", "options": ["a", "b", "c"],
            "correct_index": 1, "reference": "b is right", "topic": "T",
            "difficulty": "medium"}
    base.update(kw)
    return base


def test_normalize_valid_mcq():
    out = normalize_questions([_mcq()])
    assert len(out) == 1
    q = out[0]
    assert q["qtype"] == "mcq" and q["correct_index"] == 1 and q["options"] == ["a", "b", "c"]


def test_mcq_without_answer_is_dropped():
    out = normalize_questions([_mcq(correct_index=None, answer=None, correct=None)])
    assert out == []


def test_context_carried_through_when_present():
    out = normalize_questions([
        {"type": "open", "question": "Verify f is differentiable.",
         "context": "f(x,y)=100-2(x-6)^2; constraints 3-x, 5-y.", "reference": "polynomial"},
    ])
    assert out and out[0]["context"] == "f(x,y)=100-2(x-6)^2; constraints 3-x, 5-y."


def test_context_defaults_to_none_when_absent_or_blank():
    out = normalize_questions([_mcq(), _mcq(context="   ")])
    assert out[0]["context"] is None and out[1]["context"] is None


def test_redundant_context_already_in_question_is_dropped():
    # The setup is already stated in the question — context must not repeat it.
    out = normalize_questions([
        {"type": "open",
         "question": "A firm has cost C(q)=2q^2. Find the marginal cost.",
         "context": "A firm has cost C(q) = 2q^2."},
    ])
    assert out and out[0]["context"] is None


def test_mcq_textual_answer_matched_to_option():
    out = normalize_questions([_mcq(correct_index=None, answer="c")])
    assert out and out[0]["correct_index"] == 2


def test_mcq_out_of_range_index_dropped():
    assert normalize_questions([_mcq(correct_index=7, answer="zzz")]) == []


def test_open_question_without_reference_is_kept_with_empty_ref():
    out = normalize_questions([
        {"type": "open", "question": "Why?", "reference": "Because X."},
        {"type": "open", "question": "Why not?"},  # no solution in the paper
    ])
    assert len(out) == 2
    assert out[1]["reference"] == ""


def test_type_inferred_from_options():
    out = normalize_questions([
        {"question": "Pick", "options": ["x", "y"], "correct_index": 0, "reference": "x"},
        {"question": "Explain", "answer": "Because."},
    ])
    assert [q["qtype"] for q in out] == ["mcq", "open"]


def test_invalid_difficulty_defaults_to_medium():
    out = normalize_questions([_mcq(difficulty="brutal")])
    assert out[0]["difficulty"] == "medium"


def test_questions_wrapper_dict_accepted():
    out = normalize_questions({"questions": [_mcq()]})
    assert len(out) == 1


def test_dedupe_drops_near_duplicates():
    a = normalize_questions([_mcq(), _mcq(question="Pick one!")])
    assert len(dedupe_questions(a)) == 1


# --- chunk_material -----------------------------------------------------------

def test_short_text_single_chunk():
    assert chunk_material("hello world") == ["hello world"]


def test_long_text_splits_on_paragraphs():
    text = "\n\n".join(f"paragraph {i} " + "x" * 500 for i in range(40))
    chunks = chunk_material(text, chunk_chars=3000, max_chunks=4)
    assert 1 < len(chunks) <= 4
    assert all(len(c) <= 3100 for c in chunks)


def test_giant_paragraph_hard_split():
    chunks = chunk_material("y" * 30000, chunk_chars=8000, max_chunks=3)
    assert len(chunks) == 3 and all(len(c) <= 8000 for c in chunks)


def test_empty_text_no_chunks():
    assert chunk_material("   ") == []


# --- rating_from_outcome ------------------------------------------------------

def test_mcq_wrong_is_again():
    assert rating_from_outcome("mcq", correct=False) == 1


def test_mcq_correct_clean_is_good():
    assert rating_from_outcome("mcq", correct=True) == 3


def test_mcq_correct_with_hint_is_hard():
    assert rating_from_outcome("mcq", correct=True, hints_used=2) == 2


def test_mcq_correct_sure_no_hints_is_easy():
    assert rating_from_outcome("mcq", correct=True, confidence="sure") == 4


def test_mcq_correct_sure_with_hint_still_hard():
    assert rating_from_outcome("mcq", correct=True, confidence="sure", hints_used=1) == 2


def test_open_low_score_is_again():
    assert rating_from_outcome("open", score=40) == 1


def test_open_partial_is_hard():
    assert rating_from_outcome("open", score=70) == 2


def test_open_good_score_is_good():
    assert rating_from_outcome("open", score=90) == 3


def test_open_high_score_with_hints_is_hard():
    assert rating_from_outcome("open", score=95, hints_used=1) == 2


def test_open_perfect_sure_is_easy():
    assert rating_from_outcome("open", score=97, confidence="sure") == 4


def test_open_perfect_unsure_is_good():
    assert rating_from_outcome("open", score=97) == 3


def test_recovers_all_objects_when_trailing_commas_and_truncation_combine():
    raw = ('[{"q": "one", "d": "easy",}, '
           '{"q": "two", "d": "med"}, '
           '{"q": "thr')
    assert parse_llm_json(raw) == [{"q": "one", "d": "easy"}, {"q": "two", "d": "med"}]



# --- letter answers (the past-paper format that used to drop everything) -------

OPTS4 = ["alpha option", "beta option", "gamma option", "delta option"]


def test_mcq_letter_answer_plain():
    out = normalize_questions([_mcq(options=OPTS4, correct_index=None, answer="b")])
    assert out and out[0]["correct_index"] == 1


def test_mcq_letter_answer_parenthesised_and_capitalised():
    out = normalize_questions([_mcq(options=OPTS4, correct_index=None, answer="(C)")])
    assert out and out[0]["correct_index"] == 2


def test_mcq_letter_answer_with_trailing_text():
    out = normalize_questions([_mcq(options=OPTS4, correct_index=None,
                                    answer="d. delta option")])
    assert out and out[0]["correct_index"] == 3


def test_mcq_correct_index_as_letter_string():
    out = normalize_questions([_mcq(options=OPTS4, correct_index="B", answer=None)])
    assert out and out[0]["correct_index"] == 1


def test_mcq_answer_prefix_match():
    out = normalize_questions([_mcq(options=OPTS4, correct_index=None,
                                    answer="gamma option (as derived above)")])
    assert out and out[0]["correct_index"] == 2


def test_mcq_letter_out_of_range_dropped():
    out = normalize_questions([_mcq(options=["x", "y"], correct_index=None, answer="f")])
    assert out == []


def test_mcq_short_ambiguous_text_not_prefix_matched():
    # short non-letter answers must not fuzzy-match an option
    out = normalize_questions([_mcq(options=OPTS4, correct_index=None, answer="zz")])
    assert out == []


# --- discovery manifest / coverage (the missing-questions safeguard) -----------

def test_canonical_qnum_strips_punctuation_and_spacing():
    assert canonical_qnum("16 b)") == "16b"
    assert canonical_qnum("16b") == "16b"


def test_canonical_qnum_strips_question_prefixes_and_zeros():
    assert canonical_qnum("Q4.") == "4"
    assert canonical_qnum("Question 4") == "4"
    assert canonical_qnum("04") == "4"


def test_canonical_qnum_empty_and_none():
    assert canonical_qnum(None) == ""
    assert canonical_qnum("  ") == ""


def test_parse_manifest_accepts_wrapper_and_bare_list():
    items = [{"number": "1", "kind": "mcq", "page": 2}]
    assert parse_question_manifest({"questions": items}) == \
        parse_question_manifest(items)


def test_parse_manifest_normalizes_fields():
    out = parse_question_manifest([
        {"number": 3, "kind": "MCQ", "page": "2"},
        {"number": " 16 a) ", "kind": "weird"},
        {"kind": "open", "page": 1},          # no number — dropped
        {"number": "3.", "kind": "open"},     # duplicate of 3 — dropped
    ])
    assert out == [
        {"number": "3", "kind": "mcq", "page": 2},
        {"number": "16 a)", "kind": "open", "page": 1},
    ]


def test_parse_manifest_garbage_returns_empty():
    assert parse_question_manifest("not json") == []
    assert parse_question_manifest({"questions": "nope"}) == []


def test_missing_numbers_canonical_comparison():
    manifest = parse_question_manifest([
        {"number": "4", "kind": "mcq", "page": 2},
        {"number": "5", "kind": "mcq", "page": 3},
        {"number": "16 b)", "kind": "open", "page": 7},
    ])
    missing = missing_question_numbers(manifest, ["Q4.", "16b"])
    assert [m["number"] for m in missing] == ["5"]


def test_missing_numbers_all_covered_or_empty_manifest():
    manifest = parse_question_manifest([{"number": "1", "kind": "mcq", "page": 1}])
    assert missing_question_numbers(manifest, ["1"]) == []
    assert missing_question_numbers([], []) == []


def test_normalize_questions_preserves_source_number():
    out = normalize_questions([
        _mcq(number="7"),
        {"type": "open", "question": "Why?", "reference": "Because."},
    ])
    assert out[0]["number"] == "7"
    assert out[1]["number"] is None


# --- answer-key pages (use solutions for references, never as questions) -------

def test_parse_answer_key_pages_normalizes():
    value = {"questions": [], "answer_key_pages": [9, "10", 9, 0, "junk"]}
    assert parse_answer_key_pages(value) == [9, 10]


def test_parse_answer_key_pages_missing_or_garbage():
    assert parse_answer_key_pages({"questions": []}) == []
    assert parse_answer_key_pages({"answer_key_pages": "9-10"}) == []
    assert parse_answer_key_pages(None) == []


# --- conclusion-style "questions" (solution steps leaked as questions) ---------

def _open(text):
    return {"qtype": "open", "question": text, "reference": "r",
            "options": None, "correct_index": None}


def test_conclusion_questions_flagged():
    assert question_is_conclusion(_open(
        "Conclude that (x,y) = (3,3) is a solution to P."))
    assert question_is_conclusion(_open("Therefore, the maximum is 80."))
    assert question_is_conclusion(_open("We conclude that f is concave."))
    # Newly covered high-precision openers.
    assert question_is_conclusion(_open(
        "Conclude, in particular, that the constraints are concave."))
    assert question_is_conclusion(_open(
        "It follows that the Hessian is negative definite."))
    assert question_is_conclusion(_open("Hence, the point (3,3) is optimal."))
    assert question_is_conclusion(_open("We deduce that f attains its maximum."))


def test_real_questions_not_flagged():
    assert not question_is_conclusion(_open("Show that f is strictly concave."))
    assert not question_is_conclusion(_open(
        "Having solved KKT, what can you conclude about the solutions of P?"))
    assert not question_is_conclusion(_open("Why does the theorem apply?"))
    # Legitimate prove/verify exam tasks must NOT be flagged by the regex layer
    # (the AI audit handles value-leaking variants).
    assert not question_is_conclusion(_open("Prove that the objective of P is concave."))
    assert not question_is_conclusion(_open(
        "Verify that the objective and constraints are differentiable."))


def test_mcq_never_flagged_as_conclusion():
    q = dict(_mcq(), question="Conclude that which option is true?")
    q["qtype"] = "mcq"
    assert not question_is_conclusion(q)


# --- prereqs_from_groups (multi-part grouping → earlier-part prerequisites) ----

def test_prereqs_from_groups_orders_earlier_parts():
    groups = [["a", "b", "c"], ["x"]]
    out = prereqs_from_groups(groups)
    assert out["a"] == []
    assert out["b"] == ["a"]
    assert out["c"] == ["a", "b"]   # all earlier parts, in order
    assert out["x"] == []           # standalone has no prerequisites


def test_prereqs_from_groups_filters_unknown_ids():
    out = prereqs_from_groups([["a", "ghost", "b"]], valid_ids={"a", "b"})
    assert out == {"a": [], "b": ["a"]}   # unknown id dropped, not a prereq


def test_prereqs_numbered_parts_only_take_smaller_labels():
    # Even if the model lists the numbered parts out of order, a labelled part
    # must only inherit strictly-smaller labels (16b never gets 16c/16d).
    groups = [["d", "b", "a", "c"]]
    nums = {"a": "16a", "b": "16b", "c": "16c", "d": "16d"}
    out = prereqs_from_groups(groups, number_by_id=nums)
    assert out["a"] == []
    assert sorted(out["b"]) == ["a"]
    assert sorted(out["c"]) == ["a", "b"]
    assert sorted(out["d"]) == ["a", "b", "c"]


def test_prereqs_numbered_part_ignores_unnumbered_siblings():
    # A numbered part must not inherit unnumbered siblings (the 16a-with-18-
    # prereqs bug): only smaller numbers count.
    groups = [["u1", "u2", "16a", "16b"]]
    nums = {"16a": "16a", "16b": "16b"}        # u1/u2 unnumbered
    out = prereqs_from_groups(groups, number_by_id=nums)
    assert out["16a"] == []                    # not [u1, u2]
    assert out["16b"] == ["16a"]
    # Unnumbered parts still fall back to positional order.
    assert out["u1"] == [] and out["u2"] == ["u1"]


# --- question_key (shared by in-run and cross-run dedupe) -----------------------

def test_question_key_normalizes_punctuation_and_case():
    assert question_key("Pick one!") == question_key("pick one")


def test_question_key_distinguishes_shared_preambles():
    a = "Consider problem P (long shared preamble). " * 5 + "Part a) formalize P."
    b = "Consider problem P (long shared preamble). " * 5 + "Part d) solve KKT."
    assert question_key(a) != question_key(b)


def test_question_key_collapses_plain_vs_latex_notation():
    # The same question extracted twice — once plain, once in LaTeX — must key
    # equal so cross-run dedupe catches it.
    plain = "Formalize P, maximizing f(x,y) = 40 ln(x) + 2y."
    latex = r"Formalize $\mathbb{P}$, maximizing $f(x,y) = 40 \ln(x) + 2y$."
    assert question_key(plain) == question_key(latex)
