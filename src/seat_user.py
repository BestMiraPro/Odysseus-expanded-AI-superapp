"""Council seats' processes run as ``odysseus-seat``, an OS user that cannot reach Odysseus's files.

In the Docker image (Dockerfile, docker/entrypoint.sh):

- ``odysseus-seat`` (uid and gid 61000) is in its own group only. ``/app``, which
  holds Odysseus's code, data (sessions, the GitHub token and its key, the
  database), logs and keys, is mode 0750 and owned by the app user, so the seat
  user gets "permission denied" anywhere below it. The bind mounts below /app
  need not enforce permissions themselves (Docker Desktop's do not): the seat
  user cannot get past /app to reach them.
- ``LAUNCHER`` is a small setuid-root program (docker/seat-launch.c) that only
  the group ``odysseus-launch`` may run; the entrypoint puts the app user in
  it. It runs one command as the seat user and as no one else.
- The app user is also in the ``odysseus-seat`` group, which is how the two
  share files: a run folder lives under ``RUNS_ROOT`` (/council-runs, outside
  /app), whose setgid bit gives everything in it that group, and both sides
  create group-writable files there (the launcher's umask 002; Odysseus's own
  file tools hand over what they write, see src/agent_tools/filesystem_tools.py).
  /council-runs is a Docker volume, not a folder mounted from the host, so its
  permissions hold on Docker Desktop too: they keep the seats out of the parts
  of a run folder Odysseus itself works in (src/council_workspace.py).
- The seat user cannot read the app's process environment or memory, or
  signal it, because it is another uid; the app cannot signal a seat's
  processes either, so ``kill`` does it as the seat user.

``unavailable()`` checks all this once per process by starting a shell as the
seat user and asking it to list the data folder. Outside the container it
runs nothing and isolation is simply unavailable: seats then run as before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Mapping, Optional, Sequence

from src.constants import DATA_DIR
from src.host_docker_access import HOST_DOCKER_SOCKET_PATH, running_in_container

try:
    import grp
except ImportError:          # Windows: no seat user, ever
    grp = None

LAUNCHER = "/usr/local/bin/odysseus-seat"
SEAT_GROUP = "odysseus-seat"
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


def _seat_gid() -> Optional[int]:
    if grp is None:
        return None
    try:
        return grp.getgrnam(SEAT_GROUP).gr_gid
    except KeyError:
        return None


def _probe() -> Optional[str]:
    if not running_in_container():
        return "Odysseus is not running in its Docker container"
    if not os.access(LAUNCHER, os.X_OK):
        return f"the seat launcher {LAUNCHER} is missing or this user may not run it (an image built before seat isolation?)"
    gid = _seat_gid()
    if gid is None or gid not in os.getgroups():
        return f"Odysseus's user is not in the {SEAT_GROUP} group (the image's entrypoint adds it)"
    try:
        proc = subprocess.run(argv(["/bin/sh", "-c", _PROBE, "probe", str(os.getuid()), DATA_DIR,
                                    HOST_DOCKER_SOCKET_PATH]),
                              env=environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the seat launcher could not run: {exc}"
    if proc.returncode != 0:
        detail = (proc.stdout.strip() or proc.stderr.strip() or f"exit code {proc.returncode}")[:300]
        return f"a process started as the seat user failed the check: {detail}"
    return None


def unavailable() -> Optional[str]:
    """Why seats cannot run as the seat user here, or None when they can (checked once per process)."""
    global _probed
    if _probed is None:
        _probed = [_probe()]
    return _probed[0]


def available() -> bool:
    return unavailable() is None


def argv(cmd: Sequence[str]) -> List[str]:
    """``cmd`` started as the seat user."""
    return [LAUNCHER, *cmd]


def environment(extra: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, str]:
    """The whole environment of a seat process: ``ENV_PASSTHROUGH``, then ``extra`` (None removes)."""
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    env.update(extra or {})
    return {key: value for key, value in env.items() if value is not None}


def kill(pid: int) -> None:
    """SIGKILL the process group ``pid`` of a seat process (Odysseus's own user may not signal it)."""
    try:
        subprocess.run(argv(["/bin/kill", "-KILL", "--", f"-{pid}"]), env=environment(),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def reaches(path: str, write: bool = False) -> bool:
    """Whether the seat user can open (and with ``write``, change) the folder ``path``."""
    test = '[ -d "$1" ] && [ -r "$1" ] && [ -x "$1" ]' + (' && [ -w "$1" ]' if write else "")
    try:
        return subprocess.run(argv(["/bin/sh", "-c", test, "reaches", path]), env=environment(),
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def remove(path: str) -> None:
    """Delete a folder both users wrote into: the seat's own files first, as the seat user."""
    try:
        subprocess.run(argv(["rm", "-rf", "--", path]), env=environment(), stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        pass
    shutil.rmtree(path, ignore_errors=True)


def share(path: str, mode: int) -> None:
    """Give the folder ``path`` (Odysseus's own) to the seat group, with ``mode``, which should set setgid."""
    os.chown(path, -1, _seat_gid())
    os.chmod(path, mode)


@contextmanager
def folder(prefix: str = "council-seat-") -> Iterator[str]:
    """A throwaway folder for one seat's run, outside the data folder, that the seat user can write in."""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        share(path, 0o2770)
        yield path
    finally:
        remove(path)


@dataclass(frozen=True)
class Seat:
    """How one seat's processes start: through the launcher, with ``home`` (a ``folder``) as HOME."""

    home: str

    def argv(self, cmd: Sequence[str]) -> List[str]:
        return argv(cmd)

    def env(self, extra: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, str]:
        return environment({"HOME": self.home, **(extra or {})})
