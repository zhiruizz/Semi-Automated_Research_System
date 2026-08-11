# Phase 4 Hermes Environment

Inspected: 2026-08-11 (Asia/Shanghai)

## Installation

- CLI: `~/.local/bin/hermes`
- Version: Hermes Agent `0.19.0` (`upstream 99fa9303`)
- Install: local git checkout managed by Hermes
- Python: `3.11.15`
- Doctor: core Python, SQLite, HTTPX, configuration, gateway service, and memory
  checks passed. The existing Hermes config reports an available migration; no
  upgrade or automatic config modification was performed.

## API server

- Endpoint: `http://127.0.0.1:8642`
- `/health`: HTTP 200, Hermes `0.19.0`
- Bearer authentication: required for detailed/capability endpoints
- `/health/detailed`: ready; state DB, config, model, disk, gateway, and
  background queues all reported healthy
- `/api/model/options`: authenticated request exceeded the 10 second probe
  timeout; model-tier configuration therefore retains `null` provider/model and
  uses the current Hermes default

No bearer value, provider credential, or secret environment value is recorded
in this document.

## Live capabilities

Confirmed enabled:

- Runs submission, status polling, SSE events, stop, and approval response
- Tool progress and approval events
- Session resources, session chat, session fork, and session model lock
- Request-scoped model options
- Skills and toolsets listing

The server reported 25 toolsets and 434 visible skills. Phase 4 does not copy
the complete inventory into Controller Events or Artifacts.

## Start idempotency finding

Local source inspection of Hermes 0.19.0 confirmed that `POST /v1/runs` creates
a fresh UUID run for every request. Its `Idempotency-Key` cache is used by other
API surfaces, is in-memory, and is not applied to the Runs handler. No durable
lookup by client-supplied run key is advertised by `/v1/capabilities`.

Integration therefore requires the SARS detached StartBridge. It prevents a
Controller-process SIGKILL from issuing a second POST. It does not claim remote
exactly-once across the theoretical host-crash gap between Hermes accepting the
request and the bridge atomically persisting the returned run id.

## Readiness

The local API server is integration-ready after the Controller process receives
the same bearer secret through `SARS_HERMES_API_KEY`. Real tests remain explicit
opt-in and are never part of default pytest.

## Minimal real integration result

After the complete fake/default suite passed, three short real Runs calls were
made in an isolated pytest temporary workspace. The first two were correctly
rejected by the Controller (one used invalid field aliases; one repeated the
start marker instead of the required end marker). No JSON repair or retry of an
uncertain start occurred. Those observations led to an exact structural result
template and Runs-level system instructions.

The final explicit run passed the complete path: real run/session persistence,
dual-result equality, strict Pydantic validation, output sandboxing, raw and
normalized Artifact ingestion, and Task verification. Hermes reported 143,262
input tokens and 1,241 output tokens for that run. The large input count comes
from the current gateway/profile context rather than the small SARS task prompt;
Phase 4 did not modify the user's Hermes profile or default model to reduce it.
