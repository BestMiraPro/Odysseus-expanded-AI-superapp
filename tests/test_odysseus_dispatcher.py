import os

import pytest

from tests.helpers.cli_loader import load_script


def _exec_bit_is_honoured(tmp_path) -> bool:
    """Probe whether this filesystem actually enforces the POSIX exec bit.

    Windows has no exec bit: chmod(0o644) leaves os.access(..., X_OK) True for
    any existing file. Probing beats assuming — a network or FAT mount on Linux
    behaves the same way, and the guard should follow the filesystem rather
    than the OS name.
    """
    probe = tmp_path / "exec-probe"
    probe.write_text("#!/bin/sh\n", encoding="utf-8")
    probe.chmod(0o644)
    return not os.access(probe, os.X_OK)


def test_is_runnable_subcommand_rejects_a_missing_path(tmp_path):
    cli = load_script("odysseus")
    assert cli._is_runnable_subcommand(tmp_path / "odysseus-absent") is False


def test_is_runnable_subcommand_rejects_a_directory(tmp_path):
    cli = load_script("odysseus")
    sub = tmp_path / "odysseus-dir"
    sub.mkdir()
    assert cli._is_runnable_subcommand(sub) is False


def test_is_runnable_subcommand_accepts_an_executable_file(tmp_path):
    cli = load_script("odysseus")
    sub = tmp_path / "odysseus-demo"
    sub.write_text("#!/bin/sh\n", encoding="utf-8")
    sub.chmod(0o755)
    assert cli._is_runnable_subcommand(sub) is True


def test_is_runnable_subcommand_requires_the_executable_bit(tmp_path):
    """The permission half of the contract, where the filesystem enforces it."""
    if not _exec_bit_is_honoured(tmp_path):
        pytest.skip(
            "filesystem does not enforce the POSIX exec bit "
            "(probed: chmod 0o644 still reports X_OK)"
        )
    cli = load_script("odysseus")
    sub = tmp_path / "odysseus-demo"
    sub.write_text("#!/bin/sh\n", encoding="utf-8")
    sub.chmod(0o644)

    assert cli._is_runnable_subcommand(sub) is False

    sub.chmod(0o755)
    assert cli._is_runnable_subcommand(sub) is True
