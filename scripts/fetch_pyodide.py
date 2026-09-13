#!/usr/bin/env python3
"""Fetch the pinned Pyodide runtime into static/lib/pyodide/<version>/.

Python in the browser -- the chat's Run button, and Study's plots -- runs on
Pyodide. It used to load from cdn.jsdelivr.net, which failed twice over: the
page's Content-Security-Policy allows only same-origin fetches
(connect-src 'self'), so Pyodide could not download its own .wasm and stdlib;
and this project had already moved KaTeX and Mermaid off that CDN because it
"broke offline installs, and announced every session to a third party"
(static/index.html).

So the runtime is served from this origin. At 26.8 MB across 18 files it is too
large to commit, so it is fetched -- at Docker build time, or once by hand for a
native install -- and every file is verified against pyodide_manifest.json
before it is written where the app can serve it.

The manifest's hashes are not "whatever the CDN served that day". They were
anchored to a second, independent provider: the pyodide tarball from the npm
registry was checked against the registry's own sha512 integrity field, the
five core runtime files on the CDN were confirmed byte-identical to that
tarball, and each wheel matched the sha256 recorded in the tarball's
pyodide-lock.json. See the commit that introduced this file.

Usage:
    python scripts/fetch_pyodide.py           # fetch anything missing, verify all
    python scripts/fetch_pyodide.py --check   # verify only; fetch nothing

Exit status: 0 all files present and verified; 1 a file failed verification or
is missing (--check); 2 the network could not deliver a file.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "pyodide_manifest.json"

CHUNK = 1 << 16
ATTEMPTS = 3
TIMEOUT_S = 120


class IntegrityError(Exception):
    """A file arrived, but not the file the manifest names. Never retried."""


class NetworkError(Exception):
    """A file could not be delivered after every attempt."""


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    source = manifest.get("source", "")
    if not (source.startswith("https://") and source.endswith("/")):
        raise ValueError(f"manifest source must be an https:// directory URL: {source!r}")
    for name, meta in manifest.get("files", {}).items():
        # Each entry becomes a filename on disk, so a tampered manifest must not
        # be able to name a path outside the destination directory.
        if name in {"", ".", ".."} or name != Path(name).name or "/" in name or "\\" in name:
            raise ValueError(f"manifest filename is not a plain basename: {name!r}")
        if len(str(meta.get("sha256", ""))) != 64 or int(meta.get("size", -1)) < 0:
            raise ValueError(f"manifest entry {name!r} lacks a sha256 or size")
    return manifest


def destination(manifest: dict, root: Path = REPO) -> Path:
    """The version is part of the path, so an upgrade changes the URL instead of
    leaving a browser holding a mismatched mix of old and new files."""
    return root / "static" / "lib" / "pyodide" / manifest["version"]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid(path: Path, meta: dict) -> bool:
    return (path.is_file()
            and path.stat().st_size == meta["size"]
            and file_sha256(path) == meta["sha256"])


def download(url: str, target: Path, meta: dict) -> None:
    """Stream url to target, verifying before the file becomes visible.

    Bytes go to a sibling .part file and are only moved into place once size and
    sha256 both match, so an interrupted or corrupted download can never be
    served. A network error is retried; a wrong hash is not -- more attempts at
    fetching the wrong bytes cannot produce the right ones.
    """
    part = target.with_name(target.name + ".part")
    last_error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response, part.open("wb") as out:
                for chunk in iter(lambda: response.read(CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            if size != meta["size"] or digest.hexdigest() != meta["sha256"]:
                part.unlink(missing_ok=True)
                raise IntegrityError(
                    f"{target.name}: got {size} bytes sha256={digest.hexdigest()}, "
                    f"expected {meta['size']} bytes sha256={meta['sha256']}")
            os.replace(part, target)
            return
        except IntegrityError:
            raise
        except (OSError, http.client.HTTPException) as exc:   # URLError is an OSError
            last_error = exc
            part.unlink(missing_ok=True)
            if attempt < ATTEMPTS:
                time.sleep(2 ** attempt)
    raise NetworkError(f"{url}: {last_error}")


def sync(manifest: dict, dest: Path, *, fetch: bool = True, downloader=download) -> dict:
    """Bring dest in line with the manifest. Returns what happened, per file."""
    report: dict = {"ok": [], "fetched": [], "missing": [], "bad": []}
    for name, meta in sorted(manifest["files"].items()):
        path = dest / name
        if is_valid(path, meta):
            report["ok"].append(name)
            continue
        if not fetch:
            report["bad" if path.exists() else "missing"].append(name)
            continue
        downloader(manifest["source"] + name, path, meta)
        report["fetched"].append(name)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the files already present; download nothing")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    dest = destination(manifest)
    if not args.check:
        dest.mkdir(parents=True, exist_ok=True)

    try:
        report = sync(manifest, dest, fetch=not args.check)
    except IntegrityError as exc:
        print(f"fetch_pyodide: verification FAILED -- {exc}", file=sys.stderr)
        return 1
    except NetworkError as exc:
        print(f"fetch_pyodide: could not download -- {exc}", file=sys.stderr)
        return 2

    total = sum(m["size"] for m in manifest["files"].values())
    print(f"Pyodide {manifest['version']} -> {dest}")
    print(f"  {len(report['ok'])} already verified, {len(report['fetched'])} fetched "
          f"and verified ({total / 1e6:.1f} MB total)")
    if report["missing"] or report["bad"]:
        for name in report["missing"]:
            print(f"  missing: {name}", file=sys.stderr)
        for name in report["bad"]:
            print(f"  failed verification: {name}", file=sys.stderr)
        print("Run `python scripts/fetch_pyodide.py` to fetch them.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
