"""Consult/explain-further must cite theory files, not exam/answer keys.
is_answer_key_material / classify_material decide a material's category."""
from src.study_ai import is_answer_key_material as ak, classify_material


def test_chapter_and_lecture_files_are_theory():
    assert ak("PrinciplesMacro_2025-26_S2_ch1.pdf") is False
    assert ak("PrinciplesMacro_2025-26_S2_ch5_part2.pdf") is False
    assert ak("Lecture 5 - Growth.pdf") is False
    assert ak("Week 3 notes.pdf") is False


def test_chapter_with_solutions_stays_theory():
    # a chapter that bundles solutions is still theory (the marker wins)
    assert ak("PrinciplesMacro_2025-26_S2_ch2.1_withsolutions.pdf") is False


def test_exam_and_solution_files_are_answer_keys():
    assert ak("2526_S1_RegularExam_SolutionTopics.pdf") is True
    assert ak("2425_S2_Resit_SolutionTopics.pdf") is True
    assert ak("2526_S1_ResitExam_SolutionTopics.pdf") is True
    assert ak("Calculus II 2025-2026 S2 Exam 1.pdf") is True


def test_unmarked_files_default_to_theory():
    assert ak("Growth and the Solow model.pdf") is False
    assert ak("") is False


def test_classify_material_returns_category():
    assert classify_material("PrinciplesMacro_2025-26_S2_ch1.pdf") == "theory"
    assert classify_material("2526_S1_RegularExam_SolutionTopics.pdf") == "exam"
    assert classify_material("random notes.pdf") == "theory"
