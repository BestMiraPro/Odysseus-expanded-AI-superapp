"""GitHub access: token storage, the personal-repos-only write allowlist, push, PRs and reads.

No test here talks to GitHub: every HTTP call goes to an ``httpx.MockTransport``
and every git command to a fake ``run_git`` (or, for the few real-git tests, to
local ``file://`` repositories in a temporary folder).
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import shutil
import subprocess

import httpx
import pytest

from src import github_access as ga

_REAL_CLIENT = httpx.Client
TOKEN = "github_pat_11AAAAAAA0" + "b" * 12 + "_" + "c" * 59
OTHER_TOKEN = "github_pat_11ZZZZZZZ0" + "d" * 12 + "_" + "e" * 59
SHA = "a" * 40
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def _repo(full_name, owner_type="User", default_branch="main", **extra):
    owner = full_name.split("/")[0]
    return {"full_name": full_name, "owner": {"login": owner, "type": owner_type}, "private": True,
            "default_branch": default_branch, "html_url": f"https://github.com/{full_name}", **extra}


def _pull(full_name="BestMiraPro/x", head_ref="odysseus/feat", head_repo=None, number=7):
    head_repo = head_repo or full_name
    return {"number": number, "title": "Add a thing", "state": "open", "merged": False, "draft": False,
            "mergeable": True, "html_url": f"https://github.com/{full_name}/pull/{number}",
            "head": {"ref": head_ref, "sha": SHA, "repo": {"full_name": head_repo}},
            "base": {"ref": "main", "sha": "f" * 40, "repo": {"full_name": full_name}}}


REPOS = {
    "BestMiraPro/x": _repo("BestMiraPro/x"),
    "augustalabs/x": _repo("augustalabs/x", "Organization"),
    "someone/x": _repo("someone/x"),
}


def _secrets():
    return importlib.import_module("src.secret_storage")


def _refuse_network(request):
    raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ga, "CONFIG_FILE", str(tmp_path / "github_access.json"))
    secrets = _secrets()
    monkeypatch.setattr(secrets, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secrets, "_fernet", None)
    # Nothing reaches the network unless a test installs a fake GitHub.
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(_refuse_network), **kw))


def _github(monkeypatch, *, logins=None, repos=None, pulls=None, user_status=200):
    """A fake api.github.com; returns the list of requests it received.

    ``logins`` maps a token to the login ``GET /user`` reports for it.
    """
    logins = {TOKEN: "BestMiraPro", OTHER_TOKEN: "OtherAccount"} if logins is None else logins
    repos = REPOS if repos is None else repos
    pulls = {("BestMiraPro/x", 7): _pull()} if pulls is None else pulls
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = request.url.path
        parts = path.strip("/").split("/")
        if path == "/user":
            if user_status != 200 or token not in logins:
                return httpx.Response(user_status if user_status != 200 else 401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"login": logins[token], "type": "User", "id": 1})
        if parts[0] != "repos" or len(parts) < 3:
            return httpx.Response(404, json={"message": "Not Found"})
        full = f"{parts[1]}/{parts[2]}"
        if full not in repos:
            return httpx.Response(404, json={"message": "Not Found"})
        rest = parts[3:]
        if not rest and request.method == "GET":
            return httpx.Response(200, json=repos[full])
        if rest == ["pulls"] and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(201, json={**_pull(full, body["head"]), "number": 12, "title": body["title"]})
        if len(rest) == 2 and rest[0] == "pulls" and request.method == "GET":
            pr = pulls.get((full, int(rest[1])))
            return httpx.Response(200, json=pr) if pr else httpx.Response(404, json={"message": "Not Found"})
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "merge" and request.method == "PUT":
            body = json.loads(request.content)
            return httpx.Response(200, json={"merged": True, "sha": "9" * 40, "message": "merged", "_sent": body})
        if len(rest) == 2 and rest[0] == "issues":
            return httpx.Response(200, json={"number": int(rest[1]), "title": "Bug", "state": "open", "body": "It broke",
                                             "labels": [{"name": "bug"}], "html_url": "https://github.com/i",
                                             "user": {"login": "someone"}})
        if len(rest) >= 3 and rest[0] == "commits" and rest[-1] == "status":
            return httpx.Response(200, json={"sha": SHA, "state": "pending", "statuses": [
                {"context": "ci/lint", "state": "success", "description": "ok", "target_url": "https://ci"}]})
        if len(rest) >= 3 and rest[0] == "commits" and rest[-1] == "check-runs":
            return httpx.Response(200, json={"total_count": 1, "check_runs": [
                {"name": "tests", "status": "completed", "conclusion": "failure", "html_url": "https://run"}]})
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


def _connect(monkeypatch, owner="alice", token=TOKEN, **kw):
    seen = _github(monkeypatch, **kw)
    ga.save_token(owner, token)
    seen.clear()
    return seen


def _writes(seen):
    return [r for r in seen if r.method != "GET"]


# ---------------------------------------------------------------------------
# tokens and the stored connection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token,reason", [
    ("ghp_" + "a" * 36, "classic"),
    ("gho_" + "a" * 36, "OAuth"),
    ("ghu_" + "a" * 36, "fine-grained"),
    ("hunter2", "fine-grained"),
    ("", "fine-grained"),
    ("github_pat_", "fine-grained"),
    ("github_pat_abc def" + "x" * 30, "fine-grained"),
])
def test_only_fine_grained_tokens_are_accepted(monkeypatch, token, reason):
    seen = _github(monkeypatch)
    with pytest.raises(ga.GitHubError, match=reason) as exc:
        ga.save_token("alice", token)
    assert exc.value.status == 400
    assert seen == []                                   # refused before GitHub is asked
    assert ga.connection("alice") == {"connected": False, "login": None}


def test_stored_token_is_encrypted_and_never_returned(monkeypatch):
    seen = _github(monkeypatch)
    result = ga.save_token("alice", f"  {TOKEN}\n")
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/user")]
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    raw = open(ga.CONFIG_FILE, encoding="utf-8").read()
    assert TOKEN not in raw and BASIC not in raw
    stored = json.loads(raw)["alice"]
    assert stored["token"].startswith("enc:") and stored["login"] == "BestMiraPro"
    assert _secrets().decrypt(stored["token"]) == TOKEN
    assert result == ga.connection("alice") == {"connected": True, "login": "BestMiraPro"}
    assert len(seen) == 1                               # connection() reads the cached login


def test_a_token_github_refuses_is_not_stored(monkeypatch):
    _github(monkeypatch, logins={})
    with pytest.raises(ga.GitHubError, match="refused") as exc:
        ga.save_token("alice", TOKEN)
    assert exc.value.status == 400                      # never 401: the UI treats that as a logout
    assert ga.connection("alice")["connected"] is False
    assert not os.path.exists(ga.CONFIG_FILE)


def test_connections_are_per_user_and_forgetting_removes_only_that_record(monkeypatch):
    _github(monkeypatch)
    ga.save_token("alice", TOKEN)
    ga.save_token("bob", OTHER_TOKEN)
    assert ga.connection("bob") == {"connected": True, "login": "OtherAccount"}
    assert ga.connection("carol") == {"connected": False, "login": None}
    assert ga.forget_token("alice") is True
    assert ga.forget_token("alice") is False
    assert ga.connection("alice")["connected"] is False
    assert ga.connection("bob")["login"] == "OtherAccount"
    assert set(json.loads(open(ga.CONFIG_FILE, encoding="utf-8").read())) == {"bob"}


# ---------------------------------------------------------------------------
# the write allowlist
# ---------------------------------------------------------------------------

class _Git:
    """A fake ``run_git`` that records every call and answers like a working copy."""

    def __init__(self, push_result=(0, "", "")):
        self.calls = []
        self.push_result = push_result

    def __call__(self, argv, cwd, env):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        if "rev-parse" in argv and "--verify" in argv:
            return 0, SHA + "\n", ""
        if "rev-parse" in argv and "--git-path" in argv:
            return 0, "/work/.git/objects\n", ""
        if "push" in argv:
            return self.push_result
        return 0, "", ""

    def call(self, verb):
        (found,) = [c for c in self.calls if verb in c["argv"]]
        return found


def test_own_repository_passes_the_allowlist_case_insensitively(monkeypatch):
    seen = _connect(monkeypatch, logins={TOKEN: "bestmirapro"})
    info = ga.check_writable("alice", "https://github.com/BestMiraPro/x")
    assert info["default_branch"] == "main"
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/repos/BestMiraPro/x")]
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("repo,reason,status", [
    ("augustalabs/x", "organisation augustalabs", 403),
    ("someone/x", "another GitHub user", 403),
    ("BestMiraPro/missing", "not found", 404),
])
def test_allowlist_gives_a_distinct_reason_for_each_refusal(monkeypatch, repo, reason, status):
    _connect(monkeypatch)
    with pytest.raises(ga.GitHubError, match=reason) as exc:
        ga.check_writable("alice", repo)
    assert exc.value.status == status


def test_allowlist_refuses_accounts_that_are_not_personal(monkeypatch):
    _connect(monkeypatch, repos={"BestMiraPro/x": _repo("BestMiraPro/x", "Bot")})
    with pytest.raises(ga.GitHubError, match="personal"):
        ga.check_writable("alice", "BestMiraPro/x")


def test_allowlist_asks_github_every_time_and_follows_a_new_token(monkeypatch):
    seen = _connect(monkeypatch)
    ga.check_writable("alice", "BestMiraPro/x")
    ga.check_writable("alice", "BestMiraPro/x")
    assert len(seen) == 2                               # no positive answer is cached
    ga.save_token("alice", OTHER_TOKEN)                 # now a different GitHub account
    with pytest.raises(ga.GitHubError, match="another GitHub user"):
        ga.check_writable("alice", "BestMiraPro/x")


def test_writes_need_a_connected_token(monkeypatch):
    seen = _github(monkeypatch)
    with pytest.raises(ga.GitHubError, match="Connect a GitHub token"):
        ga.check_writable("alice", "BestMiraPro/x")
    assert seen == []


def _push(repo, git):
    return ga.push_branch("alice", repo, "/work", "odysseus/feat", run_git=git)


def _open(repo, git):
    return ga.open_pull_request("alice", repo, "odysseus/feat", "main", "Add a thing", "Body")


def _merge(repo, git):
    return ga.merge_pull_request("alice", repo, 7, SHA)


@pytest.mark.parametrize("write", [_push, _open, _merge], ids=["push", "open", "merge"])
@pytest.mark.parametrize("repo,reason", [
    ("augustalabs/x", "organisation"),
    ("someone/x", "another GitHub user"),
    ("BestMiraPro/missing", "not found"),
])
def test_writes_outside_personal_repositories_are_refused_before_any_write(monkeypatch, write, repo, reason):
    pulls = {(r, 7): _pull(r) for r in REPOS}
    seen = _connect(monkeypatch, pulls=pulls)
    git = _Git()
    with pytest.raises(ga.GitHubError, match=reason):
        write(repo, git)
    assert [(r.method, r.url.path) for r in seen] == [("GET", f"/repos/{repo}")]
    assert _writes(seen) == [] and git.calls == []


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def _config_pairs(env):
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}


@pytest.mark.parametrize("branch,reason", [
    ("feature/x", "odysseus/"),
    ("main", "odysseus/"),
    ("+odysseus/feat", r"\+"),
    ("odysseus/a+b", r"\+"),
    ("odysseus/feat:main", "not a valid branch"),
    ("odysseus/../main", "not a valid branch"),
    ("odysseus/", "not a valid branch"),
])
def test_push_refuses_branches_outside_the_odysseus_rules(monkeypatch, branch, reason):
    seen = _connect(monkeypatch)
    git = _Git()
    with pytest.raises(ga.GitHubError, match=reason):
        ga.push_branch("alice", "BestMiraPro/x", "/work", branch, run_git=git)
    assert seen == [] and git.calls == []


def test_push_refuses_the_default_branch(monkeypatch):
    seen = _connect(monkeypatch, repos={"BestMiraPro/x": _repo("BestMiraPro/x", default_branch="odysseus/main")})
    git = _Git()
    with pytest.raises(ga.GitHubError, match="default branch"):
        ga.push_branch("alice", "BestMiraPro/x", "/work", "odysseus/main", run_git=git)
    assert _writes(seen) == [] and git.calls == []


def test_push_uses_the_modules_url_and_keeps_the_token_out_of_argv(monkeypatch):
    seen = _connect(monkeypatch)
    git = _Git()
    result = ga.push_branch("alice", "BestMiraPro/x", "/work", "odysseus/feat", run_git=git)
    assert result == {"repo": "BestMiraPro/x", "branch": "odysseus/feat", "sha": SHA}
    assert _writes(seen) == []

    push = git.call("push")
    argv = push["argv"]
    assert argv[0] == "git" and argv[argv.index("push") + 1:] == [
        "--no-verify", "https://github.com/BestMiraPro/x.git", f"{SHA}:refs/heads/odysseus/feat"]
    assert not any("force" in a or a == "-f" or a.startswith("+") for a in argv)
    for call in git.calls:
        assert TOKEN not in " ".join(call["argv"]) and BASIC not in " ".join(call["argv"])
        if call is not push:
            assert TOKEN not in json.dumps(call["env"]) and BASIC not in json.dumps(call["env"])

    env = push["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ASKPASS"] == ""
    pairs = _config_pairs(env)
    assert pairs["http.https://github.com/.extraheader"] == f"Authorization: Basic {BASIC}"
    assert pairs["core.hooksPath"] == os.devnull
    assert TOKEN not in json.dumps(env)                 # only its Basic-auth encoding, only in the env

    # The push runs from a fresh git folder that borrows the working copy's
    # objects, so nothing in the working copy's own config takes part.
    git_dir = argv[argv.index("--git-dir") + 1]
    assert push["cwd"] == git_dir and not git_dir.startswith("/work")
    assert env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == "/work/.git/objects"
    in_workdir = [c["argv"] for c in git.calls if c["cwd"] == "/work"]
    assert in_workdir and all(a[1] == "rev-parse" for a in in_workdir)


def test_push_scrubs_the_token_from_git_errors(monkeypatch):
    _connect(monkeypatch)
    git = _Git(push_result=(128, f"remote: {TOKEN}\n",
                            f"fatal: Authorization: Basic {BASIC} for https://x-access-token:{TOKEN}@github.com/\n"))
    with pytest.raises(ga.GitHubError) as exc:
        ga.push_branch("alice", "BestMiraPro/x", "/work", "odysseus/feat", run_git=git)
    message = str(exc.value)
    assert TOKEN not in message and BASIC not in message
    assert "fatal: Authorization" in message and exc.value.status == 502


def test_push_reports_a_branch_missing_from_the_working_copy(monkeypatch):
    _connect(monkeypatch)
    git = _Git()
    git_call = git.__call__

    def missing(argv, cwd, env):
        if "--verify" in argv:
            git.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
            return 128, "", "fatal: Needed a single revision\n"
        return git_call(argv, cwd, env)

    with pytest.raises(ga.GitHubError, match="no branch odysseus/feat"):
        ga.push_branch("alice", "BestMiraPro/x", "/work", "odysseus/feat", run_git=missing)
    assert not [c for c in git.calls if "push" in c["argv"]]


_needs_git = pytest.mark.skipif(shutil.which("git") is None or os.name == "nt", reason="needs git on POSIX")


def _sh_git(*args, cwd):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout.strip()


@_needs_git
def test_real_git_push_ignores_the_working_copys_config_and_hooks(monkeypatch, tmp_path):
    """Local file:// remotes only: a hostile working copy cannot redirect the push."""
    _connect(monkeypatch)
    remotes = tmp_path / "remotes"
    good, evil, work = remotes / "BestMiraPro" / "x.git", tmp_path / "evil.git", tmp_path / "work"
    good.parent.mkdir(parents=True)
    for bare in (good, evil):
        assert _sh_git("init", "-q", "--bare", str(bare), cwd=tmp_path)[0] == 0
    assert _sh_git("init", "-q", str(work), cwd=tmp_path)[0] == 0
    _sh_git("commit", "-q", "--allow-empty", "-m", "one", cwd=work)
    _sh_git("checkout", "-q", "-b", "odysseus/feat", cwd=work)
    _sh_git("commit", "-q", "--allow-empty", "-m", "two", cwd=work)
    sha = _sh_git("rev-parse", "HEAD", cwd=work)[1]

    good_url = f"file://{good}"
    marker = tmp_path / "hook-ran"
    hooks = work / "evil-hooks"
    hooks.mkdir()
    for folder in (hooks, work / ".git" / "hooks"):
        (folder / "pre-push").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        (folder / "pre-push").chmod(0o755)
    _sh_git("config", f"url.file://{evil}.pushInsteadOf", good_url, cwd=work)
    _sh_git("config", "core.hooksPath", str(hooks), cwd=work)
    monkeypatch.setattr(ga, "GIT_BASE", f"file://{remotes}")

    result = ga.push_branch("alice", "BestMiraPro/x", str(work), "odysseus/feat")
    assert result["sha"] == sha
    assert _sh_git("--git-dir", str(good), "rev-parse", "refs/heads/odysseus/feat", cwd=tmp_path) == (0, sha)
    assert _sh_git("--git-dir", str(evil), "rev-parse", "--verify", "-q", "refs/heads/odysseus/feat",
                   cwd=tmp_path)[0] != 0
    assert not marker.exists()

    # Control: the same push run from the working copy itself is redirected and runs the hook.
    _sh_git("push", "-q", good_url, "refs/heads/odysseus/feat:refs/heads/odysseus/other", cwd=work)
    assert _sh_git("--git-dir", str(evil), "rev-parse", "refs/heads/odysseus/other", cwd=tmp_path) == (0, sha)
    assert marker.exists()


@_needs_git
def test_real_git_reads_the_token_header_only_for_github(monkeypatch, tmp_path):
    _connect(monkeypatch)
    git = _Git()
    ga.push_branch("alice", "BestMiraPro/x", "/work", "odysseus/feat", run_git=git)
    env = git.call("push")["env"]

    def get(*args):
        done = subprocess.run(["git", "config", *args], cwd=tmp_path, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout.strip()

    assert get("--get-urlmatch", "http.extraheader", "https://github.com/BestMiraPro/x.git") == (
        0, f"Authorization: Basic {BASIC}")
    assert get("--get-urlmatch", "http.extraheader", "https://evil.example/x.git") == (1, "")
    assert get("--get", "core.hooksPath") == (0, os.devnull)


# ---------------------------------------------------------------------------
# pull requests
# ---------------------------------------------------------------------------

def test_open_pull_request_posts_within_the_own_repository(monkeypatch):
    seen = _connect(monkeypatch)
    pr = ga.open_pull_request("alice", "BestMiraPro/x", "odysseus/feat", "main", "Add a thing", "Body")
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/repos/BestMiraPro/x"),
                                                     ("POST", "/repos/BestMiraPro/x/pulls")]
    assert json.loads(seen[1].content) == {"title": "Add a thing", "head": "odysseus/feat", "base": "main",
                                           "body": "Body"}
    assert pr["number"] == 12 and pr["head"]["ref"] == "odysseus/feat" and pr["base"]["ref"] == "main"


@pytest.mark.parametrize("head,base,reason", [
    ("someone:odysseus/feat", "main", "same repository"),
    ("BestMiraPro:odysseus/feat", "main", "same repository"),
    ("feature", "main", "odysseus/"),
    ("odysseus/feat", "someone:main", "same repository"),
    ("odysseus/feat", "odysseus/feat", "different"),
])
def test_open_pull_request_refuses_cross_repository_and_unprefixed_heads(monkeypatch, head, base, reason):
    seen = _connect(monkeypatch)
    with pytest.raises(ga.GitHubError, match=reason):
        ga.open_pull_request("alice", "BestMiraPro/x", head, base, "Title", "")
    assert _writes(seen) == []


def test_open_pull_request_needs_a_title(monkeypatch):
    seen = _connect(monkeypatch)
    with pytest.raises(ga.GitHubError, match="title"):
        ga.open_pull_request("alice", "BestMiraPro/x", "odysseus/feat", "main", "  ", "")
    assert _writes(seen) == []


@pytest.mark.parametrize("sha", ["", None, "abc123", "g" * 40])
def test_merge_requires_the_exact_head_sha(monkeypatch, sha):
    seen = _connect(monkeypatch)
    with pytest.raises(ga.GitHubError, match="commit"):
        ga.merge_pull_request("alice", "BestMiraPro/x", 7, sha)
    assert seen == []


def test_merge_sends_the_sha_and_nothing_else(monkeypatch):
    seen = _connect(monkeypatch)
    result = ga.merge_pull_request("alice", "BestMiraPro/x", 7, SHA.upper())
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/repos/BestMiraPro/x"),
                                                     ("GET", "/repos/BestMiraPro/x/pulls/7"),
                                                     ("PUT", "/repos/BestMiraPro/x/pulls/7/merge")]
    assert json.loads(seen[2].content) == {"sha": SHA}
    assert result == {"merged": True, "sha": "9" * 40, "message": "merged"}


def test_merge_uses_a_method_the_repository_allows(monkeypatch):
    repo = _repo("BestMiraPro/x", allow_merge_commit=False, allow_squash_merge=True, allow_rebase_merge=True)
    seen = _connect(monkeypatch, repos={"BestMiraPro/x": repo})
    ga.merge_pull_request("alice", "BestMiraPro/x", 7, SHA)
    assert json.loads(seen[-1].content) == {"sha": SHA, "merge_method": "squash"}


@pytest.mark.parametrize("pull,reason", [
    (_pull(head_ref="feature"), "odysseus/"),
    (_pull(head_repo="someone/x"), "same repository"),
    ({**_pull(), "head": {"ref": "odysseus/feat", "sha": SHA, "repo": None}}, "same repository"),
])
def test_merge_refuses_unprefixed_or_cross_repository_pull_requests(monkeypatch, pull, reason):
    seen = _connect(monkeypatch, pulls={("BestMiraPro/x", 7): pull})
    with pytest.raises(ga.GitHubError, match=reason):
        ga.merge_pull_request("alice", "BestMiraPro/x", 7, SHA)
    assert _writes(seen) == []


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def test_reads_return_the_few_fields_a_caller_needs(monkeypatch):
    seen = _connect(monkeypatch, pulls={("augustalabs/x", 3): _pull("augustalabs/x", number=3)})
    # Reads have no allowlist beyond what the token can see: an organisation repository is fine.
    assert ga.repository("alice", "augustalabs/x") == {
        "full_name": "augustalabs/x", "owner": "augustalabs", "owner_type": "Organization", "private": True,
        "default_branch": "main", "html_url": "https://github.com/augustalabs/x"}
    pr = ga.pull_request("alice", "augustalabs/x", 3)
    assert pr["head"] == {"ref": "odysseus/feat", "sha": SHA, "repo": "augustalabs/x"} and pr["state"] == "open"
    assert ga.issue("alice", "augustalabs/x", 5) == {
        "number": 5, "title": "Bug", "state": "open", "body": "It broke", "labels": ["bug"],
        "author": "someone", "html_url": "https://github.com/i", "is_pull_request": False}
    assert ga.ci_status("alice", "augustalabs/x", "odysseus/feat") == {
        "ref": "odysseus/feat", "sha": SHA, "state": "pending",
        "statuses": [{"context": "ci/lint", "state": "success", "description": "ok", "url": "https://ci"}],
        "check_runs": [{"name": "tests", "status": "completed", "conclusion": "failure", "url": "https://run"}]}
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/repos/augustalabs/x"), ("GET", "/repos/augustalabs/x/pulls/3"),
        ("GET", "/repos/augustalabs/x/issues/5"), ("GET", "/repos/augustalabs/x/commits/odysseus/feat/status"),
        ("GET", "/repos/augustalabs/x/commits/odysseus/feat/check-runs")]
    assert {r.url.host for r in seen} == {"api.github.com"}
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)


@pytest.mark.parametrize("call", [
    lambda: ga.repository("alice", "https://evil.example/a/b"),
    lambda: ga.repository("alice", "a/b/../../user"),
    lambda: ga.ci_status("alice", "BestMiraPro/x", "../../../user"),
    lambda: ga.ci_status("alice", "BestMiraPro/x", "main?x=1"),
    lambda: ga.issue("alice", "BestMiraPro/x", "5/../../x"),
])
def test_reads_only_build_api_github_com_paths_from_valid_input(monkeypatch, call):
    seen = _connect(monkeypatch)
    with pytest.raises(ga.GitHubError):
        call()
    assert seen == []


def test_with_seat_isolation_the_working_copy_is_asked_as_the_seat_user(monkeypatch):
    # The run's working copy is writable by council seats, so its git config
    # is theirs: Odysseus does not run git inside it as itself.
    from src import seat_user

    _connect(monkeypatch)
    monkeypatch.setattr(seat_user, "available", lambda: True)
    monkeypatch.setenv("SOME_ODYSSEUS_SECRET", "do-not-pass")
    git = _Git()
    ga.push_branch("alice", "BestMiraPro/x", "/work", "odysseus/feat", run_git=git)
    queries = [c for c in git.calls if "rev-parse" in c["argv"]]
    assert len(queries) == 2
    for call in queries:
        assert call["argv"][:4] == [seat_user.LAUNCHER, "git", "-c", "safe.directory=*"] and call["cwd"] == "/work"
        assert "SOME_ODYSSEUS_SECRET" not in call["env"] and "HOME" not in call["env"]
    others = [c for c in git.calls if "rev-parse" not in c["argv"]]
    # The push itself stays Odysseus's, in its own clean folder.
    assert others and all(c["argv"][0] == "git" for c in others)
