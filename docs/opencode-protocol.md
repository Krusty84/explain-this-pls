# OpenCode interface verification and remaining upstream blocker

**No upstream OpenCode release is currently enabled for production by this change.**
The HTTP transport is implemented and exercised offline, but the requested bound
on native format retries cannot be verified in the inspected upstream versions.
`--check` and ordinary execution fail with `BACKEND_INCOMPATIBLE` before any model
request. This is a known incomplete part of the requested OpenCode integration,
not a successful implementation of bounded native retries. There is no fallback
to `run --format json`, no production bypass, and no automatic backend/model change.

## Evidence, checked 2026-09-29

The wire adapter targets **v1.2.27**, commit
`4ee426ba549131c4903a71dfb6259200467aca81`. The newer **v1.18.33**, commit
`1eacc1bdb919bc54fc1c9f745b0da02e83ebbdf9`, was also inspected specifically for retry
enforcement. Neither inspected prompt loop reads `retryCount`; missing structured
output produces `StructuredOutputError` with `retries: 0`. Tool errors can also
enter the ordinary agent loop; setting `retryCount: 0` is not proof that corrections
are disabled. Consequently even zero is not advertised as a verified retry bound.

Primary sources:

- [v1.2.27 generated OpenAPI](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/sdk/openapi.json)
- [v1.2.27 prompt loop and StructuredOutput](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/prompt.ts)
- [v1.2.27 message types](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/message-v2.ts)
- [v1.2.27 HTTP session routes](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/server/routes/session.ts)
- [v1.2.27 chronological session message ordering](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/index.ts)
- [v1.2.27 LLM tool permission filtering](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/llm.ts)
- [v1.2.27 server authentication and listener](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/server/server.ts)
- [v1.2.27 network flags](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/cli/network.ts)
- [v1.18.33 prompt loop](https://github.com/anomalyco/opencode/blob/v1.18.33/packages/opencode/src/session/prompt.ts)
- [Upstream issue about ignored retryCount](https://github.com/anomalyco/opencode/issues/25430)

The docs' `format`/`outputFormat` discrepancy is resolved by the tagged OpenAPI:
`POST /session/{sessionID}/message` takes **`format`** with
`{type: "json_schema", schema: {...}, retryCount: N}`. The actual tagged assistant
response uses **`info.structured`**, not the SDK documentation's
`info.structured_output`. Ordinary text parts are not the result. Adding a JSON
schema to a prompt or using the CLI transport flag does not enforce that schema.

The local version gate accepts only the inspected wire version, then the separate
native-retry capability gate refuses the unimplemented guarantee. This is based on
source inspection; a field appearing in `/doc` alone cannot prove it is enforced.
Enabling a future release requires checking its actual retry loop, errors, finish
behavior, permissions and OpenAPI, updating the fixtures and version gate, and
adding tests for zero retries, correction success and exhaustion. No invented
version marker or schema flag is used to pretend an upstream fix exists.

## HTTP transport prepared for a compatible implementation

The parent starts its own `opencode serve --hostname 127.0.0.1 --port PORT --mdns false`.
The port is locally allocated; the client first requires the owned CLI's exact
post-bind listener announcement, then checks authenticated health and `/doc`.
A bind race cannot succeed merely because a different server answers health. The
client does not discover or attach to an existing server. A random Basic-auth
secret is provided through `OPENCODE_SERVER_PASSWORD`, with an explicit username.
It is never included in command arguments, saved request bodies or authorization
logs. HTTP uses literal loopback and does not consult proxy environment variables
or follow redirects. The environment, credentials and provider configuration of
the configured executable are preserved; no global configuration is written.

Each invocation has a new server and a new root session. Review receives only its
explicit document/context; it does not resume study. Compare runs in a temporary
directory outside the source tree. Its permission map denies all tools except
`StructuredOutput`. Study and review retain only read/glob/grep/list plus that
exact tool name. The extra permission is required by `LLM.resolveTools` in v1.2.27;
it does not grant shell, write, MCP, task or network tools. This is native tool
policy, **not filesystem isolation**. User plugins/profile code still run with
the caller's privileges.

The synchronous prompt response confirms the native prompt loop returned. While
it is outstanding, read-only message snapshots are polled. The returned session,
parent request, assistant, part IDs and agent must match; completed timestamp,
finish reason and completed StructuredOutput call are required. A final history
read rejects an older result followed by a newer response. Backend errors outrank
a supplied object. `length`, absent completion and absent native output fail.
Local `validate_result()` then enforces the original schema and semantic rules.

The HTTP fixtures test this transport by **explicitly mocking only the capability
gate in test code**. They do not demonstrate successful upstream native retries.
No real model was invoked; no user-reported raw response was available, so its
particular cause has not been reproduced or attributed to a model or wrapper.

## Budgets and cleanup

`execution.stage_timeout_seconds` defaults to 3600 and must be a positive finite
number; `idle_timeout_seconds` defaults to null and otherwise has the same numeric
rule. Booleans are rejected. `opencode_format_retries` is an integer 0–2 (default
2). It is reserved for the native format mechanism, not provider/network retries,
ordinary tool calls or the other backends. Existing configurations receive these
defaults. No outer LLM repair/research retry was introduced.

One monotonic deadline spans backend startup, request and output waiting. CLI
preflight and individual Git operations have independent 30-second limits. Server
readiness has a 20-second cap. Ordinary HTTP operations have a 5-second cap; the
long-running synchronous model request is watched against the remaining stage
budget. A worker watchdog bounds even a peer that drips response headers. Owned
process groups are killed on completion, error, timeout or interruption, including
descendants holding output pipes. Detached hostile daemons are outside this
process-group guarantee. Cleanup attempts abort/delete of the owned session within
5 seconds, then kills/reaps the owned group (2-second wait). A failed cleanup is
recorded and prevents publication on an otherwise successful invocation; it does
not mask an earlier failure. Remote provider cancellation is best effort.

CLI stdout/stderr bytes count as activity for Codex/Claude Code. HTTP activity is a
changed, validated session message snapshot (text/reasoning/tool/message progress),
not health checks, repeated snapshots, or service heartbeats. Total deadline always
wins. Git integrity checks and safe restoration run after agent timeout using
their own bounded Git operations; no force reset or clean hides changes.

## Private artifacts and acceptance

Stage logs now contain `attempt-001/`, `attempt-002/`, etc. Each has its own input,
invocation metadata and raw output; creating a later attempt does not overwrite
earlier evidence. There is no automatic outer retry. Stage-level `invocation.json`
summarizes the latest attempt. Native responses are saved as `response-NNN.json`,
the final response as `response.json`, and the extracted object as `extracted.json`
before local validation. The server's own logs are named `server.stdout.log` and
`server.stderr.log`; API responses are not called stdout. Partial HTTP data is
saved when cancellation permits. Polled snapshots are the history actually
available to this client; they are not a lossless SSE recording or invented
individual retry responses. Unknown retry counters/metadata remain null.

Created artifact directories use 0700, files 0600. Raw artifacts and prompt inputs
can contain sensitive source code, model content and backend messages: protect the
run directory and do not publish it wholesale. Full environments and authorization
headers are not saved. Public logs use trusted error categories, JSON coordinates,
and schema paths rather than raw model text. Duplicate keys and non-finite values
are rejected during local JSON decoding; duplicates already normalized by upstream
cannot be detected. Reports are published only after validation and backend cleanup;
a failed publication is never marked accepted in the manifest.

Example safe diagnostic (no response excerpt):

```json
{
  "code": "INVALID_RESPONSE",
  "failure_kind": "INVALID_JSON",
  "failure_layer": "result",
  "message": "Invalid JSON syntax.",
  "details": {"json_error": "Expecting value", "line": 2, "column": 12, "position": 13}
}
```

Other kinds include `BACKEND_ERROR`, `TRANSPORT_ERROR`, `INCOMPLETE_OUTPUT`,
`SCHEMA_ERROR`, `SEMANTIC_ERROR`, `STRUCTURED_OUTPUT_EXHAUSTED`, `STAGE_TIMEOUT`
and `IDLE_TIMEOUT`. Backend incompatibility, source integrity and interruption
remain distinct. Exit codes and report schemas are unchanged.

## Tests

Run on Linux/macOS with Python 3.11+ and Git; no accounts, installed agent CLI or
network access are required (the HTTP fixtures need loopback):

```sh
python3.11 -B -m unittest discover -s tests -v
```

The fixtures' provenance is recorded alongside their OpenAPI subset. Synthetic
malformations are labelled separately. A live CLI readiness check is local and
must never trigger a paid smoke test. Model testing remains separate and requires
an authorized, configured environment. Duplication between Markdown and structured
report arrays is unchanged and remains a possible future task.
