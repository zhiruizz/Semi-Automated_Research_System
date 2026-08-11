# Semi-Automated Research System

This repository contains Phase 1 of the Research Controller: a deterministic,
auditable, crash-recoverable control plane for local research computation.
Agents and school compute are deliberately not connected yet.

## What works

- SQLite with WAL, foreign keys, and a busy timeout.
- The six core entities: Project, Task, Event, AgentRun, ComputeJob, Artifact.
- Relational task dependencies and task/artifact links.
- Audited Task and ComputeJob transitions. State mutation and Event append share
  one database transaction, and ORM guards reject direct status mutation.
- Strict Pydantic v2 compute protocols with unknown fields rejected.
- Local CPU execution through an idempotent ComputeProvider.
- Optional deterministic GPU inventory using `nvidia-smi` (GPU leasing belongs
  to Phase 2 and is not claimed here).
- `research-runner`, which creates `manifest.json`, `run.out`, `run.error`, and
  atomically written `exit.json`.
- Content-addressed immutable local Artifact storage.
- Ordered Controller ticks and recovery at every local submission boundary.
- A runnable count demo and automated vertical-slice/crash tests.

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
| `agent_runs` | Reserved Phase 1 schema for future typed Agent attempts; no adapter is active |
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

## Controller tick order

Every tick preserves this order:

1. Reconcile already-known ComputeJobs.
2. Recover expired leases.
3. Reconcile CREATED/uncertain submissions.
4. Reconcile dependencies into READY.
5. Verify completed work.
6. Evaluate deterministic project gates (Phase 1 hook only).
7. Dispatch new active-project Compute Tasks.

External I/O is not awaited inside a long database transaction. Providers
receive immutable `ComputeJobView` values, never ORM objects.

## Tests

```bash
.venv/bin/pytest -q
```

The suite covers transition legality and transaction rollback, direct mutation
guards, dependencies, idempotency, lease recovery, strict schemas, Artifact
hashing/immutability, successful and failed local jobs, uncertain submission
reconciliation, all persisted recovery boundaries, and an actual Controller
subprocess `SIGKILL` followed by restart.

## Repository layout

```text
config/                     Controller and local provider configuration
examples/count.py           Phase 1 deterministic demo workload
src/research_controller/
  artifacts/                Immutable content-addressed store
  compute/                  Provider interface/router and LocalProvider
  db/                       SQLAlchemy models, engine, session guards
  domain/                   Enums and opaque ids
  protocols/                Strict Pydantic compute protocols
  runner/                   Durable process wrapper
  services/                 Event, transition, readiness, dispatch, reconcile, validation
  cli.py                    CLI entrypoint
  controller.py             Ordered Controller tick
tests/                      Unit, integration, and crash-recovery tests
```

## Explicitly deferred

- Local GPU allocation/lease and two-GPU concurrency (Phase 2).
- Agent protocols, Agent Gateway, and MockAgent (Phase 3).
- Hermes and Codex adapters and escalation (Phases 4–6).
- MUST VPN, Paramiko, Slurm, dynamic school resources, and B20X (Phases 7–8).
- Scientific workflow, Experiment Contract, paper flow, Web UI, and migrations.

No credentials, remote submission code, or real GPU workload are included.
