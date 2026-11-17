"""Nothing under test may kill the processes of the person running the tests.

Seat cleanup signals every process of a seat user (src/seat_user.py). In the
Docker image that user is a seat's own. Under test there is no such user: a
stand-in launcher runs commands as the developer, and ``kill -KILL -- -1``
then ends every app they have open. Three things stand in the way, each
checked here without running a kill:

- the cleanup command itself only signals when it runs as the seat user;
- tests point ``seat_user.KILL`` at a program that does not exist (conftest.py);
- the one stand-in launcher tests may use records kill commands, never runs them.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from src import seat_user
from tests.helpers.seat_launcher import fake_launcher, kills

TESTS = Path(__file__).resolve().parent
posix_only = pytest.mark.skipif(os.name == "nt", reason="the seat users exist only in the Linux image")


def test_under_test_the_kill_program_does_not_exist():
    assert seat_user.KILL != "/bin/kill" and not os.path.exists(seat_user.KILL)


def test_every_kill_goes_through_the_one_kill_program(monkeypatch):
    source = (TESTS.parent / "src" / "seat_user.py").read_text(encoding="utf-8")
    assert source.count("/bin/kill") == 1 and 'KILL = "/bin/kill"' in source
    calls = []
    monkeypatch.setattr(seat_user.subprocess, "run", lambda argv, **kw: calls.append(argv))
    seat = seat_user.Seat(3)
    seat.kill(4242)
    seat.kill_all()
    assert calls == [
        [seat_user.LAUNCHER, "3", "3", seat_user.KILL, "-KILL", "--", "-4242"],
        [seat_user.LAUNCHER, "3", "3", "/bin/sh", "-c", seat_user._KILL_ALL, "kill-all",
         str(seat_user.BASE_ID + 3), seat_user.KILL],
    ]


@posix_only
def test_killing_all_of_a_seat_users_processes_does_nothing_as_any_other_user(tmp_path):
    # A stand-in for kill that only writes down how it was called.
    called = tmp_path / "called"
    stub = tmp_path / "stub"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {called}\n")
    stub.chmod(0o755)

    def run(uid):
        return subprocess.run(["/bin/sh", "-c", seat_user._KILL_ALL, "kill-all", str(uid), str(stub)],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30)

    # Meant for a seat user, started as this one (a launcher that did not switch): nothing is signalled.
    assert run(seat_user.BASE_ID).returncode != 0 and not called.exists()
    # Started as the user it was meant for, it signals all of that user's processes.
    assert run(os.getuid()).returncode == 0 and called.read_text() == "-KILL -- -1\n"


@posix_only
def test_the_stand_in_launcher_records_a_kill_and_runs_nothing(tmp_path, monkeypatch):
    launcher = fake_launcher(tmp_path)
    monkeypatch.setattr(seat_user, "LAUNCHER", str(launcher))
    seat = seat_user.Seat(0)
    seat.kill(4242)
    seat.kill_all()
    marker = tmp_path / "ran"
    # Any command that mentions kill, however it is wrapped, and whatever body the launcher has.
    subprocess.run([str(launcher), "0", "0", "/bin/sh", "-c", f"touch {marker}; : kill"], timeout=30)
    subprocess.run([str(fake_launcher(tmp_path, f"touch {marker}")), "0", "0", "/nonexistent/pkill", "x"], timeout=30)
    assert not marker.exists()
    recorded = kills(tmp_path)
    assert len(recorded) == 4
    assert recorded[0] == f"0 0 {seat_user.KILL} -KILL -- -4242" and recorded[1].endswith(f"{seat_user.KILL}")
    # And it still runs everything else.
    assert subprocess.run([str(fake_launcher(tmp_path)), "0", "0", "/bin/sh", "-c", "echo ok"],
                          capture_output=True, text=True, timeout=30).stdout == "ok\n"


def test_tests_build_a_seat_launcher_only_through_the_stand_in():
    # A test that writes its own launcher script, or names the kill program
    # itself, could run a real kill as the developer.
    own_launcher = re.compile(r"""/\s*["']odysseus-seat["']""")
    allowed = {TESTS / "helpers" / "seat_launcher.py", Path(__file__).resolve()}
    offenders = []
    for path in TESTS.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if own_launcher.search(text):
            offenders.append(f"{path.name}: writes its own odysseus-seat launcher")
        if "seat_user" in text and re.search(r"""["'](/usr)?/bin/kill["']""", text):
            offenders.append(f"{path.name}: names the real kill program")
        if re.search(r"""setattr\(seat_user,\s*["']KILL["']""", text) and path.name != "conftest.py":
            offenders.append(f"{path.name}: changes seat_user.KILL")
    assert not offenders, offenders
