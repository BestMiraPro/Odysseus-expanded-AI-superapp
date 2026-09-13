"""The app page must let WebAssembly compile, and nothing broader.

Every in-browser Python run -- the chat's Run button and Study's plots alike --
hung on "Loading Python runtime" forever. Reproduced in a browser, the page's
Content-Security-Policy produced two violations on each attempt:

    script-src   blocked "wasm-eval"
    connect-src  blocked pyodide.asm.wasm, python_stdlib.zip, pyodide-lock.json

The first is independent of where the files are served from: without
'wasm-unsafe-eval' the browser refuses to compile WebAssembly at all, even a
module fetched from this origin. So it is required, and these tests pin it.

They also pin what must NOT come with it. 'wasm-unsafe-eval' is scoped to
WebAssembly compilation; 'unsafe-eval' would re-enable eval() and
new Function(), and 'unsafe-inline' would undo the nonce -- either of which
turns an injected string back into running script. The tokens are compared
whole, because 'wasm-unsafe-eval' contains 'unsafe-eval' as a substring and a
substring check would pass or fail for the wrong reason.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware


def _csp_for(path: str) -> str:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    async def endpoint():
        return {"ok": True}

    app.add_api_route(path, endpoint)
    response = TestClient(app).get(path)
    assert response.status_code == 200
    return response.headers.get("content-security-policy", "")


def _tokens(csp: str, directive: str) -> list[str]:
    """The source list of one directive, as whole tokens."""
    for part in csp.split(";"):
        bits = part.strip().split()
        if bits and bits[0] == directive:
            return bits[1:]
    return []


def test_the_app_page_allows_webassembly_compilation():
    assert "'wasm-unsafe-eval'" in _tokens(_csp_for("/"), "script-src"), (
        "without 'wasm-unsafe-eval' Pyodide cannot compile and every Python "
        "run hangs on 'Loading Python runtime'"
    )


def test_general_eval_stays_blocked():
    """The whole point of the wasm-only token."""
    script_src = _tokens(_csp_for("/"), "script-src")
    assert "'unsafe-eval'" not in script_src, (
        "'unsafe-eval' re-enables eval() and new Function(); only WebAssembly "
        "compilation was needed"
    )


def test_scripts_stay_nonce_gated():
    script_src = _tokens(_csp_for("/"), "script-src")
    assert "'unsafe-inline'" not in script_src
    assert any(t.startswith("'nonce-") for t in script_src), (
        "the nonce is what keeps injected inline <script> from running"
    )


def test_report_pages_do_not_gain_webassembly():
    """Research reports run no Python; their policy should not widen with this."""
    script_src = _tokens(_csp_for("/api/research/report/r-1"), "script-src")
    assert script_src, "the report page lost its script-src"
    assert "'wasm-unsafe-eval'" not in script_src
