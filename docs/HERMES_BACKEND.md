# Hermes Backend

Phase 4 connects production `HERMES` Tasks for `implementation_worker` and
`debug_worker` to the local Hermes asynchronous Runs API. It does not implement
Codex, automatic escalation, school compute, or scientific workflow policy.

## Configuration and secrets

The non-secret defaults live in `config/hermes.yaml`:

```yaml
base_url: http://127.0.0.1:8642
api_key_env: SARS_HERMES_API_KEY
```

Set the bearer value only in the Controller environment:

```bash
export SARS_HERMES_API_KEY='...'
```

`SARS_HERMES_BASE_URL` may override the endpoint for tests or deployment. The
key value is read just before HTTP use and is never serialized. Status output
shows the environment variable name, not its value.

Check the server without starting an Agent run:

```bash
.venv/bin/research-controller hermes status
.venv/bin/research-controller hermes models
```

The adapter requires live `run_submission` and `run_status` capabilities. It
does not fall back to Chat Completions when Runs is absent. Capability results
are cached for 60 seconds by default.

## Data flow and result contract

The Controller persists `AgentRun STARTING`, builds an Artifact-only context
manifest, creates an isolated work directory, then delegates one POST to the
detached StartBridge. Hermes receives a stable prompt with role, objective,
instructions, workspace, manifest, deliverables, permissions, and final result
sections. Large context files are referenced through `context_manifest.json`,
not inlined.

All deliverables must be below:

```text
workspace/<Project.id>/agent-runs/<AgentRun.id>/outputs/
```

Hermes must both write `outputs/agent_result.json` and end its final output with:

```text
<<<SARS_AGENT_RESULT_V1>>>
{strict agent-result/v0.1 JSON}
<<<END_SARS_AGENT_RESULT_V1>>>
```

The two JSON values are canonicalized and must be identical. Missing markers,
Markdown-only JSON, malformed JSON, trailing text, schema errors, and mismatches
are rejected without repair. The raw final output is ingested as
`RAW_AGENT_RESPONSE`; a valid parsed value is separately ingested as
`AGENT_RESPONSE`. Neither is automatically scientific Evidence.

## Recovery and idempotency boundary

The inspected Hermes 0.19.0 `POST /v1/runs` creates a fresh UUID on every call.
Its in-memory idempotency cache is not applied to Runs, and live capabilities do
not advertise durable client-key lookup. The Controller therefore stores:

```text
workspace/.hermes-bridge/<AgentRun.id>/
  request.json
  launch.claim
  response.json | error.json
```

The bridge obtains `launch.claim` with an exclusive filesystem create and is
detached from the Controller. Controller restart reconciles these records before
calling `start()`. Tests SIGKILL a real Controller process and confirm one fake
remote run and one POST.

This guarantee is deliberately bounded: Controller-process SIGKILL is safe, but
a host-level crash after Hermes accepts a run and before `response.json` is
atomically stored cannot be proven exactly-once. A dropped/uncertain start is
classified `START_STATE_UNCERTAIN`, blocks the Task, and is never automatically
retried. A known remote run returning 404 similarly blocks with
`REMOTE_RUN_NOT_FOUND`.

## Sessions, models, usage, and cancellation

- `NEW` and `EPHEMERAL` omit an old session id; the real returned id is stored.
- `RESUME_ROLE` reuses only a successful, non-ephemeral run with the same
  Project, role, backend, and model tier.
- A tier change starts a new session. `cheap` and `strong` mappings exist but
  default to the current Hermes model; no provider/model is hard-coded.
- `FORK_ROLE` currently starts conservatively without reuse. The API client has
  a fork primitive, but Controller fork policy remains deferred.
- Exact input/output/cache token counts are stored when the server reports them.
  Cost remains unset because no price table is assumed.
- Timeout sends `/stop` once and continues polling. Only observed remote
  `cancelled` changes the AgentRun to `TIMEOUT`; sending stop is not terminal.

Unknown remote status values remain non-terminal and record
`UNKNOWN_REMOTE_STATUS`. Temporary poll transport failure is an observability
failure, not proof that the Agent failed.

## Approvals

The Controller never auto-approves a Hermes tool request. A live
`waiting_for_approval` state maps to `WAITING_APPROVAL` and appends a bounded
`AGENT_APPROVAL_REQUIRED` Event. Operators may choose only one-time approval or
denial:

```bash
.venv/bin/research-controller agent approvals
.venv/bin/research-controller agent approve <AgentRun.id>
.venv/bin/research-controller agent deny <AgentRun.id>
```

CLI decisions append `HUMAN_AGENT_APPROVAL`. Session/permanent approval choices
are intentionally not exposed.

## Tests and explicit real calls

The normal suite uses a local `FakeHermesApiServer` and makes zero real model
calls:

```bash
.venv/bin/pytest -q
```

Real integration is opt-in and should use the tiny fixture/workspace only:

```bash
SARS_HERMES_INTEGRATION=1 \
SARS_HERMES_API_KEY='...' \
.venv/bin/pytest -q -m hermes_integration
```

The explicit CLI demo is also small:

```bash
.venv/bin/research-controller hermes-demo --real --run
```

Do not run either command against an untrusted endpoint. Phase 4 does not
modify, upgrade, or reconfigure the Hermes installation or its default model.
