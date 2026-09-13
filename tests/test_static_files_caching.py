"""Static caching: revalidate the app's own source, keep pinned runtimes outright.

The app ships raw ES modules with no build step, so its .js/.css/.html must
revalidate on every load or a deploy never reaches the browser. Pyodide is the
opposite case: 26.8 MB served from a version-pinned directory, whose bytes can
never change under a given URL. Revalidating all of it on every page load would
be pure waste, so those URLs are served immutable.

The rules have to be precise in both directions, and each direction has a way to
fail quietly:

  * too narrow -- StaticFiles passes an OS-normalised path, so on Windows the
    prefix arrives with backslashes and a naive check never matches;
  * too broad  -- static/lib/ also holds KaTeX and Mermaid, which are NOT
    versioned in their paths; pinning those would freeze every browser on a
    stale copy through the next upgrade;
  * a 404 must never be cached, or a native install that had not yet fetched the
    runtime would stay broken for a year after the fix.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from core.static_files import (
    IMMUTABLE,
    RevalidatingStatic,
    is_versioned_vendor_asset,
    register_runtime_mimetypes,
)

RUNTIME = "lib/pyodide/0.27.5"
WHEEL = "numpy-2.0.2-cp312-cp312-pyodide_2024_0_wasm32.whl"


@pytest.fixture
def client(tmp_path):
    register_runtime_mimetypes()
    runtime = tmp_path / RUNTIME
    runtime.mkdir(parents=True)
    (runtime / "pyodide.js").write_text("// runtime", encoding="utf-8")
    (runtime / "pyodide.asm.wasm").write_bytes(b"\0asm\x01\0\0\0")
    (runtime / WHEEL).write_bytes(b"PK\x03\x04")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "study.js").write_text("export {};", encoding="utf-8")
    (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "icon.png").write_bytes(b"\x89PNG")
    (tmp_path / "lib" / "katex.min.js").write_text("// katex", encoding="utf-8")

    app = Starlette(routes=[Mount("/static", app=RevalidatingStatic(directory=str(tmp_path)))])
    return TestClient(app)


# --------------------------------------------------------------------------
# The pinned runtime
# --------------------------------------------------------------------------

def test_the_runtime_script_is_immutable_even_though_it_is_js(client):
    """Checked before the .js rule, or no-cache would win."""
    response = client.get(f"/static/{RUNTIME}/pyodide.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE


def test_the_wasm_is_immutable_and_typed_for_streaming_compilation(client):
    response = client.get(f"/static/{RUNTIME}/pyodide.asm.wasm")
    assert response.headers["cache-control"] == IMMUTABLE
    assert response.headers["content-type"].startswith("application/wasm"), (
        "WebAssembly.instantiateStreaming rejects any other type"
    )


def test_wheels_are_not_served_as_text(client):
    response = client.get(f"/static/{RUNTIME}/{WHEEL}")
    assert response.headers["content-type"].startswith("application/zip")


def test_a_missing_runtime_file_is_never_cached(client):
    """A native install that has not run fetch_pyodide.py gets a 404 here. If
    that 404 were marked immutable, fetching the runtime would not fix the
    browser for a year."""
    response = client.get(f"/static/{RUNTIME}/not-fetched-yet.js")
    assert response.status_code == 404
    assert "immutable" not in response.headers.get("cache-control", "")


# --------------------------------------------------------------------------
# Everything else keeps its existing behaviour
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["js/study.js", "style.css"])
def test_app_source_still_revalidates(client, path):
    assert client.get(f"/static/{path}").headers["cache-control"] == "no-cache"


def test_an_unversioned_vendored_library_is_not_pinned(client):
    """KaTeX sits in static/lib too, but its path carries no version."""
    response = client.get("/static/lib/katex.min.js")
    assert response.headers["cache-control"] == "no-cache"


def test_other_assets_are_left_alone(client):
    assert "cache-control" not in client.get("/static/icon.png").headers


# --------------------------------------------------------------------------
# The prefix check itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "lib/pyodide/0.27.5/pyodide.js",
    "lib\\pyodide\\0.27.5\\pyodide.asm.wasm",   # what StaticFiles passes on Windows
])
def test_runtime_paths_match_on_every_platform(path):
    assert is_versioned_vendor_asset(path)


@pytest.mark.parametrize("path", [
    "lib/pyodide",                 # the directory name without its separator
    "lib/pyodide-evil/x.js",       # a sibling that merely shares the prefix
    "libpyodide/x.js",
    "js/lib/pyodide/x.js",         # the prefix deeper in the tree
    "lib/katex.min.js",
])
def test_lookalike_paths_do_not(path):
    assert not is_versioned_vendor_asset(path)
