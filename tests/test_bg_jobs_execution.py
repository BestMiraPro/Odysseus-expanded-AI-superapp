import os
import tracemalloc

import pytest

from src import bg_jobs


@pytest.fixture
def completed_launch(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "job files"
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    real_popen = bg_jobs.subprocess.Popen

    def wait_for_local_command(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        try:
            proc.wait(timeout=10)
        except BaseException:
            proc.kill()
            proc.wait(timeout=10)
            raise
        return proc

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", wait_for_local_command)
    return bg_jobs.launch


@pytest.mark.parametrize("exit_code", [0, 7])
def test_bash_job_records_explicit_exit(completed_launch, exit_code):
    bash = bg_jobs.find_bash()
    if not bash:
        pytest.skip("bash is not installed")

    rec = completed_launch(f"echo job-output\nexit {exit_code}", "test-session")
    result = bg_jobs.get(rec["id"])

    assert result["exit_code"] == exit_code
    assert result["status"] == ("done" if exit_code == 0 else "failed")
    assert result["output"] == "job-output\n"
    assert not result.get("died")


def test_bash_job_preserves_line_continuations(completed_launch):
    if not bg_jobs.find_bash():
        pytest.skip("bash is not installed")
    command = "printf '%s\\n' " + chr(92) + "\n'joined'"
    rec = completed_launch(command, "test-session")
    result = bg_jobs.get(rec["id"])
    assert result["exit_code"] == 0
    assert result["output"] == "joined\n"


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_cmd_job_records_explicit_exit(completed_launch, monkeypatch, exit_code):
    monkeypatch.setattr(bg_jobs, "find_bash", lambda: None)

    rec = completed_launch(f"echo job-output\nexit {exit_code}", "test-session")
    result = bg_jobs.get(rec["id"])

    assert result["exit_code"] == exit_code
    assert result["status"] == ("done" if exit_code == 0 else "failed")
    assert result["output"] == "job-output\n"
    assert not result.get("died")


def test_large_log_keeps_head_and_tail_without_loading_entire_file(tmp_path):
    log_path = tmp_path / "large.log"
    with log_path.open("wb") as log:
        log.write(b"head\n")
        for _ in range(32):
            log.write(b"x" * 65536)
        log.write(b"\ntail")

    tracemalloc.start()
    try:
        output = bg_jobs._read_output({"log_path": str(log_path)})
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert output.startswith("head\n")
    assert output.endswith("\ntail")
    assert "[truncated]" in output
    assert len(output) < 17000
    assert peak_bytes < 1_000_000


def test_log_truncation_preserves_unicode_and_normalizes_newlines(tmp_path, monkeypatch):
    log_path = tmp_path / "unicode.log"
    log_path.write_bytes(("α\r\nβ" + "😀" * 12 + "終\r\n了").encode("utf-8"))
    monkeypatch.setattr(bg_jobs, "_MAX_OUTPUT_CHARS", 8)

    assert bg_jobs._read_output({"log_path": str(log_path)}) == "α\nβ😀\n…[truncated]…\n😀終\n了"


def test_small_log_is_returned_without_truncation(tmp_path, monkeypatch):
    log_path = tmp_path / "small.log"
    log_path.write_bytes("α\r\n😀終".encode("utf-8"))
    monkeypatch.setattr(bg_jobs, "_MAX_OUTPUT_CHARS", 8)

    assert bg_jobs._read_output({"log_path": str(log_path)}) == "α\n😀終"
