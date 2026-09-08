"""Every file in static/js must parse as an ES module.

The frontend has no bundler and no compile step, so a syntax error ships: the
browser refuses the whole module and the feature it backs silently does not
exist. Nothing in the suite noticed, because the JS tests here lift individual
functions out of a file and run those — a broken *file* still yields a working
function body.

study.js is the sharpest edge. It carries its stylesheet in a template literal,
so a single backtick in a CSS comment ends the literal early and takes the rest
of the module with it. That is one keystroke, in a comment, in a file of ~4,000
lines, with no error until the panel fails to open.

``node --check`` parses without executing, so this needs no DOM.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JS_DIR = REPO / "static" / "js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node binary not on PATH"
)

MODULES = sorted(JS_DIR.glob("*.js"))


def test_there_are_modules_to_check():
    """A glob that silently matches nothing would make every check vacuous."""
    assert len(MODULES) > 20, f"only found {len(MODULES)} modules in {JS_DIR}"


@needs_node
@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_the_module_parses(path, tmp_path):
    # node picks the goal (script vs module) from the extension, and there is
    # no --input-type for --check, so check a .mjs copy.
    copy = tmp_path / (path.stem + ".mjs")
    copy.write_bytes(path.read_bytes())
    proc = subprocess.run(
        ["node", "--check", str(copy)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, (
        f"static/js/{path.name} does not parse — the browser would refuse the "
        f"whole module:\n{proc.stderr}"
    )
