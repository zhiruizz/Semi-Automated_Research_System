# Phase 5 Implementation Plan

Scope: a real Codex App Server backend that preserves the existing typed Agent
Gateway, durable AgentRun state machine, artifact provenance, sandbox boundaries,
session isolation, and recovery rules. Automatic escalation, scientific workflow
orchestration, school compute integration, and a web UI remain deferred.

1. Add secret-free `config/codex.yaml`, typed configuration/model/auth/status
   models, one stdio JSON-RPC production runtime, and an explicit status/model
   probe that never performs login, logout, upgrade, or global config writes.
2. Build deterministic scientific prompts, stage ContextPack artifacts as
   verified disposable copies, generate native schemas from Pydantic result
   models, and validate the final response directly without JSON repair.
3. Add a detached Codex StartBridge. It owns the App Server connection through
   the whole turn and atomically persists request, claim, thread, turn,
   approval, usage, terminal response, and bounded errors.
4. Map `AgentRun.session_id` to the Codex thread id and
   `AgentRun.external_run_id` to the turn id. Resume only within the same
   project, role, backend, and model tier; reviewer and changed-tier work starts
   a fresh thread.
5. Reconcile a known thread/unknown turn with `thread/read` and the stable
   `SARS_AGENT_RUN_ID=<AgentRun.id>` marker. Never issue a second `turn/start`
   after an uncertain response, and block an unknown new-thread start gap as
   `CODEX_START_STATE_UNCERTAIN`.
6. Enforce workspace-write/no-network by default, expose only backend-neutral
   one-time approval and denial, interrupt once on cancellation/timeout, and
   report turn-local usage without double counting resumed threads.
7. Add a durable Fake Codex App Server for protocol, auth/model, schema,
   sandbox, session, approval, interruption, usage, uncertainty, and SIGKILL
   recovery tests. Keep the real facts-fixture integration to one or two turns
   behind `SARS_CODEX_INTEGRATION=1`.
8. Add Codex CLI status/models/demo surfaces, backend documentation, request/raw/
   normalized artifacts, and run the full Phase 1–5 regression and sensitive
   information scan.

The bridge does not claim global exactly-once execution. A known thread can be
reconciled by durable history and the run marker. If thread creation may have
succeeded but no thread id was durably recorded, the Controller blocks the task
instead of guessing or retrying.
