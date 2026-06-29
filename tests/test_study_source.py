from src.study_source import build_original_question_link, infer_source_page


def test_build_original_question_link_uses_upload_page_anchor():
    link = build_original_question_link("paper.pdf", page=7, name="Exam paper")

    assert link == {
        "file_id": "paper.pdf",
        "name": "Exam paper",
        "page": 7,
        "label": "Exam paper, p.7",
        "url": "/api/upload/paper.pdf?inline=1#page=7",
    }


def test_build_original_question_link_without_file_returns_none():
    assert build_original_question_link("", page=3) is None
    assert build_original_question_link(None, page=3) is None


def test_infer_source_page_from_text_layer_markers_and_question_text():
    content = (
        "Page 1 text]:\n"
        "1. A warm-up problem.\n\n"
        "Page 2 text]:\n"
        "3. Consider the following problem: P: opt f(x,y)=x-y s. to x>=0.\n"
        "Which of the following propositions is FALSE?\n\n"
        "Page 3 text]:\n"
        "4. Another question.\n"
    )

    page = infer_source_page(
        content,
        number="3",
        question="Consider the following problem: P: opt f(x,y)=x-y s. to x>=0.",
    )

    assert page == 2
