# Codex Backend

Phase 5 routes production `CODEX` roles through the installed Codex App Server
over stdio JSONL. There is one production driver: the adapter does not use
`codex exec`, maintain a parallel SDK implementation, or fall back to Hermes or
MockAgent. Supported roles are `scientific_supervisor`, `experiment_planner`,
`result_reviewer`, `paper_writer`, `paper_reviewer`, and `integrity_reviewer`.
This backend supplies typed execution infrastructure only; it does not implement
scientific workflow gates or automatic escalation.

## Environment and configuration

Non-secret defaults are in `config/codex.yaml`. A null model and effort use the
default returned by live `model/list`; workflows refer to the logical
`supervisor` tier, not a hard-coded model name. An explicitly configured
model/effort is validated against discovery and is never replaced by a fuzzy
match.

Read-only status calls never log in, log out, alter global configuration, or
upgrade Codex:

```bash
.venv/bin/research-controller codex status
.venv/bin/research-controller codex models
```

Output contains only CLI/runtime information, authentication category, default
model/effort, and model inventory. Account email and credentials are neither
returned nor persisted. The adapter does not modify `~/.codex/config.toml`.

## Data flow and durable identity

```text
Task READY -> AgentGateway -> AgentRun STARTING -> CodexAdapter
  -> detached CodexStartBridge -> App Server Thread -> App Server Turn
  -> raw final message -> strict AgentResult -> Controller validation
  -> AgentRun SUCCEEDED -> Task VERIFYING -> Task SUCCEEDED/FAILED
```

The authoritative mapping is:

```text
AgentRun.session_id      = Codex thread.id
AgentRun.external_run_id = Codex turn.id
```

`NEW` starts a thread. `RESUME_ROLE` resumes only a successful non-ephemeral
thread from the same Project, role, backend, and model tier, then starts a new
turn. Reviewer roles use `NEW` by configuration. A tier change starts a new
thread. `EPHEMERAL` identities remain auditable but are excluded from resume
lookup. `FORK_ROLE` is explicitly deferred rather than silently treated as
resume or new.

The App Server session-tree id, selected model/effort, and instruction-source
paths are retained as bounded metadata when available. Instruction file
contents and hidden reasoning are not copied into Events or Artifacts.

## Workspace, context, and sandbox

Every turn uses this directory as process and protocol `cwd`:

```text
workspace/<Project.id>/agent-runs/<AgentRun.id>/
  inputs/
  outputs/
  result/
  context_manifest.json
  prompt.txt
  result_schema.json
  raw_response.txt
  parsed_result.json
```

The SARS source repository is never the turn working directory. ContextBuilder
accepts only verified Artifacts. The adapter hash-checks every immutable CAS
object, copies it (never hardlinks it) into `inputs/`, marks the copy read-only,
and hash-checks it again before result acceptance. The source CAS is never in a
writable sandbox root.

Tasks without filesystem-write permission use the native read-only policy.
Permitted tasks use `workspaceWrite`; output/result paths are listed as writable
roots. Network is false by default and becomes true only through the typed task
permission. `dangerFullAccess` is rejected with
`CODEX_FULL_ACCESS_FORBIDDEN`.

The installed sandbox is workspace-granular, so run-root context copies are a
disposable enforcement boundary. The immutable source remains outside it, and
post-turn hashes detect modification of the copy. This is not a claim of a
fine-grained read-only bind mount.

## Structured output and artifacts

Domain Pydantic schemas are not sent directly to the App Server. The explicit,
versioned `CodexStructuredOutputAdapter` projects `AgentResult` and
`DecisionResult` onto conservative Codex wire models. Paths cross as strings;
open metadata mappings cross as required nullable JSON strings; every object is
closed and requires every declared property. A local compatibility validator
rejects unsupported `format`, open mappings, incomplete `required` sets, and
residual Pydantic annotations before authentication, staging, bridge creation,
or any remote call.

Inspect the exact final schema without starting a model turn:

```bash
.venv/bin/research-controller codex schema agent-result
.venv/bin/research-controller codex schema decision-result --json
```

The final agent message is decoded once, validated against its wire model,
deterministically converted, and authoritatively validated as the generic domain
model. Existing Controller permission and path checks then run unchanged.
Markdown fences, unknown fields, malformed JSON, and schema violations fail
without regex extraction, LLM repair, or retry.

The Controller records three separate immutable artifacts:

- `AGENT_REQUEST`, including prompt, domain/wire/final-schema, adapter-version,
  instruction, and sandbox hashes/metadata;
- `RAW_AGENT_RESPONSE`, containing only the final assistant message, not streamed
  reasoning or chain-of-thought;
- normalized `AGENT_RESPONSE` from the validated Pydantic value.

Produced files remain non-evidence by default. Transition requests, requested
tasks, protocol amendments, and escalation flags are validated and recorded but
cannot directly mutate Project state, create Tasks, or invoke escalation.

## Approvals, interruption, and usage

The bridge owns the bidirectional connection and durably records bounded
command/file approval metadata. Nothing is automatically approved. The generic
CLI exposes only:

```bash
.venv/bin/research-controller agent approvals
.venv/bin/research-controller agent approve <AgentRun.id>  # App Server accept
.venv/bin/research-controller agent deny <AgentRun.id>     # App Server decline
```

Session-wide approval, exec-policy amendments, and permanent network rules are
not exposed. The bridge can survive a Controller restart while approval is
pending and consume the later durable decision.

Cancel and timeout each create one durable interrupt request. The bridge sends
`turn/interrupt` once and waits for terminal `interrupted`; Controller intent
distinguishes `CANCELLED` from `TIMEOUT`. Sending an interrupt is not terminal.

Usage prefers `thread/tokenUsage/updated.tokenUsage.last`, which is turn-local.
Input, cached input, output, and reasoning-output counts are stored per AgentRun.
The cumulative thread total is not charged again to resumed runs, and no dollar
cost is estimated without an authoritative price table.

## StartBridge and recovery boundary

Bridge state is under:

```text
workspace/.codex-bridge/<AgentRun.id>/
  request.json
  launch.claim
  thread.json
  turn.json
  approval.json | approval-resolved.json
  interrupt-request.json | interrupt.sent.json
  usage.json
  terminal.json | error.json
  trace.json
```

The detached bridge exclusively creates `launch.claim`, owns the App Server
connection, and atomically persists identity and terminal records. A Controller
SIGKILL does not kill it. Restart reconciles the same AgentRun/Thread/Turn and
never dispatches another active Task attempt.

Every prompt begins with `SARS_AGENT_RUN_ID=<AgentRun.id>`. If `turn/start` may
have succeeded but its response was lost after a known thread was saved, the
bridge reconnects, calls `thread/read(includeTurns=true)`, and attaches only when
exactly one turn contains that marker. It never sends a second `turn/start`.

There is no global exactly-once claim. If thread creation may have succeeded but
no thread id was durably recorded, recovery cannot safely enumerate a unique
thread. The bridge records `CODEX_START_STATE_UNCERTAIN`, blocks the Task, and
does not retry. A missing known thread similarly blocks with
`CODEX_THREAD_NOT_FOUND` rather than silently starting over.

## Tests and real-call budget

Default pytest uses `tests/fake_codex_app_server.py` and performs zero real Codex
turns. It covers auth/models, native schema, workspaces/sandbox, context copies,
sessions and isolation, approvals, usage, cancellation/timeout, invalid results,
lost responses, result collection recovery, and a real Controller subprocess
SIGKILL with exactly one fake `turn/start`.

One tiny facts-fixture turn is explicit opt-in:

```bash
SARS_CODEX_INTEGRATION=1 .venv/bin/pytest -q -m codex_integration
```

The equivalent CLI demo is:

```bash
.venv/bin/research-controller codex-demo --real --run
```

Phase 5 tests protocol correctness and recovery, not scientific quality.
