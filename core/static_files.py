"""Static file serving: two caching rules, and the types one runtime needs.

Lives here rather than inline in app.py so the rules can be exercised on their
own. Importing app.py runs the whole application's startup, which is why no test
had ever touched the static mount -- and one rule below has a Windows-only
failure mode that only a real request exercises.
"""

from __future__ import annotations

import mimetypes

from fastapi.staticfiles import StaticFiles

# Vendored runtimes whose URL carries their version. The bytes behind one of
# these URLs never change -- an upgrade is a new directory -- so a browser may
# keep them outright instead of revalidating ~27 MB of Pyodide on every load.
#
# Only runtimes pinned by version belong here. static/lib/ also holds KaTeX and
# Mermaid, which are NOT versioned in their paths, and marking those immutable
# would pin every browser to a stale copy through a future upgrade.
VERSIONED_VENDOR_PREFIXES = ("lib/pyodide/",)

IMMUTABLE = "public, max-age=31536000, immutable"


def register_runtime_mimetypes() -> None:
    """Types the in-browser Python runtime needs, which the OS database cannot be
    trusted to supply.

    Streaming WebAssembly compilation demands application/wasm exactly; on
    Windows that mapping comes from the registry, where it can be absent. Wheels
    have no registered type on any platform, so Starlette would serve them as
    text/plain.
    """
    mimetypes.add_type("application/wasm", ".wasm")
    mimetypes.add_type("application/zip", ".whl")


def is_versioned_vendor_asset(path: str) -> bool:
    """True for a file under a version-pinned vendored runtime.

    StaticFiles hands get_response an OS-normalised path, which on Windows uses
    backslashes. A bare startswith("lib/pyodide/") therefore never matched on a
    native Windows install -- silently, since the only symptom is a missed cache.
    """
    return path.replace("\\", "/").startswith(VERSIONED_VENDOR_PREFIXES)


class RevalidatingStatic(StaticFiles):
    """Serve static assets normally, but force the browser to REVALIDATE
    source files (.js/.css/.html) on every load instead of serving a stale
    copy from disk cache. The app ships raw ES modules with no build step or
    versioned URLs, so browsers were caching modules across deploys — a code
    change wouldn't appear without a manual hard-refresh. `no-cache` keeps the
    cached bytes but requires a conditional request; unchanged files still
    return a cheap 304 (ETag/Last-Modified are preserved).

    The exception is a version-pinned vendored runtime, checked first because it
    also ships .js files: those URLs are immutable by construction.

    A missing file is never marked immutable. StaticFiles raises for a 404 before
    this method can add a header, and that ordering matters: a native install
    that has not fetched the runtime yet must not have the 404 cached for a year
    and outlive the fix.
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if is_versioned_vendor_asset(path):
            resp.headers["Cache-Control"] = IMMUTABLE
        elif path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
