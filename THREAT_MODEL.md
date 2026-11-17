# Threat Model

Odysseus is a **self-hosted AI workspace with privileged local access**. This document states the trust boundary so contributors can reason about security decisions without reading through the full auth and middleware stack.

## Trust Boundary

Odysseus is designed for **trusted users on a private network**, not public exposure. The README describes it as "treat it like an admin console" — that framing is accurate. A logged-in admin can execute shell commands, read and write files, send email, and control model serving. This is intentional. The threat model does not try to prevent admins from doing these things. It does try to prevent:

- Unauthenticated access
- Non-admins reaching admin-only capabilities
- The AI agent acting on instructions injected through untrusted content (web results, emails, fetched pages, memories)
- Internal services (ChromaDB, Ollama, SearXNG, etc.) being reachable from outside the host

## Roles and Capabilities

| Capability | Admin | Non-admin (default) |
|---|---|---|
| Chat with agent | ✓ | ✓ |
| Browser tool | ✓ | ✓ |
| Documents | ✓ | ✓ |
| Research mode | ✓ | ✓ |
| Image generation | ✓ | ✓ |
| Memory management | ✓ | ✓ |
| Shell / Python execution | ✓ | ✗ |
| File read / write | ✓ | ✗ |
| Email send / read | ✓ | ✗ |
| MCP tools | ✓ | ✗ |
| Calendar management | ✓ | ✗ |
| Token / webhook management | ✓ | ✗ |
| Model serving | ✓ | ✗ |
| Vault | ✓ | ✗ |
| Settings | ✓ | ✗ |

Non-admin defaults are in `core/auth.py:DEFAULT_PRIVILEGES`. Tool enforcement is in `src/tool_security.py:NON_ADMIN_BLOCKED_TOOLS`. Any tool whose name starts with `mcp__` is also blocked for non-admins. Admins always get full access regardless of stored privilege values.

## Authentication

- **Sessions:** bcrypt passwords, 7-day session tokens stored atomically in `data/sessions.json` via `core/atomic_io.py`.
- **2FA:** TOTP with 8 single-use backup codes. Verified after password check, before session issuance.
- **Reserved usernames:** request sentinels and the Default/Local storage owner cannot be registered or renamed into. Defined in `core/auth.py:RESERVED_USERNAMES`.
  - `internal-tool` is security-critical: `core/middleware.py:require_admin` treats any request where `request.state.current_user == "internal-tool"` as the in-process tool loopback and grants admin unconditionally. A real account with that name would silently pass every `require_admin` check.
- **Orphan sessions:** `validate_token` re-checks that the user record still exists on every call. A deleted user's cookie is dropped on next request rather than continuing to authenticate.

## Internal Tool Loopback

Agent tool calls reach admin-gated HTTP routes over an in-process HTTP loopback. The mechanism:

1. At app startup, `core/middleware.py` generates a random `INTERNAL_TOOL_TOKEN` via `secrets.token_hex(32)`. It is never persisted and never sent to clients.
2. Loopback requests carry `X-Odysseus-Internal-Token: <token>` or have `request.state.current_user` already set to `"internal-tool"` by the auth middleware.
3. `require_admin` recognises either signal and grants access without checking the session user.

The agent may be running in a non-admin user's session, but tool dispatch first calls `src/tool_security.py:owner_is_admin_or_single_user` to verify the session owner is an admin before issuing any loopback call. Non-admin users cannot invoke admin tools even via the agent.

## Prompt-Injection Hardening

External content that reaches the LLM is treated as untrusted via `src/prompt_security.py`:

- `untrusted_context_message(label, content)` wraps the content in a `user`-role message with a header block instructing the model not to follow instructions inside it. Content goes in as data, not as a system instruction.
- `UNTRUSTED_CONTEXT_POLICY` is a system-prompt preamble that states the same policy at the top of every session where untrusted data may appear.

**Untrusted surfaces that must go through this wrapper:** web search results, fetched URLs, emails (read), saved memories, skill text, notes, and any tool output sourced from outside the server. Injecting untrusted content directly into the system role is a security bug.

## Security Headers

`core/middleware.py:SecurityHeadersMiddleware` sets headers on every response:

- `X-Frame-Options: DENY` + `frame-ancestors 'none'` on all routes except tool-render iframes (which are sandboxed at the HTML level).
- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` everywhere.
- **CSP:** nonce-based `script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net`. `style-src 'unsafe-inline'` is intentionally kept — `static/index.html` ships inline `<style>` blocks and JS modules set `style=""` attributes at runtime. Inline styles do not execute script so the risk is visual-only. Removing this requires templating the HTML files and auditing all JS-set style attributes.

## Council Seats and Unattended Work Runs

Council seats are model agents that run tools. A work run (`routes/council_work_routes.py`) lets them work round after round without the owner, each with a shell, in a run folder. Odysseus holds the owner's GitHub token and carries out pull requests and merges the agents ask for only when the owner approves them in the page.

**Who runs what.** The Docker image has 64 seat users, `odysseus-seat-0` to `-63` (uid and gid 61100 and up), each in its own group only. Every process that runs a seat's tools starts as one of them, through the launcher `/usr/local/bin/odysseus-seat` (`docker/seat-launch.c`: setuid root, mode 4750, group `odysseus-launch`, of which only the app user is a member). A seat holds its user for as long as it runs, so no two seats running at the same time share one; a work run's seats and chairman each keep theirs for the whole run, and the run holds one more, which every git command Odysseus itself runs in the run folder uses once it has created it. That covers the Claude Code CLI, `codex exec`, the agent loop's `bash` and `python` tools and everything those start. When a seat's call ends, and when a run ends, every process still running as its user is killed. The agent loop's file tools (read, write, edit, apply_patch, grep, glob, ls) run inside Odysseus's process, confined to the workspace; what they write for a seat is made group-writable file by file, and Odysseus's umask is left as it is, so nothing else it writes changes mode. The history MCP server runs as Odysseus behind a socket in the seat's throwaway folder, which only that seat's user and Odysseus can open; the seat's CLI reaches it through `socat`. Run folders live in `/council-runs` (the `council-runs` volume), outside `/app`. A run folder belongs to the group of the run's own user, which is the primary group of that run's seats and of no other; each seat's home and outbox in it belong to the seat's own group. When the run ends the folder is closed to every seat user, and Odysseus reads it afterwards (for a push the owner approves later) as a seat user it lets in for that one read.

**The network.** The entrypoint, as root, sets `iptables` rules for the seat users before the app starts (this takes the `NET_ADMIN` capability, which the compose files grant; the app's own user never holds it). A seat reaches the internet. Private and link-local addresses are refused: the other services on the Compose network, the Docker host and its LAN. On localhost, and on the container's own address, only ports 32768 and up are open, where a seat's own test servers get their ports; Odysseus and what it starts listen below that. The name servers in `/etc/resolv.conf` stay reachable for DNS, and so does each `host:port` listed in `ODYSSEUS_SEAT_NET_ALLOW`. `src/seat_user.py` describes the layout.

A work run refuses to start unless a shell started as a seat user proves it is another uid, cannot list the data folder and cannot write the Docker socket, and unless a seat user is refused at a port of Odysseus's own on localhost (so a container started without `NET_ADMIN` runs no work run). Ordinary council questions with tools use the seat user whenever that check passes and run as before when it does not (outside the container); a workspace the seat user cannot reach is then refused.

**What a seat cannot do** (each tried in a container by `scripts/check_seat_isolation.sh`):

- Read or write anything under `/app`, which is mode 0750: the sessions file, `github_access.json` and `.app_key`, `auth.json`, the database, `logs/`, `/app/.ssh`, Odysseus's code. It gets "permission denied" at `/app`, whatever the permissions of the folders mounted below it (Docker Desktop's folder mounts enforce none).
- Read the environment or memory of Odysseus's processes, among them the git process that pushes with the token in its environment, attach to them with ptrace, or signal them. The token is not in any command line.
- Become root, the app user or another seat. It may not run the launcher; the launcher takes two numbers within the seat users' range and no user name, keeps no supplementary group but the seat's own, sets the real, effective and saved ids, leaves no capability and sets `no_new_privs`, so `su` and other setuid programs cannot raise it.
- Hold a credential other than its own model credential. Its environment is `PATH`, `LANG`, `LC_ALL`, `TZ`, its home, its model credential (the Claude OAuth token, or a `CODEX_HOME` whose `auth.json` holds a ChatGPT access token without the refresh token) and the git settings of the seat. No GitHub or SSH login, no Odysseus API token, no database URL.
- Make Odysseus run a program of its choosing through git. Repository config a seat writes (`core.fsmonitor`, filters, merge drivers, `gpg.program`, `include.path`, ...) runs as a seat user, because Odysseus's own git in the run folder runs as the run's seat user. Odysseus's push asks the run's main copy for the branch's commit and its object folder as a seat user too, then pushes from a clean folder of its own that only reads those objects.
- Change the folders Odysseus works in. The run folder, `seats/`, `messages/`, `messages/outbox/`, `sent/` and `rejected/` are not writable by the seat; it writes only in its worktree, in main and in its outbox, and delivered messages are read-only to it. Outbox files are opened without following links; misplaced messages are moved through folder descriptors that follow none.
- Approve its own request, or call Odysseus as anyone: it holds no Odysseus credential, a work run requires login for every caller, and the Odysseus API refuses its connections.
- Reach another seat. It cannot read another seat's environment or memory (where that seat's model credential is), signal it, open its home or throwaway folder, connect to its history socket, or write in its outbox. It cannot open the folder of a run it is not in, running or finished.
- Reach the services next to Odysseus: the Odysseus API, ChromaDB, SearXNG, ntfy, Omnigent's server, Ollama or anything else on the Docker host, unless `ODYSSEUS_SEAT_NET_ALLOW` names it.

**What remains open:**

- The shell is not sandboxed beyond the seat user's permissions. It can read what any user can read outside `/app` (the image, `/tmp` files left world-readable) and do anything in its run folder.
- A seat reaches the whole internet, and can send anything it holds there. On localhost it reaches whatever listens on port 32768 or above: other seats' test servers, and any service of Odysseus's that happened to get such a port (none is configured to).
- A seat holds its own model credential and can use or send it anywhere. A Claude token from `claude setup-token` is long-lived.
- The seats of one work run share its repository: one can write in another's worktree or in main's `.git`. The diff Odysseus attaches to each report, and the reviewers, are the check on that. Command lines in `/proc` are readable by every user, so a seat sees other seats' paths and arguments (never a credential: those travel in the environment or in files).
- 64 seat users: more seats than that at once are refused until one is free.
- The agent loop's file tools run as Odysseus. Inside a worktree a seat that swaps a folder for a link between a tool's path check and its open can redirect `read_file`, `write_file`, `edit_file`, `apply_patch` or `grep` to a file Odysseus can reach. This is a race, open to API and local seats (the CLI engines' tools run as the seat).
- Odysseus reads the owner's source folder as itself to clone it. A seat can write to that folder when its permissions let the seat user, which includes any folder mounted from Docker Desktop outside `/app`.
- Outside the container (a developer's machine) there is no seat user: seats run as Odysseus, and work runs do not start.

## Known Gaps

These are open, acknowledged, and contributor help is welcome:

1. **No shell/filesystem sandbox.** The agent `bash` and `read_file`/`write_file` tools run as the app process user with no network egress filtering or filesystem confinement. A successful prompt-injection reaching a shell-enabled admin session can make outbound requests to internal services. See #1058 for the sandbox proposal. Council seats in the Docker image run their shell as a separate user instead (see above), with the network still unfiltered.

2. **SSRF via `/api/v1/chat` `base_url` parameter.** A chat-scoped API token can supply an arbitrary `base_url`; the server forwards the LLM request to that host without validating the scheme or address. PR #1039 fixes this.

3. **`src/search/` partial consolidation.** `src.search.core` and `src.search.providers` correctly alias `services.search` via `sys.modules` replacement. `analytics`, `cache`, `content`, `query`, and `ranking` are still independent copies that can drift. The SSRF regression tests in `tests/test_webhook_ssrf_resilience.py` test `src.webhook_manager` directly (separate from search), so the safety net there is intact. See #1058.

4. **Token scopes are coarse.** There is no way to grant a session a subset of the owning user's privileges. Companion/mobile tokens carry either `chat` or `admin` scope with no per-capability granularity.
