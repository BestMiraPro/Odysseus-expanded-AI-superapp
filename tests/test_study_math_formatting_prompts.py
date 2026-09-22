"""Every prompt that writes Study text must keep LaTeX to delimited maths.

KaTeX only typesets what sits inside $...$ or $$...$$. Extracting a
LaTeX-typeset exam copied its text markup along with its maths -
``\\textbf{true}``, ``\\begin{itemize} ... \\item`` - and some references
carried formulas with no delimiters at all. Both reached the student as raw
source code, so the prompts must say where the maths ends, not only that maths
is LaTeX.
"""

from __future__ import annotations

import pytest

import src.study_ai as study_ai

TEXT_WRITING_PROMPTS = [
    # Question text, options, context and references.
    "EXTRACT_QUESTIONS_SYSTEM", "AUTHOR_QUESTIONS_SYSTEM",
    # Material text that extraction then reads, and the display-repair pass.
    "TRANSCRIBE_SYSTEM", "REFORMAT_SYSTEM",
    # Everything else the practice view renders.
    "GRADE_OPEN_SYSTEM", "EXPLAIN_FURTHER_SYSTEM", "HINT_SYSTEM",
    "ASK_COACH_SYSTEM", "ASK_ELABORATE_SYSTEM", "ASK_TUTOR_SYSTEM",
    "EXPLAIN_SYSTEM", "STUDY_NOTES_SYSTEM", "SUBJECT_OVERVIEW_SYSTEM",
]


@pytest.mark.parametrize("name", TEXT_WRITING_PROMPTS)
def test_prompt_puts_every_latex_command_inside_delimiters(name):
    prompt = getattr(study_ai, name)
    assert "Every LaTeX command belongs inside $...$ or $$...$$" in prompt, (
        f"{name} does not say a bare formula shows as source")


@pytest.mark.parametrize("name", TEXT_WRITING_PROMPTS)
def test_prompt_formats_text_with_markdown_not_latex(name):
    prompt = getattr(study_ai, name)
    assert "**bold**" in prompt
    for command in (r"\textbf", r"\begin{itemize}", r"\item"):
        assert command in prompt, f"{name} does not rule out {command}"
