# Phase 3 Implementation Plan

Scope: typed Agent protocols, an Agent Gateway, a durable MockAgent adapter,
AgentRun recovery, verification, and audit integration. Hermes, Codex, OpenAI,
remote compute, scientific workflows, and escalation automation remain deferred.

1. Add strict Pydantic v2 Agent/decision/transition schemas and validators for
   task identity, deliverables, permissions, decision choices, and safe paths.
2. Protect AgentRun status with a legal state machine, append-only Events, and
   the same ORM direct-mutation guard used by Task and ComputeJob.
3. Add Router, ContextBuilder, SessionManager, adapter registry/interface, and
   AgentGateway orchestration without granting adapters ORM access.
4. Implement MockAgentAdapter as filesystem-backed external state under
   `workspace/.mock-agent/runs/<AgentRun.id>`, with an atomic durable launch
   reservation and detached runner. `AgentRun.id` is the idempotency key.
5. Add Agent dispatch/reconciliation before new dispatch in each Controller
   tick, including STARTING/uncertain-start and RUNNING/result recovery.
6. Persist normalized request/response artifacts, safely ingest declared output
   files, then keep AgentRun success separate from Task verification success.
7. Add an Agent CLI demo plus protocol, policy, session, path-sandbox,
   vertical-slice, recovery, exactly-once, and real Controller SIGKILL tests.
8. Run the full Phase 1/2/3 regression suite and update README/status docs.

Crash invariant: after external launch, every retry first reconciles the same
`AgentRun.id`; a Controller SIGKILL cannot launch a second Mock workload.
