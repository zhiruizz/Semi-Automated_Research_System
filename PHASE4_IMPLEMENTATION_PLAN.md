# Phase 4 Implementation Plan

Scope: an optional real Hermes Runs API backend that preserves the Controller's
typed-result, audit, sandbox, session, and crash-recovery invariants. Codex,
automatic escalation, remote compute, and scientific workflows remain deferred.

1. Record a secret-free live Hermes 0.19.0 environment/capability snapshot and
   add configuration that references, but never stores, the bearer-key env var.
2. Build a typed async `HermesApiClient`, bounded error classification, health
   and capability cache, centralized remote-status mapping, prompt builder, and
   deterministic dual-path AgentResult parser.
3. Add a detached `HermesStartBridge`: AgentRun.id owns one durable bridge
   request/claim/response. Controller restart reconciles the bridge before any
   launch and never repeats an uncertain POST.
4. Route production HERMES implementation/debug roles to Hermes, keep Mock only
   for explicit tests/demo, and reject CODEX until its own adapter exists.
5. Persist real remote run/session identity, isolate sessions by model tier, map
   approval/stopping/cancelled/timeout states, preserve raw output, and continue
   Controller-side output sandbox and Task verification.
6. Add a local FakeHermesApiServer for health/auth/network/status/approval,
   session, invalid-result, dual-result mismatch, uncertain-start, and SIGKILL
   recovery tests. Default pytest must make zero real model calls.
7. Add explicit Hermes status/models/demo and approval CLI surfaces, opt-in tiny
   real integration tests, backend documentation, and full Phase 1–4 regression.

Hermes 0.19.0 does not provide durable Runs API client idempotency. The bridge
guarantees no duplicate POST after Controller-process SIGKILL, but cannot prove
exactly-once across a host-level crash after remote acceptance and before the
bridge persists the response. Such starts become `START_STATE_UNCERTAIN` and
are never retried automatically.
