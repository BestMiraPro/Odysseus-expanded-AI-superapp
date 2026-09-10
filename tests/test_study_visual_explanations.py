"""The two post-answer explanation surfaces must be able to draw.

"Explain further" and "Ask the tutor" both run after the student has committed
an answer, which is the point where a picture is allowed to do the explaining:
a supply/demand curve, the shape of a distribution, how a derivation branches.

Two halves have to line up or the feature silently does nothing:

* The prompts must tell the model the panel renders diagrams. A model that is
  never told will not emit a ```mermaid fence.
* The panel must actually render them. mdToHtml only turns a fence into
  <pre class="mermaid"> and defers un-loadable maths to pending spans; the
  library runs only when renderMermaid/renderMath are called. Chat, documents
  and group all called them. Study never did, so a diagram would have arrived
  as its own source code -- exactly what the guidance warns the model about.

The guidance is also deliberately NOT given to the pre-answer prompts: a
diagram in a hint leaks the shape of the answer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import src.study_ai as study_ai

REPO = Path(__file__).resolve().parent.parent
STUDY_JS = (REPO / "static" / "js" / "study.js").read_text(encoding="utf-8")
AGENT_JS = (REPO / "static" / "js" / "studyAgent.js").read_text(encoding="utf-8")
MARKDOWN_JS = (REPO / "static" / "js" / "markdown.js").read_text(encoding="utf-8")

POST_ANSWER = ["ASK_TUTOR_SYSTEM", "EXPLAIN_FURTHER_SYSTEM"]
# Everything that runs before the student commits, plus the scoring pass.
PRE_ANSWER = [
    "HINT_SYSTEM", "ASK_COACH_SYSTEM", "GRADE_OPEN_SYSTEM",
    "EXTRACT_QUESTIONS_SYSTEM", "EXPLAIN_SYSTEM",
]


# --------------------------------------------------------------------------
# The prompts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_offers_diagrams(name):
    prompt = getattr(study_ai, name)
    assert "mermaid" in prompt, f"{name} never tells the model it can draw"


@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_sends_quantitative_plots_to_matplotlib(name):
    """Mermaid cannot plot y = 2*sqrt(x). Asked for a production function with
    only diagram syntax available, a model draws ASCII art -- which is what it
    did before matplotlib was offered."""
    prompt = getattr(study_ai, name)
    assert "matplotlib" in prompt
    assert "ASCII art" in prompt, f"{name} does not rule out ASCII art"


@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_names_structural_diagram_types(name):
    """A bare "you may draw" yields flowcharts for everything."""
    prompt = getattr(study_ai, name)
    for kind in ("flowchart", "mindmap", "stateDiagram-v2"):
        assert kind in prompt, f"{name} does not mention {kind}"


@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_does_not_ask_for_plt_show(name):
    """Figures are captured after the snippet returns; plt.show() is a no-op
    under the Agg backend and models add it out of habit."""
    assert "Do not call plt.show()" in getattr(study_ai, name)


@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_asks_for_plain_text_labels(name):
    """Mermaid runs at securityLevel 'loose', so HTML in a label is markup in
    the page -- and this model reads the student's uploaded PDFs."""
    prompt = getattr(study_ai, name)
    assert "plain text" in prompt.lower()
    assert "HTML" in prompt


@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_requires_prose_alongside_the_diagram(name):
    """A screen-reader user gets the prose and nothing else."""
    assert "screen reader" in getattr(study_ai, name).lower()


@pytest.mark.parametrize("name", POST_ANSWER)
def test_the_prompt_warns_that_broken_syntax_is_shown_raw(name):
    prompt = getattr(study_ai, name)
    assert "source code" in prompt, (
        f"{name} does not warn that an unparseable diagram is shown as source"
    )


def test_the_json_prompt_says_where_the_fence_goes():
    """explain-further returns JSON; a fence outside the object breaks parsing."""
    prompt = study_ai.EXPLAIN_FURTHER_SYSTEM
    assert "INSIDE" in prompt and "explanation" in prompt


@pytest.mark.parametrize("name", PRE_ANSWER)
def test_pre_answer_prompts_are_not_told_to_draw(name):
    """A diagram in a hint gives away the shape of the answer."""
    assert "mermaid" not in getattr(study_ai, name), (
        f"{name} runs before the student commits, so it must not offer diagrams"
    )


# --------------------------------------------------------------------------
# The panel actually renders them
# --------------------------------------------------------------------------

def test_markdown_only_emits_a_placeholder_for_a_fence():
    """The premise of the tests below: a fence is inert until a renderer runs."""
    assert 'pre class="mermaid"' in MARKDOWN_JS
    assert "export function renderMermaid" in MARKDOWN_JS


@pytest.mark.parametrize("mod", ["study.js", "studyAgent.js"])
def test_the_module_imports_both_renderers(mod):
    # Look the source up by name rather than parametrising on it: a file's whole
    # text as a parameter becomes the test id, and pytest exports that id in
    # PYTEST_CURRENT_TEST, which blows the 32767-character limit Windows puts on
    # an environment variable.
    src = {"study.js": STUDY_JS, "studyAgent.js": AGENT_JS}[mod]
    match = re.search(r"import \{([^}]*)\} from '\./markdown\.js'", src)
    assert match, f"{mod} does not import from markdown.js"
    names = match.group(1)
    assert "renderMermaid" in names, f"{mod} imports no diagram renderer"
    assert "renderMath" in names, f"{mod} imports no math renderer"


def test_explain_further_renders_what_it_receives():
    """_renderMarkdownInto backs the Explain further panel, notes and overview."""
    body = re.search(r"function _renderMarkdownInto\(.*?\n\}", STUDY_JS, re.DOTALL)
    assert body, "_renderMarkdownInto is gone"
    assert "_enrichRendered" in body.group(0), (
        "Explain further sets innerHTML but never runs the diagram/math renderers"
    )


def test_the_practice_view_renders_the_tutor_thread():
    """The Ask-AI thread, the grade feedback and Explain options all live in it."""
    body = re.search(r"async function renderPractice\(.*?\n\}", STUDY_JS, re.DOTALL)
    assert body and "_enrichRendered" in body.group(0)


def test_the_tutor_tab_renders_its_log():
    body = re.search(r"function renderLog\(.*?\n\}", AGENT_JS, re.DOTALL)
    assert body, "renderLog is gone"
    assert "renderMermaid" in body.group(0), (
        "the tutor tab writes messages but never renders their diagrams"
    )


def test_rendering_failures_cannot_break_the_panel():
    """A missing library must degrade to readable source, not an empty pane."""
    helper = re.search(r"function _enrichRendered\(.*?\n\}", STUDY_JS, re.DOTALL)
    assert helper, "_enrichRendered is gone"
    assert helper.group(0).count("try {") >= 2, (
        "an exception from either renderer would propagate into the caller"
    )
