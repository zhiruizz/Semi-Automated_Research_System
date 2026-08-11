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

## Phase 4 Definition of Done

- [x] Inspected the live local Hermes 0.19.0 install, doctor output, authenticated
  capabilities, toolsets, skills, model-options timeout, and Runs source without
  recording any credential.
- [x] Added an async typed `HermesApiClient` for health, capabilities, models,
  start/get/stop/approval, session get, and session fork.
- [x] Bearer configuration stores only `api_key_env`; response/error text is
  bounded and the secret value is absent from DB, bridge, Events, and Artifacts.
- [x] Registered `HermesAdapter`; production implementation/debug HERMES Tasks
  route to it while Mock is explicit and CODEX remains not implemented.
- [x] Runs capability is mandatory. There is no synchronous chat fallback.
- [x] Added stable prompts, context manifests, isolated per-AgentRun workdirs,
  output sandboxing, strict final envelope parsing, dual-file canonical equality,
  and no JSON repair.
- [x] Polling centrally maps running, approval, stopping, completed, failed,
  cancelled, and unknown statuses. Unknown status remains non-terminal.
- [x] Approval is never automatic; the CLI exposes only `once` and `deny` and
  records human actions.
- [x] Stop is a request, not a terminal observation. Timeout waits for remote
  cancellation before recording `TIMEOUT`.
- [x] Real returned session ids and exact reported usage are persisted. Resume
  is isolated by Project, role, backend, non-ephemeral mode, and model tier.
- [x] Raw Hermes output and normalized AgentResult are separate immutable
  Artifacts; invalid raw output is preserved.
- [x] Source inspection proved Hermes 0.19.0 Runs lacks native idempotency. A
  detached persistent StartBridge owns one POST per AgentRun.
- [x] Dropped HTTP response becomes `START_STATE_UNCERTAIN` and is never retried;
  missing known remote run becomes `REMOTE_RUN_NOT_FOUND` and blocks.
- [x] Fake server tests cover auth/capabilities/models, submission/poll/stop,
  approval once/deny, timeout, result errors, sessions/tier, unknown/missing run,
  HTTP uncertainty, and Controller SIGKILL with one POST.
- [x] Default pytest is zero-real-call; real integration is marker/env opt-in.
- [x] A final explicit tiny real Hermes NEW run passed strict result validation,
  Artifact collection, and Task verification. Real resume was not repeated after
  earlier contract-negative calls; fake coverage proves the resume request shape.
- [x] README and `docs/HERMES_BACKEND.md` document operation and the exact
  idempotency limitation.
- [x] No Codex, escalation, school compute, Hermes upgrade, or model reconfiguration
  was added.

### Phase 4 idempotency boundary

Controller-process SIGKILL recovery is tested. Host-level exactly-once is not
guaranteed across the gap after remote acceptance and before bridge response
persistence. Phase 4 explicitly blocks rather than retrying that uncertain case.

### Phase 4 verified result

```text
Default: 87 passed, 1 skipped (zero real model calls)
Opt-in real: 1 passed
```

The successful real run persisted its Hermes run/session identity and exact
reported usage (143,262 input tokens, 1,241 output tokens). The live gateway's
large base/profile context dominates that input count; the SARS objective and
deliverable were intentionally tiny.

## Phase 5 Definition of Done

- [x] Inspected Codex CLI 0.145.0, the absent Python SDK, fresh App Server
  schemas, auth category, models, sandbox, approval, interruption, and usage
  fields without changing login or global configuration.
- [x] Selected one production driver: App Server stdio JSONL. `codex exec` is
  not a backend and there is no parallel SDK path.
- [x] Registered `CodexAdapter`; supported CODEX roles route to it while Hermes
  routing and explicit Mock routing remain unchanged.
- [x] Model discovery/default selection and configured model/effort validation
  work without a hard-coded model name.
- [x] Persisted `thread.id` as AgentRun.session_id and `turn.id` as
  AgentRun.external_run_id. NEW, RESUME_ROLE, reviewer/role isolation, tier
  isolation, and EPHEMERAL behavior pass Fake integration tests.
- [x] Codex FORK_ROLE is explicitly deferred and rejected rather than silently
  changing semantics.
- [x] AgentResult and DecisionResult use one explicit, versioned Codex wire
  layer and strict compatibility validator. Invalid output is rejected without
  JSON repair.
- [x] Artifact context is hash-verified, copied (not hardlinked), marked
  read-only, and verified against DB-derived expectations after the turn. A
  malicious manifest-plus-copy mutation is detected while the CAS stays intact.
- [x] Codex cwd is the isolated AgentRun directory. Read-only/workspace-write,
  default-off network, explicit network permission, and forbidden full access
  are tested.
- [x] Approval is never automatic. The generic CLI maps `once`/`deny` to native
  `accept`/`decline` and survives Controller restart.
- [x] Timeout and human cancellation send one interrupt and are distinguished
  only after terminal `interrupted` observation.
- [x] Turn-local `tokenUsage.last` prevents resumed-thread double counting; no
  price table or dollar estimate is assumed.
- [x] Request, raw final message, and normalized result are separate immutable
  Artifacts. Bounded traces contain event metadata only, not chain-of-thought.
- [x] Detached StartBridge records request/claim/thread/turn/approval/usage/
  terminal state atomically. Lost turn responses reconcile through history and
  the stable AgentRun marker without a second start.
- [x] Unknown new-thread creation and missing known threads block safely; no
  global exactly-once guarantee is claimed.
- [x] A real Controller subprocess SIGKILL test proves bridge survival, same
  AgentRun/Thread/Turn reconciliation, and exactly one fake `turn/start`.
- [x] Transition and requested-task outputs remain proposals only. No automatic
  escalation, school compute, scientific workflow, or Web UI was added.
- [x] Codex Fake/unit tests pass; default pytest performs zero real Codex model
  calls.
- [x] One separately authorized Phase 5.1 turn passed strict schema preflight,
  executed the model, and passed wire/domain/security validation after
  same-thread result hydration.
- [ ] The recovered real AgentRun succeeded, but the Task correctly blocked
  because the no-local-command demo exposed only a file reference. The fixture
  is now also inline, but no second Phase 5.1 turn was allowed to revalidate it.
- [x] Full default suite passes: 131 passed, 2 opt-in integrations skipped.
- [x] Hermes and local GPU/recovery specialized regressions pass independently.
- [x] README, environment/plan notes, and `docs/CODEX_BACKEND.md` are updated.

### Phase 5 recovery boundary

Controller-process SIGKILL is tested and starts exactly one turn. A host crash
in the new-thread response gap remains unknowable and blocks as
`CODEX_START_STATE_UNCERTAIN`. A known thread can be searched by the stable
AgentRun prompt marker. These are scoped recovery guarantees, not global
exactly-once execution.

### Phase 5 verified result

```text
Default: 131 passed, 2 skipped (zero real model calls)
Codex schema/lifecycle focused: 44 passed
Controller SIGKILL duplicate turn/start count: 1
Phase 5 original real: 2 schema-preflight failures, 0 reported tokens
Phase 5.1 real: preflight PASS; model executed; AgentRun SUCCEEDED; Task BLOCKED
Phase 5.1 usage: 15,866 input, 0 cached input, 352 output
```

The structured-output blocker is resolved. Phase 6 escalation is not yet
recommended because the required real Task end state was not `SUCCEEDED` and the
adjusted inline fixture has not been re-called.

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
6. Hermes and Codex are optional; automatic escalation/retry policy and
   scientific gates remain deferred.
7. Codex FORK_ROLE, context summarization/excerpts, and automatic requested-task
   policy gates are deferred.
