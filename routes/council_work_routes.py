"""AI Council work runs: the seats work on a goal in a copy of a repository, round after round.

A run is one asyncio task in this process (``src.council_work.run_work``), like
an ask in routes/council_routes.py. Each round is saved as an ordinary council
turn in the session, with the round's work data in the turn's ``work`` column,
so the Council page shows it like any other turn. The session stays busy for
the whole run, and ``_RUNNING`` always holds the current round's turn, so the
Stop button on that round stops the whole run; finished rounds stay saved.

Each seat gets a full shell that is not sandboxed, so a work run only starts
when Odysseus runs in its Docker container (the container keeps the shell off
the host) and login is required for every caller (so the shell cannot call
Odysseus, approvals included, without credentials).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.database import CouncilTurn, SessionLocal, utcnow_naive
from routes import council_routes as cr
from src import budget, council, council_tools, council_work, council_workspace, reasoning_effort
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

RUNS_DIRNAME = "council-runs"
# The run failed (a git error, a seat that can no longer be resolved): not one
# of council_work's stop rules.
STOP_ERROR = "error"

# Running work-run tasks. The event loop only keeps weak references to tasks,
# and _RUNNING holds a run only once its first round has a turn.
_TASKS: set = set()


def runs_root() -> Path:
    """Where run folders live, under the data folder; they are kept when a run ends."""
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / RUNS_DIRNAME


def in_docker() -> bool:
    """Whether Odysseus runs inside its container.

    The image sets no variable of its own to test (the Dockerfile's only ENV
    is UV_TOOL_DIR / UV_TOOL_BIN_DIR), so this reads /.dockerenv, which Docker
    creates in every container, with the cgroup fallback the rest of Odysseus
    uses (src/host_docker_access.py).
    """
    from src.host_docker_access import running_in_container

    return running_in_container()


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
    why = unauthenticated_access()
    if why:
        raise HTTPException(403, f"Work runs need login on for every caller, and {why}: a seat's shell could "
                                 "then call Odysseus, approvals included, without credentials.")


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
                 context, estimate, stream):
        self.owner, self.session_id, self.body, self.goal, self.source = owner, session_id, body, goal, source
        self.ws = workspace
        self.members, self.chairman = members, chairman      # the current round's seats
        self.context, self.estimate, self.stream = context, estimate, stream
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
        }

    def stream_for(self, level: str, cwd: str, env: Dict[str, Optional[str]]):
        return council_tools.stream_fn_for(
            council_tools.ToolSettings(mode=level, owner=self.owner, workspace=cwd, env=env,
                                       max_rounds=WORK_AGENT_ROUNDS))

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
        await asyncio.to_thread(cr._save_turn, turn_id, status=status, error=error, work=work,
                                **cr._result_fields(result))
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
        try:
            with reasoning_effort.for_owner(self.owner):
                result = await council_work.run_work(
                    goal=self.goal, members=self.members, chairman=self.chairman, rounds=self.body.rounds,
                    workspace=self.ws, stream_for=self.stream_for, emit=self.stream.emit,
                    save_round=self.save_round, budget_check=self.budget_check, policy=self.body.while_waiting,
                    begin_round=self.begin_round, context=self.context)
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
            cr._RUNNING.pop(self.current, None)
            try:
                await self._finish(reason, error)
            except Exception as exc:
                logger.warning("council work: finishing failed error_type=%s", type(exc).__name__)
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
            await asyncio.to_thread(cr._save_turn, turn_id,
                                    work={**work, "run_status": "finished", "stop_reason": reason})


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
                       estimate, stream)
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

    return router
