"""Requests from a work run's agents to the owner: open a pull request on GitHub, or merge one.

A seat or the chairman asks by writing a message to ``odysseus`` that starts
with a front-matter block (``FORMAT``), which ``parse`` reads strictly. A valid
request is kept with its round's work data as pending and makes no GitHub
call: only the owner's click in the Odysseus page approves it, and Odysseus
then carries it out through ``src.github_access`` (routes/council_work_routes.py).
Nothing in this module talks to GitHub.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.prompt_security import untrusted_context_message

OPEN_PR = "open_pr"
MERGE_PR = "merge_pr"

PENDING = "pending"
APPROVING = "approving"     # approved by the owner, being carried out
APPROVED = "approved"
DENIED = "denied"
FAILED = "failed"

MAX_TITLE = 200

# Requests this process is carrying out right now. One saved as approving
# that is not here was cut off by a restart (see ``effective``).
EXECUTING: set = set()

INTERRUPTED = ("Odysseus stopped while carrying out this request, so whether it reached GitHub is not known: "
               "check GitHub before asking again.")

FORMAT = (
    "A request is a message to odysseus (an outbox file odysseus__<slug>.md) that starts with a block between "
    "two --- lines. To open a pull request from the run's branch:\n"
    "---\n"
    "action: open_pr\n"
    "title: <one line>\n"
    "base: <the branch to merge into; leave this line out for the repository's default branch>\n"
    "---\n"
    "<the pull request's description>\n"
    "To merge a pull request of the run's branch:\n"
    "---\n"
    "action: merge_pr\n"
    "number: <the pull request's number>\n"
    "---\n"
    "<why it is ready; optional>"
)

HELP = (
    "\nGitHub: Odysseus pushes the run's branch to GitHub after each round that changes it; never push it "
    "yourself. Opening a pull request and merging one need the owner's approval, which only the owner can give, "
    "one request at a time, in the Odysseus page.\n" + FORMAT + "\n"
    "A request covers the run's branch exactly as it is committed in the main copy when Odysseus reads your "
    "message, so ask once that work is committed there (usually the chairman, after integrating). If the branch "
    "moves before the owner answers, carrying out the request fails and it has to be asked again. Odysseus "
    "tells you when the owner decides."
)

_KEY_RE = re.compile(r"^([a-z_]+)[ \t]*:(.*)$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,9}$")
# What each action needs and may have, besides ``action``.
_FIELDS = {OPEN_PR: (("title",), ("base",)), MERGE_PR: (("number",), ())}
_ORIGIN_RE = re.compile(
    r"^(?:https://(?:[^@/\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$",
    re.IGNORECASE)


class RequestError(ValueError):
    """Why a message to odysseus is not a request Odysseus can read."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def parse(text: str) -> Dict[str, Any]:
    """The request in a message: action, title, base, number and body; RequestError otherwise."""
    lines = (text or "").replace("\r\n", "\n").lstrip("﻿").split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start == len(lines) or lines[start].strip() != "---":
        raise RequestError("it does not start with a --- line")
    fields: Dict[str, str] = {}
    for end in range(start + 1, len(lines)):
        line = lines[end].strip()
        if not line:
            continue
        if line == "---":
            break
        match = _KEY_RE.match(line)
        if not match:
            raise RequestError(f"the line {line[:60]!r} in the block is not 'key: value'")
        key = match.group(1)
        if key in fields:
            raise RequestError(f"{key} is given twice")
        fields[key] = _unquote(match.group(2))
    else:
        raise RequestError("the block has no closing --- line")
    body = "\n".join(lines[end + 1:]).strip()

    action = fields.pop("action", "")
    if action not in _FIELDS:
        raise RequestError("action must be open_pr or merge_pr")
    required, optional = _FIELDS[action]
    extra = sorted(set(fields) - set(required) - set(optional))
    if extra:
        raise RequestError(f"{action} does not take {', '.join(extra)}")
    for key in required:
        if not fields.get(key):
            raise RequestError(f"{action} needs {key}")
    if action == OPEN_PR:
        title, base = fields["title"], fields.get("base") or None
        if len(title) > MAX_TITLE:
            raise RequestError(f"the title is longer than {MAX_TITLE} characters")
        if base is not None and (not _BRANCH_RE.match(base) or ".." in base):
            raise RequestError(f"base {base[:60]!r} is not a branch name")
        return {"action": OPEN_PR, "title": title, "base": base, "number": None, "body": body}
    if not _NUMBER_RE.match(fields["number"]):
        raise RequestError("number must be a pull request number, such as 12")
    return {"action": MERGE_PR, "title": None, "base": None, "number": int(fields["number"]), "body": body}


def message_body(text: str) -> str:
    """A delivered message without the header Odysseus put on top of it (see council_workspace)."""
    if text.startswith("---\n"):
        _, sep, body = text[4:].partition("\n---\n\n")
        if sep:
            return body
    return text


def github_repo(url: str) -> Optional[str]:
    """``owner/name`` when the git remote ``url`` is a github.com repository (https or ssh), else None."""
    match = _ORIGIN_RE.match((url or "").strip())
    if not match:
        return None
    repo = match.group("repo")
    return None if repo.split("/", 1)[1] in (".", "..") else repo


def new_request(parsed: Dict[str, Any], *, sender: str, round_no: int, branch: str, head_sha: str,
                repo: str) -> Dict[str, Any]:
    """A pending request: what was asked, by whom, and the run branch's commit when it was asked."""
    return {"id": uuid.uuid4().hex[:12], **parsed, "from": sender, "round": round_no, "branch": branch,
            "head_sha": head_sha, "repo": repo, "status": PENDING, "created_at": _now(), "decided_at": None,
            "error": None, "pr_number": None, "pr_url": None, "merge_sha": None}


def decided(request: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    """``request`` updated in place with the owner's decision or its outcome; returns it."""
    request.update(fields, decided_at=_now())
    return request


def effective(request: Dict[str, Any]) -> Dict[str, Any]:
    """``request`` as it stands: one a restart left approving shows as failed."""
    if request.get("status") == APPROVING and request.get("id") not in EXECUTING:
        return {**request, "status": FAILED, "error": request.get("error") or INTERRUPTED}
    return request


def guarded(label: str, text: str) -> str:
    """``text`` that came from GitHub, inside the untrusted-content guard (src/prompt_security.py)."""
    return untrusted_context_message(label, text)["content"]


def summary(request: Dict[str, Any]) -> str:
    """One line naming a request, for the agents."""
    if request.get("action") == MERGE_PR:
        what = f"merge pull request #{request.get('number')}"
    else:
        what = f"open a pull request titled {request.get('title')!r}"
    return f"request {request.get('id')} from {request.get('from')} to {what} (commit {str(request.get('head_sha'))[:10]})"


def malformed(name: str, reason: str) -> str:
    """The notice for a message to odysseus that is not a request."""
    return (f"Your message {name} to odysseus is not a request Odysseus can read: {reason}. Nothing was recorded. "
            f"Odysseus reads every message addressed to it as a request.\n{FORMAT}")


def outcome(request: Dict[str, Any]) -> str:
    """What the agents are told once the owner has decided on ``request`` (GitHub's text guarded)."""
    what = summary(request)
    status = request.get("status")
    if status == DENIED:
        return f"The owner denied {what}. Nothing was done on GitHub."
    if status == APPROVED and request.get("action") == MERGE_PR:
        return (f"The owner approved {what}, and Odysseus merged exactly that commit. GitHub's answer:\n"
                + guarded("GitHub", f"merge commit {request.get('merge_sha')} {request.get('pr_url') or ''}"))
    if status == APPROVED:
        return (f"The owner approved {what}, and Odysseus opened the pull request. GitHub's answer:\n"
                + guarded("GitHub", f"#{request.get('pr_number')} {request.get('pr_url')}"))
    return (f"The owner approved {what}, but Odysseus could not carry it out, so it is marked failed and nothing "
            "more was done. Send a new request to try again. The reason:\n"
            + guarded("GitHub", request.get("error") or "unknown"))
