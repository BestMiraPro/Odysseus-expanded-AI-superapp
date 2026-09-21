import importlib.util
import json
import os
from pathlib import Path

import pytest


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("odysseus_setup_under_test", Path("setup.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _headless(monkeypatch):
    """Force the non-interactive branch so no test blocks on a prompt."""
    monkeypatch.setenv("ODYSSEUS_SKIP_ADMIN_PROMPT", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)


def test_create_default_admin_normalizes_env_username(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("ODYSSEUS_ADMIN_USER", " AdminUser ")
    monkeypatch.setenv("ODYSSEUS_ADMIN_PASSWORD", "temporary-password")

    assert setup_module.create_default_admin() == "created"

    auth_path = tmp_path / "auth.json"
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "adminuser" in data["users"]
    assert "AdminUser" not in data["users"]


def test_main_loads_admin_password_from_env_file(tmp_path, monkeypatch):
    """Regression: setup.py must honor an admin password pre-seeded in .env on
    native installs, even when the var is not exported into the shell
    (website/setup.md documents this). Previously setup.py never called
    load_dotenv(), so os.getenv() saw nothing and a random password was
    generated instead."""
    import bcrypt

    setup_module = _load_setup_module()

    # Credentials live ONLY in a .env beside setup.py (written with a UTF-8 BOM,
    # the Notepad-on-Windows case that utf-8-sig must tolerate) — not exported.
    monkeypatch.delenv("ODYSSEUS_ADMIN_USER", raising=False)
    monkeypatch.delenv("ODYSSEUS_ADMIN_PASSWORD", raising=False)
    (tmp_path / ".env").write_text(
        "ODYSSEUS_ADMIN_USER=presetuser\nODYSSEUS_ADMIN_PASSWORD=fromenvfile12345\n",
        encoding="utf-8-sig",
    )

    # Point setup at the temp dir and neutralize main()'s heavy steps.
    monkeypatch.setattr(setup_module, "BASE_DIR", str(tmp_path))
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(auth_path))
    monkeypatch.setattr(setup_module, "check_arch", lambda: None)
    monkeypatch.setattr(setup_module, "create_dirs", lambda: None)
    monkeypatch.setattr(setup_module, "create_env", lambda: None)
    monkeypatch.setattr(setup_module, "check_deps", lambda: None)
    monkeypatch.setattr(setup_module, "init_database", lambda: None)
    # Force the non-interactive branch so the test never blocks on a prompt.
    monkeypatch.setenv("ODYSSEUS_SKIP_ADMIN_PROMPT", "1")

    try:
        setup_module.main()
    finally:
        # load_dotenv writes real os.environ entries; undo so sibling tests
        # don't inherit them.
        os.environ.pop("ODYSSEUS_ADMIN_USER", None)
        os.environ.pop("ODYSSEUS_ADMIN_PASSWORD", None)

    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "presetuser" in data["users"], data
    assert bcrypt.checkpw(
        b"fromenvfile12345", data["users"]["presetuser"]["password_hash"].encode()
    ), "admin password from .env was ignored; a random one was generated"


def test_headless_setup_without_password_fails_before_writing(
        tmp_path, monkeypatch, capsys):
    """Regression (security plan S4): a fresh headless install must NOT
    invent and print a credential. It fails with guidance, and no auth.json
    is created."""
    import sys

    setup_module = _load_setup_module()
    sys_stdin = sys.stdin
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("ODYSSEUS_ADMIN_USER", raising=False)
    monkeypatch.delenv("ODYSSEUS_ADMIN_PASSWORD", raising=False)
    _headless(monkeypatch)

    assert setup_module.create_default_admin() == "failed"
    auth_path = tmp_path / "auth.json"
    assert not auth_path.exists(), "a failed bootstrap wrote an auth file"
    out = capsys.readouterr().out
    assert "Set ODYSSEUS_ADMIN_PASSWORD for headless setup" in out


def test_headless_password_is_hashed_and_never_printed(
        tmp_path, monkeypatch, capsys):
    """Supplied credentials produce a bcrypt hash; the password appears in
    neither stdout nor stderr."""
    import bcrypt

    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("ODYSSEUS_ADMIN_USER", "admin")
    monkeypatch.setenv("ODYSSEUS_ADMIN_PASSWORD", "s3cret-n3ver-printed-1")
    _headless(monkeypatch)

    assert setup_module.create_default_admin() == "created"
    captured = capsys.readouterr()
    assert "s3cret-n3ver-printed-1" not in captured.out
    assert "s3cret-n3ver-printed-1" not in captured.err
    data = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert bcrypt.checkpw(
        b"s3cret-n3ver-printed-1",
        data["users"]["admin"]["password_hash"].encode(),
    )


def test_headless_too_short_password_fails_and_writes_nothing(
        tmp_path, monkeypatch, capsys):
    """The minimum-length check applies to password-only configuration with
    the default admin username (security plan S4)."""
    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("ODYSSEUS_ADMIN_USER", raising=False)
    monkeypatch.setenv("ODYSSEUS_ADMIN_PASSWORD", "short")
    _headless(monkeypatch)

    assert setup_module.create_default_admin() == "failed"
    assert not (tmp_path / "auth.json").exists()
    assert "must be at least" in capsys.readouterr().out


def test_existing_auth_file_is_untouched(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"users": {"admin": {"x": 1}}}', encoding="utf-8")
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(auth_path))
    monkeypatch.setenv("ODYSSEUS_ADMIN_PASSWORD", "s3cret-n3ver-printed-1")
    _headless(monkeypatch)

    assert setup_module.create_default_admin() == "exists"
    assert auth_path.read_text(encoding="utf-8") == \
        '{"users": {"admin": {"x": 1}}}'


def test_interactive_prompt_path_still_works(tmp_path, monkeypatch):
    """Interactive input (with its own minimum/mismatch loop) still creates
    the account and prints no password."""
    import bcrypt

    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("ODYSSEUS_ADMIN_USER", raising=False)
    monkeypatch.delenv("ODYSSEUS_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "admin")
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "interactive-pass-1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # keep the skip var unset so the interactive branch is taken

    assert setup_module.create_default_admin() == "created"
    data = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert bcrypt.checkpw(
        b"interactive-pass-1", data["users"]["admin"]["password_hash"].encode(),
    )


def test_main_failed_status_exits_nonzero_with_configuration_message(
        tmp_path, monkeypatch, capsys):
    import sys

    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("ODYSSEUS_ADMIN_USER", raising=False)
    monkeypatch.delenv("ODYSSEUS_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ODYSSEUS_SKIP_ADMIN_PROMPT", "1")
    monkeypatch.setattr(setup_module, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(setup_module, "check_arch", lambda: None)
    monkeypatch.setattr(setup_module, "create_dirs", lambda: None)
    monkeypatch.setattr(setup_module, "create_env", lambda: None)
    monkeypatch.setattr(setup_module, "check_deps", lambda: None)
    monkeypatch.setattr(setup_module, "init_database", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        setup_module.main()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "did NOT complete" in out
    assert "ODYSSEUS_ADMIN_PASSWORD" in out
