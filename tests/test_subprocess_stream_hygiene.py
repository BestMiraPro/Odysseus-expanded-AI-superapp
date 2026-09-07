"""A test's subprocess must not inherit pytest's captured stdout/stderr.

Under pytest's default fd capture on Windows, an inherited stdout or stderr is
not always a handle ``CreateProcess`` can duplicate, and the child fails to
start with::

    OSError: [WinError 50] The request is not supported
      ... at subprocess.py, _winapi.DuplicateHandle

It is nondeterministic — it depends on what else in the session has touched
fd 1 and 2 — which is how ``test_rag_vector_id_stability`` and
``test_slash_setup_provider_aliases`` came to pass and fail on identical
back-to-back runs at the same commit.

``tests/conftest.py`` already defaults **stdin** to DEVNULL for this reason;
the comment there records the same class of failure (WinError 6) flipping
40-100 tests across 63 files. stdout and stderr cannot be defaulted globally
without swallowing output tests legitimately inherit, so they are checked here
instead.

``subprocess.check_output`` pipes stdout but leaves stderr inherited, which is
why it is not exempt.
"""

from __future__ import annotations

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent

# Callables that start a child process.
_RUNNERS = {"run", "check_output", "check_call", "call", "Popen"}


def _unredirected_calls():
    """Yield (file, line, func, kwargs) for calls leaving a stream inherited."""
    for path in sorted(TESTS.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None)
            base = getattr(getattr(func, "value", None), "id", None)
            if name not in _RUNNERS or base != "subprocess":
                continue
            kwargs = {k.arg for k in node.keywords}
            captures = "capture_output" in kwargs
            # check_output pipes stdout for you, but never stderr.
            out_ok = captures or "stdout" in kwargs or name == "check_output"
            err_ok = captures or "stderr" in kwargs
            if not (out_ok and err_ok):
                yield path.name, node.lineno, name, sorted(kwargs)


def test_no_test_subprocess_inherits_a_captured_stream():
    offenders = list(_unredirected_calls())
    detail = "\n".join(
        f"  {f}:{line}  subprocess.{fn}({', '.join(kw) or 'no redirection'})"
        for f, line, fn, kw in offenders
    )
    assert not offenders, (
        "these subprocess calls inherit pytest's captured stdout/stderr, which "
        "fails nondeterministically on Windows with WinError 50:\n" + detail
    )
