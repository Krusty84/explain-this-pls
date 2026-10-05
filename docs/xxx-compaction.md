# XXX compaction: checked client history and remaining compatibility gate

**Full automatic compaction support is not established.** This change implements
the client transition validator, accounting, diagnostics, source-reading guidance,
offline regression fixtures and an opt-in installed-runtime smoke test. It does
not claim that the installed proprietary XXX preserves StructuredOutput across
compaction. XXX was not found in Windows PATH or the available AlmaLinux WSL
profile during this work. No paid provider call or real continuation was recorded.

The default production profile accepts the reported start of compaction and waits
for incomplete summaries within the original stage budget. It rejects an
unverified continuation explicitly rather than manufacturing success. This
controlled failure is **not** completion of the full compaction support criterion.
No version string, `/doc` match, environment variable or user configuration option
opens the continuation gate. Tests bypass precisely that gate for synthetic data.

## Evidence and protocol boundary

The supplied diagnosis of run `20261005T115531Z-fd6a487216` reports CLI
`1.2602.15`, API `1.2.27`, and this prefix: original user request, stage assistants,
an automatic non-overflow compaction user, then an assistant with
`agent=compaction`, `summary=true`, the service user's parent ID and empty parts.
It is a shortened observation, not a complete saved HTTP response. Neither a
finished summary nor a continuation is evidenced by it. No fixtures here are
represented as recordings from that run. Old run artifacts are not changed.

Reference sources (not proof about XXX):

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

`src/backends/xxx_history.py` is scoped to XXX. The unchanged original
`request_id` identifies one orchestrator attempt. Internal requests have separate
membership. The sequence is:

```
root request -> stage assistants -> compaction request -> summary
    -> [verified native continuation required] -> stage assistants -> ...
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
The reference continuation recognizer requires the preceding completed summary,
the exact reference service part, unchanged task settings and exact original
format; even then production authorization remains closed. No root ID, parent,
agent or native structured object is rewritten to satisfy checks.

The final envelope must be the latest stage response, match the final HTTP
history exactly and pass the existing completion/finish checks. It must contain
native `info.structured` and a completed StructuredOutput call with matching input.
Summary agents and `summary=true` cannot become a stage result. Schema, immutable
binding, source identity, semantic/evidence, source integrity and publication
checks still run locally. Neither prose/Markdown JSON nor a summary substitutes
for the native result. `UNKNOWN_CATALOG_PATH` still requires an exact existing
path; no fuzzy path correction was introduced.

## Minimum backend work still required

This is a compatibility requirement, **not a patch for unseen fork source**:

1. Establish, with runtime evidence, a supported mechanism that retains the
   original `format.type`, exact schema and `retryCount` on the user message used
   by the next model call, before tool construction. Preserve the stage agent,
   model selection, tools/system/variant and the managed permission overlay.
   Exercise this through repeated compactions and all supported replay paths.
2. Document the exact service/continuation wire shapes and their lifecycle order,
   including progressive message/part snapshots. Extend the closed profile only
   for those verified forms. Preserve the native result/parent IDs on the wire.
3. Demonstrate automatic compaction, continuation, native StructuredOutput and
   independent local validation in the installed-runtime smoke. Wire schema
   compatibility and successful runs without compaction are insufficient.

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

Once the fork's supported hook is confirmed, load this supplement through a
run-local plugin outside the investigated repository, append to the default
prompt, preserve existing hooks and detect whole-prompt replacements in the
confirmed execution order. Such a conflict must fail explicitly, including
replacements by later hooks; simply appending context that is then ignored is
insufficient. No packages, global profile edits or repository plugins are
installed by this change. Hook installation/conflict execution tests remain
pending that backend integration; current tests check non-interference and the
explicit unavailable status instead of simulating a proven hook.

## Budgets, diagnostics and usage

All transitions share the original `Budget`: startup, polling, compaction and
continued work cannot restart total or idle time. Repeated snapshots do not
constitute activity. Abort/delete, owned-process cleanup and source guards retain
their existing independent bounds and failure handling.

Safe diagnostic events use the existing Reporter format:
`compaction_capability`, `compaction_started`, `compaction_completed`,
`session_continued`, `compaction_rejected`. Attempt metadata includes counts,
capability status, applied settings and a closed reason code. Examples are
`COMPACTION_FORMAT_MISSING`, `COMPACTION_FORMAT_CHANGED`,
`COMPACTION_FORMAT_RETENTION_UNVERIFIED`, `CONTINUATION_FORM_UNSUPPORTED`,
`MESSAGE_IDENTITY_CHANGED`, `FINAL_SNAPSHOT_MISMATCH`. No summary, prompt, source,
tool output or credentials appear in these events. Full HTTP snapshots remain
private attempt artifacts.

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
The explicit synthetic gate bypass is in tests only. Successful test continuations
must never be cited as real XXX evidence.

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
gate. Missing XXX skips with an explicit not-run reason. Genuine success remains
dependent on the backend work above.

Validation on 2026-10-05: the repository-tracked suite plus the new compaction
tests ran in AlmaLinux 9 / Python 3.11 with external networking disabled: 502
tests, OK, 5 skipped (including the installed-XXX smoke). Subsequent focused
checks of the final history/accounting/configuration changes passed 45 tests;
the standalone history suite passed 20 tests after the exact JSON input comparison
was strengthened. All 17 shared HTTP/OpenCode tests passed after that change as
well. No architectural-quality or token-savings benchmark was run.

An unrestricted discovery in this existing working directory also loaded two
pre-existing **Git-ignored** local test files (`test_openapi_diff.py` and
`test_verify_agent.py`). That run attempted 537 tests and reported 15 failures
and 29 errors, all in those two modules: their local utility scripts still import
the absent top-level `openapi_contract` / `contracts` modules. Those unrelated
ignored utilities were not repaired or removed. The tracked-suite result above
does not hide or reclassify the unrestricted discovery failure.
