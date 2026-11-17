"""Checks, inside the Odysseus container, that Council seats run as users that cannot reach Odysseus or each other.

Run by scripts/check_seat_isolation.sh as Odysseus's own user (gosu odysseus,
as the entrypoint starts the app), with the app itself running as PID 1. It
starts seat processes through the same code the app uses (src/seat_user.py,
src/council_workspace.py, src/github_access.py) and prints one PASS or FAIL
line per check with what actually came back. Exits 1 if any check fails.

Never run it against a real instance: it writes placeholder secrets into the
data folder when they are missing and creates a work run.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace

sys.path.insert(0, "/app")
os.chdir("/app")

from src import council_tools, council_workspace as cw, github_access, seat_user  # noqa: E402

FAILED = []
DATA, LOGS = "/app/data", "/app/logs"


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    for line in str(detail).strip().splitlines()[:12]:
        print(f"        {line}")
    if not ok:
        FAILED.append(name)


def as_seat(script, *args, seat=None, env=None, cwd="/tmp", stdin=None):
    """Run ``script`` with /bin/sh as a seat user (GUEST, in no run, unless given), as the app starts a seat's processes."""
    proc = subprocess.run((seat or GUEST).argv(["/bin/sh", "-c", script, "seat", *args]),
                          env=env or seat_user.environment({"HOME": "/tmp"}), cwd=cwd, input=stdin,
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def user_name(seat):
    return f"odysseus-seat-{seat.slot}"


def running_as(seat):
    """The pids of the processes alive as ``seat``'s user (a killed one nobody waited for yet does not count)."""
    out = subprocess.run(["ps", "-o", "pid=,stat=", "-u", str(seat_user.BASE_ID + seat.slot)], capture_output=True,
                         text=True)
    return [line.split()[0] for line in out.stdout.splitlines() if not line.split()[1].startswith("Z")]


def denied(rc, out, marker=""):
    return rc != 0 and ("Permission denied" in out or "Operation not permitted" in out) and \
        (not marker or marker not in out)


def attempt(fn):
    try:
        return fn()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def hold_connections(listener, held):
    """Accept connections and never answer them, so a push waits on its first request."""
    while True:
        try:
            held.append(listener.accept()[0])
        except OSError:
            return


def app_git(cwd, *args):
    env = {**seat_user.environment(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "owner", "GIT_AUTHOR_EMAIL": "owner@example.invalid",
           "GIT_COMMITTER_NAME": "owner", "GIT_COMMITTER_EMAIL": "owner@example.invalid", "HOME": "/tmp"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


print("== the setting")
print(subprocess.run(["id"], capture_output=True, text=True).stdout.strip())
print(subprocess.run(["ls", "-ld", "/app", "/council-runs", seat_user.LAUNCHER, DATA], capture_output=True,
                     text=True).stdout.strip())
with open("/proc/1/status") as fh:
    app_uid = next(line.split()[1] for line in fh if line.startswith("Uid:"))
with open("/proc/1/cmdline", "rb") as fh:
    app_cmd = fh.read().replace(b"\0", b" ").decode().strip()
print(f"PID 1 (the app): uid {app_uid}: {app_cmd}")
why = seat_user.unavailable()
check("seat isolation is available (the app's own probe)", why is None, why or "")
why = seat_user.unfiltered()
check("the seats' network is filtered (the app's own probe)", why is None, why or "")
GUEST = seat_user.lease()

print("\n== who a seat process is")
rc, out = as_seat('id; grep -E "^(Uid|Gid|Groups|CapPrm|CapEff|NoNewPrivs)" /proc/self/status; umask; env | sort')
me = f"{seat_user.BASE_ID + GUEST.slot}({user_name(GUEST)})"
check("the launcher starts it as a seat user, in its own group only, with no capabilities and no_new_privs",
      rc == 0 and f"uid={me} gid={me} groups={me}" in out
      and "NoNewPrivs:\t1" in out and "CapEff:\t0000000000000000" in out and "0002" in out, out)

print("\n== Odysseus's files")
planted = {}
for name in ("sessions.json", "github_access.json", ".app_key", "app.db", "auth.json"):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(f"PROOF-SECRET-{name}\n")
    planted[path] = f"PROOF-SECRET-{name}"
log = os.path.join(LOGS, "proof.log")
with open(log, "a") as fh:
    fh.write("PROOF-SECRET-log\n")
planted[log] = "PROOF-SECRET-log"
for path, marker in planted.items():
    rc, out = as_seat('cat -- "$1"', path)
    # The output is not printed when the check fails because the placeholder leaked.
    leaked = marker in out
    check(f"reading {path} is denied", denied(rc, out) and not leaked,
          "<output withheld: it holds the placeholder>" if leaked else out)
for path in (DATA, LOGS, "/app", "/app/.ssh", "/app/src"):
    rc, out = as_seat('ls -- "$1"', path)
    check(f"listing {path} is denied", denied(rc, out), out)
rc, out = as_seat('echo forged > "$1"', os.path.join(DATA, "sessions.json"))
check("writing the sessions file is denied", denied(rc, out), out)

print("\n== the app's process")
rc, out = as_seat("cat /proc/1/environ")
check("reading the app's environment (/proc/1/environ) is denied", denied(rc, out), out)
rc, out = as_seat("head -c 16 /proc/1/mem")
check("reading the app's memory (/proc/1/mem) is denied", denied(rc, out), out)
rc, out = as_seat("python3 -c 'import ctypes, os; libc = ctypes.CDLL(None, use_errno=True); "
                  "r = libc.ptrace(16, 1, 0, 0); print(r, os.strerror(ctypes.get_errno()))'")
check("PTRACE_ATTACH to the app fails", "-1 Operation not permitted" in out, out)
rc, out = as_seat("kill -0 1; kill -TERM 1")
check("signalling the app is denied", "Operation not permitted" in out, out)
print("        (no Yama ptrace_scope here)" if not os.path.exists("/proc/sys/kernel/yama/ptrace_scope")
      else f"        ptrace_scope {open('/proc/sys/kernel/yama/ptrace_scope').read().strip()}")

print("\n== becoming root or the app user")
rc, out = as_seat(f"{seat_user.LAUNCHER} 0 0 id")
check("the seat user may not run the launcher", denied(rc, out), out)
rc, out = as_seat("su -c id root </dev/null; su -s /bin/sh -c id odysseus </dev/null")
check("su gets the seat user nowhere", "uid=0" not in out and "uid=1000" not in out, out)
rc, out = as_seat("for c in 'os.setuid(0)' 'os.setuid(1000)' 'os.setgid(1000)' 'os.setgroups([1000])' "
                  "'os.setresuid(1000, 1000, 1000)'; do python3 -c \"import os; $c\" 2>&1 | tail -1; done")
check("no set*id call works", out.count("PermissionError") == 5, out)
refused = [subprocess.run([seat_user.LAUNCHER, *args, "id"], capture_output=True, text=True)
           for args in (["--user=root", "0"], ["root", "0"], [str(seat_user.SLOTS), "0"], ["0", str(seat_user.SLOTS)],
                        ["-1", "0"], ["0", "-61100"], ["0x0", "0"], ["0", ""])]
check("the launcher takes two seat numbers in its range and nothing else (no user name, no other id)",
      all(proc.returncode == 64 and "usage" in proc.stderr and not proc.stdout for proc in refused),
      "\n".join(f"{proc.args[1:3]} -> {proc.returncode} {proc.stdout.strip()}" for proc in refused))
proc = subprocess.run([seat_user.LAUNCHER, "0", "0", "/bin/sh", "-c", "env"], capture_output=True, text=True,
                      env={"PATH": "/usr/bin:/bin", "LD_PRELOAD": "/tmp/x.so", "TMPDIR": "/x", "KEEP": "1"})
check("the launcher drops LD_* and TMPDIR and passes the rest as given",
      "LD_PRELOAD" not in proc.stdout and "TMPDIR" not in proc.stdout and "KEEP=1" in proc.stdout, proc.stdout)

print("\n== a work run")
source = tempfile.mkdtemp(prefix="owner-repo-")
app_git(source, "init", "-q", "-b", "main")
with open(os.path.join(source, "README.md"), "w") as fh:
    fh.write("hello\n")
app_git(source, "add", "README.md")
app_git(source, "commit", "-q", "-m", "first")
ws = cw.create_run(source, seat_user.RUNS_ROOT, "prove seat isolation", seats=2)
base = ws.start_round()
print(subprocess.run(["ls", "-lan", str(ws.root), str(ws.messages), str(ws.messages / "outbox"), str(ws.root / "seats"),
                      str(ws.root / "homes")], capture_output=True, text=True).stdout.strip())
print(f"        the run's users: { {who: user_name(seat) for who, seat in ws.users.items()} }")


def call(who):
    """One call of ``who``, as council_tools.stream_fn_for sets it up: its run user, a new folder in its home."""
    seat = ws.seat(who)
    folder = tempfile.mkdtemp(prefix="call-", dir=seat.home)
    seat.share(folder, 0o2770)
    seat = replace(seat, folder=folder)
    return {"seat": seat, "env": seat.env({**council_tools.credential_free_git_env(folder), **ws.env(who),
                                           **dict.fromkeys(council_tools.ODYSSEUS_API_ENV)})}


S1, S2, CHAIR = call("seat-1"), call("seat-2"), call("chair")
seat1, home1 = S1["seat"], S1["seat"].folder
env1 = S1["env"]
print(f"        a seat's environment: {sorted(env1)}")
rc, out = as_seat('echo "seat work" > feature.txt && git status --short && git add -A && '
                  'git commit -q -m "seat-1: feature" && git log --oneline -2', **S1, cwd=str(ws.workdir("seat-1")))
check("seat-1 writes, sees git status, commits and reads git log in its worktree", rc == 0 and "seat-1: feature" in out,
      out)
rc, out = as_seat('git merge -q --no-edit seat-1 && echo chair >> README.md && git commit -qam "chair: integrate" '
                  '&& git status --short && git log --oneline -3', **CHAIR, cwd=str(ws.main))
check("the chairman merges and commits in the run's main copy", rc == 0 and "chair: integrate" in out, out)
outbox = ws.messages / "outbox" / "seat-1"
rc, out = as_seat('printf "hello from seat-1\\n" > "$1/all__hello.md" && ls -l "$1"', str(outbox), **S1)
check("seat-1 writes a message into its outbox", rc == 0 and "all__hello.md" in out, out)
rc, out = as_seat('touch "$1/forged.md"', str(ws.messages), **S1)
check("writing straight into messages/ is denied", denied(rc, out), out)
rc, out = as_seat('mv "$1/seats/1" "$1/seats/x" || mv "$1/main" "$1/x" || ln -s /app/data "$1/link"', str(ws.root),
                  **S1)
check("swapping seats/<n>, main or messages for a link is denied", denied(rc, out) and ws.workdir("seat-1").is_dir(),
      out)

# Odysseus's side: read, audit, deliver, commit, diff, all from the run folder.
with open(ws.workdir("seat-1") / "feature.txt") as fh:
    check("Odysseus reads what the seat wrote", fh.read() == "seat work\n")
# As the agent loop's in-process file tools do: with the seat active they
# hand over what they write, and Odysseus's umask is not touched.
from src import tool_execution
from src.agent_tools import filesystem_tools

notes = str(ws.workdir("seat-1") / "pkg" / "notes.txt")
with tool_execution.subprocess_env_overrides({}, seat=seat1):
    filesystem_tools.make_parent_dirs(notes)
    with open(notes, "w") as fh:
        fh.write("written by Odysseus\n")
    filesystem_tools.share_with_seat(notes)
rc, out = as_seat('echo "and by the seat" >> pkg/notes.txt && cat pkg/notes.txt && touch pkg/new-by-seat.txt',
                  **S1, cwd=str(ws.workdir("seat-1")))
check("the seat edits a file Odysseus's own file tools wrote, and adds one in the folder they made",
      rc == 0 and "and by the seat" in out, out)
current = os.umask(0)
os.umask(current)
own = tempfile.NamedTemporaryFile("w", prefix="probe-own-", delete=False)
own.write("x\n")
own.close()
plain = "probe-own-plain"
with open(os.path.join(os.path.dirname(own.name), plain), "w") as fh:
    fh.write("x\n")
mode = os.stat(fh.name).st_mode & 0o777
os.remove(fh.name)
os.remove(own.name)
check("a file Odysseus writes for itself is not group-writable (its umask is unchanged)",
      current & 0o020 == 0o020 and mode & 0o020 == 0, f"umask {current:03o}, mode {mode:03o}")
check("Odysseus commits what was left uncommitted (its git runs as the run's own user)",
      ws.commit_pending("seat-1", "seat-1: left over"))
attachment = ws.diff_attachment("seat-1", base)
check("Odysseus attaches the seat's diff", "feature.txt" in attachment and "pkg/notes.txt" in attachment,
      attachment[:400])
head = ws.head()
direct = subprocess.run(["git", "-C", str(ws.main), "rev-parse", "HEAD"], capture_output=True, text=True,
                        env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
                             "GIT_CONFIG_GLOBAL": os.devnull})
check("Odysseus's own git (as its user) still reads main, e.g. for a push", direct.stdout.strip() == head,
      direct.stdout + direct.stderr)
delivered, notices = ws.deliver(1)
notices += ws.audit()
check("Odysseus delivers the seat's message from the outbox",
      [d.sender for d in delivered] == ["seat-1"] and "hello from seat-1" in delivered[0].text,
      f"{[os.path.basename(d.path) for d in delivered]} notices={[n.text for n in notices]}")
rc, out = as_seat('cat "$1" >/dev/null && echo edited >> "$1"', delivered[0].path, **S1)
check("a seat reads a delivered message but cannot edit it", denied(rc, out), out)

# Seat-planted git config must not run as Odysseus.
marker, hook = str(ws.main / ".git" / "fsmonitor-ran-as"), str(ws.main / ".git" / "probe-hook")
rc, out = as_seat(f'printf "#!/bin/sh\\nid -un > {marker}\\n" > {hook} && chmod 755 {hook} && '
                  f'git config core.fsmonitor {hook}', **S1, cwd=str(ws.main))
ws.diff_attachment("chair", base)
with open(marker) as fh:
    ran_as = fh.read().strip()
check("a core.fsmonitor a seat set in main/.git/config runs as the run's seat user, not as Odysseus",
      rc == 0 and ran_as == user_name(ws.user), f"ran as {ran_as}")
as_seat("git config --unset core.fsmonitor", **S1, cwd=str(ws.main))

# The GitHub push: the token is in the environment of Odysseus's git.
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(5)
held = []
threading.Thread(target=hold_connections, args=(listener, held), daemon=True).start()
github_access.GIT_BASE = f"http://127.0.0.1:{listener.getsockname()[1]}"     # never github.com
token = "github_pat_PROOF" + "x" * 30
github_access._writable = lambda owner, repo: ({"default_branch": "main"}, token)
result = {}
pusher = threading.Thread(target=lambda: result.update(
    out=attempt(lambda: github_access.push_branch("owner", "owner/repo", str(ws.main), ws.branch,
                                                   seat=ws.user))), daemon=True)
pusher.start()
pushing = []
for _ in range(200):
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                if b"GIT_CONFIG_VALUE_0=Authorization: Basic" in fh.read():
                    pushing.append(pid)
        except OSError:
            pass
    if pushing:
        break
    time.sleep(0.05)
check("Odysseus's push is running git with the token in its environment", bool(pushing), f"pids {pushing}")
for pid in pushing:
    with open(f"/proc/{pid}/cmdline", "rb") as fh:
        cmdline = fh.read().replace(b"\0", b" ").decode()
    rc, out = as_seat('cat "/proc/$1/environ"', pid)
    check(f"reading the environment of that git process ({cmdline.strip()[:60]}) is denied",
          denied(rc, out, "Authorization"), out)
    rc, out = as_seat('tr "\\0" " " < "/proc/$1/cmdline"', pid)
    check("its command line, which any user may read, holds no token", "PROOF" not in out and "Basic" not in out, out)
listener.close()
for conn in held:
    conn.close()
pusher.join(timeout=60)
print(f"        push ended: {result.get('out')}")

# The history MCP server: it runs as Odysseus, the seat reaches it through a socket.
sock = os.path.join(home1, "odysseus-history.sock")
bridge = council_tools._start_history_bridge(sock, council_tools.mcp_server_spec("admin"))
for _ in range(100):
    if os.path.exists(sock):
        break
    time.sleep(0.05)
hello = ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05",'
         '"capabilities":{},"clientInfo":{"name":"proof","version":"1"}}}\n')
rc, out = as_seat(f'(cat; sleep 5) | timeout 20 socat STDIO "UNIX-CONNECT:$1" | head -c 300', sock, **S1,
                  stdin=hello)
check("a seat reaches the history server through the socket, as its CLI would", '"serverInfo"' in out, out)
rc, out = as_seat('socat STDIO "UNIX-CONNECT:$1" </dev/null', sock, **S2)
check("another seat of the same run cannot connect to that socket", denied(rc, out), out)
os.killpg(bridge.pid, 15)
bridge.wait(timeout=10)

# Stop: Odysseus may not signal a seat process; it kills it as the seat user.
sleeper = subprocess.Popen(seat1.argv(["sleep", "300"]), env=seat_user.environment(), start_new_session=True)
time.sleep(0.5)
try:
    os.killpg(sleeper.pid, 9)
    direct_kill = "allowed"
except PermissionError as exc:
    direct_kill = f"PermissionError: {exc}"
seat1.kill(sleeper.pid)
check("Odysseus cannot signal a seat process itself, and kills it through the launcher",
      direct_kill.startswith("PermissionError") and sleeper.wait(timeout=10) == -9, direct_kill)
# A process that left its session is out of reach of that, not of the end of the call.
as_seat("setsid sleep 300 >/dev/null 2>&1 &", **S1)
as_seat("setsid sleep 300 >/dev/null 2>&1 &", **S2)
time.sleep(0.5)
before = running_as(seat1)
seat1.kill_all()
time.sleep(0.5)
check("every process a seat left running, in whatever session, is killed with its user; other seats' are not",
      bool(before) and not running_as(seat1) and bool(running_as(S2["seat"])),
      f"before {before}, after {running_as(seat1)}, seat-2 {running_as(S2['seat'])}")

print("\n== seats and each other")
holder = subprocess.Popen(seat1.argv(["sleep", "300"]), start_new_session=True,
                          env=seat1.env({"CLAUDE_CODE_OAUTH_TOKEN": "PROOF-seat-1-model-token"}))
time.sleep(0.5)
rc, out = as_seat('cat "/proc/$1/environ"', str(holder.pid), **S2)
check("a seat cannot read another seat's environment, where its model credential is",
      denied(rc, out, "PROOF"), "<withheld>" if "PROOF" in out else out)
rc, out = as_seat('head -c 16 "/proc/$1/mem"; kill -0 "$1"', str(holder.pid), **S2)
check("nor its memory, nor signal it", out.count("Permission denied") + out.count("not permitted") >= 2, out)
seat1.kill(holder.pid)
rc, out = as_seat('ls "$1"; ls "$2"; touch "$3/all__forged.md"', seat1.home, home1,
                  str(ws.messages / "outbox" / "seat-1"), **S2)
check("a seat cannot open another seat's home or call folder, or write in its outbox",
      out.count("Permission denied") == 3, out)
rc, out = as_seat('cat README.md && git log --oneline -1', **S2, cwd=str(ws.main))
check("the seats of one run do share its repository", rc == 0 and "chair: integrate" in out, out)
rc, out = as_seat('ls "$1"; ls "$2"; cat "$2/README.md"', str(ws.root), str(ws.main))
check("a seat that is not in the run cannot open the run's folder", out.count("Permission denied") == 3, out)
as_seat('mkdir -p "$HOME/.local/bin" && printf "#!/bin/sh\\necho kept\\n" > "$HOME/.local/bin/probe-tool" && '
        'chmod 755 "$HOME/.local/bin/probe-tool"', **S1)
rc, out = as_seat('probe-tool; python3 -m site --user-site', **call("seat-1"))
check("what a seat installed in its home is there, and on its PATH, in its next call",
      rc == 0 and out.startswith("kept") and seat1.home in out, out)
check("a workspace inside /app is out of a seat's reach; its own run folder is not",
      not seat_user.reaches("/app/static") and not seat_user.reaches(DATA)
      and seat1.reaches(str(ws.main), write=True) and not GUEST.reaches(str(ws.main)))

print("\n== the three engines' own launch code, with stand-in CLIs (no model is called)")
import asyncio  # noqa: E402

from src import claude_subscription as cs, codex_cli  # noqa: E402
from src.tool_execution import _direct_fallback, subprocess_env_overrides  # noqa: E402

# Secrets in Odysseus's own environment that no seat may inherit.
os.environ.update(ODYSSEUS_INTERNAL_TOKEN="PROOF-internal-token", GH_TOKEN="PROOF-gh-token",
                  DATABASE_URL="postgresql://odysseus:PROOF-db-password@db/odysseus")
STAND_IN = r"""#!/bin/sh
out="$HOME/launched-$(basename "$0")"
sys=""; prev=""
for a in "$@"; do [ "$prev" = --system-prompt-file ] && sys="$a"; prev="$a"; done
{ echo "user=$(id -un) cwd=$(pwd)"; echo "args=$*"; env | sort
  [ -n "$sys" ] && echo "system prompt: $(head -c 40 "$sys")"
  [ -n "${CODEX_HOME:-}" ] && echo "auth.json: $(head -c 30 "$CODEX_HOME/auth.json")"; } > "$out" 2>&1
cat > /dev/null
"""
for name in ("claude", "codex"):
    path = f"/tmp/stand-in-{name}"
    with open(path, "w") as fh:
        fh.write(STAND_IN)
    os.chmod(path, 0o755)
os.environ.update(CLAUDE_CLI_PATH="/tmp/stand-in-claude", CODEX_CLI_PATH="/tmp/stand-in-codex")


async def drain(agen):
    return [chunk async for chunk in agen]


seat = call("seat-1")["seat"]
home = seat.home


def engine_env_ok(text):
    return f"user={user_name(seat)}\n" in text.replace(" cwd=", "\n") and "PROOF-" not in text


overrides = {**council_tools.credential_free_git_env(tempfile.mkdtemp(dir=seat.folder)), **ws.env("seat-1"),
             **dict.fromkeys(council_tools.ODYSSEUS_API_ENV)}
asyncio.run(drain(cs.stream_chat(
    "https://claude-subscription.invalid/x", "claude-opus-5-5", [{"role": "user", "content": "q"}],
    credentials=(cs.AUTH_MODE_TOKEN, "sk-ant-oat01-SEAT-OWN-token"),
    tools=council_tools.claude_native_tools(council_tools.ToolSettings(
        mode="full", owner="admin", workspace=str(ws.workdir("seat-1")), env=overrides, seat=seat),
        {"command": "socat", "args": ["STDIO", "UNIX-CONNECT:/x.sock"]}))))
with open(os.path.join(home, "launched-stand-in-claude")) as fh:
    text = fh.read()
check("Claude Code CLI: runs as the seat, in its worktree, with only its token and the seat overrides; "
      "it reads its system prompt",
      engine_env_ok(text) and f"cwd={ws.workdir('seat-1')}" in text
      and "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SEAT-OWN-token" in text and "DATABASE_URL" not in text
      and "system prompt: You are" in text, text)
asyncio.run(drain(codex_cli.stream(codex_cli.CodexRun(
    model="gpt-x", prompt="q", access_token="SEAT-OWN-codex-token", sandbox=codex_cli.SANDBOX_FULL,
    cwd=str(ws.main), mcp_servers={"odysseus": {"command": "socat", "args": ["STDIO", "UNIX-CONNECT:/x.sock"]}},
    env=overrides, seat=seat))))
with open(os.path.join(home, "launched-stand-in-codex")) as fh:
    text = fh.read()
check("codex exec: runs as the seat, in main, with its own CODEX_HOME whose auth.json it reads",
      engine_env_ok(text) and f"cwd={ws.main}" in text and 'auth.json: {"OPENAI_API_KEY": null' in text
      and "DATABASE_URL" not in text and "mcp_servers.odysseus.command=\"socat\"" in text, text)

async def agent_tools():
    with subprocess_env_overrides(overrides, seat=seat):
        from src import tool_execution

        token = tool_execution._active_workspace.set(str(ws.workdir("seat-1")))
        try:
            bash = await _direct_fallback("bash", "id -un; pwd; env | sort")
            py = await _direct_fallback("python", "import os; print(os.getuid(), os.getcwd())")
        finally:
            tool_execution._active_workspace.reset(token)
    return bash, py

bash, py = asyncio.run(agent_tools())
check("agent loop bash: runs as the seat in the worktree, with the seat environment only",
      bash["output"].startswith(f"{user_name(seat)}\n{ws.workdir('seat-1')}") and "PROOF-" not in bash["output"]
      and "DATABASE_URL" not in bash["output"], bash["output"])
check("agent loop python: runs as the seat in the worktree",
      py["output"].strip() == f"{seat_user.BASE_ID + seat.slot} {ws.workdir('seat-1')}", py["output"])

print("\n== the network")
FETCH = ("import sys, urllib.request, urllib.error\n"
         "try:\n    print('HTTP', urllib.request.urlopen(sys.argv[1], timeout=20).status)\n"
         "except urllib.error.HTTPError as e:\n    print('HTTP', e.code)\n"
         "except Exception as e:\n    print(type(e).__name__, getattr(e, 'reason', e))\n")
with open("/tmp/probe-fetch.py", "w") as fh:
    fh.write(FETCH)
own = subprocess.run([sys.executable, "/tmp/probe-fetch.py", "http://127.0.0.1:7000/api/council/sessions"],
                     capture_output=True, text=True).stdout.strip()
rc, out = as_seat('python3 /tmp/probe-fetch.py http://127.0.0.1:7000/api/council/sessions; '
                  'python3 /tmp/probe-fetch.py "http://$(hostname -i | cut -d" " -f1):7000/"', **S1)
check("the Odysseus API answers Odysseus (401 without credentials) and refuses a seat, on localhost and on the "
      "container's own address", own == "HTTP 401" and out.count("Connection refused") == 2, f"own: {own}\n{out}")
rc, out = as_seat('python3 /tmp/probe-fetch.py https://pypi.org/simple/pip/', **S1)
check("a seat reaches the internet, names resolved", out == "HTTP 200", out)
rc, out = as_seat(f'cd /tmp && (python3 -m http.server {seat_user.LOCAL_PORTS_FROM + 4000} --bind 127.0.0.1 >/dev/null 2>&1 &) '
                  f'&& (python3 -m http.server 8123 --bind 127.0.0.1 >/dev/null 2>&1 &) && sleep 2 && '
                  f'python3 /tmp/probe-fetch.py http://127.0.0.1:{seat_user.LOCAL_PORTS_FROM + 4000}/; '
                  'python3 /tmp/probe-fetch.py http://127.0.0.1:8123/', **S1)
check("a seat reaches its own test server on a high localhost port, and none below",
      out.splitlines()[:1] == ["HTTP 200"] and "Connection refused" in out, out)
rc, out = as_seat(f'python3 /tmp/probe-fetch.py http://127.0.0.1:{seat_user.LOCAL_PORTS_FROM + 4000}/', **S2)
print(f"        (another seat reaches that test server too: {out})")
gateway = subprocess.run("getent ahostsv4 host.docker.internal | awk 'NR==1 {print $1}'", shell=True,
                         capture_output=True, text=True).stdout.strip() or "172.17.0.1"
listening = socket.socket()
listening.settimeout(5)
own = listening.connect_ex((gateway, 9))
listening.close()
rc, out = as_seat(f'python3 /tmp/probe-fetch.py http://{gateway}:9/', **S1)
check(f"a seat is refused at the Docker host's private address {gateway} before anything leaves the container",
      "Connection refused" in out, f"{out} (Odysseus itself: errno {own})")

print("\n== the end of the run")
taken = set(seat_user._held)
ws.close()
check("closing the run kills its users' processes and gives the users back",
      not any(running_as(seat) for seat in (seat1, S2["seat"], CHAIR["seat"])) and set(seat_user._held) == {GUEST.slot},
      f"held before {sorted(taken)}, after {sorted(seat_user._held)}")
with seat_user.user() as later:
    rc, out = as_seat('ls "$1"; cat "$1/main/README.md"', str(ws.root), seat=later)
    check("a seat user taken afterwards cannot open the finished run", out.count("Permission denied") == 2, out)
with seat_user.visitor(str(ws.root)) as visiting:
    proc = cw.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{ws.branch}^{{commit}}"], ws.main, check=False,
                  seat=visiting)
    check("Odysseus still reads the finished run's branch, as a visiting seat user (for a push approved later)",
          proc.returncode == 0 and len(proc.stdout.strip()) == 40, proc.stdout + proc.stderr)
check("and the folder is closed again after the visit", oct(os.stat(ws.root).st_mode & 0o7777) == "0o700",
      oct(os.stat(ws.root).st_mode & 0o7777))
seat_user.release(GUEST)

print()
print(f"{len(FAILED)} check(s) failed: {FAILED}" if FAILED else "All checks passed.")
sys.exit(1 if FAILED else 0)
