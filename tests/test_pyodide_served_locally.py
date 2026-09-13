"""Pyodide is served from this origin, and the page's policy was not widened for it.

Python in the browser hung forever because the runtime loaded from
cdn.jsdelivr.net while the page's CSP allowed only same-origin fetches
(connect-src 'self'). There were two ways out: widen the policy to the CDN, or
serve the runtime from here. The second was chosen, for the same reason KaTeX and
Mermaid were already moved off that CDN -- it breaks offline installs and tells a
third party every time a session runs Python.

That choice is only real while several separate files keep agreeing with each
other: the URL the browser loads, the version in the manifest, the directory the
fetch script writes to, the build step that fetches it, and the ignore files that
keep a local copy out of git and out of the image. These pin that agreement, and
pin the policy itself, so the CDN cannot drift back in unnoticed.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware

REPO = Path(__file__).resolve().parent.parent
RUNNER = (REPO / "static" / "js" / "codeRunner.js").read_text(encoding="utf-8")
MANIFEST = json.loads((REPO / "scripts" / "pyodide_manifest.json").read_text(encoding="utf-8"))


def _index_url() -> str:
    match = re.search(r"^const PYODIDE_INDEX_URL = '([^']+)';", RUNNER, re.MULTILINE)
    assert match, "PYODIDE_INDEX_URL is no longer a plain string constant"
    return match.group(1)


def _active_lines(text: str) -> list[str]:
    """Non-comment lines of an ignore file, stripped."""
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# --------------------------------------------------------------------------
# The browser loads from this origin
# --------------------------------------------------------------------------

def test_the_runtime_is_loaded_from_this_origin():
    url = _index_url()
    assert "://" not in url, f"the runtime is loaded from another host: {url}"
    assert url.startswith("/static/lib/pyodide/")


def test_no_code_path_still_reaches_for_the_cdn():
    """Checked on code lines only: the comment explaining the move names the
    CDN, and a naive substring test would fail on the explanation."""
    code = [ln for ln in RUNNER.splitlines()
            if not ln.strip().startswith(("//", "*", "/*"))]
    offenders = [ln.strip() for ln in code if "jsdelivr" in ln]
    assert not offenders, offenders


def test_the_loaded_version_is_the_pinned_version():
    assert _index_url() == f"/static/lib/pyodide/{MANIFEST['version']}/", (
        "codeRunner.js and scripts/pyodide_manifest.json name different Pyodide "
        "versions; the browser would request files that were never fetched"
    )


def test_the_fetch_script_writes_where_the_browser_loads_from():
    spec = importlib.util.spec_from_file_location(
        "fetch_pyodide", REPO / "scripts" / "fetch_pyodide.py")
    fp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fp)

    written = fp.destination(MANIFEST, root=REPO).relative_to(REPO / "static").as_posix()
    assert f"/static/{written}/" == _index_url()


# --------------------------------------------------------------------------
# The policy stays tight
# --------------------------------------------------------------------------

def _csp_tokens(directive: str) -> list[str]:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    csp = TestClient(app).get("/").headers["content-security-policy"]
    for part in csp.split(";"):
        bits = part.split()
        if bits and bits[0] == directive:
            return bits[1:]
    return []


def test_connect_src_was_not_widened_for_the_runtime():
    """Serving locally is precisely what let this stay 'self'. If a CDN is ever
    added here, every Python run starts announcing itself to that host again."""
    assert _csp_tokens("connect-src") == ["'self'"]


# --------------------------------------------------------------------------
# The build fetches it; git and the image context never carry a local copy
# --------------------------------------------------------------------------

def test_the_fetched_runtime_is_kept_out_of_git():
    assert "static/lib/pyodide/" in _active_lines(
        (REPO / ".gitignore").read_text(encoding="utf-8"))


def test_a_local_copy_cannot_ride_into_the_image_unverified():
    assert "static/lib/pyodide/" in _active_lines(
        (REPO / ".dockerignore").read_text(encoding="utf-8"))


def test_the_image_fetches_and_verifies_before_copying_the_app():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    copy_script = dockerfile.find("COPY scripts/fetch_pyodide.py scripts/pyodide_manifest.json")
    fetch = dockerfile.find("RUN python scripts/fetch_pyodide.py")
    copy_app = dockerfile.find("\nCOPY . .")
    assert -1 not in (copy_script, fetch, copy_app), (copy_script, fetch, copy_app)
    assert copy_script < fetch < copy_app, (
        "the runtime must be fetched from a layer that only depends on the script "
        "and manifest, before the app is copied over it"
    )
