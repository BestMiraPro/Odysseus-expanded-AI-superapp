"""GitHub access on the owner's behalf, so agents never hold a GitHub credential.

Odysseus keeps one fine-grained personal access token per user (encrypted at
rest, like the shared-memory token) and performs a small set of GitHub
operations itself. Reads work on any repository the token can see. Every
write (push, open a pull request, merge one) first passes ``check_writable``:
the repository must be owned by the token's own personal GitHub account.
Organisation repositories and other users' repositories are refused in code,
whatever the token itself would allow.

Naming: ``owner`` / ``owner_user`` is the Odysseus user whose token is used;
"repository owner" always means the GitHub account that owns a repository.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
from typing import Callable, Dict, Optional, Tuple

from src.constants import DATA_DIR

CONFIG_FILE = os.path.join(DATA_DIR, "github_access.json")
GITHUB_API = "https://api.github.com"
GIT_BASE = "https://github.com"
BRANCH_PREFIX = "odysseus/"
FETCH_TIMEOUT = 15
GIT_TIMEOUT = 300

_TOKEN_RE = re.compile(r"^github_pat_[A-Za-z0-9_]{20,250}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class GitHubError(RuntimeError):
    """A user-facing reason a GitHub operation was refused or failed."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _call(method: str, path: str, token: str, body: Optional[Dict] = None, *,
          not_found: str = "That was not found on GitHub, or the token cannot see it."):
    """One request to api.github.com; the decoded JSON body, or GitHubError."""
    import httpx

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Odysseus-github-access",
        "Authorization": f"Bearer {token}",
    }
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
            resp = client.request(method, f"{GITHUB_API}{path}", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub could not be reached ({type(exc).__name__}).", 502) from None
    if resp.status_code == 404:
        raise GitHubError(not_found, 404)
    if resp.status_code == 401:
        # Not 401: the web UI treats a 401 from Odysseus as "log in again".
        raise GitHubError("GitHub refused the token: it may have expired or been revoked. "
                          "Connect a new one.", 400)
    if resp.status_code == 403:
        raise GitHubError("GitHub refused the request: check the token's permissions for this "
                          "repository.", 403)
    if 400 <= resp.status_code < 500:
        try:
            message = str(resp.json().get("message") or "")
        except Exception:
            message = ""
        raise GitHubError(f"GitHub refused the request ({resp.status_code}){': ' + message if message else ''}.",
                          resp.status_code)
    if not resp.is_success:
        raise GitHubError(f"GitHub answered {resp.status_code}.", 502)
    try:
        return resp.json()
    except ValueError:
        raise GitHubError("GitHub sent an answer Odysseus could not read.", 502) from None


# ---------------------------------------------------------------------------
# Token store
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


def _record(owner: Optional[str]) -> Dict[str, str]:
    raw = _load_all().get(_key(owner))
    if not isinstance(raw, dict) or not raw.get("token") or not raw.get("login"):
        return {}
    return {"token": str(raw["token"]), "login": str(raw["login"])}


def _check_token_format(token: str) -> str:
    token = (token or "").strip()
    if token.startswith("ghp_"):
        raise GitHubError("That is a classic personal access token (ghp_). Odysseus accepts only "
                          "fine-grained personal access tokens (github_pat_...), which can be limited "
                          "to your own repositories.")
    if token.startswith("gho_"):
        raise GitHubError("That is an OAuth token (gho_). Odysseus accepts only fine-grained personal "
                          "access tokens (github_pat_...).")
    if not _TOKEN_RE.match(token):
        raise GitHubError("That is not a fine-grained GitHub personal access token. Create one under "
                          "GitHub Settings > Developer settings > Fine-grained tokens; it starts with "
                          "github_pat_.")
    return token


def connection(owner: Optional[str]) -> Dict[str, object]:
    """Whether ``owner`` has a token stored and the GitHub login it belongs to; nothing secret."""
    record = _record(owner)
    return {"connected": bool(record), "login": record.get("login") or None}


def save_token(owner: Optional[str], token: str) -> Dict[str, object]:
    """Check ``token`` with GitHub and store it encrypted for ``owner``; GitHubError otherwise."""
    from core.atomic_io import atomic_write_json
    from src.secret_storage import encrypt

    token = _check_token_format(token)
    login = str(_call("GET", "/user", token).get("login") or "")
    if not login:
        raise GitHubError("GitHub did not say which account this token belongs to.", 502)
    with _store_lock:
        data = _load_all()
        data[_key(owner)] = {"token": encrypt(token), "login": login}
        atomic_write_json(CONFIG_FILE, data, indent=2)
    return connection(owner)


def forget_token(owner: Optional[str]) -> bool:
    """Remove ``owner``'s stored token record; True when there was one."""
    from core.atomic_io import atomic_write_json

    with _store_lock:
        data = _load_all()
        if _key(owner) not in data:
            return False
        del data[_key(owner)]
        atomic_write_json(CONFIG_FILE, data, indent=2)
    return True


def _credentials(owner_user: Optional[str]) -> Tuple[str, str]:
    """(token, login) for ``owner_user``; GitHubError when no usable token is stored."""
    from src.secret_storage import decrypt

    record = _record(owner_user)
    token = decrypt(record["token"]) if record else ""
    if not token:
        raise GitHubError("Connect a GitHub token first (Settings > GitHub).", 400)
    return token, record["login"]


# ---------------------------------------------------------------------------
# The write allowlist
# ---------------------------------------------------------------------------

def _repo(value: str) -> str:
    """``owner/name`` from ``owner/name`` or a github.com URL (the shared-memory rules)."""
    from src.shared_memory import parse_repo

    try:
        return parse_repo(value)
    except ValueError as exc:
        raise GitHubError(str(exc)) from None


def check_writable(owner_user: Optional[str], repo: str) -> Dict:
    """GitHub's record of ``repo`` if Odysseus may write to it for ``owner_user``.

    The repository must exist, be visible to the token, and be owned by the
    token's own personal account. Asked of GitHub on every call: a yes is
    never cached.
    """
    return _writable(owner_user, repo)[0]


def _writable(owner_user: Optional[str], repo: str) -> Tuple[Dict, str]:
    """``check_writable`` plus the token it checked, which the write then uses.

    Every write goes through here, so the write cannot use a token other than
    the one whose account was just compared with the repository owner.
    """
    repo = _repo(repo)
    token, login = _credentials(owner_user)
    data = _call("GET", f"/repos/{repo}", token,
                 not_found=f"{repo} was not found on GitHub, or this token cannot see it. Odysseus "
                           "only writes to repositories it can confirm you own.")
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    kind, holder = owner.get("type"), str(owner.get("login") or "")
    rule = f"Odysseus only writes to repositories owned by your personal GitHub account ({login})."
    if kind == "Organization":
        raise GitHubError(f"{repo} belongs to the organisation {holder}. {rule}", 403)
    if kind != "User":
        raise GitHubError(f"{repo} is not owned by a personal GitHub account. {rule}", 403)
    if holder.lower() != login.lower():
        raise GitHubError(f"{repo} belongs to another GitHub user ({holder}). {rule}", 403)
    return data, token


def _branch(value: str, *, odysseus: bool) -> str:
    """A plain branch name; with ``odysseus`` it must also start with ``odysseus/``."""
    branch = value or ""
    if "+" in branch:
        raise GitHubError("A branch name must not contain '+': Odysseus never force-pushes.")
    if odysseus and not branch.startswith(BRANCH_PREFIX):
        raise GitHubError(f"Odysseus only writes branches whose names start with {BRANCH_PREFIX}.")
    if (not _BRANCH_RE.match(branch) or ".." in branch or "//" in branch or "/." in branch
            or branch.startswith(("/", ".", "-")) or branch.endswith(("/", ".", ".lock"))):
        raise GitHubError(f"'{branch}' is not a valid branch name.")
    return branch


# ---------------------------------------------------------------------------
# Reads (any repository the token can see; always api.github.com)
# ---------------------------------------------------------------------------

def repository(owner_user: Optional[str], repo: str) -> Dict:
    """``repo``'s owner, visibility and default branch."""
    repo = _repo(repo)
    token, _login = _credentials(owner_user)
    data = _call("GET", f"/repos/{repo}", token, not_found=f"{repo} was not found, or this token cannot see it.")
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    return {"full_name": data.get("full_name"), "owner": owner.get("login"), "owner_type": owner.get("type"),
            "private": data.get("private"), "default_branch": data.get("default_branch"),
            "html_url": data.get("html_url")}


def pull_request(owner_user: Optional[str], repo: str, number: int) -> Dict:
    """Pull request ``number`` in ``repo``: state, head and base."""
    repo = _repo(repo)
    number = _number(number)
    token, _login = _credentials(owner_user)
    return _pull_fields(_call("GET", f"/repos/{repo}/pulls/{number}", token,
                              not_found=f"Pull request #{number} was not found in {repo}."))


def issue(owner_user: Optional[str], repo: str, number: int) -> Dict:
    """Issue ``number`` in ``repo``: title, state, body and labels."""
    repo = _repo(repo)
    number = _number(number)
    token, _login = _credentials(owner_user)
    data = _call("GET", f"/repos/{repo}/issues/{number}", token,
                 not_found=f"Issue #{number} was not found in {repo}.")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return {"number": data.get("number"), "title": data.get("title"), "state": data.get("state"),
            "body": data.get("body") or "", "labels": [str(label.get("name")) for label in data.get("labels") or []
                                                       if isinstance(label, dict)],
            "author": user.get("login"), "html_url": data.get("html_url"),
            "is_pull_request": "pull_request" in data}


def ci_status(owner_user: Optional[str], repo: str, ref: str) -> Dict:
    """The combined commit status and the check runs for ``ref`` (a branch or commit SHA)."""
    repo = _repo(repo)
    ref = _branch(ref, odysseus=False)
    token, _login = _credentials(owner_user)
    missing = f"{ref} was not found in {repo}."
    combined = _call("GET", f"/repos/{repo}/commits/{ref}/status", token, not_found=missing)
    checks = _call("GET", f"/repos/{repo}/commits/{ref}/check-runs", token, not_found=missing)
    return {
        "ref": ref,
        "sha": combined.get("sha"),
        "state": combined.get("state"),
        "statuses": [{"context": s.get("context"), "state": s.get("state"), "description": s.get("description"),
                      "url": s.get("target_url")} for s in combined.get("statuses") or [] if isinstance(s, dict)],
        "check_runs": [{"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion"),
                        "url": c.get("html_url")} for c in checks.get("check_runs") or [] if isinstance(c, dict)],
    }


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

def _run_git(argv, cwd, env) -> Tuple[int, str, str]:
    """Run git with a list argv (never a shell); (returncode, stdout, stderr)."""
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"git could not be run ({type(exc).__name__})."
    return done.returncode, done.stdout, done.stderr


def _git_env(**extra: str) -> Dict[str, str]:
    """A minimal environment for git: nothing inherited that could steer it, no
    system or global config, no prompts and no askpass program."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG")
           if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
    env.update(extra)
    return env


def _ask_working_copy(run_git, argv, workdir, seat=None):
    """Run a read-only git question in the working copy at ``workdir``.

    A council run's working copy is writable by the seats, so its git config
    is theirs. With seat isolation the caller passes ``seat``, a seat user
    that can read the copy (src/seat_user.py): the question is put as that
    user, with none of Odysseus's environment, so nothing a seat wrote there
    runs as Odysseus.
    """
    plain = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
             "GIT_ASKPASS": ""}
    if seat is not None:
        return run_git(seat.argv([argv[0], "-c", "safe.directory=*", *argv[1:]]), workdir, seat.env(plain))
    return run_git(argv, workdir, _git_env())


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def push_branch(owner_user: Optional[str], repo: str, workdir: str, branch: str,
                run_git: Callable[..., Tuple[int, str, str]] = _run_git, seat=None) -> Dict[str, str]:
    """Push ``branch`` from the working copy at ``workdir`` to the same branch on GitHub.

    The repository must pass ``check_writable``, the branch must start with
    ``odysseus/`` and must not be the repository's default branch, and the
    push is never forced. The working copy is only asked for the branch's
    commit and where its objects live: the push itself runs from a fresh, empty
    git folder that borrows those objects, so nothing in the working copy's
    config (remotes, ``url.*.insteadOf`` rewrites, hooks, credential helpers,
    proxies) takes part. The remote URL is built here, and the token reaches
    git only as an ``http.extraheader`` for github.com in the environment.
    """
    repo = _repo(repo)
    branch = _branch(branch, odysseus=True)
    info, token = _writable(owner_user, repo)
    if branch == info.get("default_branch"):
        raise GitHubError(f"{branch} is the default branch of {repo}; Odysseus never pushes to it.", 403)

    plain = _git_env()
    code, out, _err = _ask_working_copy(
        run_git, ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], workdir, seat)
    sha = out.strip()
    if code != 0 or not _SHA_RE.match(sha):
        raise GitHubError(f"The working copy has no branch {branch}.")
    code, out, _err = _ask_working_copy(
        run_git, ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"], workdir, seat)
    objects = out.strip()
    if code != 0 or not objects:
        raise GitHubError("That folder is not a git working copy.")

    credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    with tempfile.TemporaryDirectory(prefix="odysseus-push-") as clean:
        code, _out, err = run_git(["git", "init", "--quiet", "--bare", "--template=", clean], clean, plain)
        if code == 0:
            env = _git_env(
                GIT_ALTERNATE_OBJECT_DIRECTORIES=objects,
                GIT_CONFIG_COUNT="2",
                GIT_CONFIG_KEY_0=f"http.{GIT_BASE}/.extraheader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {credential}",
                GIT_CONFIG_KEY_1="core.hooksPath",
                GIT_CONFIG_VALUE_1=os.devnull,
            )
            code, _out, err = run_git(["git", "--git-dir", clean, "push", "--no-verify",
                                       f"{GIT_BASE}/{repo}.git", f"{sha}:refs/heads/{branch}"], clean, env)
    if code != 0:
        detail = _scrub((err or "").strip(), token, credential)[-1500:]
        raise GitHubError(f"git push failed: {detail or 'no details'}", 502)
    return {"repo": repo, "branch": branch, "sha": sha}


# ---------------------------------------------------------------------------
# Pull requests
# ---------------------------------------------------------------------------

_SAME_REPO = ("Odysseus only works with pull requests between two branches of the same repository: a "
              "cross-repository pull request would write to someone else's repository.")


def _number(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        raise GitHubError("That is not a valid pull request or issue number.")
    return number


def _pull_fields(pr: Dict) -> Dict:
    def side(value) -> Dict:
        value = value if isinstance(value, dict) else {}
        repo = value.get("repo") if isinstance(value.get("repo"), dict) else {}
        return {"ref": value.get("ref"), "sha": value.get("sha"), "repo": repo.get("full_name")}

    return {"number": pr.get("number"), "title": pr.get("title"), "state": pr.get("state"),
            "draft": pr.get("draft"), "merged": pr.get("merged"), "mergeable": pr.get("mergeable"),
            "html_url": pr.get("html_url"), "head": side(pr.get("head")), "base": side(pr.get("base"))}


def open_pull_request(owner_user: Optional[str], repo: str, head: str, base: str, title: str,
                      body: str = "") -> Dict:
    """Open a pull request from ``head`` (an ``odysseus/`` branch) into ``base``, both in ``repo``."""
    repo = _repo(repo)
    if ":" in (head or "") or ":" in (base or ""):
        raise GitHubError(_SAME_REPO, 403)
    head = _branch(head, odysseus=True)
    base = _branch(base, odysseus=False)
    if head == base:
        raise GitHubError("The pull request's head and base must be different branches.")
    title = (title or "").strip()
    if not title:
        raise GitHubError("A pull request needs a title.")
    _info, token = _writable(owner_user, repo)
    pr = _call("POST", f"/repos/{repo}/pulls", token,
               {"title": title, "head": head, "base": base, "body": body or ""})
    return _pull_fields(pr)


def _merge_method(info: Dict) -> Optional[str]:
    """A merge method the repository allows; None keeps GitHub's default (a merge commit)."""
    if info.get("allow_merge_commit") is not False:
        return None
    for method, flag in (("squash", "allow_squash_merge"), ("rebase", "allow_rebase_merge")):
        if info.get(flag):
            return method
    return None


def merge_pull_request(owner_user: Optional[str], repo: str, number: int, sha: str) -> Dict:
    """Merge pull request ``number`` only if its head is still exactly commit ``sha``."""
    repo = _repo(repo)
    sha = (sha or "").strip().lower()
    if not _SHA_RE.match(sha):
        raise GitHubError("Merging needs the exact head commit of the pull request (its full commit SHA).")
    number = _number(number)
    info, token = _writable(owner_user, repo)
    pr = _pull_fields(_call("GET", f"/repos/{repo}/pulls/{number}", token,
                            not_found=f"Pull request #{number} was not found in {repo}."))
    if not str(pr["head"]["ref"] or "").startswith(BRANCH_PREFIX):
        raise GitHubError(f"Odysseus only merges pull requests from branches starting with {BRANCH_PREFIX}.", 403)
    head_repo, base_repo = str(pr["head"]["repo"] or "").lower(), str(pr["base"]["repo"] or "").lower()
    if not head_repo == base_repo == repo.lower():
        raise GitHubError(_SAME_REPO, 403)
    request = {"sha": sha}
    method = _merge_method(info)
    if method:
        request["merge_method"] = method
    result = _call("PUT", f"/repos/{repo}/pulls/{number}/merge", token, request)
    return {"merged": bool(result.get("merged")), "sha": result.get("sha"), "message": result.get("message")}
