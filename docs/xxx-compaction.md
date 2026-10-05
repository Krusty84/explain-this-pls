# XXX history: metadata, streaming and checked compaction continuation

The production client accepts ordinary user metadata updates, reference stream
finalization and the format-preserving automatic continuation profile below.
These are client guarantees verified offline, **not proof of full compatibility
with real XXX**. XXX, its proprietary source, account and provider are unavailable
by task definition. No executable discovery, installation, live CLI/version check,
real smoke or model request was performed for this fix.

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
  and the saved `schemas/xxx-declarations.json`: distinct user/assistant summary shapes.
- [OpenCode v1.2.27 summary.ts](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/summary.ts):
  user summary metadata receives diff updates, including empty arrays.
- [OpenCode v1.2.27 processor.ts](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/session/processor.ts):
  text/reasoning finalization applies `trimEnd()`; arbitrary completion-hook rewrites
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

The explicit supported trim set is U+0009–000D, U+0020, U+00A0, U+1680,
U+2000–200A, U+2028–2029, U+202F, U+205F, U+3000 and U+FEFF, following
[ECMAScript TrimString](https://tc39.es/ecma262/multipage/text-processing.html#sec-trimstring).
Python's default `rstrip()` is not used: U+0085/U+001C are not removable, while
U+FEFF is. Content rewrites, non-whitespace removal, timestamp regression and
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
those values or establish new verified evidence. OpenCode's closed capability
gate and native-retry policy remain unchanged. Native compaction creates no new
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
