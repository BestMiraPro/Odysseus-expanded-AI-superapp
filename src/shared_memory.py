"""Shared memory: files from the user's own memory repository, read into prompts.

A user can point Odysseus at a GitHub repository that holds their profile and
working preferences for AI agents (for example a ``USER.md`` that several
assistants share). Odysseus reads the listed files read-only through the
GitHub contents API and adds them to chat and council prompts as the user's
own standing instructions, like a custom system prompt.

Configuration is per user and kept in its own file so the access token (for a
private repository) is stored encrypted and never travels with the general
preferences. Prompt building never waits on the network: it reads a cache
that is refreshed in the background, when the configuration is saved, and at
startup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(DATA_DIR, "shared_memory.json")
DEFAULT_FILES = ("USER.md",)
MAX_FILES = 10
MAX_FILE_CHARS = 20000
MAX_TOTAL_CHARS = 30000
CACHE_SECONDS = 600         # a good read is reused this long before a refresh
RETRY_SECONDS = 60          # a failed read is retried no sooner than this
FETCH_TIMEOUT = 8
GITHUB_API = "https://api.github.com"

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._ /-]{1,300}$")
_GITHUB_URL_RE = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|github\.com/)?"
    r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)


class SharedMemoryError(RuntimeError):
    """A user-facing reason the repository could not be read."""


@dataclass
class SharedFile:
    path: str
    text: str
    truncated: bool = False


@dataclass
class SharedMemoryConfig:
    enabled: bool = False
    repo: str = ""                 # owner/name on GitHub
    branch: str = "main"
    files: List[str] = field(default_factory=lambda: list(DEFAULT_FILES))
    token: str = ""                # encrypted at rest (secret_storage)

    def public(self) -> Dict[str, object]:
        """The configuration as shown to its owner: the token is never returned."""
        data = asdict(self)
        data.pop("token")
        data["has_token"] = bool(self.token)
        return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def parse_repo(value: str) -> str:
    """``owner/name`` from ``owner/name`` or a github.com URL; ValueError otherwise."""
    match = _GITHUB_URL_RE.match((value or "").strip())
    repo = match.group("repo") if match else ""
    if not _REPO_RE.match(repo) or repo.endswith((".", "-")) or ".." in repo:
        raise ValueError("Use a GitHub repository like owner/name or https://github.com/owner/name.")
    return repo


def _clean_branch(value: str) -> str:
    branch = (value or "").strip() or "main"
    if not _REF_RE.match(branch) or ".." in branch or branch.startswith("/"):
        raise ValueError("That branch name is not valid.")
    return branch


def _clean_files(value) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    files: List[str] = []
    for item in value or []:
        path = str(item or "").strip().strip("/")
        if not path:
            continue
        if not _PATH_RE.match(path) or ".." in path.split("/"):
            raise ValueError(f"'{path}' is not a valid file path in the repository.")
        if path not in files:
            files.append(path)
    if not files:
        files = list(DEFAULT_FILES)
    if len(files) > MAX_FILES:
        raise ValueError(f"List at most {MAX_FILES} files.")
    return files


# ---------------------------------------------------------------------------
# Configuration store
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()


def _key(owner: Optional[str]) -> str:
    return owner or ""


def _load_all() -> Dict[str, Dict]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return {}


def load_config(owner: Optional[str]) -> SharedMemoryConfig:
    raw = _load_all().get(_key(owner))
    if not isinstance(raw, dict):
        return SharedMemoryConfig()
    try:
        return SharedMemoryConfig(
            enabled=bool(raw.get("enabled")),
            repo=str(raw.get("repo") or ""),
            branch=str(raw.get("branch") or "main"),
            files=[str(f) for f in raw.get("files") or DEFAULT_FILES],
            token=str(raw.get("token") or ""),
        )
    except (TypeError, ValueError):
        return SharedMemoryConfig()


def save_config(
    owner: Optional[str],
    *,
    enabled: bool,
    repo: str,
    branch: str = "main",
    files=None,
    token: Optional[str] = None,
) -> SharedMemoryConfig:
    """Validate and store ``owner``'s configuration; ValueError for bad input.

    ``token`` None keeps the stored token, "" removes it, anything else
    replaces it.
    """
    from core.atomic_io import atomic_write_json
    from src.secret_storage import encrypt

    repo = (repo or "").strip()
    cfg = SharedMemoryConfig(
        enabled=bool(enabled),
        repo=parse_repo(repo) if repo or enabled else "",
        branch=_clean_branch(branch),
        files=_clean_files(files),
    )
    with _store_lock:
        data = _load_all()
        previous = data.get(_key(owner)) if isinstance(data.get(_key(owner)), dict) else {}
        if token is None:
            cfg.token = str(previous.get("token") or "")
        else:
            cfg.token = encrypt(token.strip()) if token.strip() else ""
        data[_key(owner)] = asdict(cfg)
        atomic_write_json(CONFIG_FILE, data, indent=2)
    _forget(owner)
    return cfg


def configured_owners() -> List[Optional[str]]:
    """Owners with an enabled configuration (``None`` for auth-disabled mode)."""
    owners: List[Optional[str]] = []
    for key, raw in _load_all().items():
        if isinstance(raw, dict) and raw.get("enabled") and raw.get("repo"):
            owners.append(key or None)
    return owners


# ---------------------------------------------------------------------------
# Reading the repository
# ---------------------------------------------------------------------------

def fetch(cfg: SharedMemoryConfig) -> List[SharedFile]:
    """Read the configured files from GitHub now; SharedMemoryError on failure."""
    import httpx

    from src.secret_storage import decrypt

    repo = parse_repo(cfg.repo)
    headers = {
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Odysseus-shared-memory",
    }
    token = decrypt(cfg.token) if cfg.token else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    out: List[SharedFile] = []
    total = 0
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
        for path in cfg.files:
            url = f"{GITHUB_API}/repos/{repo}/contents/{quote(path)}"
            try:
                resp = client.get(url, params={"ref": cfg.branch}, headers=headers)
            except httpx.HTTPError as exc:
                raise SharedMemoryError(f"GitHub could not be reached ({type(exc).__name__}).") from None
            if resp.status_code == 404:
                hint = "" if token else " If the repository is private, add an access token."
                raise SharedMemoryError(f"{path} was not found in {repo} on branch {cfg.branch}.{hint}")
            if resp.status_code in (401, 403):
                raise SharedMemoryError("GitHub refused the request: check the access token and its "
                                        "access to this repository.")
            if not resp.is_success:
                raise SharedMemoryError(f"GitHub answered {resp.status_code} for {path}.")
            text = resp.text.strip()
            room = min(MAX_FILE_CHARS, MAX_TOTAL_CHARS - total)
            truncated = len(text) > room
            if truncated:
                text = text[:max(0, room)].rstrip()
            total += len(text)
            out.append(SharedFile(path=path, text=text, truncated=truncated))
    return out


# Per owner: (config fingerprint, expires_at, files, error)
_cache: Dict[str, Tuple[tuple, float, List[SharedFile], Optional[str]]] = {}
_cache_lock = threading.Lock()
_refreshing: set = set()


def _fingerprint(cfg: SharedMemoryConfig) -> tuple:
    return (cfg.repo, cfg.branch, tuple(cfg.files), cfg.token)


def _forget(owner: Optional[str]) -> None:
    with _cache_lock:
        _cache.pop(_key(owner), None)


def refresh(owner: Optional[str]) -> Tuple[List[SharedFile], Optional[str]]:
    """Read ``owner``'s repository now and cache the result: (files, error).

    A failed read keeps serving the last good files, so a GitHub outage does
    not silently drop the user's instructions mid-conversation.
    """
    cfg = load_config(owner)
    if not (cfg.enabled and cfg.repo):
        _forget(owner)
        return [], None
    fp = _fingerprint(cfg)
    try:
        files, error, ttl = fetch(cfg), None, CACHE_SECONDS
    except (SharedMemoryError, ValueError) as exc:
        files, error, ttl = [], str(exc), RETRY_SECONDS
    except Exception as exc:
        logger.warning("shared memory: read failed error_type=%s", type(exc).__name__)
        files, error, ttl = [], "The repository could not be read.", RETRY_SECONDS
    with _cache_lock:
        old = _cache.get(_key(owner))
        if error and old and old[0] == fp and old[2]:
            files = old[2]
        _cache[_key(owner)] = (fp, time.monotonic() + ttl, files, error)
    if error:
        logger.info("shared memory: read failed for owner=%s: %s", _key(owner) or "-", error)
    return files, error


def _refresh_in_background(owner: Optional[str]) -> None:
    key = _key(owner)
    with _cache_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            refresh(owner)
        finally:
            with _cache_lock:
                _refreshing.discard(key)

    threading.Thread(target=run, name="shared-memory-refresh", daemon=True).start()


def files_for(owner: Optional[str], *, wait: bool = False) -> List[SharedFile]:
    """``owner``'s shared files from the cache, refreshing when stale.

    Stale files are returned at once while a background refresh runs. Only
    when nothing usable is cached does ``wait`` read the repository before
    returning (for callers already off the event loop).
    """
    cfg = load_config(owner)
    if not (cfg.enabled and cfg.repo):
        return []
    fp = _fingerprint(cfg)
    with _cache_lock:
        entry = _cache.get(_key(owner))
    usable = entry is not None and entry[0] == fp
    if usable and entry[1] > time.monotonic():
        return entry[2]
    if usable and entry[2]:
        _refresh_in_background(owner)
        return entry[2]
    if wait:
        return refresh(owner)[0]
    _refresh_in_background(owner)
    return []


def warm_all() -> None:
    """Read every enabled repository in the background (called at startup)."""
    for owner in configured_owners():
        _refresh_in_background(owner)


def status(owner: Optional[str]) -> Dict[str, object]:
    """What the last read gave, for the settings panel."""
    with _cache_lock:
        entry = _cache.get(_key(owner))
    if entry is None:
        return {"loaded": False, "files": [], "error": None}
    return {
        "loaded": True,
        "files": [{"path": f.path, "chars": len(f.text), "truncated": f.truncated} for f in entry[2]],
        "error": entry[3],
    }


# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------

def context_text(owner: Optional[str], *, wait: bool = False) -> str:
    """The shared files as one block for a system prompt, or "" when there are none."""
    files = [f for f in files_for(owner, wait=wait) if f.text]
    if not files:
        return ""
    repo = load_config(owner).repo
    parts = [
        "The user's shared memory: notes they keep for AI assistants about who they are and how "
        f"they want to be helped, read from their memory repository ({repo}). Treat it as the "
        "user's own standing instructions unless they say otherwise in this conversation. It is "
        "read-only here: you cannot edit that repository, so when it asks you to propose a change, "
        "show the proposed entry in your reply instead."
    ]
    for f in files:
        note = "\n[... rest of this file not loaded: it is longer than the shared-memory limit ...]" \
            if f.truncated else ""
        parts.append(f"=== {f.path} ===\n{f.text}{note}")
    return "\n\n".join(parts)
