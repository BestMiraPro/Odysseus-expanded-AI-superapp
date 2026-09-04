# Omnigent — Custom Agents, Saved Configs & Customizable Orchestrator

- **Date:** 2026-06-25
- **Status:** Draft (awaiting user review)
- **Owner area:** Odysseus → Omnigent integration

## 1. Summary

Let the user **define, configure, save, edit, and delete their own Omnigent agents**, choose
each agent's **backend** (Claude subscription / ChatGPT subscription / API), and **customize the
orchestrator's model**. Odysseus is the **control plane**; Omnigent remains the **execution
engine** — saved configs compile into a valid Omnigent `config.yaml` that runs through Omnigent.
Agents run with a **Claude-Code-grade environment and toolset**, not just the scoped Odysseus API
tools.

## 2. Goals / Requirements

1. Add agents manually and **persist** them (owner-scoped).
2. Per-agent **backend**: `claude-subscription`, `chatgpt-subscription`, `api`.
3. For `api`, the model is chosen from the user's existing **`ModelEndpoint`** configs (no raw
   keys re-entered per agent).
4. **Orchestrator** has a customizable `{ backend, model }`.
5. Agents run with the **environment quality and tools of Claude Code**: native
   Read/Write/Edit, Bash/shell, Glob/Grep, WebFetch/WebSearch, in a real workspace directory —
   layered with the scoped Odysseus tools.
6. Saved config **compiles to a runnable Omnigent `config.yaml`**.
7. The existing in-app "Launch crew" run uses the user's **saved agents** as the roster.

### Non-goals (v1)
- A real in-app multi-agent execution engine (do not rebuild Omnigent's orchestration).
- Live subscription execution from inside the Odysseus Docker container (subscription backends
  run through Omnigent in WSL2, where Claude Code / Codex are installed and logged in).

## 3. Current state (what exists today)

- `src/omnigent_manager.py` — wraps the external Omnigent CLI; **hardcoded** worker roster.
- `src/omnigent_native.py` — owner-scoped, JSON-persisted (`data/omnigent_runs.json`)
  **deterministic** crew runner with a fixed `WORKER_LIBRARY` + `PRESETS`. No per-agent model, no
  orchestrator-model setting, no real model calls.
- `routes/omnigent_routes.py` — `/api/omnigent/*` (status, server start/stop, sessions, workers,
  providers, capabilities, runs, bundle). `_providers()` already lists the three backends.
- `integrations/omnigent/config.yaml` — real Omnigent spec: `executor.config.harness: claude-sdk`
  + `prompt` + `tools` (the scoped Odysseus bridge tools).
- `static/js/omnigent.js` — modal UI: hero, goal+presets, fixed worker roster, timeline, recent
  runs, "Account and model options" (providers grid + ChatGPT device-flow link + advanced bridge).

Gap: nothing lets a user **create/save** an agent bound to a backend/model, or set the
orchestrator model.

## 4. Design

### 4.1 Data model (owner-scoped, persisted)

Stored in `data/omnigent_agents.json` (mirrors the native run store's load/save + owner-scoping
pattern), managed by a new `OmnigentAgentStore`:

```jsonc
{
  "agents": [
    {
      "id": "agent-xxxxxxxx",
      "owner": "admin",
      "name": "Researcher",
      "role": "Find context across docs, web, and Odysseus tools.",
      "backend": "api",                 // claude-subscription | chatgpt-subscription | api
      "model": { "endpoint_id": 12, "model": "kimi-k2.6" },  // api only; else null
      "enabled": true,
      "created_at": "...", "updated_at": "..."
    }
  ],
  "orchestrator": {                     // one per owner
    "owner": "admin",
    "backend": "claude-subscription",   // claude-subscription | chatgpt-subscription | api
    "model": null,                      // for api: { endpoint_id, model }
    "workspace": "",                    // optional project dir; default = per-run scratch
    "updated_at": "..."
  }
}
```

A small new class `OmnigentAgentStore` (in `src/omnigent_native.py` or a sibling
`src/omnigent_agents.py`) owns load/save/CRUD with atomic writes, mirroring
`NativeOmnigentManager`.

### 4.2 Backends and model sources

| Backend | Harness | Model source | Runtime |
|---|---|---|---|
| `claude-subscription` | `claude-sdk` (Claude Code) | subscription default (optional model hint) | Omnigent / WSL2 |
| `chatgpt-subscription` | `codex` | subscription default | Omnigent / WSL2 |
| `api` | `claude-sdk` w/ API model, or generic | a configured `ModelEndpoint` (+ model name) | in-app or Omnigent |

The API model list is the user's existing enabled `ModelEndpoint`s (owner-filtered), reusing the
same source as the "models" settings tab.

### 4.3 Claude-Code-grade environment & tools (Requirement 5)

Each agent and the orchestrator are compiled to run under a harness that provides Claude Code's
native tools, **two layers**:

1. **Native coding-agent tools** (from the harness): `Read`, `Write`, `Edit`, `Bash`/shell,
   `Glob`, `Grep`, `WebFetch`, `WebSearch`, todo tracking — i.e., Claude Code quality. For
   `claude-subscription`/`api` this is the `claude-sdk` harness; for `chatgpt-subscription` it's
   the Codex harness's equivalent file/shell tools.
2. **Odysseus scoped tools** (from `integrations/omnigent/config.yaml`): `odysseus_capabilities`,
   `odysseus_todos`, `odysseus_email_search`, `odysseus_memory`, `odysseus_calendar_events`,
   `odysseus_documents`, `odysseus_cookbook_tasks`.

Plus a **real workspace**: the compiled config sets a working directory (`workspace`, default a
per-run scratch dir; user-overridable to a project path) so agents can read/edit/run code like
Claude Code.

**Control-plane responsibility (what we build):** the compiled config requests the right harness,
the full native toolset, the workspace, and the Odysseus tools. **Runtime responsibility (user
env):** Claude Code / Codex installed and logged in where Omnigent runs (WSL2). The Omnigent
status panel already surfaces install/login state; we add a readiness hint for the chosen backend.

### 4.4 Compile to Omnigent `config.yaml`

New `GET /api/omnigent/agents/compile` renders a valid spec from saved agents + orchestrator:

- `executor.type: omnigent`, `executor.config.harness` ← orchestrator backend's harness; model
  injected for `api`.
- `prompt` ← orchestrator role + crew framing.
- `agents`/`workers` ← each enabled agent (name, role/prompt, harness/model per its backend).
- `tools` ← the Odysseus bridge tools (unchanged) + native tools enabled via the harness.
- `workspace`/`cwd` ← orchestrator workspace.

Returned as text (downloadable) and reused by the existing bundle/bridge run path.

### 4.5 API surface (new, owner-scoped, under `/api/omnigent`)

- `GET    /agents` — list the caller's agents.
- `POST   /agents` — create `{ name, role, backend, model?, enabled? }`.
- `PUT    /agents/{id}` — update.
- `DELETE /agents/{id}` — delete.
- `GET    /orchestrator` — get `{ backend, model, workspace }`.
- `PUT    /orchestrator` — update.
- `GET    /agents/compile` — return the compiled `config.yaml` text.
- `GET    /models` (or reuse existing) — enabled `ModelEndpoint`s for the dropdowns.

All require an authenticated request and use `owner_filter` / owner scoping like the rest.

### 4.6 UI (extends `static/js/omnigent.js`, existing patterns/CSS)

- **Orchestrator** control near the hero: backend `<select>` + model `<select>` (+ optional
  workspace field).
- **Agents** section replaces the fixed roster: list of saved agents (name + backend pill +
  model), **Add agent** → small modal form (name, role, backend, model), edit/delete per row.
- **Presets** become optional seeds: picking one creates editable agents rather than a fixed crew.
- "Launch crew" uses the saved enabled agents as the roster; a "View run config" action shows the
  compiled `config.yaml`.

### 4.7 Native run behavior

The deterministic native run stays as a quick in-app preview, but its roster is the user's saved
agents (and reflects the orchestrator). Real model-backed execution is via the compiled Omnigent
config. (A future increment may add native execution for `api` single agents.)

## 5. Security considerations

- Agents under the full toolset have **real shell + filesystem access in the Omnigent runtime
  sandbox** (WSL2) — acceptable for a single-user self-hosted instance; documented clearly.
- Odysseus bridge tools remain gated by the **scoped API token** the user mints (unchanged).
- All agent/orchestrator records are **owner-scoped**; no cross-user reads.
- No provider secrets stored per-agent; `api` references existing `ModelEndpoint`s.

## 6. Testing plan

- Unit: `OmnigentAgentStore` CRUD + owner isolation + atomic persistence.
- Unit: compile produces a valid Omnigent spec for each backend (harness/model/tools/workspace).
- API: owner-scoped CRUD, auth required, 404 on others' records.
- Frontend smoke: add/edit/delete agent, set orchestrator model, view compiled config.
- Regression: existing native run + bundle endpoints unaffected.

## 7. Assumptions to confirm at review

1. "Claude Code environment & tools" = the harness-native toolset (files/shell/search/web) +
   workspace + Odysseus tools, as in §4.3.
2. Persisted as a JSON store (vs a DB table) — chosen for consistency with the native run store
   and to avoid a migration.
3. Subscription backends execute via Omnigent in WSL2 (not from Docker); the control plane only
   guarantees the compiled config + readiness hints.
