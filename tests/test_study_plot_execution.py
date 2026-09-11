"""A tutor asked for a graph must produce a graph, not a picture of one.

Given only diagram syntax, a model asked to show a production function drew
this:

    Q ^
      |        ____----
      |    __--
      |  _/
      +----------------> L (workers)

Mermaid is right for structure and cannot plot y = 2*sqrt(x) at all. The
runtime that can was already in the repo -- codeRunner runs Python through
Pyodide -- it simply was never offered to Study.

The security shape matters as much as the feature. codeRunner has two backends:
Pyodide in the browser, and runServer, which POSTs to /api/shell/exec. This code
is written by a model that reads the student's uploaded PDFs, so it is
attacker-influenced input; it must never reach the shell one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._study_js_harness import needs_node, run_js

REPO = Path(__file__).resolve().parent.parent
STUDY_JS = (REPO / "static" / "js" / "study.js").read_text(encoding="utf-8")
RUNNER_JS = (REPO / "static" / "js" / "codeRunner.js").read_text(encoding="utf-8")
RUNNER_PATH = REPO / "static" / "js" / "codeRunner.js"


# --------------------------------------------------------------------------
# Detecting a plot
# --------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("snippet", [
    "import matplotlib.pyplot as plt",
    "from matplotlib import pyplot",
    "plt.plot(x, y)",
    "import numpy as np\nimport matplotlib.pyplot as plt\nplt.figure()",
])
def test_plotting_snippets_are_recognised(snippet):
    out = run_js("", "codeUsesMatplotlib", epilogue=f"""
console.log(JSON.stringify({{ hit: codeUsesMatplotlib({snippet!r}) }}));
""", source_path=RUNNER_PATH)
    assert out["hit"] is True


@needs_node
@pytest.mark.parametrize("snippet", [
    "print(2 + 2)",
    "x = [1, 2, 3]\nprint(sum(x))",
    "",
])
def test_non_plotting_snippets_are_not(snippet):
    """A 15 MB package download for `print(4)` would be absurd."""
    out = run_js("", "codeUsesMatplotlib", epilogue=f"""
console.log(JSON.stringify({{ hit: codeUsesMatplotlib({snippet!r}) }}));
""", source_path=RUNNER_PATH)
    assert out["hit"] is False


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------

def test_the_backend_is_forced_before_matplotlib_loads():
    """Pyodide's default backend draws to its own canvas and savefig yields
    nothing; MPLBACKEND must be set before the first import."""
    assert "MPLBACKEND" in RUNNER_JS
    order = RUNNER_JS.index("MPLBACKEND") < RUNNER_JS.index("loadPackage")
    assert order, "the backend is set after the package loads, which is too late"


def test_figures_are_captured_without_requiring_plt_show():
    """Models write plt.show() or not, at random; both must work."""
    assert "get_fignums" in RUNNER_JS
    assert "savefig" in RUNNER_JS


def test_a_plot_run_gets_a_longer_timeout():
    """Rendering plus a first-run package unpack does not fit in 10 s."""
    assert re.search(r"wantsPlot\s*\?\s*45000\s*:\s*10000", RUNNER_JS)


def test_a_failed_capture_does_not_lose_the_text_output():
    assert "plot capture failed" in RUNNER_JS


# --------------------------------------------------------------------------
# Study's wiring, and the boundary it must not cross
# --------------------------------------------------------------------------

def _code_only(src: str) -> str:
    """Strip // and /* */ comments. The rule below is about what the module
    calls, and the comment explaining the rule names the very endpoint it
    forbids."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.split("\n"))


def test_study_runs_plots_in_the_browser_not_on_the_server():
    """runServer shells out to /api/shell/exec. Model-written code, influenced
    by uploaded PDFs, must never be handed to it."""
    code = _code_only(STUDY_JS)
    assert "runServer" not in code, (
        "Study calls the server shell runner; model-written code must stay in "
        "the Pyodide sandbox"
    )
    assert "/api/shell/exec" not in code


def test_study_imports_the_sandboxed_runner():
    match = re.search(r"import \{([^}]*)\} from '\./codeRunner\.js'", STUDY_JS)
    assert match, "Study does not import the code runner"
    assert "renderPythonPlots" in match.group(1)


def _plot_runner() -> str:
    body = re.search(r"export function renderPythonPlots\(.*?\n\}", RUNNER_JS, re.DOTALL)
    assert body, "renderPythonPlots is gone"
    return body.group(0)


def test_only_plotting_blocks_run_unattended():
    """Auto-running arbitrary model code is a different decision from drawing
    a figure it asked for."""
    assert "codeUsesMatplotlib" in _plot_runner(), (
        "every python block would run, not just the plots"
    )


def test_a_block_is_not_run_twice():
    """Practice re-renders on every interaction, and the tutor log re-renders
    on every streamed chunk."""
    assert "plotRunDone" in _plot_runner()


def test_the_code_stays_reachable_behind_a_toggle():
    """A student checking the maths must be able to read the snippet."""
    assert "Show plot code" in _plot_runner()


def test_a_failed_run_reveals_the_source_again():
    """Better a visible snippet than a blank space where a graph should be."""
    assert "pre.hidden = false" in _plot_runner()


# Both tutors must draw. They render the same markdown through different
# modules, and the Tutor tab was missed the first time: it rendered mermaid but
# never ran a plot, and its prompt never mentioned either. Asked for a graph it
# replied "I can't generate visual charts -- I'm a text-based agent".
@pytest.mark.parametrize("mod", ["study.js", "studyAgent.js"])
def test_both_render_surfaces_run_plots(mod):
    src = {"study.js": STUDY_JS,
           "studyAgent.js": (REPO / "static" / "js" / "studyAgent.js")
           .read_text(encoding="utf-8")}[mod]
    assert "renderPythonPlots" in src, f"{mod} never runs a plot block"


def test_the_runner_is_defined_once():
    """It lived in study.js, so the Tutor tab could not reach it."""
    assert "function renderPythonPlots" not in STUDY_JS, (
        "the plot runner is duplicated instead of shared"
    )
