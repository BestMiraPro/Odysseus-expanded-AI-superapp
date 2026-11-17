"""Seat isolation: when seats can run as OS users of their own, and how their processes start.

The real launcher is a setuid binary that only exists in the Docker image
(docker/seat-launch.c); scripts/check_seat_isolation.sh proves it there. Here
the switch is faked: a launcher script that runs its arguments as the same user
(tests/helpers/seat_launcher.py, which never runs a kill).
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
from tests.helpers.seat_launcher import fake_launcher as _launcher, kills

posix_only = pytest.mark.skipif(os.name == "nt", reason="the seat users exist only in the Linux image")


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(seat_user, "_probed", None)
    monkeypatch.setattr(seat_user, "_filter_probed", None)
    monkeypatch.setattr(seat_user, "SLOT_DIR", str(tmp_path / "slots"))


def _in_container(monkeypatch, launcher):
    monkeypatch.setattr(seat_user, "running_in_container", lambda: True)
    monkeypatch.setattr(seat_user, "LAUNCHER", str(launcher))
    # A group this test process is in, standing in for every seat user's.
    monkeypatch.setattr(seat_user, "_gid", lambda slot: os.getgid())


def test_outside_the_container_nothing_is_probed_and_nothing_changes(monkeypatch):
    monkeypatch.setattr(seat_user, "running_in_container", lambda: False)
    monkeypatch.setattr(seat_user.subprocess, "run", lambda *a, **k: pytest.fail("probed outside Docker"))
    assert "container" in seat_user.unavailable()
    assert seat_user.available() is False


def test_an_image_without_the_launcher_has_no_isolation(monkeypatch, tmp_path):
    _in_container(monkeypatch, tmp_path / "missing")
    assert "launcher" in seat_user.unavailable()


@posix_only
def test_the_app_user_must_be_in_every_seat_users_group(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path))
    monkeypatch.setattr(seat_user, "_gid", lambda slot: os.getgid() if slot < seat_user.SLOTS - 1 else -5)
    assert "seat users' groups" in seat_user.unavailable()


@posix_only
def test_a_launcher_that_does_not_change_user_is_refused(monkeypatch, tmp_path):
    # The fake launcher runs the probe as this same user, as a container
    # whose PUID is a seat user's uid would: isolation must not be reported.
    _in_container(monkeypatch, _launcher(tmp_path))
    why = seat_user.unavailable()
    assert why and "Odysseus's own user" in why
    assert seat_user.available() is False


@posix_only
def test_the_probe_asks_a_seat_user_to_read_the_data_folder(monkeypatch, tmp_path):
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
    assert seat_user.unavailable() == "a process started as a seat user failed the check: it can read the data folder"
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


def test_a_seat_starts_commands_through_the_launcher_as_its_own_user(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    seat = seat_user.Seat(7, "/tmp/council-seat-x")
    assert seat.argv(["git", "status"]) == [seat_user.LAUNCHER, "7", "7", "git", "status"]
    env = seat.env({"A": "b"})
    assert env["HOME"] == "/tmp/council-seat-x" and env["A"] == "b"
    assert seat.gid == seat_user.BASE_ID + 7


def test_a_work_runs_seat_keeps_its_home_and_gets_the_runs_group(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    seat = seat_user.Seat(7, "/runs/r/homes/seat-1/call-x", home="/runs/r/homes/seat-1", group=3)
    assert seat.argv(["id"]) == [seat_user.LAUNCHER, "7", "3", "id"]
    env = seat.env()
    # What the seat installed for itself in an earlier stage is on its PATH.
    assert env["HOME"] == "/runs/r/homes/seat-1" and env["PATH"] == f"/runs/r/homes/seat-1/.local/bin{os.pathsep}/usr/bin"


def test_a_seat_user_with_no_folder_has_no_home():
    assert "HOME" not in seat_user.Seat(0).env()


@posix_only
def test_no_two_holders_get_the_same_seat_user(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path, "exit 0"))
    monkeypatch.setattr(seat_user, "SLOTS", 2)
    first, second = seat_user.lease(), seat_user.lease()
    assert {first.slot, second.slot} == {0, 1}
    with pytest.raises(seat_user.SeatsBusy):
        seat_user.lease()
    seat_user.release(first)
    again = seat_user.lease()
    assert again.slot == first.slot
    seat_user.release(second)
    seat_user.release(again)


@posix_only
def test_a_seat_user_is_cleared_of_processes_when_taken_and_when_given_back(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path))
    with seat_user.user() as seat:
        asked = f"kill-all {seat_user.BASE_ID + seat.slot} {seat_user.KILL}"
        assert [line.endswith(asked) for line in kills(tmp_path)] == [True]
    assert [line.endswith(asked) for line in kills(tmp_path)] == [True, True]


@posix_only
def test_taking_a_seat_user_closes_what_a_dead_holder_left_open_to_it(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path))
    runs = tmp_path / "runs"
    monkeypatch.setattr(seat_user, "RUNS_ROOT", str(runs))
    monkeypatch.setattr(seat_user, "_gid", lambda slot: os.getgid() if slot == 0 else -5)
    temp = tmp_path / "temp"
    monkeypatch.setattr(seat_user.tempfile, "tempdir", str(temp))
    stale_run, stale_call, other = runs / "crashed-run", temp / "council-seat-dead", temp / "someone-elses"
    for path in (stale_run, stale_call, other):
        path.mkdir(parents=True)
        path.chmod(0o2750)
    seat = seat_user.lease()
    assert seat.slot == 0
    # What Odysseus died without closing is shut to the user's next holder.
    assert stat.S_IMODE(os.stat(stale_run).st_mode) == 0o700 and stat.S_IMODE(os.stat(stale_call).st_mode) == 0o700
    # Nothing else in the temp folder is touched.
    assert stat.S_IMODE(os.stat(other).st_mode) == 0o2750
    seat_user.release(seat)


@posix_only
def test_reaches_asks_a_seat_user(monkeypatch, tmp_path):
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
def test_a_seats_folder_is_its_own_groups_and_removed_as_the_seat(monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    # When the folder is removed, how many times the seat's processes were killed before.
    _in_container(monkeypatch, _launcher(
        tmp_path, f'[ "$1" != rm ] || echo "$(wc -l < {tmp_path / "seat-kills"}) $*" > {seen}; exec "$@"'))
    with seat_user.seat() as seat:
        path = seat.folder
        st = os.stat(path)
        assert stat.S_IMODE(st.st_mode) & 0o777 == 0o770 and st.st_gid == os.getgid()
        assert not path.startswith(str(seat_user.DATA_DIR))
        Path(path, "made-by-the-seat").mkdir()
    assert not os.path.exists(path)
    # Whatever the seat left running is killed before its folder goes: once
    # when the user was taken, once more before the removal, and again after.
    assert seen.read_text().split() == ["2", "rm", "-rf", "--", path]
    assert len(kills(tmp_path)) == 3


@posix_only
def test_a_finished_run_opens_to_one_visitor_and_closes_again(monkeypatch, tmp_path):
    _in_container(monkeypatch, _launcher(tmp_path, "exit 0"))
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    with seat_user.visitor(str(root)) as seat:
        assert isinstance(seat, seat_user.Seat)
        assert stat.S_IMODE(os.stat(root).st_mode) == 0o2750
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


@posix_only
def test_a_seat_user_that_reaches_a_low_port_on_localhost_means_no_filter(monkeypatch, tmp_path):
    # The stand-in launcher filters nothing: the connection gets through.
    _in_container(monkeypatch, _launcher(tmp_path))
    monkeypatch.setattr(seat_user, "_probed", [None])
    why = seat_user.unfiltered()
    assert why and "connected to a port" in why and "NET_ADMIN" in why


@posix_only
def test_a_seat_user_kept_from_a_low_port_on_localhost_is_filtered(monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    _in_container(monkeypatch, _launcher(tmp_path, f'echo "$@" > {seen}; exit 1'))
    monkeypatch.setattr(seat_user, "_probed", [None])
    assert seat_user.unfiltered() is None
    port = int(seen.read_text().split()[-1])
    assert port < seat_user.LOCAL_PORTS_FROM
    # Checked once per process.
    seen.unlink()
    assert seat_user.unfiltered() is None and not seen.exists()


def test_without_seat_users_the_network_is_not_filtered_either(monkeypatch):
    monkeypatch.setattr(seat_user, "_probed", ["Odysseus is not running in its Docker container"])
    assert "container" in seat_user.unfiltered()


def test_the_entrypoint_filters_the_seat_users_network_and_compose_lets_it():
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert f"--dport {seat_user.LOCAL_PORTS_FROM}:65535 -j ACCEPT" in entrypoint
    assert '-m owner --uid-owner "$SEAT_UIDS" -j ODYSSEUS_SEATS' in entrypoint
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8", "fc00::/7"):
        assert net in entrypoint
    assert "iptables" in (root / "Dockerfile").read_text(encoding="utf-8")
    for name in ("docker-compose.yml", "docker-compose.gpu-nvidia.yml", "docker-compose.gpu-amd.yml"):
        compose = yaml.safe_load((root / name).read_text(encoding="utf-8"))
        assert compose["services"]["odysseus"]["cap_add"] == ["NET_ADMIN"], name
        # Killed seat processes need someone to wait for them.
        assert compose["services"]["odysseus"]["init"] is True, name


def test_the_launcher_source_switches_only_to_the_compiled_in_seat_users():
    source = (Path(__file__).resolve().parents[1] / "docker" / "seat-launch.c").read_text(encoding="utf-8")
    assert "setgroups(2, groups)" in source and "setresgid(gid, gid, gid)" in source
    assert "setresuid(uid, uid, uid)" in source and "PR_SET_NO_NEW_PRIVS" in source
    assert "close_range(3" in source and "umask(002)" in source
    # Both ids come from the compiled-in range, and both arguments are checked against it.
    assert "uid = SEAT_BASE + seat;" in source and "gid = SEAT_BASE + group;" in source
    assert "value < 0 || value >= SEAT_SLOTS" in source
    # No user name and nothing read from the environment.
    assert "getenv" not in source and "getpwnam" not in source and "atoi" not in source


def test_the_image_installs_the_launcher_for_the_launch_group_only():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert f"-static -DSEAT_BASE={seat_user.BASE_ID} -DSEAT_SLOTS={seat_user.SLOTS}" in dockerfile
    assert f"for n in $(seq 0 {seat_user.SLOTS - 1})" in dockerfile and f"id=$(({seat_user.BASE_ID} + n))" in dockerfile
    assert ":/nonexistent:/usr/sbin/nologin" in dockerfile
    assert "chown root:odysseus-launch /usr/local/bin/odysseus-seat" in dockerfile
    assert "chmod 4750 /usr/local/bin/odysseus-seat" in dockerfile
    assert "chmod 0750 /app" in dockerfile
    entrypoint = (root / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'usermod -aG "${SEAT_GROUPS}odysseus-launch" "$ODY_USER"' in entrypoint
    assert f'-m 0700 {seat_user.SLOT_DIR}' not in entrypoint       # the fixture moved SLOT_DIR
    assert "-m 0700 /run/odysseus-seats" in entrypoint and "chmod 0711 /council-runs" in entrypoint
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
        with tool_execution.subprocess_env_overrides({}, seat=seat_user.Seat(0, str(tmp_path))):
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
    with tool_execution.subprocess_env_overrides({}, seat=seat_user.Seat(0, str(tmp_path))):
        fst.share_with_seat(str(link))
    assert _mode(secret) == 0o600


def test_the_three_file_tools_share_what_they_write():
    source = (Path(__file__).resolve().parents[1] / "src" / "agent_tools" / "filesystem_tools.py").read_text()
    # edit_file, write_file and apply_patch each hand over what they wrote.
    assert source.count("share_with_seat(path)") == 3
    assert source.count("make_parent_dirs(path)") == 2 and source.count("os.makedirs(") == 1
