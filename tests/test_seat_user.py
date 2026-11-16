"""Seat isolation: when seats can run as their own OS user, and how their processes start.

The real launcher is a setuid binary that only exists in the Docker image
(docker/seat-launch.c); scripts/check_seat_isolation.sh proves it there. Here
the switch is faked: a launcher script that runs its arguments as the same user.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src import seat_user

posix_only = pytest.mark.skipif(os.name == "nt", reason="the seat user exists only in the Linux image")


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    monkeypatch.setattr(seat_user, "_probed", None)


def _launcher(tmp_path, body='exec "$@"'):
    path = tmp_path / "odysseus-seat"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _in_container(monkeypatch, launcher):
    monkeypatch.setattr(seat_user, "running_in_container", lambda: True)
    monkeypatch.setattr(seat_user, "LAUNCHER", str(launcher))
    # A group this test process is in, standing in for odysseus-seat.
    monkeypatch.setattr(seat_user, "_seat_gid", lambda: os.getgid())


def test_outside_the_container_nothing_is_probed_and_nothing_changes(monkeypatch):
    monkeypatch.setattr(seat_user, "running_in_container", lambda: False)
    monkeypatch.setattr(seat_user.subprocess, "run", lambda *a, **k: pytest.fail("probed outside Docker"))
    assert "container" in seat_user.unavailable()
    assert seat_user.available() is False


def test_an_image_without_the_launcher_has_no_isolation(monkeypatch, tmp_path):
    _in_container(monkeypatch, tmp_path / "missing")
    assert "launcher" in seat_user.unavailable()


@posix_only
def test_the_app_user_must_be_in_the_seat_group(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path))
    monkeypatch.setattr(seat_user, "_seat_gid", lambda: None)
    assert "odysseus-seat group" in seat_user.unavailable()


@posix_only
def test_a_launcher_that_does_not_change_user_is_refused(monkeypatch, tmp_path):
    # The fake launcher runs the probe as this same user, as a container
    # whose PUID is the seat's uid would: isolation must not be reported.
    _in_container(monkeypatch, _launcher(tmp_path))
    why = seat_user.unavailable()
    assert why and "Odysseus's own user" in why
    assert seat_user.available() is False


@posix_only
def test_the_probe_asks_the_seat_user_to_read_the_data_folder(monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    _in_container(monkeypatch, _launcher(tmp_path, f'printf "%s\\n" "$@" > {seen}; exit 0'))
    monkeypatch.setattr(seat_user, "DATA_DIR", "/data/dir")
    assert seat_user.unavailable() is None and seat_user.available() is True
    argv = seen.read_text().splitlines()
    assert argv[:2] == ["/bin/sh", "-c"]
    assert argv[-3:] == [str(os.getuid()), "/data/dir", seat_user.HOST_DOCKER_SOCKET_PATH]
    # Probed once per process.
    seen.unlink()
    assert seat_user.available() is True and not seen.exists()


@posix_only
def test_the_probe_reports_what_the_seat_user_could_still_do(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path, 'echo "it can read the data folder"; exit 1'))
    assert seat_user.unavailable() == "a process started as the seat user failed the check: it can read the data folder"
    monkeypatch.setattr(seat_user, "_probed", None)
    _in_container(monkeypatch, _launcher(tmp_path, 'echo "odysseus-seat: cannot switch to the seat user" >&2; exit 126'))
    assert "cannot switch to the seat user" in seat_user.unavailable()


def test_the_seat_environment_is_an_allowlist(monkeypatch):
    for key, value in {"PATH": "/usr/bin", "LANG": "C.UTF-8", "GH_TOKEN": "ghp_x", "DATABASE_URL": "postgres://u:p@db",
                       "ODYSSEUS_INTERNAL_TOKEN": "i", "HOME": "/app", "CLAUDE_CODE_OAUTH_TOKEN": "ambient",
                       "LD_PRELOAD": "/x.so"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    env = seat_user.environment({"HOME": "/seat", "GIT_DIR": None, "X": "1"})
    assert env == {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/seat", "X": "1"}


def test_a_seat_starts_commands_through_the_launcher_with_its_own_home(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    seat = seat_user.Seat("/tmp/council-seat-x")
    assert seat.argv(["git", "status"]) == [seat_user.LAUNCHER, "git", "status"]
    assert seat.env({"A": "b"})["HOME"] == "/tmp/council-seat-x"
    assert seat.env({"A": "b"})["A"] == "b"


def test_kill_signals_the_seats_process_group_as_the_seat_user(monkeypatch):
    calls = []
    monkeypatch.setattr(seat_user.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    seat_user.kill(4242)
    (argv, kw), = calls
    assert argv == [seat_user.LAUNCHER, "/bin/kill", "-KILL", "--", "-4242"]
    assert set(kw["env"]) <= set(seat_user.ENV_PASSTHROUGH)


@posix_only
def test_reaches_asks_the_seat_user(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path))
    folder = tmp_path / "ws"
    folder.mkdir()
    assert seat_user.reaches(str(folder)) and seat_user.reaches(str(folder), write=True)
    folder.chmod(0o500)
    try:
        assert seat_user.reaches(str(folder)) and not seat_user.reaches(str(folder), write=True)
    finally:
        folder.chmod(0o700)
    assert not seat_user.reaches(str(tmp_path / "missing"))


@posix_only
def test_a_seat_folder_is_shared_through_the_seat_group_and_removed_as_the_seat(monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    _in_container(monkeypatch, _launcher(tmp_path, f'echo "$@" >> {seen}; exec "$@"'))
    with seat_user.folder() as path:
        st = os.stat(path)
        assert stat.S_IMODE(st.st_mode) & 0o777 == 0o770 and st.st_gid == os.getgid()
        assert not path.startswith(str(seat_user.DATA_DIR))
        Path(path, "made-by-the-seat").mkdir()
    assert not os.path.exists(path)
    assert f"rm -rf -- {path}" in seen.read_text()


def test_the_launcher_source_switches_only_to_the_compiled_in_seat_user():
    source = (Path(__file__).resolve().parents[1] / "docker" / "seat-launch.c").read_text(encoding="utf-8")
    assert "setgroups(1, &gid)" in source and "setresgid(gid, gid, gid)" in source
    assert "setresuid(uid, uid, uid)" in source and "PR_SET_NO_NEW_PRIVS" in source
    assert "close_range(3" in source and "umask(002)" in source
    # No user argument and nothing read from the environment.
    assert "getenv" not in source and "getpwnam" not in source and "atoi" not in source


def test_the_image_installs_the_launcher_for_the_launch_group_only():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "useradd --system --uid 61000 --gid odysseus-seat" in dockerfile
    assert "--shell /usr/sbin/nologin odysseus-seat" in dockerfile
    assert "-static -DSEAT_UID=61000 -DSEAT_GID=61000" in dockerfile
    assert "chown root:odysseus-launch /usr/local/bin/odysseus-seat" in dockerfile
    assert "chmod 4750 /usr/local/bin/odysseus-seat" in dockerfile
    assert "chmod 0750 /app" in dockerfile
    entrypoint = (root / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'usermod -aG odysseus-seat,odysseus-launch "$ODY_USER"' in entrypoint
    # The app keeps its usual umask: only what it writes into a seat's
    # workspace is made group-writable, file by file (filesystem_tools).
    assert "umask" not in entrypoint
    for name in ("docker-compose.yml", "docker-compose.gpu-nvidia.yml", "docker-compose.gpu-amd.yml"):
        compose = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        # A volume, where permissions hold on every platform, not a host folder.
        assert "council-runs:/council-runs" in compose["services"]["odysseus"]["volumes"], name
        assert "council-runs" in compose["volumes"], name


# ---------------------------------------------------------------------------
# files the app's own file tools write for a seat
# ---------------------------------------------------------------------------

def _mode(path):
    import stat

    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_what_the_file_tools_write_for_a_seat_is_group_writable(tmp_path):
    from src import tool_execution
    from src.agent_tools import filesystem_tools as fst

    old = os.umask(0o022)
    try:
        with tool_execution.subprocess_env_overrides({}, seat=seat_user.Seat(str(tmp_path))):
            target = tmp_path / "pkg" / "sub" / "mod.py"
            fst.make_parent_dirs(str(target))
            target.write_text("x = 1\n")
            fst.share_with_seat(str(target))
        assert _mode(target) & 0o060 == 0o060
        for folder in (tmp_path / "pkg", tmp_path / "pkg" / "sub"):
            assert _mode(folder) & 0o2070 == 0o2070       # group rwx and setgid
    finally:
        os.umask(old)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_without_a_seat_the_file_tools_leave_modes_to_the_umask(tmp_path):
    from src.agent_tools import filesystem_tools as fst

    old = os.umask(0o022)
    try:
        target = tmp_path / "pkg" / "mod.py"
        fst.make_parent_dirs(str(target))
        target.write_text("x = 1\n")
        fst.share_with_seat(str(target))
        assert _mode(target) == 0o644 and _mode(tmp_path / "pkg") == 0o755
    finally:
        os.umask(old)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_sharing_with_a_seat_does_not_follow_a_link_put_in_its_place(tmp_path):
    from src import tool_execution
    from src.agent_tools import filesystem_tools as fst

    secret = tmp_path / "secret"
    secret.write_text("keep out\n")
    os.chmod(secret, 0o600)
    link = tmp_path / "written.txt"
    link.symlink_to(secret)
    with tool_execution.subprocess_env_overrides({}, seat=seat_user.Seat(str(tmp_path))):
        fst.share_with_seat(str(link))
    assert _mode(secret) == 0o600


def test_the_three_file_tools_share_what_they_write():
    source = (Path(__file__).resolve().parents[1] / "src" / "agent_tools" / "filesystem_tools.py").read_text()
    # edit_file, write_file and apply_patch each hand over what they wrote.
    assert source.count("share_with_seat(path)") == 3
    assert source.count("make_parent_dirs(path)") == 2 and source.count("os.makedirs(") == 1
