# Phase 5.1 Codex Structured Output Schema Fix

Updated: 2026-08-11 (Asia/Shanghai)

## Root cause

Codex App Server supports native Structured Output, but the upstream strict
response format accepts a subset of JSON Schema. A Pydantic domain schema is not
automatically a valid Codex wire contract. Phase 5 sent `AgentResult`'s domain
schema after a generic recursive rewrite; that approach did not define how
Python-specific values and arbitrary mappings should cross the wire.

The corrected boundary is:

```text
AgentResult domain model
  -> CodexAgentResultWire
  -> codex-structured-schema/v0.1 projector
  -> local compatibility validator
  -> turn/start.outputSchema
  -> CodexAgentResultWire validation
  -> deterministic conversion
  -> authoritative AgentResult validation
  -> existing AgentResultValidator permissions/path checks
```

The generic Agent protocols are unchanged. Hermes does not use this projection.

## Two prior sanitized preflight failures

Both failures were terminal HTTP 400 responses before model execution. Neither
reported token usage.

1. Error code: `invalid_json_schema`

   ```text
   Invalid schema for response_format 'codex_output_schema': In
   context=('properties', 'metadata'), 'additionalProperties' is required to be
   supplied and to be false.
   ```

   Classification: the domain `metadata: dict[str, Any]` was an open mapping.

2. Error code: `invalid_json_schema`

   ```text
   Invalid schema for response_format 'codex_output_schema': In
   context=('properties', 'path'), 'path' is not a valid format.
   ```

   Classification: domain `pathlib.Path` generated `format: path`.

No account information, local temporary path, thread id, turn id, or credential
from those runs is retained here.

## Wire representation

- Paths, stages, and other domain values cross the wire as conservative JSON
  strings. Pydantic reconstructs and validates the domain type afterward.
- Artifact `metadata` crosses as required nullable `metadata_json`. Non-null
  values must parse as a JSON object.
- Protocol amendment changes similarly use `proposed_changes_json`.
- Optional fields are always required and represent absence with `null`.
- Every object, including `$defs` and array item objects, is closed with
  `additionalProperties: false` and requires every declared property.
- Wire models use only strings, numbers, booleans, nulls, arrays, fixed objects,
  literals/enums, and numeric constraints.

The projector removes only annotation-only Pydantic keywords (`title`,
`examples`, `default`, `deprecated`, `readOnly`, and `writeOnly`). It does not
strip unknown semantic constraints. `format` is rejected by the validator; the
wire models avoid generating it instead of weakening a domain schema.

## Static compatibility checks

`validate_codex_output_schema()` walks every schema node with stable JSON
pointers. It rejects:

- `format` and residual annotation-only keywords;
- objects without a fixed `properties` map;
- objects not closed with `additionalProperties: false`;
- schema-valued or true `additionalProperties` mappings;
- property sets whose `required` list is incomplete.

The old failures now produce precise local pointers:

```text
$.properties.metadata.additionalProperties
$.properties.path.format
```

The validator runs while creating the canonical contract and again in
`CodexAdapter.start()` before auth/model probing, context staging, bridge
creation, remote Thread creation, or `turn/start`.

## Pre-real-call inspection

Layer A — explicit wire schema generation: PASS.

Layer B — local compatibility validation: PASS.

Layer C — the installed App Server exposes `turn/start.outputSchema` but no
separate no-model schema-preflight endpoint. No extra turn is used as a
preflight substitute.

Current schema inspection:

```text
adapter: codex-structured-schema/v0.1
AgentResult Codex schema SHA256: ec3d0b35c010925275f43bb2839cdd2db2baeb5963815086203a4983a331a8a4
AgentResult top-level properties: 11
AgentResult $defs: 4
AgentResult closed objects: 5
DecisionResult $defs: 2
DecisionResult closed objects: 3
compatibility validator: PASS
```

Reproduce without a model call:

```bash
research-controller codex schema agent-result
research-controller codex schema decision-result --json
```

Request Artifacts record domain, raw wire, and final Codex schema hashes plus the
schema adapter version.

## Zero-real-call regression result

```text
Full default suite: 131 passed, 2 skipped
Codex schema/lifecycle focused: 44 passed
Hermes/GPU/Agent specialized: 56 passed
compileall: PASS
git diff --check: PASS
```

The two skipped tests are the explicit Codex and Hermes real integrations.

## Single real turn result

Exactly one Phase 5.1 turn was started; there was no automatic retry and no real
DecisionResult or RESUME call.

```text
schema preflight: PASS
model execution: reached and completed
wire validation: PASS
domain AgentResult validation: PASS
Controller permission/path validation: PASS
AgentRun: SUCCEEDED
Task: BLOCKED
usage: 15,866 input / 0 cached input / 352 output / 193 reasoning output
fixture: 254 bytes
prompt: 1,748 bytes
context manifest: 517 bytes
bridge request: 6,999 bytes
```

The installed App Server emitted a summary-only `turn/completed`; the complete
agent message was present in `thread/read`. The bridge now hydrates that same
durable turn before result extraction. A Fake regression proves the hydration
uses one `turn/start`. The already-created real turn was recovered read-only and
strictly validated; no second turn was sent.

The valid real AgentResult selected `blocked` because local-command access was
false and the authoritative values were referenced only through a staged file.
The Controller therefore correctly blocked the Task. The tiny demo now embeds a
deterministic copy of those fixture values in its instructions while keeping all
permissions false. That test-fixture correction remains unverified by a real
turn because the one-turn budget is exhausted.

Under the required coarse failure taxonomy, the end-to-end result is
`B. MODEL_EXECUTION_FAILED` (semantic task outcome `blocked`), not schema
preflight, malformed structured output, Controller validation, network, or auth
failure. Structured Output compatibility itself is verified.

## Sensitive-data review

The repository scan found zero credential-pattern files, zero email-pattern
files, and zero personal identifier/path files. Live account data and the real
thread/turn identifiers are not committed.
