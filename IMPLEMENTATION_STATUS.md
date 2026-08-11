# Implementation Status

Updated: 2026-08-11

## Phase 0

- [x] Inspected repository; it was empty and had no Git metadata.
- [x] Confirmed there was no existing DB/task/agent/artifact/Slurm code to reuse.
- [x] Recorded scope and deviations in `IMPLEMENTATION_PLAN.md`.

## Phase 1 Definition of Done

- [x] SQLite DB initializes with WAL, foreign keys, and busy timeout.
- [x] All six core tables exist; AgentRun is schema-only in this phase.
- [x] Task status changes are guarded and routed through TransitionService.
- [x] State change and Event append commit/rollback atomically.
- [x] Task dependencies work.
- [x] Expired Task leases recover without duplicating known external work.
- [x] ComputeTaskSpec uses strict Pydantic v2 validation.
- [x] LocalProvider discovers CPU and optionally inventories NVIDIA GPUs.
- [x] research-runner creates logs, manifest, and atomic exit.json.
- [x] Local processes produce deterministic JobObservation values.
- [x] ArtifactStore uses SHA-256 content-addressed immutable storage.
- [x] Task completion always passes through VERIFYING.
- [x] Controller restart does not duplicate a Task execution.
- [x] Uncertain submission reconciles by submission_key before retry.
- [x] Demo vertical slice passes.
- [x] Persisted crash-point and actual SIGKILL recovery tests pass.
- [x] README documents setup, operation, schema, and recovery.
- [x] No real secret or remote credential is present.

## Verified result

```text
16 passed
```

This includes a real Controller subprocess SIGKILL while its detached local job
is active, followed by restart, collection, verification, and exactly one
recorded workload launch.

## Phase 2 Definition of Done

- [x] `nvidia-smi` GPU/process parsing is pure and hardware-independent in tests.
- [x] Physical capability and current schedulability are separate.
- [x] Active local ComputeJobs durably reserve selected GPU resource classes.
- [x] External compute processes/memory/utilization prevent allocation by default.
- [x] Two single-GPU Tasks receive different GPUs concurrently.
- [x] A third Task remains READY/deferred while both GPUs are reserved.
- [x] The third Task starts automatically after a GPU is reconciled as released.
- [x] CPU Tasks are independent of GPU reservations.
- [x] `min_gpu_memory_gb` is enforced.
- [x] Local `gpu_count > 1` is explicitly unsupported and blocks structurally.
- [x] `CUDA_VISIBLE_DEVICES` exactly matches the selected `local_gpu_N`.
- [x] `prepare()` consumes the selected resource without rediscovery.
- [x] CREATED and uncertain-submit jobs retain reservations across restart.
- [x] Reconcile-before-retry and single-launch behavior remain intact.
- [x] Cancel does not release a GPU before terminal observation.
- [x] Busy queue backoff produces no repeated Event spam.
- [x] Original 16 Phase 1 tests still pass.
- [x] Complete suite passes: `33 passed`.
- [x] Real read-only inventory parsed two 20 GB NVIDIA GPUs.
- [x] No school compute, Agent Gateway, secret, or seventh core entity was added.

### Phase 2 verified result

```text
33 passed in 5.54s
```

The real host check found GPU indices 0 and 1, both NVIDIA GeForce RTX 3080 with
20,480 MB total memory. Two external compute processes and material memory usage
were present, so no real GPU workload was launched and the conservative policy
would mark both devices externally busy.

## Phase 3 Definition of Done

- [x] Strict versioned AgentTaskSpec, AgentResult, DecisionRequest,
  DecisionResult, and TransitionRequest protocols reject unknown fields.
- [x] AgentResult validates Task identity, required deliverables, artifact
  kind/name uniqueness, and requested-task/transition/amendment rights.
- [x] Produced Artifact paths are confined to the AgentRun work directory.
- [x] TransitionRequest is recorded and cannot directly change Project.stage.
- [x] RequestedTask values are recorded and cannot create Tasks automatically.
- [x] AgentRun state changes use TransitionService, append Events atomically,
  and reject direct ORM status mutation.
- [x] Agent Gateway, role Router, Artifact ContextBuilder, adapter Registry, and
  SessionManager are implemented without giving adapters ORM write access.
- [x] NEW, RESUME_ROLE, and EPHEMERAL work; resume is isolated by Project, role,
  backend, successful prior run, and non-ephemeral mode.
- [x] Agent request and raw/normalized response are immutable Artifacts.
- [x] MockAgent is a detached durable external process using AgentRun.id as its
  run key and an atomic launch claim for exactly-once workload execution.
- [x] STARTING, uncertain-start, RUNNING, result-before-collect, and expired-lease
  recovery are covered.
- [x] A real Controller subprocess SIGKILL leaves MockAgent alive; restart
  reconciles the same AgentRun, collects it, verifies the Task, and records one
  workload launch.
- [x] AgentRun success remains separate from Task success; semantic BLOCKED and
  FAILED remain separate from backend/protocol failures.
- [x] Existing work is reconciled before new Agent dispatch and remains observed
  when a Project is not ACTIVE.
- [x] `agent-demo --run` completes with request, response, and summary Artifacts.
- [x] All original Phase 1/2 tests still pass.
- [x] Complete suite passes: `66 passed`.
- [x] No Hermes, Codex, OpenAI API, school GPU, credential, or real token use was added.

### Phase 3 verified result

```text
66 passed in 18.73s
```

The SIGKILL recovery test waits until the detached MockAgent has started, kills
the Controller process, restarts it against the same SQLite DB/workspace, and
asserts Task/AgentRun success plus exactly one line in `launch_count.txt`.

## Known risks / TODO

1. Phase 1 uses metadata-based schema creation; introduce Alembic before schema
   upgrades on persistent deployments.
2. Local GPU allocation is single-Controller only. Multiple Controller processes
   could race and are not yet guaranteed safe.
3. SQLite is correct for the single-Controller design. Multi-process dispatch is
   out of scope and would require a deliberate concurrency review.
4. Artifact copies are immutable and retry-safe, but a crash between CAS copy
   and DB commit can leave an unreferenced blob. A future maintenance command may
   garbage-collect unreachable blobs after a conservative retention period.
5. Local jobs are limited to exclusive zero/one-GPU execution. Multi-GPU, MIG,
   MPS, fractional allocation, memory packing, and preemption are deferred.
6. Phase 3 uses MockAgent only. Hermes/Codex adapters, usage accounting,
   escalation/retry policy, and scientific gates remain deferred.
7. Agent FORK_ROLE, context summarization/excerpts, and automatic requested-task
   policy gates are deferred.
