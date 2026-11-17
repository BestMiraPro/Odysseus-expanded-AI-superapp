"""The only stand-in for the setuid seat launcher (docker/seat-launch.c) that tests may use.

The real launcher switches to a seat user; a stand-in cannot, so whatever it
runs, it runs as the person running the tests. Seat cleanup kills every
process of "the seat user" (``kill -KILL -- -1``): run as that person it ends
their whole login session. So this launcher never runs a command that
mentions kill. It writes the command to ``KILLS`` in its folder and exits 0.

tests/test_seat_kill_safety.py fails when a test builds a launcher any other way.
"""

from __future__ import annotations

from pathlib import Path

KILLS = "seat-kills"

_GUARD = """\
#!/bin/sh
for arg in "$@"; do
    case "$arg" in
        *kill*) printf '%s\\n' "$*" >> '{kills}'; exit 0 ;;
    esac
done
shift 2
"""


def fake_launcher(folder: Path, body: str = 'exec "$@"') -> Path:
    """Write the launcher into ``folder``; ``body`` runs after the two seat arguments are dropped."""
    path = Path(folder) / "odysseus-seat"
    path.write_text(_GUARD.format(kills=Path(folder) / KILLS) + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def kills(folder: Path) -> list:
    """The commands the launcher in ``folder`` was asked to run and did not, one line each."""
    log = Path(folder) / KILLS
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
