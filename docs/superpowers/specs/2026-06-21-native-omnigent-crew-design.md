# Native Omnigent Crew Design

## Goal

Replace the current install-first Omnigent surface with a native Odysseus crew workspace that provides an Omnigent-like multi-agent experience inside Odysseus, without requiring the external Omnigent CLI for the default path.

## User Experience

The existing Omnigent sidebar item opens a native control room. A user can enter a goal, choose a crew preset, start a run, watch worker progress, and inspect outputs without leaving Odysseus. The default experience works when Odysseus has at least one usable model endpoint. External Omnigent, Claude CLI, Codex CLI, or bridge bundles remain optional advanced integrations instead of setup blockers.

## Native Crew Model

Odysseus owns the crew run. A run has an id, owner, goal, status, selected preset, worker list, timeline events, created time, updated time, and final summary. Workers are lightweight role definitions such as Architect, Researcher, Coder, Reviewer, and Executor. Each worker records status, model hint, current step, and output.

The first implementation can persist runs in a JSON state file under `data/` to avoid schema churn. The state file must be written atomically and scoped by owner. Later, if crew runs need richer querying, the same shape can migrate to database tables.

## Backend

The existing `/api/omnigent/*` namespace stays stable, but it becomes native-first.

Routes:

- `GET /api/omnigent/status`: reports native availability, configured model readiness, and optional external CLI status.
- `GET /api/omnigent/workers`: returns native worker presets and optional external workers.
- `GET /api/omnigent/sessions`: returns native crew runs in the same broad shape as the old session list.
- `POST /api/omnigent/runs`: creates a native crew run for the current user.
- `GET /api/omnigent/runs/{run_id}`: returns one owner-scoped run.
- `POST /api/omnigent/runs/{run_id}/start`: starts or simulates a run through the native manager.
- `POST /api/omnigent/runs/{run_id}/cancel`: cancels a run.

The existing external server start/stop endpoints can remain for compatibility, but the UI should not require them for normal use.

## Execution

The first shippable version should produce a useful native run without launching uncontrolled background agents. It should create a deterministic worker plan and timeline, then mark the run completed with a summary that tells the user which workers ran and what the next action is. This establishes the native state model, routes, and UI without over-coupling to chat streaming internals.

After the native shell is proven, workers can call existing Odysseus agent/chat internals one at a time with owner-scoped model endpoints and tool permissions.

## Frontend

`static/js/omnigent.js` becomes the native crew UI. It keeps the modal registration and sidebar hooks, but replaces the install/setup-first content with:

- Goal composer.
- Preset buttons for Balanced, Build, Research, and Review.
- Worker roster.
- Run timeline.
- Current and recent runs.
- Optional advanced section for external Omnigent bridge details.

The UI should retain the current styling family and avoid nested card-heavy layout changes. It should show setup guidance only when there is no configured model endpoint or when the user explicitly opens advanced bridge details.

## Error Handling

All run access is owner-scoped. Missing runs return 404. Invalid goals return 400. If no model endpoint is configured, the status response explains that native crew planning can still be prepared, but model-backed execution requires adding an endpoint. External Omnigent CLI failures stay non-fatal and are shown as optional external status only.

## Testing

Add focused tests for:

- Native run creation, listing, owner scoping, start, and cancel.
- Status response showing native mode even when the external CLI is absent.
- Worker roster including native workers.
- Static UI no longer presenting Omnigent as install-first, and including goal composer, presets, timeline, and run list.

Existing Omnigent bundle tests should continue to pass so advanced bridge downloads remain available.
