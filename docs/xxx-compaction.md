# OpenCode 1.2.27 / XXX: HTTP compatibility and compaction

## Current supported profile

`backend: "xxx"` names the OpenCode **1.2.27** HTTP integration, including
XXX-branded executables. Use `agent.executable` for the installed command or path;
the existing `backend: "opencode"` continues to select the V2 CLI. No backend
alias, automatic version switching or installation is involved. CLI and health
version strings are preserved; optional `expected_version` still requires the
exact CLI output. Model selection and the user's authentication stay with the
configured executable.

The profile is `opencode-v1.2.27-http`, based on the six used paths and referenced
schemas from upstream commit `4ee426ba549131c4903a71dfb6259200467aca81`. The production
declarations are generated from the pinned test fixture. `compactionCount: number`
on Session (required or optional) and the recorded `queued` / `unattended_retry`
SessionStatus variants are independently optional extensions. Only those exact
shapes are allowed; unknown changes, changed shapes and duplicate declarations
are rejected. Status extensions do not prove completion or refresh idle time.

Preflight and each stage server check authentication and the API before sending
a model request. `--check` creates no session. Private `api-delta.json` contains
the original delta, while `api-compatibility.json` lists `accepted_extensions`
and the remaining `residual_delta`. Invocation metadata records raw `api_changes`
and `api_allowed_extensions`. Thus stock 1.2.27 has zero API changes; the full
previous XXX extension set retains all four permitted changes in the diagnostic.

Requests carry `format: {type: "json_schema", schema, retryCount: 0}`. Results
come from `info.structured` with native-envelope and local contract validation.
There is no native retry-enforcement guarantee. Explicit orchestrator format
repairs keep their existing setting/default and stage budget; they do not repair
transport identity or compaction compatibility failures.

Automatic compaction accepts `auto: true` with `overflow` absent (stock threshold
path) or exactly `false` (processor path). Wire fields remain unchanged and
immutable across observations. Manual compaction and `overflow: true`/replay
remain unsupported with `COMPACTION_FORM_UNSUPPORTED`. Linked summary and
continuation identity, settings and format checks still apply.

Stock 1.2.27 loses `format` when creating a compaction service user and its
ordinary continuation. After a fully verified automatic chain, the validator now
returns typed `RecoveryRequired` without setting `history.failed`. It validates
any already present continuation response as `origin=discarded`; streaming or
completed text cannot be accepted as the result. Missing format anywhere else,
changed schema (`COMPACTION_FORMAT_CHANGED`), foreign IDs/settings, unfinished
summary, manual compaction and overflow/replay still fail.

The client aborts a pending synchronous message POST and waits for **that original
HTTP request** to return completely. `abort=true`, idle status and a locally closed
socket do not suffice. Naturally completed requests need no recovery abort. It
then fetches and validates a fresh full history, including the discarded response.
Only the expected `MessageAbortedError` for the continuation cancelled by this
mechanism is exempted. All other errors retain their meaning. An unconfirmed stop
fails with `TRANSPORT_ERROR / COMPACTION_RECOVERY_STOP_UNCONFIRMED` unless a more
specific error or timeout has already occurred. No next POST is sent in that case.

After confirmed stopping, the validator registers a new user message ID. Its POST
uses the same session, original `format`/schema and `retryCount: 0`, verified
original agent and actually selected model, and original applicable `system`,
`tools`, `variant`. Its fixed short instruction asks to continue the previous task
from preserved context and finish through StructuredOutput. It does not repeat
the research prompt or history. The accepted result must have the registered
parent or belong to a later verified compaction chain and still pass native
StructuredOutput, schema, evidence and source-binding validation.

There are at most **two recovery messages per `Runner.invoke`**, summed across
its structured-output repair attempts. This internal limit has no setting.
Recovery does not consume `structured_output_repair_attempts`. The next format
loss fails with `BACKEND_INCOMPATIBLE / COMPACTION_RECOVERY_LIMIT_EXCEEDED`, reason
`COMPACTION_FORMAT_MISSING`. The original deadline and idle budget remain active;
stopping additionally uses `http_timeout_seconds`. An uncertain recovery send is
never automatically repeated. Cleanup runs on timeout, error and user interrupt.

Private artifacts keep `request.json` and the original `response.json`, every
numbered HTTP response/history, each `recovery-NNN-request.json` and
`recovery-NNN-response.json`, and detection/stop/continuation/result events in
invocation metadata. `request_id` stays the operation root; `recovery_ids` and
`final_parent_id` identify later messages. Usage includes original work,
compaction and discarded continuations, deduplicated by message/part identity.
Incomplete or cancelled work remains marked partial even if recovery succeeds.
The application changes no installed agent, global configuration, history,
summary or synthetic message. V2 and session-free `--check` remain unchanged.

The existing native patch below is separate reference material. It is **not** a
requirement or an automatically applied part of this stock integration. The
historical verification sections record earlier behavior and test counts; the
current profile above supersedes their explicit-overflow-only restriction and
their immediate failure on missing continuation format.

### Reproduce stock native behavior without a model provider

Use a disposable checkout of the pinned upstream commit with its runtime
dependencies and Bun already prepared. This mode never applies the reference patch:

```sh
bash .github/ci/offline-tests.sh python3.11 scripts/test-native-compaction.py \
  /path/to/disposable/opencode --bun /path/to/bun --stock-only \
  --artifacts-dir /path/to/new-private-artifact-directory
```

The native harness substitutes only the language provider with deterministic local
responses. It exercises normal structured output and both automatic compaction
paths. Unmodified native histories pass through the production Python validator:
normal results must succeed and both format-loss paths must require recovery.
`compaction-recovery.test.ts` additionally runs the production Python HTTP client
against the real native HTTP routes, with only the language provider replaced.
It covers one/two recoveries, natural text completion, streaming cancellation,
early abort/idle, delayed original HTTP completion and the third-loss limit.
The native runtime itself creates and executes StructuredOutput and sets
`info.structured`; no HTTP fixture supplies that value. The client checks the
native envelope and schema. Existing 47 native tests and TypeScript checking run
afterward. Bun, runtime dependencies and a local `rg` in PATH are prerequisites
for this offline HTTP server test. Injected tests are removed afterward; source
hashes remain pinned. `--artifacts-dir` preserves native histories and HTTP
artifacts in a new directory even on failure. No installed agent or global
runtime configuration is modified.

### Recovery verification (2026-10-06)

Implemented from repository HEAD `8a9b7b1`. All checks used WSL AlmaLinux 9,
Python 3.11.13, Git 2.52.0 and Bun 1.3.10. External networking was disabled with
the existing namespace harness; only loopback and deterministic local providers
were used. No installed-agent smoke, paid model call, native patch or global
configuration change was performed.

| Check | Result |
| --- | --- |
| Stock prompt/compaction baseline | 3 passed across 6 sessions; 6 reference-patch cases intentionally skipped |
| Native HTTP recovery | 4 passed: natural text, one/two aborted streaming continuations, third-loss limit |
| Native history validation | 2 original structured results accepted; 4 format-loss histories require recovery and cannot be final results |
| Existing upstream tests | 47 passed; `bun run typecheck` passed |
| Final focused Python suite | 121 passed in 82.668 s: recovery, history, owned cleanup, HTTP, structured repair, V2 CLI and metrics |
| Owned Runner compaction suite | 14 passed in 76.331 s: Folder/Git, separate review, branch comparison, shared repair cap and interruption |
| Full Python discovery | 611 tests in 495.101 s; 16 failures, 29 errors, 5 skips, all failure entries classified below |
| Non-root permission checks | 3 passed in 4.306 s |
| Native source integrity | All 398 `packages/opencode/src` files byte-equal to the pinned archive; both existing SHA-256 checks match; injected tests removed |
| Whitespace | `git diff --check` passed |

The final focused suite ran after full discovery and also includes the additional
owned stop-failure cleanup regression and final timeout/idle adjustments. An
initial version of that new fixture hung catalog cleanup as well as the intended
study abort; it was corrected to target the recovery cycle and rerun successfully.
The full-discovery count above is the actual completed run, not a claim that the
currently collected suite or all local tests are green.

The stock native HTTP cases use two, three and three message POSTs for one,
two and three losses respectively. Each successful case executes one native
StructuredOutput callback and passes the production client plus local schema
validation. The third-loss case executes no final StructuredOutput and reports
`COMPACTION_RECOVERY_LIMIT_EXCEEDED` with reason `COMPACTION_FORMAT_MISSING`.
Native traces show abort/idle returning about 200 ms before the original HTTP
response; the next POST occurs only after that response completes. Natural
completion retains complete reported usage; aborted cases correctly remain partial.

Previously existing failures reproduced separately:

- `test_all_examples_load_without_credentials`, subtest
  `config.opencode.example.jsonc`: the unchanged example selects
  `deepseek/deepseek-flash`; the test expects `None` (one failure).
- Git-ignored local `test_openapi_diff.py`: 20 failure/error entries caused by
  its utility importing missing top-level `openapi_contract`.
- Git-ignored local `test_verify_agent.py`: 24 failure/error entries, including
  subprocess assertions, caused by missing top-level `contracts` imports in its
  local utilities. Those files and imports were not modified.

The five skips are the three actual non-root checks (passed separately) and the
two opt-in installed-agent smokes. No remaining product-test regression was
observed. A runtime that fails to finish its original HTTP request after abort
still receives a diagnostic stop failure and ordinary owned-resource cleanup;
it cannot send a competing recovery message.

Local private verification artifacts are retained in
`docs/compaction-recovery-20261006-artifacts/` (Git-ignored):
[summary](compaction-recovery-20261006-artifacts/summary.json),
[full log](compaction-recovery-20261006-artifacts/full.log),
[focused log](compaction-recovery-20261006-artifacts/focused.log),
[native log](compaction-recovery-20261006-artifacts/native.log), and
[source verification](compaction-recovery-20261006-artifacts/source-verification.json).
The `native/http-*` directories contain each original/recovery request, untouched
HTTP history/response, client metadata and the native event trace.

Reproduce the focused and full Python checks:

```sh
PYTHONPATH=tests bash .github/ci/offline-tests.sh python3.11 -B -m unittest \
  test_xxx_recovery test_xxx_history \
  test_xxx_compaction.CompactionHTTPTests.test_recovery_stop_failures_cleanup_owned_resources \
  test_opencode_http test_structured_output test_opencode_cli test_metrics -v
EXPLAIN_OPENCODE_SMOKE=0 EXPLAIN_XXX_COMPACTION_SMOKE=0 AUDIT_GIT_BUILD=modern \
  bash .github/ci/offline-tests.sh python3.11 -B -m unittest discover -s tests -v
```

### Earlier stock-profile verification (2026-10-06, before recovery)

Implementation started at `4232598` with a clean tracked tree. Checks used WSL
AlmaLinux 9, Python 3.11.13, Git 2.52.0 and Bun 1.3.10. HTTP/native/full tests
ran with external networking disabled and no paid model or installed-agent smoke.

| Check | Result |
| --- | --- |
| OpenAPI, extension combinations and history regressions | 65 tests passed |
| HTTP, structured output and V2 CLI integration | All covered cases passed in final full discovery; the initial focused run exposed a new test's incorrect comparison-field assertion, corrected and rerun |
| Stock native prompt/compaction path | 3 tests passed across 6 sessions; 6 reference-patch cases intentionally skipped |
| Unmodified native histories through Python | 2 normal results accepted; 4 exact format-loss rejections across both compaction paths |
| Existing native compaction/structured-output tests and type checking | 47 tests passed; `bun run typecheck` passed |
| Full Python discovery | 597 tests in 456.485 s; 16 failures, 29 errors, 5 skips |
| Permission cases rerun as the normal WSL user | All 3 passed |
| Source integrity / whitespace | Original native source hashes unchanged; injected test removed; `git diff --check` passed |

The full discovery is **not green**. Its only project-test failure is the existing
`test_all_examples_load_without_credentials` subtest for the unchanged V2 example:
it expects `model: null`, while the committed example sets
`deepseek/deepseek-flash`. The other 44 failure/error entries belong to unchanged,
Git-ignored `test_openapi_diff.py` / `test_verify_agent.py`, whose local utilities
still import missing top-level modules. All XXX/OpenCode HTTP, history, repair,
V2, timeout and cleanup tests passed in that same discovery. Of its five skips,
the three root-related permission checks passed separately; the two installed
agent smokes remain explicitly not run. This is not a real-provider quality test.

The full discovery used `unittest.defaultTestLoader.discover('tests')` and the
standard verbose `TextTestRunner`, equivalent to:

```sh
EXPLAIN_OPENCODE_SMOKE=0 EXPLAIN_XXX_COMPACTION_SMOKE=0 AUDIT_GIT_BUILD=modern \
  bash .github/ci/offline-tests.sh python3.11 -B -m unittest discover -s tests -v
```

## Historical reference-patch investigation

## 2026-10-06: native format-retention patch, verified without XXX

The starting working tree was clean at
`1cb9d32b0d5114159bc581e4b80244284947a5d1`. The supplied observation from
`20261006T073018Z-cc08be1b47` reports a completed summary followed by
`BACKEND_INCOMPATIBLE / COMPACTION_FORMAT_MISSING`, with `phase=continuation`,
`role=user`, `field=info.format`, `transitions=1`. The raw run artifacts were not
provided; the regression below reproduces that sequence with synthetic data.

The versioned [native patch](../patches/opencode-v1.2.27-compaction-format.patch)
fixes the loss in stock **OpenCode 1.2.27**, verified through the real native
prompt/compaction/tool path with a deterministic fake provider. XXX source and
build mapping remain unavailable; **installed XXX is unverified and unchanged**.
The patch does not require XXX, a paid provider, a plugin, or a Python validation
change. It is independent of the older ignored structured-output-validation patch.

### Cause and repair point

At stock OpenCode v1.2.27, commit
`4ee426ba549131c4903a71dfb6259200467aca81`,
[compaction creation and processing](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/opencode/src/session/compaction.ts)
omit the request format on both the service user and ordinary continuation.
`process()` selects the service user by its parent ID; copying its format only
at continuation creation would therefore copy an already missing value.

The pinned [native prompt loop](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/opencode/src/session/prompt.ts)
has two automatic creation sites: the threshold check before normal processing
and the `result === "compact"` branch after processing. Both need the active
request's contract before `SessionCompaction.create()` replaces the latest user.
The latter sets `overflow` from the absence of a finish reason; the former omits
that field. At that revision the client required explicit `overflow=false`;
the current stock profile accepts the omitted field without rewriting it.

The patch adds five lines across the two native files:

1. `prompt.ts`: both automatic call sites pass the active `lastUser.format`
   before the service message becomes the latest user.
2. `compaction.ts`: typed creation input accepts the existing format type and
   saves it on the service user's existing `format` field. Continuation creation
   includes that format in its **first save**, with fresh native IDs/timestamps.
   Subsequent compactions carry the retained contract in the same way.

The complete schema and `retryCount: 0` survive. Existing agent/model/permission
and other settings are untouched. Summary processing still uses empty tools and
its normal prompt. Normal stage processing then creates the native tool/callback
and structured instructions with required tool choice. No extension hook, history
search, summary parsing, GET rewriting or second competing prompt is used.

### Native regression and client acceptance

`tests/native/compaction-format.test.ts` is loaded into the actual pinned native
package. Only `Provider.getLanguage()` is replaced by a fake LanguageModelV2;
observation wrappers around `LLM.stream()` and `Session.updateMessage()` call
their original implementations unchanged. Native prompt processing, compaction,
permission filtering, read tools, SDK streaming/schema validation, persistence,
StructuredOutput execution and result capture all run. No HTTP fixture fabricates
`info.structured` in this test.

- The unpatched control observes format loss and absent native tool at the
  provider boundary after automatic, explicitly non-overflow compaction.
- Patched cases complete zero, one and two processor-triggered compactions with
  real native read calls before and after continuation, then the real native
  StructuredOutput callback. They check original format before transformation,
  effective tool schema (including normal removal of `$schema`), instructions,
  required tool choice, first-save format, new identities and final integrity.
- The pre-loop threshold branch is exercised before a second processor-triggered
  compaction. Its original omitted overflow flag remains omitted; it is tested
  natively. The current client also validates these unchanged threshold histories.
- Two distinct schemas run in separate sessions in one native instance. Explicit
  text format and omitted format remain text after two compactions. Summaries
  contain ordinary text, no structured tool, and no accepted stage result.

`tests/native/validate_compaction.py` feeds the resulting **unaltered native
histories** to production `SessionHistory`, `extract_result()` and local schema
validation. Both baseline sessions fail with the exact reported diagnostic;
all six supported structured cases (two schemas times zero/one/two compactions)
pass, including final-envelope/history equality and tool-input/result equality.
Text and omitted-overflow histories are explicitly outside this client check.

Production validation, including `_format()` and the previous timestamp fix,
remains unchanged. Additional existing client regression coverage was strengthened:

- History regression now observes a completed summary before rejecting missing
  continuation format, after either the first or second compaction. It checks
  the exact diagnostic, event order, denied membership and unchanged input.
- Existing service fields can carry the expected format through one or two
  synthetic transitions; conflicting retry values still fail. Interleaved
  sessions with distinct nested schemas retain independent client contracts,
  and a continuation carrying the other session's schema is rejected.
- The existing HTTP negative case retains its rejection and now checks the exact
  diagnostic/event sequence. The one/two-compaction HTTP cases also check one
  prompt POST per stage session and one continuation per completed summary.
  Existing cases cover native-envelope integrity, local report validation,
  foreign identities, incomplete results, deadlines, cleanup and accounting.

The Python HTTP fixtures remain synthetic; native evidence comes from the
separate TypeScript test above. Neither proves which source installed XXX uses.

Verification on 2026-10-06:

| Check | Result |
| --- | --- |
| Native unpatched control | 1 test passed, exercising 2 independent sessions with the original defect |
| Native patched regression | 6 tests passed, including both creation paths, two compactions, two contracts and both text modes |
| Native histories through Python validation | 2 expected baseline rejections; 6 accepted final results |
| Existing native compaction/structured-output tests | 47 tests passed |
| Native package type checking, including the new test | `bun run typecheck` passed |
| Existing Python XXX suite | 71 tests in 74.912 s: 70 passed, 1 installed-XXX smoke skipped |
| Installed XXX smoke | Skipped; no XXX executable or paid provider used |
| Whitespace validation | `git diff --check` passed |

Native tests used Bun 1.3.10, WSL AlmaLinux 9 and Python 3.11.13. Baseline/patched
mode skips are deliberate: each mode runs only its own assertions. Tests ran with
external networking disabled and loopback enabled. Root was used only for the
existing namespace harness and client ownership checks.

### Reproduce and apply

Use a disposable checkout/archive of commit
`4ee426ba549131c4903a71dfb6259200467aca81` and a local Bun 1.3.10 executable.
`scripts/test-native-compaction.py` checks the pristine native source hashes and
version, installs the test temporarily, runs the baseline, applies the patch,
runs native/client validation and type checking, and restores the source files
and removes the injected test in `finally`. No global configuration is modified.

Dependency preparation needs network access once. The pinned full-workspace
frozen install fails and resolves an unreachable unrelated web dependency.
`--install` narrows only the temporary root workspace manifest to the runtime's
dependency closure, keeps the native package versions/catalog and upstream lock
as resolution inputs, disables lifecycle scripts, then restores the root manifest.
Bun updates the disposable lockfile; no dependency change is part of the patch.
After that, run verification offline from this repository:

```sh
python3.11 scripts/test-native-compaction.py /path/to/disposable/opencode \
  --bun /path/to/bun --install
bash .github/ci/offline-tests.sh python3.11 scripts/test-native-compaction.py \
  /path/to/disposable/opencode --bun /path/to/bun
```

To apply the repair permanently in a matching runtime source checkout, run
`git apply --check /path/to/opencode-v1.2.27-compaction-format.patch`, then
`git apply /path/to/opencode-v1.2.27-compaction-format.patch` and build that runtime
normally. The verifier itself intentionally restores its disposable sources.
This repository's Python adapter does not automatically patch an installed CLI.

Existing client verification:

```sh
EXPLAIN_XXX_COMPACTION_SMOKE=0 bash .github/ci/offline-tests.sh \
  python3.11 -B -m unittest discover -s tests -p 'test_xxx*.py' -v
```

The reference native repair is verified. Applying it to XXX still requires its
source/build mapping; installed XXX remains unverified. Manual compaction and
overflow/replay support are outside this change.

The sections below describe earlier client repairs and their historical results.

The production client accepts ordinary user metadata updates, reference stream
finalization and the format-preserving automatic continuation profile below.
These are client guarantees verified offline, **not proof of full compatibility
with real XXX**. For those earlier client repairs, XXX, its proprietary source,
account and provider were unavailable by task definition. No executable discovery,
installation, live CLI/version check, real smoke or model request was performed
for those repairs.

The former unconditional `COMPACTION_FORMAT_RETENTION_UNVERIFIED` rejection has
been replaced by runtime validation of the received sequence. The production
`SessionHistory` and `xxx.Server` are exercised without subclass/monkeypatch
bypasses of membership, continuation or native/local result validation. No
certificate, trusted configuration flag or developer live test is required.

## Evidence and protocol boundary

The starting HEAD was `d374a7c92ac917dffbac4265c26398edb6d0efca`; the tracked working
tree was clean. The reported eight `MESSAGE_IDENTITY_CHANGED` failures in catalog
and study do not identify the changed field. They establish a transport failure,
not a negative assessment of the analyzed project. In particular, they do **not**
prove that all eight failures were caused by `user.summary`. The regression below
is independently reproduced from client code and the public reference protocol.

The supplied diagnosis of run `20261005T115531Z-fd6a487216` reports CLI
`1.2602.15`, API `1.2.27`, and this prefix: original user request, stage assistants,
an automatic non-overflow compaction user, then an assistant with
`agent=compaction`, `summary=true`, the service user's parent ID and empty parts.
It is a shortened observation, not a complete saved HTTP response. Neither a
finished summary nor a continuation is evidenced by it. No fixtures here are
represented as recordings from that run. Old run artifacts are not changed.

Reference sources (not proof about XXX):

- [OpenCode v1.2.27 message-v2.ts](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/message-v2.ts)
  and the then-current XXX declarations (now replaced by the stock 1.2.27
  baseline): distinct user/assistant summary shapes.
- [OpenCode v1.2.27 summary.ts](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/summary.ts):
  user summary metadata receives diff updates, including empty arrays.
- [OpenCode v1.2.27 processor.ts, commit 4ee426ba](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/opencode/src/session/processor.ts#L291-L340):
  text finalization applies `trimEnd()` and replaces `time` with separate
  `Date.now()` calls for `start` and `end`.
  [Reasoning completion](https://github.com/anomalyco/opencode/blob/4ee426ba549131c4903a71dfb6259200467aca81/packages/opencode/src/session/processor.ts#L63-L108)
  preserves the existing start and adds end. Arbitrary completion-hook rewrites
  are outside the supported client profile.
- [OpenCode v1.2.27 compaction.ts](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/compaction.ts):
  the ordinary auto continuation omits format; overflow replay takes a different path.
- [OpenCode v1.2.27 prompt.ts](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/prompt.ts):
  StructuredOutput is selected from the last user's format before the messages transform hook.
- [Plugin documentation](https://opencode.ai/docs/plugins/#compaction-hooks):
  context supplements and whole-prompt replacement are different hook operations.
- [Current compaction configuration](https://opencode.ai/docs/config/#compaction):
  current documentation must not be assumed to describe this older fork.

In the inspected upstream source, `auto=false` bypasses overflow-triggered
compaction. `reserved` participates in an input/context threshold calculation;
it does not enlarge the real model limit. Pruning examines older completed tool
outputs with protection/threshold rules and runs only at particular lifecycle
points, not continuously on every long prompt. Its upstream v1.2.27 default also
differs from current documentation. None of these runtime semantics is verified
for installed XXX here.

## Client state and acceptance

### Role-specific identity and streaming

The old `_merge()` protected `summary` for all roles and then compared the entire
user info again. Both paths rejected ordinary updates. The client now compares
explicit protected fields and validates mutable metadata separately:

| Field category | Policy |
| --- | --- |
| IDs, session, role, parent, agent and task binding | Immutable; no ID rewriting |
| User model/format/system/tools/variant, creation time | Explicit protected projection; original supplied settings also checked on first observation |
| `user.summary` | Object, required `diffs` array; optional string title/body; FileDiff string file/before/after, numeric additions/deletions, optional declared status |
| `assistant.summary` | Optional boolean, immutable once observed (presence also protected) |
| Assistant usage/completion/results | Lifecycle updates; observed completion/result values and completed totals cannot be replaced |
| Unknown message info fields | Closed profile: rejected with a controlled field label; never silently stripped or automatically trusted |

An absent summary followed by `{"diffs": []}`, then updated title/body/diffs is
valid even without compaction. Empty diffs are metadata, not evidence of changed
sources. Source integrity checks remain independent. The original HTTP objects
and private artifacts retain all metadata; identity and activity projections do
not alter them. Exact JSON comparison keeps `true` distinct from `1`.

Open text/reasoning parts accept append-only growth. When the part acquires an
`end` timestamp, the client also accepts exactly the prior text with trailing
ECMAScript whitespace removed. A poll may include the final appended chunk and
completion together; the existing prefix must still match. Removing whitespace
from inside the observed prefix before appending content is rejected. The client
checks both received strings and never normalizes the saved text itself.

For a text part that previously had no `part.time.end`, the first valid end may
arrive with a replacement `part.time.start`. For example, an empty text part
with `{"start":100}` may become `"Result."` with `{"start":120,"end":121}`.
Completion is determined by **the part's end**, independently of
`info.time.completed`, `info.finish`, stage name or compaction count. This also
applies while the assistant message remains running. Preserving the original
start is still valid; reasoning parts must preserve it. Open text start changes
without an end, or changes to either timestamp after completion, remain errors.
All supplied timestamps must still be finite, nonnegative JSON numbers (never
booleans), with `end >= start`; `start == end` is not required. Ownership,
identity, content and user/request protections run unchanged. Received timestamps
are retained exactly in stored snapshots and HTTP artifacts, never restored to
the old start to make comparisons pass.

This repairs one known OpenCode 1.2.27 text completion lifecycle defect. It does
not establish compatibility with every XXX feature or fix compaction format
retention; the format and final native StructuredOutput requirements below remain.

The explicit supported trim set is U+0009–000D, U+0020, U+00A0, U+1680,
U+2000–200A, U+2028–2029, U+202F, U+205F, U+3000 and U+FEFF, following
[ECMAScript TrimString](https://tc39.es/ecma262/multipage/text-processing.html#sec-trimstring).
Python's default `rstrip()` is not used: U+0085/U+001C are not removable, while
U+FEFF is. Content rewrites, non-whitespace removal, unsupported timestamp changes and
changes to an ended part remain errors. Completed tool calls retain their
existing integrity checks (only native prune's compacted timestamp is special).

### Compaction chain and final result

`src/backends/xxx_history.py` is scoped to XXX. The unchanged original
`request_id` identifies one orchestrator attempt. Internal requests have separate
membership. The sequence is:

```
root request -> stage assistants -> auto/non-overflow compaction request
    -> linked completed summary -> format-preserving continuation -> stage assistants -> ...
```

Array order establishes the sequence; the client does not parse or sort message
IDs. It preserves first-seen messages, part owners, identities and progression
across snapshots. Missing parts can arrive later; an empty summary, including a
completed info record whose text has not arrived, remains intermediate. A bare
user identity with no parts receives no assistant-parent authority. Later empty
or subset snapshots cannot erase checked history or count as activity. The final
snapshot must contain all accumulated messages and parts.

The observed production prefix is automatic, explicitly `overflow=false`.
Overflow/replay, manual compaction, unexpected roles, additional normal users,
foreign sessions/owners, duplicate IDs, identity changes and unsupported parent
links fail with closed diagnostic codes. A synthetic flag alone proves nothing.
The production continuation recognizer requires the immediately preceding linked
completed summary, the exact reference synthetic text part with valid completed
timing, unchanged task settings and exact original format. A user info without
parts grants no membership; a summary text with an open part time stays pending.
The service user has no parentID in this profile; ordering plus the linked summary
and exact service form establish its membership. Counters ensure one continuation
per completed compaction. No root ID, parent, agent, format or native structured
object is rewritten to satisfy checks.

The final envelope must be the latest stage response, match the final HTTP
history exactly and pass the existing completion/finish checks. It must contain
native `info.structured` and a completed StructuredOutput call with matching input.
Summary agents and `summary=true` cannot become a stage result. Schema, immutable
binding, source identity, semantic/evidence, source integrity and publication
checks still run locally. Neither prose/Markdown JSON nor a summary substitutes
for the native result. `UNKNOWN_CATALOG_PATH` still requires an exact existing
path; no fuzzy path correction was introduced.

## Limits of this guarantee

Received continuation format is compared to the original type, schema and retry
count. Missing format yields `COMPACTION_FORMAT_MISSING`; a mismatch yields
`COMPACTION_FORMAT_CHANGED`. These factual failures do not affect ordinary
sessions or valid format-preserving chains. The upstream reference continuation
that omits format remains unsupported; the client does not invent a schema from
summary prose or extract a result from Markdown.

Wire validation cannot establish what XXX did internally before a model call.
The metadata records `format_retention=checked_on_continuation` and
`backend_pre_model_retention=unverified` separately. Owner live verification can
test the actual backend later; it is not a condition of completing this client
repair. Overflow/replay, manual compaction, arbitrary user messages, altered
service text and unknown message fields require separate evidence/profile work.

The client does not mutate private runtime databases, race polling against
message edits, or send another prompt while the original synchronous POST runs.
Its copy of the original schema/task binding, snapshot, coverage plan and access
restrictions remains authoritative. A model summary cannot reconstruct or amend
those values or establish new verified evidence. OpenCode V2 uses its separate
CLI adapter; this HTTP profile applies only to XXX. Native compaction creates no new
orchestrator attempt; actual configured format repairs still do.

## Configuration and summary specialization

No new switches are added. `execution` retains only execution/retry budgets.
`OPENCODE_CONFIG_CONTENT` still receives only the existing private stage-agent
overlay; unrelated fields, native compaction settings, user agents, plugins,
provider/model/authentication settings and `OPENCODE_CONFIG` are preserved.
The client applies no compaction defaults, does not disable auto, does not claim
effective global settings are known and never increases declared model limits.

`prompts/compaction.md` is a prepared **inactive supplement**, not a loaded plugin.
It specifies exact evidence locators, relationships/activation, hypotheses/gaps,
per-area coverage and next steps. Its 1500–3000-token target is advice only.
No installed XXX hook was verified, so metadata explicitly says
`summary_hook=not_applied_unverified`, `hook_conflict_check=unavailable` and
`applied_settings={}`. The client does not claim that user hook conflicts were
checked. A configured replacement hook remains untouched and no specialization
is claimed to have taken effect.

Hook integration is outside this fix. No packages, global profile edits or
repository plugins are installed. Tests check non-interference and the explicit
unavailable hook status. No research prompts, coverage plan, result policy,
models/prices, native retries or Git/source path semantics are changed.

## Budgets, diagnostics and usage

All transitions share the original `Budget`: compaction cannot restart total
time. Only substantive activity refreshes idle. New parts, changed content and
processing transitions count; repeated snapshots and user summary metadata do
not. Stored snapshots are updated independently of activity. No timeouts were
increased. Abort/delete, owned-process cleanup and source guards retain
their existing independent bounds and failure handling.

Safe diagnostic events use the existing Reporter format:
`compaction_capability`, `compaction_started`, `compaction_completed`,
`session_continued`, `session_rejected`, `compaction_rejected`. Attempt metadata includes counts,
capability status, applied settings and a closed reason code. Examples are
`COMPACTION_FORMAT_MISSING`, `COMPACTION_FORMAT_CHANGED`,
`CONTINUATION_FORM_UNSUPPORTED`, `USER_SUMMARY_INVALID`,
`MESSAGE_IDENTITY_CHANGED`, `FINAL_SNAPSHOT_MISMATCH`. No summary, prompt, source,
tool output or credentials appear in these events. Full HTTP snapshots remain
private attempt artifacts.

Identity/content rejection details contain a controlled `field`, role, part type
where relevant, session phase and transition count. For example:
`code=MESSAGE_IDENTITY_CHANGED field=info.agent role=user phase=stage transitions=0`.
Unknown keys are reported as `info.unknown`/`part.unknown`; arbitrary key names or
values are never emitted. A rejection before the first compaction emits
`session_rejected`, not a claim that compression failed.

XXX's HTTPUsage consumes the transport's checked membership directly. It includes
stage work before/after continuation and compaction work, with `origin=stage` or
`origin=compaction` and reported actual model. The stage's requested model is not
assigned to the compaction agent; its requested model is unknown (`null`). Run
aggregation preserves origin even when both agents report the same model.
Per message it chooses unique step-finish records OR message totals, never both.
Repeated snapshots replace rather than add usage. Partial/error histories stay
partial; missing counters stay unavailable. Complete is reported accounting,
not independently reconciled billing or proof of unreported runtime activity.

## Context measurements and scope

The catalog/study/revise reading protocol uses symbol/registration search,
relevant ranges (initial guide: 100–200 lines), relationship verification and brief
findings. It preserves required coverage, cross-component scenarios, sections,
diagrams and evidence checks. Tests, SQL, configuration, generators and third-party
code are not excluded for size. There is no subsystem splitting, persistent
memory, checkpoint/resume or automatic catalog repair.

Existing `_inventory` and expanded coverage membership projections are unchanged.
Each attempt records exact template, projected-context, schema and prompt sizes
in characters and UTF-8 bytes (`input_measurements`). Format repairs record zero
template bytes because they use a different prompt. HTTP metadata separately
measures the serialized native format/schema and complete request body, including
JSON escaping. `tokens=null` explicitly means no tokenizer measurement.

The complete schema remains both in prompt text and native `format.schema`.
Removing the text copy is deferred until native compatibility is established.
The following reproducible **synthetic size sample** uses
`python3 -B scripts/measure_prompt_sizes.py`; it is not a production investigation
context and does not estimate the problematic run:

| Template | Template bytes | Projected context bytes | Schema text bytes | Prompt bytes |
| --- | ---: | ---: | ---: | ---: |
| catalog | 3878 | 304 | 983 | 5375 |
| study | 16110 | 302 | 2354 | 18976 |
| revise | 6564 | 302 | 2354 | 9430 |
| review | 12153 | 303 | 2895 | 15561 |

These are bytes, **not tokens or a savings percentage**. Added reading guidance
increases template size; whether it reduces subsequent tool context or preserves
architectural quality requires real comparative measurements. Offline contract
checks cannot establish the factual correctness of an architectural report.

## Verification

Offline suite (Python 3.11+, Linux/macOS; no installed XXX or paid model):

```sh
python3 -B -m unittest discover -s tests -v
```

`test_xxx_history.py` exercises identities, format, progression, totals and budgets.
`test_xxx_compaction.py` runs owned fake HTTP servers through the real Runner,
including folder/Git, local acceptance, timeouts, errors, signals and cleanup.
The main positive tests use the production validator, including zero/one/multiple
compactions. Fake HTTP integration runs `Runner -> xxx.Server -> SessionHistory`,
metadata updates and text/reasoning completion through native extraction, local
validation and publication on a minimal artificial source tree. Backend HTTP and
unit-test clocks are synthetic; business checks are not bypassed. Successful
test continuations must never be cited as real XXX evidence.

The text completion regression starts from complete valid snapshots with stable
session/message/part IDs and consistent message/tool times. It calls production
`observe()`, closes text both before and with message completion, and continues
to native StructuredOutput extraction. It also checks preserved starts, reasoning,
append/trimEnd, repeated observations, input immutability and rejection boundaries.
The fake HTTP `history-text-completion` scenario advances one snapshot per GET
and holds the synchronous POST until the script is consumed. Thus the client
must validate the open and closed-but-running snapshots before requesting the
native result. Assertions inspect the saved HTTP artifacts and local publication.
The metadata fixture also resets text start while preserving reasoning start.

Installed-runtime smoke is skipped by default and **was not run** here:

```sh
EXPLAIN_XXX_COMPACTION_SMOKE=1 EXPLAIN_XXX_EXECUTABLE=/path/to/xxx \
  python3 -B -m unittest discover -s tests -p test_xxx_smoke.py -v
```

Optional `EXPLAIN_XXX_MODEL=provider/model` selects the smoke's stage model.
This explicit opt-in may incur provider costs. It uses only a temporary synthetic
source tree, a 180-second stage budget and no format repair. No project code is
executed. It preserves the user's runtime configuration; it cannot guarantee the
small fixture will trigger compaction for every real context size. No observed
automatic transition means failure of the smoke criterion, not success. A
production compatibility rejection also fails it; the smoke never bypasses the
validator. Missing XXX skips with an explicit not-run reason. This owner-only test
is not required for offline acceptance of the client fix.

The regression tests were run before production edits: 22 tests, with 12 failed
assertions/subtests and 10 errors, including the summary identity refusal, final
trim rejection and unconditional continuation gate. Test-only continuation
overrides were removed. Final verification results are recorded below after the
offline suite. No architectural-quality or token-savings benchmark was run.

Final verification on 2026-10-05 used AlmaLinux 9 / Python 3.11 in WSL. Commands
below ran from the repository with the existing CI network namespace wrapper,
as root solely for `unshare`/the ownership tests. External networking was disabled;
loopback fake HTTP was enabled. The smoke opt-in was explicitly set to zero.

```sh
EXPLAIN_XXX_COMPACTION_SMOKE=0 bash .github/ci/offline-tests.sh \
  python3.11 -B -m unittest discover -s tests -v
EXPLAIN_XXX_COMPACTION_SMOKE=0 bash .github/ci/offline-tests.sh \
  python3.11 -B -m unittest discover -s tests -p 'test_xxx*.py' -v
git diff --check
```

| Run | Actual result |
| --- | --- |
| Full discovery | 557 tests in 451.260 s; FAILED: 15 failures, 29 errors, 5 skips |
| Tracked tests within that discovery | 517 cases, no failures/errors; 5 skips |
| Final XXX suite, after five additional history tests and the final diagnostic edit | 61 tests in 72.606 s; OK, 1 skip (real XXX smoke) |
| Three permission tests rerun as the normal WSL user | 3 tests in 4.295 s; OK |
| Diff whitespace check | Passed |

All 44 failure/error entries in unrestricted discovery came from two pre-existing
**Git-ignored** local modules (`test_openapi_diff.py`, `test_verify_agent.py`, 40
test methods including failing subtests). Their utility scripts import absent
top-level `openapi_contract` / `contracts`. Those unrelated files were not changed
or excluded from the reported discovery result. The 517-case tracked subset is
an accounting of that same log, not a claim that unrestricted discovery passed.

The three skips caused by running as root covered unreadable files, an inaccessible results
directory and the real non-root CLI test; all passed when loaded explicitly by
`python3.11 -B -` / `unittest.TextTestRunner` as the normal WSL user. The Git-build
behavioral matrix case remains skipped because `AUDIT_GIT_BUILD` is not configured
in this environment. Real XXX smoke remains not run by task definition. No
checks of the analyzed user's project were executed.

### Text completion repair verification (2026-10-06)

Implementation used the current `main` at
`c1d59713a2054262b5f76c68e629fa33390d0359`; all six relevant files still matched
the investigation commit and the tracked tree was clean. The original history
suite passed all 36 tests. With only the new exact-transition regression and its
fixture added, the following command failed in both subtests (text closes before
or with message completion):

```sh
EXPLAIN_XXX_COMPACTION_SMOKE=0 python3.11 -B -m unittest discover -s tests \
  -p test_xxx_history.py -k test_text_completion_replaces_start_before_or_with_message_completion -v
```

Both errors were `PART_TIME_CHANGED`, through `observe -> _ingest -> _merge`,
with `phase=stage role=assistant field=part.time part_type=text transitions=0`.
After the fix, that regression and the expanded rejection/variant coverage pass.
AlmaLinux 9 in WSL used Python 3.11.13 (the default `python3` is only 3.9).
Focused verification commands:

```sh
EXPLAIN_XXX_COMPACTION_SMOKE=0 python3.11 -B -m unittest discover -s tests -p test_xxx_history.py -v
EXPLAIN_XXX_COMPACTION_SMOKE=0 bash .github/ci/offline-tests.sh \
  python3.11 -B -m unittest discover -s tests -p test_xxx_compaction.py -v
git diff --check
```

Results: **42 history tests passed** (0.136 s), **11 fake HTTP tests passed**
(55.425 s), and the whitespace check passed. The HTTP command ran as WSL root
only for the existing network-namespace/ownership setup; production startup and
listener ownership checks were not changed. After guarding the metadata test's
artifact assertions against valid empty polls, that test was rerun: one test,
both ordinary/compaction scenarios, passed in 9.334 s. Three permission tests
passed separately as the normal WSL user (4.131 s); the actual Git ownership probe
passed with `AUDIT_GIT_BUILD=modern` and Git 2.52.0 (0.367 s).

Required full discovery was also run as WSL root with external networking disabled:

```sh
EXPLAIN_XXX_COMPACTION_SMOKE=0 bash .github/ci/offline-tests.sh \
  python3.11 -B -m unittest discover -s tests -v
```

It ran **584 tests in 443.172 s: FAILED (16 failures, 29 errors, 6 skips)**.
The tracked 544 cases account for 537 passes, one failure and six skips in that
same run; this is not a claim that full discovery passed. The tracked failure is
`test_all_examples_load_without_credentials`: the unchanged
`config.opencode.example.jsonc` already specifies `deepseek/deepseek-flash` at
the investigation commit, while the test expects `None`. It also failed when
isolated as the normal user. The other 44 failure/error entries are from the
same two pre-existing Git-ignored utility modules described above, with missing
top-level `openapi_contract` / `contracts` imports. None of those files was changed.
All history and compaction HTTP tests passed again within full discovery.

Of the six skips, three permission tests and the installed Git probe passed in
the separate runs above. The remaining two are the opt-in installed OpenCode
and XXX smoke tests, which were **not run**. The macOS, other Python-version and
other Git-package CI matrix jobs were not reproduced locally.

No installed proprietary XXX runtime or paid model was used. This verifies the
client lifecycle repair against synthetic offline snapshots, not real runtime
compatibility or compaction format retention.

## Owner follow-up (later, outside offline acceptance)

1. Wait for current processes to finish. Preserve previous run artifacts. Start
   a new run on one branch with `continue_on_error=false` in the configuration
   chosen by the owner. This fix does not edit the user's configuration.
2. First check catalog/study's early history handling: absent user summary,
   `diffs: []`, subsequent title/body/diffs and stream completion should not cause
   identity errors. Verify source integrity and ordinary native/local acceptance.
3. Separately try a long session that actually triggers compaction. Check
   transitions/completed/continuations, unchanged root request ID, native
   StructuredOutput, local validation and publication. A short successful run
   with zero compactions establishes nothing about real continuation support.

If another failure occurs, share only safe diagnostic metadata initially:
adapter HEAD, failure kind/code, controlled field name, role/part type, phase,
transition/completed/continuation counts, stage and timeout classification,
whether format was present/equal (not its schema), whether message/part completion
timestamps were present, native/local-validation flags, usage coverage and cleanup
status. If relationships need inspection, use consistent pseudonyms for message,
parent and part IDs plus sequence positions/types. For a summary-shape failure,
report presence/types of title/body/diffs, not their contents. For text completion,
report lengths, timestamp presence and whether the removed suffix was in the
documented whitespace set. Do not upload full prompts, system text, schema,
summary, source files, tool output or credentials. Full private artifacts should
be retained by the owner for local examination, never rewritten to pass checks.
