"""AI Council work runs: the seats work on a goal in a copy of a repository, round after round.

A run is one asyncio task in this process (``src.council_work.run_work``), like
an ask in routes/council_routes.py. Each round is saved as an ordinary council
turn in the session, with the round's work data in the turn's ``work`` column,
so the Council page shows it like any other turn. The session stays busy for
the whole run, and ``_RUNNING`` always holds the current round's turn, so the
Stop button on that round stops the whole run; finished rounds stay saved.

Each seat gets a full shell that is not sandboxed, so a work run only starts
when Odysseus runs in its Docker container (the container keeps the shell off
the host), every seat process runs as the separate seat user (src/seat_user.py:
it cannot read Odysseus's sessions, GitHub token, keys or database, nor its
environment) and login is required for every caller (so the shell cannot call
Odysseus, approvals included, without credentials).

GitHub: when the owner's folder has a github.com ``origin`` and the owner has
connected a token, Odysseus pushes the run's branch after each round that
moves it. Agents ask for a pull request or a merge with a message to
odysseus (src/council_requests.py); the request is saved with its round's
work data, so it stays answerable after the run and after a restart, and
nothing reaches GitHub until the owner approves it here, one request at a
time. Every write goes through src/github_access.py and its allowlist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.database import CouncilTurn, SessionLocal, utcnow_naive
from routes import council_routes as cr
from src import (budget, council, council_requests, council_tools, council_work, council_workspace, github_access,
                 reasoning_effort, seat_user)
from src.auth_helpers import get_current_user
from src.council_workspace import Notice

logger = logging.getLogger(__name__)

RUNS_DIRNAME = "council-runs"
# The run failed (a git error, a seat that can no longer be resolved): not one
# of council_work's stop rules.
STOP_ERROR = "error"

# Running work-run tasks. The event loop only keeps weak references to tasks,
# and _RUNNING holds a run only once its first round has a turn.
_TASKS: set = set()
# Runs still going, by run id, so the owner's decisions reach their seats.
_LIVE: Dict[str, "_WorkRun"] = {}
# One writer at a time for a turn's work data: the run saving its rounds and
# the owner deciding on its requests both read, change and write it back.
_WORK_LOCK = threading.Lock()


PUSH_HELD = ("Odysseus does not push this run's branch to GitHub after each round: the branch holds GitHub Actions "
             "workflows (.github/workflows), and a push could run them on work the owner has not seen. The branch "
             "is pushed when the owner approves a request to open a pull request.")


def runs_root() -> Path:
    """Where run folders live; they are kept when a run ends.

    With seat isolation that is /council-runs, outside /app where the seat user
    cannot go (a volume in docker-compose.yml); otherwise council-runs/ under
    the data folder.
    """
    from src.constants import DATA_DIR

    return Path(seat_user.RUNS_ROOT) if seat_user.available() else Path(DATA_DIR) / RUNS_DIRNAME


def in_docker() -> bool:
    """Whether Odysseus runs inside its container.

    The image sets no variable of its own to test (the Dockerfile's only ENV
    is UV_TOOL_DIR / UV_TOOL_BIN_DIR), so this reads /.dockerenv, which Docker
    creates in every container, with the cgroup fallback the rest of Odysseus
    uses (src/host_docker_access.py).
    """
    from src.host_docker_access import running_in_container

    return running_in_container()


def seats_not_isolated() -> Optional[str]:
    """Why the seats' processes cannot run as the separate seat user here, or None when they can."""
    return seat_user.unavailable()


def seats_unfiltered() -> Optional[str]:
    """Why the seats' network is not filtered here, or None when it is."""
    return seat_user.unfiltered()


def unauthenticated_access() -> Optional[str]:
    """Why a caller without credentials could get into Odysseus, or None when nobody can."""
    from src.auth_helpers import _auth_disabled

    if _auth_disabled():
        return "login is off (AUTH_ENABLED=false)"
    # Parsed as app.py and require_user parse it.
    if os.getenv("LOCALHOST_BYPASS", "false").lower() == "true":
        return "LOCALHOST_BYPASS is on, so callers on this machine skip login"
    return None


# Model calls an API/local seat may make in one stage of a work run. A real
# run showed a question's 12 spent on reading, editing and testing with none
# left to write the report, which made the seat's answer empty.
WORK_AGENT_ROUNDS = 40


class WorkRequest(BaseModel):
    goal: str = Field(..., max_length=council.MAX_QUESTION_CHARS)
    members: List[cr.Seat] = Field(..., max_length=council.MAX_MEMBERS)
    chairman: cr.Seat
    rounds: int = Field(5, ge=1, le=council_work.MAX_ROUNDS)
    # The owner's repository (its top level). It is cloned, never written to.
    workspace: str = Field(..., max_length=1024)
    memory: bool = False
    budget_confirmed: bool = False
    # What the seats do whenever they are waiting for the owner's input.
    while_waiting: Literal["stop", "continue", "vote"] = council_work.WAIT_STOP


def _refuse_unsafe(request: Request) -> None:
    """Refuse a run where a seat's unsandboxed shell would reach too far (see the module docstring)."""
    from src.tool_security import owner_is_admin_or_single_user

    if not owner_is_admin_or_single_user(get_current_user(request)):
        raise HTTPException(403, "Work runs (full tools on a copy of a repository) need an admin account.")
    if not in_docker():
        raise HTTPException(403, "Work runs only start when Odysseus runs in its Docker container: every seat "
                                 "gets a shell that is not sandboxed, and the container keeps it off this machine.")
    why = seats_not_isolated()
    if why:
        raise HTTPException(403, f"Work runs only start when every seat runs as the separate seat user, which "
                                 f"cannot read Odysseus's sessions, GitHub token or database, and here {why}.")
    why = seats_unfiltered()
    if why:
        raise HTTPException(403, f"Work runs only start when the seats' network is filtered, so a seat's shell "
                                 f"reaches the internet but not the services next to Odysseus, and here {why}.")
    why = unauthenticated_access()
    if why:
        raise HTTPException(403, f"Work runs need login on for every caller, and {why}: a seat's shell could "
                                 "then call Odysseus, approvals included, without credentials.")


def _owner_in_person(request: Request) -> str:
    """The owner, signed in to the Odysseus page. Requests gate what agents may do
    on GitHub and agents can hold API tokens, so a token never lists or decides them."""
    if getattr(request.state, "api_token", False):
        raise HTTPException(403, "Only the owner, signed in to Odysseus, can see and decide agents' requests; "
                                 "API tokens cannot.")
    return cr._owner(request)


def _source_repo(raw: str) -> str:
    """The vetted top level of the owner's git repository."""
    from src.tool_execution import vet_workspace

    path = vet_workspace(raw)
    if path is None:
        raise HTTPException(400, "That workspace folder can't be used: it must be an existing, "
                                 "non-sensitive folder that is not a filesystem root.")
    top = council_workspace.git(["rev-parse", "--show-toplevel"], path, check=False)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(path).resolve():
        raise HTTPException(400, f"A work run needs the top level of a git repository, and {path} is not one.")
    return path


def _origin_repo(source: str) -> Optional[str]:
    """``owner/name`` of the owner's folder's ``origin`` when it is on github.com, else None (only read)."""
    proc = council_workspace.git(["remote", "get-url", "origin"], source, check=False)
    return council_requests.github_repo(proc.stdout.strip()) if proc.returncode == 0 else None


# ── work data and the requests in it ──

def _update_work(turn_id: str, change: Callable[[Dict[str, Any]], Any]) -> Any:
    """Apply ``change`` to a turn's saved work data and save it; returns what ``change`` returned."""
    with _WORK_LOCK:
        db = SessionLocal()
        try:
            turn = db.query(CouncilTurn).filter(CouncilTurn.id == turn_id).first()
            if turn is None:
                return None
            work = cr._loads(turn.work, {})
            result = change(work)
            turn.work = json.dumps(work)
            db.commit()
            return result
        finally:
            db.close()


def _save_work(turn_id: str, work: Dict[str, Any]) -> None:
    """Save the run's own view of a round's work data; its requests stay as saved (the owner decides those)."""
    def change(saved: Dict[str, Any]) -> None:
        requests = saved.get("requests") or []
        saved.clear()
        saved.update(work, requests=requests)

    _update_work(turn_id, change)


def _add_request(turn_id: str, request: Dict[str, Any]) -> None:
    _update_work(turn_id, lambda work: work.setdefault("requests", []).append(request))


def _open_requests(turn_ids: List[str]) -> bool:
    """Whether any of these turns holds a request still waiting for the owner (or being carried out)."""
    db = SessionLocal()
    try:
        rows = db.query(CouncilTurn.work).filter(CouncilTurn.id.in_(turn_ids)).all()
    finally:
        db.close()
    return any(council_requests.effective(r).get("status") in (council_requests.PENDING, council_requests.APPROVING)
               for (raw,) in rows for r in cr._loads(raw, {}).get("requests") or [])


def _work_turns(db, owner: Optional[str]) -> List[CouncilTurn]:
    return (db.query(CouncilTurn).filter(CouncilTurn.owner == owner, CouncilTurn.work.isnot(None))
            .order_by(CouncilTurn.created_at.asc()).all())


def _list_requests(owner: Optional[str], status: Optional[str]) -> List[Dict[str, Any]]:
    """``owner``'s requests across all their work runs, oldest first, with where each was made."""
    db = SessionLocal()
    try:
        found = []
        for turn in _work_turns(db, owner):
            work = cr._loads(turn.work, {})
            for request in work.get("requests") or []:
                request = council_requests.effective(request)
                if status in (None, request.get("status")):
                    found.append({**request, "turn_id": turn.id, "session_id": turn.session_id,
                                  "run_id": work.get("run_id"), "goal": work.get("goal")})
        return found
    finally:
        db.close()


def _claim(owner: Optional[str], request_id: str, status: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Move ``owner``'s pending request to ``status`` (approving or denied); (turn id, request, work data).

    404 for a request that is not ``owner``'s, 409 for one already decided:
    a decision is final, and claiming it under the lock means two clicks
    cannot both carry it out.
    """
    with _WORK_LOCK:
        db = SessionLocal()
        try:
            for turn in _work_turns(db, owner):
                work = cr._loads(turn.work, {})
                for request in work.get("requests") or []:
                    if request.get("id") != request_id:
                        continue
                    current = council_requests.effective(request).get("status")
                    if current != council_requests.PENDING:
                        raise HTTPException(409, f"This request is already {current}: a decision is final.")
                    if status == council_requests.APPROVING:
                        request["status"] = status
                        council_requests.EXECUTING.add(request_id)
                    else:
                        council_requests.decided(request, status=status)
                    turn.work = json.dumps(work)
                    db.commit()
                    return turn.id, dict(request), work
            raise HTTPException(404, "Request not found")
        finally:
            db.close()


def _settle(turn_id: str, request_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Save how carrying out an approved request ended; the request as saved."""
    def change(work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for request in work.get("requests") or []:
            if request.get("id") == request_id:
                return dict(council_requests.decided(request, **fields))
        return None

    return _update_work(turn_id, change)


@contextmanager
def _reader(run_id: Optional[str], main: str) -> Iterator[Optional[seat_user.Seat]]:
    """The seat user to read a run's main copy as: git there obeys config the seats wrote.

    The run's own user while it is going, a visitor to its closed folder
    afterwards, None without seat isolation.
    """
    live = _LIVE.get(run_id or "")
    if live is not None and live.ws.users:
        yield live.ws.user
    elif seat_user.available() and Path(seat_user.RUNS_ROOT) in Path(main).parents:
        with seat_user.visitor(str(Path(main).parent)) as seat:
            yield seat
    else:
        yield None


def _branch_head(main: str, branch: str, seat: Optional[seat_user.Seat]) -> Optional[str]:
    """The commit of ``branch`` in the run's main copy, or None when the copy or the branch is gone."""
    proc = council_workspace.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"], main,
                                 check=False, seat=seat)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _moved(branch: str, asked: str, now: Optional[str], where: str) -> str:
    return (f"The run's branch {branch} has moved since the request was made: it was at {asked[:10]}, and {where} "
            f"it is at {str(now)[:10]}. The owner's approval covers only the commit the agents asked about, so "
            "nothing was done; the agents must ask again.")


def _carry_out(owner: Optional[str], request: Dict[str, Any], work: Dict[str, Any]) -> Dict[str, Any]:
    """Do on GitHub what the owner approved; the fields to save on the request, or GitHubError.

    Everything is checked again now, through github_access: the token, the
    allowlist, and that the run's branch is still at the recorded commit.
    """
    repo, branch, sha = request["repo"], request["branch"], request["head_sha"]
    info = github_access.check_writable(owner, repo)
    if request["action"] == council_requests.OPEN_PR:
        main = work.get("main") or ""
        gone = github_access.GitHubError(f"The run's copy {main} or its branch {branch} is gone, so Odysseus "
                                         "cannot push it.", 409)
        if not main or not Path(main).is_dir():
            raise gone
        with _reader(work.get("run_id"), main) as seat:
            local = _branch_head(main, branch, seat)
            if local is None:
                raise gone
            if local != sha:
                raise github_access.GitHubError(_moved(branch, sha, local, "in the run's copy"), 409)
            # GitHub must hold exactly the requested commit on the run's branch;
            # a no-op when the automatic push already put it there.
            pushed = github_access.push_branch(owner, repo, main, branch, seat=seat)
        if pushed["sha"] != sha:
            raise github_access.GitHubError(_moved(branch, sha, pushed["sha"], "in the run's copy"), 409)
        base = request.get("base") or info.get("default_branch") or ""
        pr = github_access.open_pull_request(owner, repo, branch, base, request["title"], request.get("body") or "")
        return {"pr_number": pr.get("number"), "pr_url": pr.get("html_url"), "base": base}

    number = request["number"]
    pr = github_access.pull_request(owner, repo, number)
    head, base_repo = pr["head"], str(pr["base"].get("repo") or "")
    if (head.get("ref") != branch or str(head.get("repo") or "").lower() != repo.lower()
            or base_repo.lower() != repo.lower()):
        raise github_access.GitHubError(
            f"Pull request #{number} is not from this run's branch {branch} in {repo} (its head is "
            f"{head.get('repo')}:{head.get('ref')}), so Odysseus does not merge it.", 409)
    if head.get("sha") != sha:
        raise github_access.GitHubError(_moved(branch, sha, head.get("sha"), f"on GitHub (pull request #{number})"),
                                        409)
    result = github_access.merge_pull_request(owner, repo, number, sha)
    if not result.get("merged"):
        raise github_access.GitHubError(f"GitHub did not merge pull request #{number}: "
                                        f"{result.get('message') or 'no reason given'}", 409)
    return {"merge_sha": result.get("sha"), "pr_url": pr.get("html_url")}


def _tell_run(run_id: Optional[str], request: Dict[str, Any]) -> None:
    """If the request's run is still going, its seats hear the owner's decision after the current stage."""
    run = _LIVE.get(run_id or "")
    if run is not None:
        run.outcomes.append(Notice("all", council_requests.outcome(request)))


async def _approve(owner: Optional[str], request_id: str) -> Dict[str, Any]:
    turn_id, request, work = await asyncio.to_thread(_claim, owner, request_id, council_requests.APPROVING)
    try:
        failure = None
        try:
            fields = await asyncio.to_thread(_carry_out, owner, request, work)
            fields["status"] = council_requests.APPROVED
        except github_access.GitHubError as exc:
            fields, failure = {"status": council_requests.FAILED, "error": str(exc)}, exc.status
        except Exception:
            logger.exception("council request: carrying out an approved request failed")
            fields, failure = {"status": council_requests.FAILED,
                               "error": "Carrying out the request failed unexpectedly."}, 500
        saved = await asyncio.to_thread(_settle, turn_id, request_id, fields) or {**request, **fields}
    finally:
        council_requests.EXECUTING.discard(request_id)
    _tell_run(work.get("run_id"), saved)
    if failure is not None:
        raise HTTPException(failure, {"message": saved["error"], "request": saved})
    return {"request": saved}


def _prepare_run(owner: Optional[str], session_id: str, body: WorkRequest, goal: str, source: str):
    """Resolve the seats, put the whole run through the budget check, and remember its choices.

    The estimate is one round's ``estimate_turn`` times the rounds; once the
    person accepts it, that total is the run's ceiling. Runs in a worker
    thread: resolving a ChatGPT seat may refresh its OAuth token.
    """
    db = SessionLocal()
    try:
        row = cr._get_session(db, owner, session_id)
        try:
            members = [cr.resolve_seat(db, owner, seat) for seat in body.members]
            chairman = cr.resolve_seat(db, owner, body.chairman)
        except council.CouncilError as exc:
            raise HTTPException(exc.status, str(exc))
        context = cr._memory_context(owner, goal, []) if body.memory else ""
        brief = council_work.round_brief(goal, 1, body.rounds, {}, "", [])
        try:
            per_round = cr._estimate(db, owner, members, chairman, council.MODE_FULL, brief, [], context)
        except Exception as exc:
            # No estimate: still enforce a reached cap, but never fail the start.
            logger.warning("council work: estimate failed error_type=%s", type(exc).__name__)
            per_round = {"total_usd": None, "metered": any(m.billing == "metered" for m in [*members, chairman])}
        each = per_round.get("total_usd")
        estimate = {**per_round, "per_round_usd": each, "rounds": body.rounds,
                    "total_usd": None if each is None else round(each * body.rounds, 6)}
        cr._budget_gate(owner, estimate, body.budget_confirmed, what="This work run")
        row.config = json.dumps({
            "members": [cr._seat_config(s) for s in body.members],
            "chairman": cr._seat_config(body.chairman),
            "mode": council.MODE_FULL,
            "tools": council_tools.TOOLS_FULL,
            "workspace": source,
            "memory": body.memory,
            "rounds": body.rounds,
            "while_waiting": body.while_waiting,
        })
        if row.title == "New council":
            row.title = goal.splitlines()[0][:80]
        row.updated_at = utcnow_naive()
        db.commit()
        return members, chairman, context, estimate
    finally:
        db.close()


def _round_turn(owner: Optional[str], session_id: str, body: WorkRequest, question: str,
                work: Dict[str, Any], memory: bool):
    """Record a running turn for one round, resolving the seats again so their credentials are fresh."""
    db = SessionLocal()
    try:
        row = cr._get_session(db, owner, session_id)
        members = [cr.resolve_seat(db, owner, seat) for seat in body.members]
        chairman = cr.resolve_seat(db, owner, body.chairman)
        seats = {
            "members": [m.public(i) for i, m in enumerate(members)],
            "chairman": chairman.public("chair"),
            "mode": council.MODE_FULL,
            "tools": council_tools.TOOLS_FULL,
            "workspace": work["source"],
            "memory": memory,
        }
        turn = CouncilTurn(id=uuid.uuid4().hex, session_id=row.id, owner=owner, question=question,
                           config=json.dumps(seats), status="running", work=json.dumps(work))
        db.add(turn)
        row.updated_at = utcnow_naive()
        db.commit()
        return turn.id, members, chairman, seats
    finally:
        db.close()


def _message(delivered: Dict[str, Any]) -> Dict[str, Any]:
    """A delivered message for the page and the export: who wrote to whom, and what they wrote."""
    text = delivered.get("text") or ""
    if text.startswith("---\n"):
        _, sep, body = text[4:].partition("\n---\n\n")     # the header Odysseus put on top
        text = body if sep else text
    return {"name": Path(delivered.get("path") or "").name, "from": delivered.get("sender"),
            "to": delivered.get("to"), "text": text}


def _round_cost(result: Dict[str, Any], members, chairman) -> float:
    """Metered cost of one round: what ``_record_turn_spend`` records plus what the agent loop billed itself."""
    cost = sum(c or 0.0 for member, _stage, _usage, c, _est in council.billable_calls(result, members, chairman)
               if member.billing == "metered")
    calls = [*(result.get("opinions") or []), *(result.get("reviews") or [])]
    if isinstance(result.get("chair"), dict):
        calls.append(result["chair"])
    return cost + sum(float(c.get("cost_usd") or 0.0) for c in calls if isinstance(c, dict) and c.get("self_billed"))


class _WorkRun:
    """One running work run: the callbacks ``run_work`` calls, and the turns its rounds saved."""

    def __init__(self, owner, session_id, body: WorkRequest, goal, source, workspace, members, chairman,
                 context, estimate, stream, repo: Optional[str] = None, github_off: Optional[str] = None):
        self.owner, self.session_id, self.body, self.goal, self.source = owner, session_id, body, goal, source
        self.ws = workspace
        self.members, self.chairman = members, chairman      # the current round's seats
        self.context, self.estimate, self.stream = context, estimate, stream
        self.repo = repo                                     # owner/name of the GitHub origin, or None
        self.github_off = github_off                         # why the run has no GitHub features, or None
        self.outcomes: List[Notice] = []                     # the owner's decisions, for the seats
        self.push_held_told = False                          # the seats were told why rounds are not pushed
        self.turns: Dict[int, str] = {}                      # round -> turn id
        self.works: Dict[str, Dict[str, Any]] = {}           # turn id -> its work data as last saved
        self.saved: set = set()                              # turn ids whose round reached save_round
        self.current: Optional[str] = None                   # the current round's turn (its key in _RUNNING)
        self.record: Optional[Dict[str, Any]] = None
        self.spent = 0.0                                     # metered cost of the rounds so far
        self.last_cost = 0.0
        self.pending: List[asyncio.Future] = []

    def work(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "run_id": self.ws.run_id,
            "round": record["round"],
            "rounds": self.body.rounds,
            "goal": self.goal,
            "source": self.source,
            "branch": self.ws.branch,
            "main": str(self.ws.main),
            "base_sha": record.get("base"),
            "head_sha": record.get("head"),
            "messages": [_message(d) for d in record.get("delivered") or []],
            "notices": list(record.get("notices") or []),
            "run_status": "running",
            "while_waiting": self.body.while_waiting,
            "repo": self.repo,
            "github_off": self.github_off,
            "pushed": record.get("pushed"),
            "push_error": record.get("push_error"),
            "push_held": record.get("push_held"),
        }

    def first_notices(self) -> List[Notice]:
        if not self.github_off:
            return []
        return [Notice("all", f"This run has no GitHub features: {self.github_off}. Odysseus does not push its "
                              "branch, and a message to odysseus asking for a pull request or a merge is answered "
                              "with a notice.")]

    async def inbox(self, record: Dict[str, Any], messages: List[Dict[str, Any]]) -> List[Notice]:
        """Odysseus's answers after a stage: one to each message sent to it, then the owner's decisions."""
        replies = [await self._take_request(record, message) for message in messages]
        replies, self.outcomes = replies + self.outcomes, []
        return replies

    async def _take_request(self, record: Dict[str, Any], message: Dict[str, Any]) -> Notice:
        """Record a request as pending (no GitHub call), or say why it was not."""
        sender, name = message["sender"], Path(message["path"]).name
        if self.github_off:
            return Notice(sender, f"Your message {name} to odysseus was not taken as a request: this run has no "
                                  f"GitHub features ({self.github_off}).")
        try:
            parsed = council_requests.parse(council_requests.message_body(message["text"]))
        except council_requests.RequestError as exc:
            return Notice(sender, council_requests.malformed(name, str(exc)))
        head = await asyncio.to_thread(self.ws.head)
        request = council_requests.new_request(parsed, sender=sender, round_no=record["round"],
                                               branch=self.ws.branch, head_sha=head, repo=self.repo)
        turn_id = self.turns[record["round"]]
        await asyncio.to_thread(_add_request, turn_id, request)
        await self.stream.emit({"type": "request", "turn_id": turn_id, "request": request})
        return Notice(sender, f"Your {council_requests.summary(request)} is recorded and waits for the owner. Only "
                              "the owner can approve it, in the Odysseus page; Odysseus tells you when they decide.")

    async def after_round(self, record: Dict[str, Any]) -> List[Notice]:
        """Push the run's branch when this round moved main. A refusal turns GitHub off for the rest of the run.

        Not when the branch holds GitHub Actions workflows: a push could start
        them, running what the seats wrote with the repository's secrets
        before the owner has seen it. The branch then reaches GitHub only
        with a pull request the owner approves (``_carry_out`` pushes it).
        """
        if self.github_off or record.get("head") == record.get("base"):
            return []
        if await asyncio.to_thread(self.ws.workflows):
            record["push_held"] = PUSH_HELD
            told, self.push_held_told = self.push_held_told, True
            return [] if told else [Notice("all", PUSH_HELD)]
        try:
            pushed = await asyncio.to_thread(github_access.push_branch, self.owner, self.repo, str(self.ws.main),
                                             self.ws.branch, seat=self.ws.user)
        except github_access.GitHubError as exc:
            record["push_error"] = str(exc)
            if exc.status >= 500:
                return []          # git or GitHub failed: the next round that moves main tries again
            self.github_off = f"GitHub refused the push of the run's branch in round {record['round']}"
            return [Notice("all", "Odysseus did not push the run's branch to GitHub, and the run goes on without "
                                  "GitHub: nothing more is pushed, and requests to open or merge a pull request are "
                                  "not taken. The reason:\n" + council_requests.guarded("GitHub", str(exc)))]
        except Exception as exc:
            logger.warning("council work: push failed error_type=%s", type(exc).__name__)
            record["push_error"] = "The push to GitHub failed unexpectedly."
            return []
        record["pushed"] = {"branch": pushed["branch"], "sha": pushed["sha"]}
        return []

    async def waiting(self, record: Dict[str, Any]) -> bool:
        """Whether a request of this run waits for the owner (the run's while_waiting policy then applies)."""
        return await asyncio.to_thread(_open_requests, list(self.turns.values()))

    def stream_for(self, level: str, cwd: str, env: Dict[str, Optional[str]], seat: Any = None):
        return council_tools.stream_fn_for(
            council_tools.ToolSettings(mode=level, owner=self.owner, workspace=cwd, env=env,
                                       max_rounds=WORK_AGENT_ROUNDS, seat=seat))

    async def begin_round(self, record: Dict[str, Any]) -> Tuple[List[council.Member], council.Member]:
        work = self.work(record)
        turn_id, members, chairman, seats = await asyncio.to_thread(
            _round_turn, self.owner, self.session_id, self.body, record["question"], work, bool(self.context))
        cr._RUNNING.pop(self.current, None)
        cr._RUNNING[turn_id] = asyncio.current_task()
        self.current, self.record = turn_id, record
        self.turns[record["round"]] = turn_id
        self.works[turn_id] = work
        self.members, self.chairman = members, chairman
        await self.stream.emit({"type": "turn", "turn_id": turn_id, "session_id": self.session_id,
                                "question": record["question"], **seats, "work": work})
        return members, chairman

    async def save_round(self, record: Dict[str, Any]) -> None:
        # Shielded: a Stop that lands mid-save must not leave the round half
        # saved or unbilled. The run waits for it before it finishes.
        job = asyncio.ensure_future(self._persist(record))
        self.pending.append(job)
        await asyncio.shield(job)

    async def _persist(self, record: Dict[str, Any]) -> None:
        turn_id = self.turns.get(record["round"])
        if turn_id is None:
            return
        result = record["result"]
        status = record["status"]
        error = "Stopped before the council finished." if status == "cancelled" else result.get("error")
        work = self.work(record)
        self.works[turn_id] = work
        self.saved.add(turn_id)
        await asyncio.to_thread(cr._save_turn, turn_id, status=status, error=error, **cr._result_fields(result))
        await asyncio.to_thread(_save_work, turn_id, work)
        await self._bill(result)

    async def _bill(self, result: Dict[str, Any]) -> None:
        self.last_cost = _round_cost(result, self.members, self.chairman)
        self.spent += self.last_cost
        try:
            await asyncio.to_thread(cr._record_turn_spend, self.owner, self.session_id, self.members,
                                    self.chairman, result)
        except Exception as exc:
            logger.warning("council work: spend not recorded error_type=%s", type(exc).__name__)

    async def budget_check(self, record: Dict[str, Any]) -> bool:
        """Whether another round fits: under the run's ceiling, and under the monthly cap now."""
        upcoming = max(self.estimate.get("per_round_usd") or 0.0, self.last_cost)
        ceiling = self.estimate.get("total_usd")
        if ceiling is not None and self.spent + upcoming > ceiling + 1e-9:
            return False
        decision = await asyncio.to_thread(budget.check, self.owner, upcoming,
                                           metered=bool(self.estimate.get("metered")), what="The next round")
        return decision.allowed

    async def run(self) -> None:
        reason, error, rounds = None, None, 0
        _LIVE[self.ws.run_id] = self
        try:
            with reasoning_effort.for_owner(self.owner):
                result = await council_work.run_work(
                    goal=self.goal, members=self.members, chairman=self.chairman, rounds=self.body.rounds,
                    workspace=self.ws, stream_for=self.stream_for, emit=self.stream.emit,
                    save_round=self.save_round, budget_check=self.budget_check, policy=self.body.while_waiting,
                    waiting=self.waiting, begin_round=self.begin_round, context=self.context,
                    inbox=self.inbox, after_round=self.after_round,
                    requests_help="" if self.github_off else council_requests.HELP, notices=self.first_notices())
            reason, rounds = result.reason, result.rounds
        except asyncio.CancelledError:
            reason = council_work.STOP_CANCELLED
        except Exception as exc:
            logger.exception("council work run failed")
            reason = STOP_ERROR
            error = (str(exc) if isinstance(exc, council.CouncilError)
                     else str(exc.detail) if isinstance(exc, HTTPException)
                     else "The work run failed unexpectedly.")
        finally:
            _LIVE.pop(self.ws.run_id, None)
            cr._RUNNING.pop(self.current, None)
            try:
                await self._finish(reason, error)
            except Exception as exc:
                logger.warning("council work: finishing failed error_type=%s", type(exc).__name__)
            try:
                await asyncio.to_thread(self.ws.close)
            except Exception as exc:
                logger.warning("council work: closing the run folder failed error_type=%s", type(exc).__name__)
            cr._ACTIVE_SESSIONS.discard(self.session_id)
            status = ("cancelled" if reason == council_work.STOP_CANCELLED
                      else "error" if reason == STOP_ERROR else "done")
            await self.stream.emit({"type": "done", "status": status, "reason": reason, "error": error,
                                    "rounds": rounds or len(self.saved), "run_id": self.ws.run_id,
                                    "branch": self.ws.branch, "main": str(self.ws.main),
                                    "turn_id": self.current})
            await self.stream.emit(None)

    async def _finish(self, reason: Optional[str], error: Optional[str]) -> None:
        """Wait for saves still writing, close a round that never saved, and mark every round finished."""
        await asyncio.gather(*self.pending, return_exceptions=True)
        if self.current and self.current not in self.saved:
            result = (self.record or {}).get("result") or {}
            await asyncio.to_thread(
                cr._save_turn, self.current,
                status="cancelled" if reason == council_work.STOP_CANCELLED else "error",
                error=error or "Stopped before the council finished.", **cr._result_fields(result))
            await self._bill(result)
        for turn_id, work in self.works.items():
            await asyncio.to_thread(_save_work, turn_id, {**work, "run_status": "finished", "stop_reason": reason})


def setup_council_work_routes() -> APIRouter:
    router = APIRouter(prefix="/api/council", tags=["council"])

    @router.post("/sessions/{session_id}/work")
    async def start_work(session_id: str, request: Request, body: WorkRequest):
        owner = cr._owner(request)
        goal = (body.goal or "").strip()
        if not goal:
            raise HTTPException(400, "Give the work run a goal.")
        if not body.members:
            raise HTTPException(400, "Seat at least one council member.")
        _refuse_unsafe(request)
        source = await asyncio.to_thread(_source_repo, body.workspace)
        repo = await asyncio.to_thread(_origin_repo, source)
        if repo is None:
            github_off = "the repository's origin is not on github.com"
        elif not (await asyncio.to_thread(github_access.connection, owner))["connected"]:
            github_off = "no GitHub token is connected in Odysseus"
        else:
            github_off = None

        if session_id in cr._ACTIVE_SESSIONS:
            raise HTTPException(409, "This council is still deliberating. Stop it or wait.")
        cr._ACTIVE_SESSIONS.add(session_id)
        try:
            members, chairman, context, estimate = await asyncio.to_thread(
                _prepare_run, owner, session_id, body, goal, source)
            # Known limit: the clone's push URL is disabled, but a seat could
            # still push to the owner's folder by giving its path. Running in
            # Docker with the source mounted read-only is the protection.
            try:
                workspace = await asyncio.to_thread(council_workspace.create_run, source, runs_root(), goal,
                                                    len(body.members))
            except council_workspace.WorkspaceError as exc:
                raise HTTPException(exc.status, str(exc))
        except BaseException:
            cr._ACTIVE_SESSIONS.discard(session_id)
            raise

        stream = cr._Broadcast()
        run = _WorkRun(owner, session_id, body, goal, source, workspace, members, chairman, context,
                       estimate, stream, repo=repo, github_off=github_off)
        task = asyncio.create_task(run.run(), name=f"council-work-{workspace.run_id}")
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        start = {"type": "work", "session_id": session_id, "run_id": workspace.run_id, "branch": workspace.branch,
                 "main": str(workspace.main), "source": source, "rounds": body.rounds,
                 "while_waiting": body.while_waiting}

        async def events():
            yield f"data: {json.dumps(start)}\n\n"
            try:
                while True:
                    queue = stream.queue
                    if queue is None:
                        break
                    event = await queue.get()
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                # The run keeps going without a listener; it saves itself.
                stream.detach()

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── the owner's side of the agents' requests ──

    @router.get("/requests")
    async def list_requests(request: Request,
                            status: Optional[Literal["pending", "approving", "approved", "denied", "failed"]] = None):
        owner = _owner_in_person(request)
        return {"requests": await asyncio.to_thread(_list_requests, owner, status)}

    @router.post("/requests/{request_id}/approve")
    async def approve_request(request_id: str, request: Request):
        """Carry out one request now, at the owner's click; 404 when not theirs, 409 once decided."""
        owner = _owner_in_person(request)
        # Shielded: a dropped connection must not leave a request half carried out.
        return await asyncio.shield(asyncio.ensure_future(_approve(owner, request_id)))

    @router.post("/requests/{request_id}/deny")
    async def deny_request(request_id: str, request: Request):
        owner = _owner_in_person(request)
        _turn_id, denied, work = await asyncio.to_thread(_claim, owner, request_id, council_requests.DENIED)
        _tell_run(work.get("run_id"), denied)
        return {"request": denied}

    return router
