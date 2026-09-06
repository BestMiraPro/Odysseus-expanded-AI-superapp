"""Run real functions out of static/js/study.js under Node, with stubs.

study.js is an ES module with no unit coverage, so its handlers have only ever
been guarded by source-string assertions — which pass happily while the code
throws at runtime. B01 in dev-docs/review-handoff-opus-5-2026-09-06.md is
exactly that failure: ``el = body()`` with no declaration is a ReferenceError
under module strict mode, and no source assertion would have caught it.

This harness lifts named top-level functions out of the real file and executes
them against a caller-supplied prelude, so tests exercise behaviour rather than
text. Nothing is duplicated: if the function changes, the test runs the change.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STUDY_JS = REPO / "static" / "js" / "study.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node binary not on PATH"
)


def extract_function(name: str, source: str | None = None) -> str:
    """Return the source of a top-level ``function name(...) {...}``.

    Top-level functions in study.js close with ``}`` in column 0, which makes
    the span unambiguous without a JS parser.
    """
    src = source if source is not None else STUDY_JS.read_text(encoding="utf-8")
    match = re.search(
        r"^(?:export\s+)?(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(.*?^\}",
        src,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        raise AssertionError(f"{name} not found as a top-level function in study.js")
    return match.group(0).replace("export function", "function", 1)


def extract_const(name: str, source: str | None = None) -> str:
    """Return the source of a top-level ``const name = ...;`` declaration.

    Handles the multi-line array/object literals study.js uses; the closing
    ``];`` or ``};`` sits in column 0.
    """
    src = source if source is not None else STUDY_JS.read_text(encoding="utf-8")
    match = re.search(
        r"^const\s+" + re.escape(name) + r"\s*=\s*.*?^(?:\]|\})\s*;",
        src,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        match = re.search(
            r"^const\s+" + re.escape(name) + r"\s*=\s*[^\n]*;", src, re.MULTILINE
        )
    if not match:
        raise AssertionError(f"{name} not found as a top-level const in study.js")
    return match.group(0)


def run_js(prelude: str, *function_names: str, epilogue: str = "") -> dict:
    """Execute the named study.js functions under Node and return parsed JSON.

    ``prelude`` defines the stubs the functions close over; ``epilogue`` drives
    them and must ``console.log(JSON.stringify(...))`` exactly one result.
    """
    src = STUDY_JS.read_text(encoding="utf-8")
    body = "\n\n".join(extract_function(n, src) for n in function_names)
    script = f"{prelude}\n\n{body}\n\n{epilogue}\n"
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO),
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited {proc.returncode}\n--- stderr ---\n{proc.stderr}"
        )
    out = proc.stdout.strip()
    if not out:
        raise AssertionError(f"no output from node\n--- stderr ---\n{proc.stderr}")
    return json.loads(out.splitlines()[-1])


# A minimal DOM good enough for keyboard handlers: elements record clicks and
# honour `disabled`, so a shortcut that bypasses a disabled control is visible.
DOM_STUB = """
const clicks = [];
const thrown = [];
function makeEl(selectors = {}, opts = {}) {
  return {
    _sel: selectors,
    querySelector(sel) {
      if (opts.missing && opts.missing.includes(sel)) return null;
      return {
        disabled: (opts.disabled || []).includes(sel),
        click() {
          if (this.disabled) { clicks.push(sel + ':BLOCKED'); return; }
          clicks.push(sel);
        },
      };
    },
  };
}
function makeEvent(key, { inInput = false } = {}) {
  let prevented = false;
  return {
    key,
    target: { closest: (sel) => (inInput ? { tagName: 'INPUT' } : null) },
    preventDefault() { prevented = true; },
    get defaultPrevented() { return prevented; },
  };
}
"""
