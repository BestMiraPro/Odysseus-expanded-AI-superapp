import subprocess
from pathlib import Path

# Resolve bash explicitly: bare "bash" can hit the Windows WSL stub.
from tests._shell_helpers import BASH, requires_bash

pytestmark = requires_bash


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-docker-amd-gpu.sh"


def test_amd_gpu_check_rejects_unknown_extra_arg_before_diagnostics():
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--bad-option"],
        capture_output=True,
        text=True, encoding="utf-8",
        check=False,
    )

    assert proc.returncode == 1
    assert "Unknown option: --bad-option" in proc.stderr


def test_amd_gpu_check_shell_syntax():
    # Capture both streams: inherited pytest handles fail nondeterministically
    # on Windows (WinError 50), and capturing surfaces the syntax error.
    proc = subprocess.run(
        [BASH, "-n", str(SCRIPT)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
