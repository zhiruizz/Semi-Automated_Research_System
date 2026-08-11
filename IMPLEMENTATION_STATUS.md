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

## Known risks / TODO

1. Phase 1 uses metadata-based schema creation; introduce Alembic before schema
   upgrades on persistent deployments.
2. Local GPU inventory is not allocation. GPU leases and concurrent scheduling
   must be implemented and tested in Phase 2 before running CUDA workloads.
3. SQLite is correct for the single-Controller design. Multi-process dispatch is
   out of scope and would require a deliberate concurrency review.
4. Artifact copies are immutable and retry-safe, but a crash between CAS copy
   and DB commit can leave an unreferenced blob. A future maintenance command may
   garbage-collect unreachable blobs after a conservative retention period.
5. AgentRun exists only as a core schema placeholder. No Agent execution is
   accepted until Phase 3 protocols and MockAgent validation are implemented.
