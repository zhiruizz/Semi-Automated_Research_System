# Semi-Automated Research System

This repository contains Phases 1–3 of the Research Controller: a deterministic,
auditable, crash-recoverable control plane for local research computation and
typed Agent execution. The only Agent backend is a durable external MockAgent;
Hermes, Codex, and school compute are deliberately not connected yet.

## What works

- SQLite with WAL, foreign keys, and a busy timeout.
- The six core entities: Project, Task, Event, AgentRun, ComputeJob, Artifact.
- Relational task dependencies and task/artifact links.
- Audited Task and ComputeJob transitions. State mutation and Event append share
  one database transaction, and ORM guards reject direct status mutation.
- Audited AgentRun transitions with the same transaction and direct-mutation
  protection.
- Strict Pydantic v2 compute protocols with unknown fields rejected.
- Strict versioned AgentTaskSpec, AgentResult, decision, transition request,
  context, route, and external observation protocols.
- An Agent Gateway with role routing, Artifact-only context packs,
  NEW/RESUME_ROLE/EPHEMERAL sessions, and role-isolated resume lookup.
- A detached filesystem-backed MockAgent whose durable identity and idempotency
  key are the persisted AgentRun.id.
- Local CPU execution through an idempotent ComputeProvider.
- Deterministic, testable NVIDIA inventory and external-process discovery using
  `nvidia-smi`, without importing PyTorch.
- Durable exclusive single-GPU allocation backed by active ComputeJob rows.
- Two-GPU concurrency, normal third-task queueing, external-busy avoidance, and
  restart-safe GPU reservations.
- `research-runner`, which creates `manifest.json`, `run.out`, `run.error`, and
  atomically written `exit.json`.
- Content-addressed immutable local Artifact storage.
- Ordered Controller ticks and recovery at every local submission boundary.
- Runnable compute and Agent demos plus automated vertical-slice/crash tests.

## Setup

Python 3.11 or newer is supported.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Initialize the database:

```bash
.venv/bin/research-controller init-db
```

Create and execute the first demo Project and Task:

```bash
.venv/bin/research-controller demo --run
```

The demo executes `examples/count.py` with `gpu_count=0` and ends only after
`metrics.json`, logs, and `exit.json` have been ingested and validated. Inspect
the persisted state with:

```bash
.venv/bin/research-controller status
```

Run the typed Agent vertical slice without any real model/API call:

```bash
.venv/bin/research-controller agent-demo --run
```

The MockAgent runs in a detached process, writes one declared summary, returns a
strict AgentResult, and succeeds the Task only after Controller verification.

Run one tick, run until all current work is terminal, or operate continuously:

```bash
.venv/bin/research-controller run --once
.venv/bin/research-controller run --until-idle --timeout 30
.venv/bin/research-controller run --interval 1
```

Global paths may be overridden before the subcommand:

```bash
.venv/bin/research-controller \
  --database /srv/research/data/controller.db \
  --workspace /srv/research/workspace \
  demo --run
```

## Database schema

| Table | Purpose |
|---|---|
| `projects` | Macro research lifecycle, phase/stage, active artifact pointers, policy and budget placeholders |
| `tasks` | Intent, dependencies, acceptance/retry policy, attempts, lease, and verified result |
| `events` | Append-only per-Project audit records with unique monotonic `seq` |
| `agent_runs` | Typed Agent attempts, backend/session identity, durable external id, request/response Artifact links, result, timing, and failure details |
| `compute_jobs` | Local/remote-neutral execution attempts and independent observation state |
| `artifacts` | Immutable, hashed, versioned persisted outputs and evidence eligibility |
| `task_dependencies` | Explicit Task-to-Task prerequisites |
| `task_artifacts` | Explicit Task input/output Artifact links |

SQLite initialization uses SQLAlchemy metadata in Phase 1. Alembic migrations
are a known prerequisite before deployed schema evolution.

## State and recovery semantics

A Task follows:

```text
PENDING -> READY -> RUNNING -> VERIFYING -> SUCCEEDED / FAILED
```

An executor's zero exit code never directly succeeds the Task. The Controller
first collects immutable artifacts, enters `VERIFYING`, and runs the acceptance
validators. Unknown or illegal transitions are rejected.

Local submission is keyed by `submission_key`. The provider atomically writes a
durable reservation before process launch and records the launched process in a
provider-owned submission record. After an uncertain submit, the Controller
always calls `reconcile_submission()` first. It launches only when reconciliation
confirms that no launched submission exists.

The runner is detached from the Controller process and persists its terminal
record atomically. A restarted Controller can therefore recover when interrupted:

- after Task READY;
- after ComputeJob CREATED;
- after process submit but before saving the external id;
- while the process is RUNNING;
- after process exit but before Artifact collection.

Collection is safe to repeat: CAS writes and Artifact rows are deduplicated by
producer, logical name, and hash.

Agent execution uses the same recovery rule. The Controller first persists an
AgentRun in `STARTING`; only then may the adapter receive
`run_key=AgentRun.id`. Every STARTING recovery calls `reconcile(run_key)` before
`start()`. Mock state is stored under:

```text
workspace/.mock-agent/runs/<AgentRun.id>/
```

An atomic `launch.claim` lets only one detached runner execute, even across the
uncertain interval between process launch and saving `external_run_id`. Request,
raw response, start, exit, output, and launch-count records survive Controller
termination. A valid backend call makes AgentRun `SUCCEEDED`; Task success is a
separate decision:

```text
AgentRun SUCCEEDED -> Task VERIFYING -> Task SUCCEEDED / FAILED
```

A semantic `blocked`/`failed` result still means the AgentRun succeeded, while
process crashes, timeouts, and invalid typed responses fail the AgentRun.
Transition requests and requested tasks are validated and recorded only; they
never mutate Project stage or create Tasks automatically.

Produced paths must resolve inside the AgentRun work directory before immutable
Artifact ingestion. Required context must reference an existing verified
Artifact; missing required context blocks the Task before external launch.

## Local GPU allocation

Phase 2 supports `gpu_count=0` and `gpu_count=1`. A local job requesting more
than one GPU is structurally unsupported and is blocked with
`RESOURCE_UNAVAILABLE`; it is never silently assigned one GPU.

GPU allocation has two layers:

```text
nvidia-smi physical inventory and external-busy signals
  + active local ComputeJob rows (CREATED/SUBMITTED/PENDING/RUNNING)
  = effective ResourceSnapshot used by ComputeRouter
```

An active job's `resource_class` (`local_gpu_0`, `local_gpu_1`, ...) is the
durable exclusive reservation. The selected index, UUID, model, and allocation
type are stored in `provider_metadata_json`. No in-memory lease set or seventh
core entity is used. `COLLECTING` and terminal jobs no longer reserve the GPU
because reconciliation has confirmed the workload stopped using it.

Physical capability and current availability remain separate. An operational
20 GB GPU can be temporarily unschedulable because it is Controller-reserved or
externally busy. A physically capable but busy Task remains READY with a short
`not_before` backoff; a Task exceeding local memory capacity becomes BLOCKED.
Normal waiting does not emit repeated Events.

External processes are never killed or represented as fake ComputeJobs. The
default exclusive policy avoids a GPU when `nvidia-smi` reports a compute
process, at least 1,024 MB used memory, or at least 20% utilization. Policy
defaults are documented in `config/providers/local.yaml` and injectable for
tests/deployment configuration.

Once routing chooses `local_gpu_N`, `prepare()` does not rediscover or reselect
hardware. It deterministically sets:

```text
CUDA_VISIBLE_DEVICES=N
```

CPU Tasks continue through `local_cpu` regardless of GPU reservations.

## Controller tick order

Every tick preserves this order:

1. Reconcile already-known ComputeJobs and AgentRuns, regardless of Project pause.
2. Recover expired leases.
3. Reconcile CREATED compute submissions and STARTING AgentRuns.
4. Reconcile dependencies into READY.
5. Verify completed compute and Agent work.
6. Evaluate deterministic project gates (Phase 1 hook only).
7. Dispatch new active-project Compute Tasks, then Agent Tasks.

External I/O is not awaited inside a long database transaction. Providers and
Agent adapters receive immutable views, never ORM objects.

## Tests

```bash
.venv/bin/pytest -q
```

The suite covers transition legality and transaction rollback, direct mutation
guards, dependencies, idempotency, lease recovery, strict schemas, Artifact
hashing/immutability, successful and failed local jobs, uncertain submission
reconciliation, all persisted recovery boundaries, and an actual Controller
subprocess `SIGKILL` followed by restart. Phase 2 adds pure `nvidia-smi` parsing,
external-busy policy, typed route failure, memory limits, unsupported multi-GPU,
two-GPU concurrency, third-task queueing, CPU independence, exact
`CUDA_VISIBLE_DEVICES`, CREATED/uncertain-submit reservations, cancellation
safety, and a real Controller `SIGKILL` while two fake-GPU jobs remain active.
Phase 3 adds strict Agent/decision protocols, permissions, path sandboxing,
context validation, session resume and role isolation, semantic-vs-backend
failure, request/response Artifacts, STARTING/uncertain/RUNNING/result recovery,
active AgentRun lease protection, exactly-once launch, and a real Controller
SIGKILL while the detached MockAgent survives.

## Repository layout

```text
config/                     Controller and local provider configuration
examples/count.py           Phase 1 deterministic demo workload
src/research_controller/
  artifacts/                Immutable content-addressed store
  agents/                   Gateway, router, context/session policy, durable MockAgent
  compute/                  Provider interface/router, LocalProvider, NVIDIA parser
  db/                       SQLAlchemy models, engine, session guards
  domain/                   Enums and opaque ids
  protocols/                Strict Pydantic compute and Agent protocols
  runner/                   Durable process wrapper
  services/                 Event, transition, readiness, dispatch, reconcile, validation
  cli.py                    CLI entrypoint
  controller.py             Ordered Controller tick
tests/                      Unit, integration, and crash-recovery tests
```

## Explicitly deferred

- Hermes and Codex adapters and escalation (Phases 4–6); Phase 3 routes both
  logical executors to MockAgent and records `backend=mock` truthfully.
- MUST VPN, Paramiko, Slurm, dynamic school resources, and B20X (Phases 7–8).
- Scientific workflow, Experiment Contract, paper flow, Web UI, and migrations.

No credentials, remote submission code, or real GPU workload are included.

## Current Agent limits

- MockAgent is the only adapter; no SDK, real model name, token use, or network
  model call is present.
- FORK_ROLE is protocol-compatible but currently uses new-session semantics.
- Context FULL/METADATA/OMIT are implemented; SUMMARY and EXCERPT remain metadata
  placeholders without LLM summarization.
- Requested Tasks, transition requests, protocol amendments, and escalation
  flags are recorded but never executed automatically.
- Agent retry/escalation policy and scientific stage gates remain deferred.

## Current local GPU limits

- Single Controller only; multi-controller allocation is not guaranteed.
- Each local job supports zero or one GPU; no local multi-GPU training.
- Exclusive whole-GPU reservations only; no MIG, MPS, fractional sharing, or
  memory packing.
- External-busy detection is conservative and snapshot-based.
- No preemption: cancel sends SIGTERM, and the reservation remains until poll
  confirms the process is terminal.
