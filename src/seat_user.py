"""Council seats' processes run as OS users of their own, which cannot reach Odysseus's files or each other's.

In the Docker image (Dockerfile, docker/entrypoint.sh):

- There are ``SLOTS`` seat users, ``odysseus-seat-<n>`` (uid and gid
  ``BASE_ID + n``), each in its own group only. A seat holds one for as long
  as it runs (``lease``), so no two seats running at the same time share a
  user: a seat cannot read another seat's environment or memory (its model
  credential), signal it, or open its folder.
- ``/app``, which holds Odysseus's code, data (sessions, the GitHub token and
  its key, the database), logs and keys, is mode 0750 and owned by the app
  user, so a seat user gets "permission denied" anywhere below it. The bind
  mounts below /app need not enforce permissions themselves (Docker Desktop's
  do not): a seat user cannot get past /app to reach them.
- ``LAUNCHER`` is a small setuid-root program (docker/seat-launch.c) that only
  the group ``odysseus-launch`` may run; the entrypoint puts the app user in
  it. It runs one command as one of the seat users and as no one else.
- The app user is in every seat user's group, which is how it shares files
  with each: a folder it makes for a seat gets that seat's group and the
  setgid bit, and both sides create group-writable files there (the
  launcher's umask 002; Odysseus's own file tools hand over what they write,
  see src/agent_tools/filesystem_tools.py).
- A work run (src/council_workspace.py) holds one more user for itself. Its
  group owns the run folder under ``RUNS_ROOT`` (/council-runs, a Docker
  volume, so its permissions hold on Docker Desktop too), and is the primary
  group of the run's seats: they share the run's repository with each other
  and with no other run. Each seat's home and outbox there belong to its own
  group.
- A seat user cannot read the app's process environment or memory, or signal
  it, because it is another uid; the app cannot signal a seat's processes
  either, so ``Seat.kill`` does it as the seat user. Giving a user back
  (``release``) kills every process still running as it.

``unavailable()`` checks this once per process by starting a shell as a seat
user and asking it to list the data folder. Outside the container it runs
nothing and isolation is simply unavailable: seats then run as before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Dict, Iterator, List, Mapping, Optional, Sequence

from src.constants import DATA_DIR
from src.host_docker_access import HOST_DOCKER_SOCKET_PATH, running_in_container

try:
    import fcntl
except ImportError:          # Windows: no seat users, ever
    fcntl = None

LAUNCHER = "/usr/local/bin/odysseus-seat"
KILL = "/bin/kill"
# Run through the launcher: signals every process of the user it runs as, and
# only when that user is the seat user it was meant for ($1). Whatever went
# wrong with the switch, Odysseus never runs it against its own user.
_KILL_ALL = '[ "$(id -u)" = "$1" ] && exec "$2" -KILL -- -1'
# The seat users' ids are BASE_ID .. BASE_ID + SLOTS - 1 (the Dockerfile
# creates them and compiles the same numbers into the launcher).
BASE_ID = 61100
SLOTS = 64
# One lock file per seat user; whoever holds its lock holds the user. The
# entrypoint makes the folder for the app user alone.
SLOT_DIR = "/run/odysseus-seats"
RUNS_ROOT = "/council-runs"
# The only variables of Odysseus's own environment a seat process inherits.
# Everything else (tokens, the database URL, proxies with passwords, ...)
# stays behind; callers add what the seat needs, its model credential included.
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ")

_PROBE = """\
[ "$(id -u)" != "$1" ] || { echo "it runs as Odysseus's own user"; exit 1; }
! ls -- "$2" >/dev/null 2>&1 || { echo "it can read the data folder $2"; exit 1; }
[ ! -w "$3" ] || { echo "it can use the Docker socket $3"; exit 1; }
"""
_PROBE_TIMEOUT = 30

_probed: Optional[List[Optional[str]]] = None       # [reason or None] once probed
_held: Dict[int, int] = {}                          # seat user -> the descriptor holding its lock
_held_lock = threading.Lock()
_visiting = threading.Lock()


class SeatsBusy(RuntimeError):
    """Every seat user is taken."""


def _gid(slot: int) -> int:
    return BASE_ID + slot


def environment(extra: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, str]:
    """The whole environment of a seat process: ``ENV_PASSTHROUGH``, then ``extra`` (None removes)."""
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    env.update(extra or {})
    return {key: value for key, value in env.items() if value is not None}


@dataclass(frozen=True)
class Seat:
    """One seat user, and how a seat's processes start as it.

    ``folder`` is the seat's throwaway folder for one call (its config, the
    history socket). ``home`` is its HOME when that outlives the call (a work
    run's seats keep what they install between stages); otherwise the folder
    is. ``group`` is the seat user whose group is the processes' primary
    group (a work run's own user), with the seat's own group as a second one.
    """

    slot: int
    folder: str = ""
    home: Optional[str] = None
    group: Optional[int] = None

    @property
    def gid(self) -> int:
        return _gid(self.slot)

    def argv(self, cmd: Sequence[str]) -> List[str]:
        """``cmd`` started as this seat user."""
        return [LAUNCHER, str(self.slot), str(self.slot if self.group is None else self.group), *cmd]

    def env(self, extra: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, str]:
        """``environment`` with this seat's HOME, and what it installed there on PATH."""
        home = self.home or self.folder
        own: Dict[str, Optional[str]] = {}
        if home:
            own = {"HOME": home, "PATH": f"{home}/.local/bin{os.pathsep}{os.environ.get('PATH', os.defpath)}"}
        return environment({**own, **(extra or {})})

    def _run(self, cmd: Sequence[str], timeout: float) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(self.argv(cmd), env=environment(), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def kill(self, pid: int) -> None:
        """SIGKILL the process group ``pid`` of this seat (Odysseus's own user may not signal it)."""
        self._run([KILL, "-KILL", "--", f"-{pid}"], 10)

    def kill_all(self) -> None:
        """SIGKILL every process running as this seat user, whatever session or group it moved to."""
        self._run(["/bin/sh", "-c", _KILL_ALL, "kill-all", str(BASE_ID + self.slot), KILL], 10)

    def share(self, path: str, mode: int) -> None:
        """Give the folder ``path`` (Odysseus's own) to this seat's group, with ``mode``, which should set setgid."""
        os.chown(path, -1, self.gid)
        os.chmod(path, mode)

    def reaches(self, path: str, write: bool = False) -> bool:
        """Whether this seat user can open (and with ``write``, change) the folder ``path``."""
        test = '[ -d "$1" ] && [ -r "$1" ] && [ -x "$1" ]' + (' && [ -w "$1" ]' if write else "")
        proc = self._run(["/bin/sh", "-c", test, "reaches", path], _PROBE_TIMEOUT)
        return proc is not None and proc.returncode == 0

    def remove(self, path: str) -> None:
        """Delete a folder both users wrote into: the seat's own files first, as the seat user."""
        self._run(["rm", "-rf", "--", path], 120)
        shutil.rmtree(path, ignore_errors=True)


def lease() -> Seat:
    """Take a seat user nobody holds; ``release`` gives it back. Raises SeatsBusy when all are taken.

    The lock is a file lock, so it holds across Odysseus's worker processes
    and goes away with a process that dies.
    """
    os.makedirs(SLOT_DIR, mode=0o700, exist_ok=True)
    for slot in range(SLOTS):
        fd = os.open(os.path.join(SLOT_DIR, str(slot)), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        with _held_lock:
            _held[slot] = fd
        seat = Seat(slot)
        seat.kill_all()         # what a holder that died without releasing left running
        return seat
    raise SeatsBusy(f"All {SLOTS} council seat users are in use; try again when a council has finished.")


def release(seat: Seat) -> None:
    """Give a leased seat user back, after killing every process still running as it."""
    seat.kill_all()
    with _held_lock:
        fd = _held.pop(seat.slot, None)
    if fd is not None:
        os.close(fd)


@contextmanager
def user() -> Iterator[Seat]:
    """A seat user for the duration, with no folder of its own."""
    seat = lease()
    try:
        yield seat
    finally:
        release(seat)


@contextmanager
def seat(prefix: str = "council-seat-") -> Iterator[Seat]:
    """A seat user with a throwaway folder, outside the data folder, that only it and Odysseus can open."""
    with user() as leased:
        path = tempfile.mkdtemp(prefix=prefix)
        try:
            leased.share(path, 0o2770)
            yield replace(leased, folder=path)
        finally:
            leased.kill_all()
            leased.remove(path)


@contextmanager
def visitor(root: str) -> Iterator[Seat]:
    """A seat user that may read the finished work run in ``root`` for the duration.

    A run's folder is closed to every seat user when the run ends
    (Workspace.close). One visitor at a time: each takes the folder's group.
    """
    with _visiting, user() as leased:
        leased.share(root, 0o2750)
        try:
            yield leased
        finally:
            os.chmod(root, 0o700)


def reaches(path: str, write: bool = False) -> bool:
    """Whether a seat user can open (and with ``write``, change) the folder ``path``."""
    with user() as leased:
        return leased.reaches(path, write)


def _probe() -> Optional[str]:
    if not running_in_container():
        return "Odysseus is not running in its Docker container"
    if not os.access(LAUNCHER, os.X_OK):
        return f"the seat launcher {LAUNCHER} is missing or this user may not run it (an image built before seat isolation?)"
    groups = {*os.getgroups(), os.getegid()}
    if any(_gid(slot) not in groups for slot in range(SLOTS)):
        return "Odysseus's user is not in the seat users' groups (the image's entrypoint adds it)"
    try:
        with user() as leased:
            proc = subprocess.run(leased.argv(["/bin/sh", "-c", _PROBE, "probe", str(os.getuid()), DATA_DIR,
                                               HOST_DOCKER_SOCKET_PATH]),
                                  env=environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, SeatsBusy) as exc:
        return f"the seat launcher could not run: {exc}"
    if proc.returncode != 0:
        detail = (proc.stdout.strip() or proc.stderr.strip() or f"exit code {proc.returncode}")[:300]
        return f"a process started as a seat user failed the check: {detail}"
    return None


def unavailable() -> Optional[str]:
    """Why seats cannot run as seat users here, or None when they can (checked once per process)."""
    global _probed
    if _probed is None:
        _probed = [_probe()]
    return _probed[0]


def available() -> bool:
    return unavailable() is None
