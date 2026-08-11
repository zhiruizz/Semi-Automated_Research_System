# Phase 0 / Phase 1 Implementation Plan

Repository inspection on 2026-08-11 found an empty directory with no Git metadata,
application code, database, task system, agent wrapper, artifact store, Hermes
integration, or Slurm integration to reuse.

The first implementation is intentionally limited to Phase 1:

1. Create a typed SQLAlchemy/SQLite domain store for Project, Task, Event,
   AgentRun, ComputeJob, and Artifact, plus relational task dependencies.
2. Route state changes through a transaction-scoped `TransitionService` that
   appends the matching per-project sequenced Event atomically.
3. Implement strict Pydantic compute protocols, an immutable local artifact
   store, and a provider interface that never receives ORM objects.
4. Implement a durable local runner and idempotent `LocalProvider`. A local
   submission record is written before process launch so an uncertain submit
   can be reconciled by `submission_key`.
5. Implement the ordered controller tick: reconcile, recover, readiness,
   verify, project evaluation placeholder, then dispatch.
6. Prove the vertical slice and recovery cases with unit and integration tests.

Deferred by design: Agent Gateway behavior, Hermes/Codex adapters, remote
providers, Paramiko, Slurm, B20X, Web UI, and the scientific workflow.

## Initial deviations

- The Phase 1 local execution protocol supports an explicit `command` vector in
  addition to the future `entrypoint_artifact_id`. This keeps the provider free
  of database access while making the bootstrap example executable. A later
  staging service can resolve code artifacts into the same prepared command.
- Alembic migrations are deferred; the Phase 1 schema is created with
  SQLAlchemy metadata. Migration tooling becomes necessary before the first
  deployed schema evolution.
