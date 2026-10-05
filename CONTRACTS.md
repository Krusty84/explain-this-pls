# Evidence ledger contract

The orchestrator selects the required response schema for each stage and validates
its structure locally.
`schemas/{catalog,study,review,compare}.schema.json` and their `folder-` variants
are **model wire** schemas generated from `MODEL_SCHEMAS` / `MODEL_FOLDER_SCHEMAS`
in `src/contracts/contracts.py`. Internal `SCHEMAS` / `FOLDER_SCHEMAS` validate the expanded
representation, which retains full integrity identities. Separate `saved-*.schema.json`
files are generated from `src/contracts/saved_contracts.py` and locally validated before publication.
The `evidence-ledger` contract requires the saved schema or explicitly marked
recovery material for rendering.
Unsupported results are rejected without modifying their artifacts.

## Data provenance and storage

| Data | Authority |
| --- | --- |
| `completion_status`, narrative, claims, evidence pointers, findings, assessments, study coverage, omission-search activity | Agent; completion is a self-assessment |
| Transport completion/error/timeout/interruption and cleanup | Backend adapter and runner |
| Expected source identities, review target, contract/artifact identifiers in invocation metadata and manifest | Orchestrator |
| `program_checks` (including coverage checks), `verdict`, frozen plan, hashes, file distribution, reverse links, tables | Python, after local wire validation |
| `model_requested` | Configuration |
| `model_actual`, `model_actual_source` | Backend metadata, or null with a reason; never inferred from executable or model prose |
| `review_quality` / `semantic_quality: NOT_MEASURED` | No substantive evaluation of this execution profile has been performed |

An attempt's private `extracted.json` preserves the original model wire object.
The orchestrator verifies opaque identity bindings and writes `expanded.json`
with full internal identities before evidence normalization. `binding.json`
records `rule: "MODEL_BINDING"`, `hash_format: "canonical-json-utf8"`,
`stage`, `mode`, ID `mappings` with role/ID/identity, permitted `reference_scope`,
`wire_sha256` and `expanded_sha256`. Both digests are null before the model call;
after verified expansion they cover the whole original wire and expanded canonical
objects, not pretty-printed artifact bytes. Binding records are private
and do not enter model context. Expanding an ID adds no facts or positive status.
Transport/stdout and stderr remain private attempt artifacts. A saved `study.json`,
`review.json` or `compare.json` is a separate representation with processed wire
fields and `program_checks`. Study narrative is authored as sections/blocks;
the program computes the Markdown, block map and every document locator.
Contract/artifact identifiers (`contract_id:
"evidence-ledger"`, `artifact_format: "evidence-ledger-artifacts"`) belong only
to invocation metadata and the manifest, not agent responses or the root of saved
results. The immutable coverage plan also records `contract_id` and is embedded
in saved review plans. Study adds `review_plan` and `normalization_provenance` (including
the normalization rule). Review also adds `review_plan`, the
unchanged `claim_registry`, and the Python-derived `verdict`. These extra fields
are forbidden in wire output. The saved representation must not be re-submitted
as an agent response. Stored `program_checks` are execution records, not signatures
against later manual tampering with report files.

`program_checks` separates `execution`, `contract`, `source`, `evidence`,
`completion_self_assessment`, `document_links`, `registry_coverage`,
`omission_coverage`, `finding_ids_by_claim` and `policy_satisfied` as applicable.
Failed attempts retain invocation/validation diagnostics; they do not get a
successful result. Source guards compare state at prescribed boundaries; they
are not a continuous immutability guarantee. Orchestrator reads do not establish
which files the agent read.

`ARCHITECTURE.md` contains exactly the UTF-8 bytes of materialized study
`report_markdown`. The pure serializer normalizes CRLF to LF in authored blocks,
preserving internal spaces, tabs, bare CR and Unicode without NFC/NFKC. It writes
`## N. title` followed by two LF, then each block with a final LF if absent and
one additional separator LF. Existing blank lines are preserved. Coordinates are
counted while writing, using `evidence.lines()` semantics. Locators cover only
serialized blocks, including their final LF, never headings or separators.
Quotes use that same normalized line view of the serialized fragment, including
the edge case where an authored trailing bare CR touches a program-added LF.
After materialization no whitespace or newline changes are allowed before hashing,
review or publication. `study.annotated.md` contains generated checks and the registry.
`ARCHITECTURE_REVIEW.md` and `BRANCH_COMPARISON.md` contain generated tables;
`study.original.md` retains the canonical program assembly of authored blocks.
`review.original.md` and `compare.original.md` preserve original prose.
`SUBSYSTEM_CATALOG.md` is generated from the validated catalog and its frozen
coverage plan, with subsystem tables, exclusions, limitations and unclassified
paths. It is included in the catalog's `artifact_hashes`; the catalog's
`report_sha256` continues to identify `catalog.json`.
The final report places limitations and assessments before narrative,
and keeps contradictory assessments visible. It is a study and automated review
summary of the selected architecture revision and its corresponding review.
Generated Markdown evidence tables include ID, source, path and lines, resolution
status and encoding, without service hash columns or values. JSON keeps the full
hashes. Prompts prohibit service fingerprints, hashes and binding IDs in narrative;
source-system hash semantics and Git commit IDs remain meaningful content.
No regular-expression cleanup or other postprocessing rewrites authored prose.

Each file is replaced atomically with private permissions. Multiple files are
**not** one atomic transaction. A stage is published only after all stage writes
and successful invocation metadata writes, then its result is recorded in the
manifest. `publication_complete` in invocation metadata and the last run manifest
is the explicit completion record. A stray Markdown file from a failed write is
not a completed artifact group and is not advertised by the CLI. A failed run may
still retain successfully published earlier stages and a diagnostic final report.
The CLI prints an existing main Markdown file's full path after each published
stage, including partial results. These paths are emitted on stderr and recorded
as `report_path` in `stage_completed` events. Study and review paths identify the
specific revision; recovered material without a published Markdown file is not
advertised as a report.

## Execution metrics

Metrics are backend metadata and runner measurements, never model-authored report
fields. They do not change report acceptance, prompts, saved report schemas, or
the contents of `FINAL_REPORT.md`. OpenCode V2 uses CLI events; XXX uses the shared
HTTP collector with its own checked profile.

Each attempt's `invocation.json` adds `metrics` with `usage`, `by_model`,
`attempts: 1` and `duration_seconds`. Its enclosing invocation still identifies
the backend, requested/actual model and `invocation_id`. The new duration measures
only that attempt. The older top-level `duration_seconds` retains its existing
meaning: elapsed time against the shared stage budget, including earlier repairs.
Do not sum that older field across attempts.

The run manifest and final stdout JSON contain the same `metrics` snapshot:

- `duration_seconds`: monotonic wall time of this run, including local preparation,
  processing, temporary-source cleanup and report publication up to final metrics capture.
- `attempts`: orchestrator attempts, including failed attempts and format repairs;
  it is not a count of the backend's internal model requests or tool calls.
- `usage`: aggregate counters and their availability, described below.
- `by_backend` and `by_model`: usage breakdowns. Model entries distinguish
  `model_actual` from `model_requested`; an unknown actual model stays null.
- `stages`: ordered stage records with branch/folder, revision, stage, status,
  wall duration, attempts and usage breakdowns. Revised study is named `revise`.
  Skipped reviews and locally generated blocked comparisons consume no model tokens.

Attempts are registered once by `invocation_id`. All revisions, all branches,
format repairs and failed calls count, regardless of which report revision is
selected. Manifest aliases of the selected revision are never additional usage.
Attempt, stage and run durations are independently measured, not summed.
Preflight probes are excluded from model attempts. A `--check` run has zero attempts
and zero usage, even though XXX readiness checks start a local server.

`usage` contains `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, `total_tokens` and `cost_usd`.
Normalized input includes cache reads and writes. Cache and reasoning are
breakdowns, not additional tokens to add to total. Each counter has an independent
entry in `coverage`: `complete`, `partial` (known subtotal), or `unavailable`.
Missing or invalid numbers remain null, not zero. Optional unavailable breakdowns
do not make an otherwise reported total incomplete. `sources` records the consumed
metadata fields; `total_tokens_estimated` marks the XXX fallback calculation.
`cost_is_estimate` describes backend-reported costs; no tariffs are fetched or
applied by the runner. A reported zero cost does not establish a free invocation.

Codex uses `--json` for private JSONL events and `--output-last-message` for the
separate schema response. `turn.completed.usage` supplies tokens; total is input
plus output, with cached input and optional reasoning already included. No monetary
cost or actual model identity is inferred from the configured model.

Claude Code prefers per-model `modelUsage` token counters, falling back to `usage`
only when the model map is absent or empty. Input includes the separate cache read
and creation counts. The envelope's `total_cost_usd` is the attempt cost; it is
never added to the per-model cost breakdown. Success and error envelopes can carry
usage. A crash result (`error_during_execution`) is partial; reported zeros are
preserved but do not establish zero spend because remaining usage is unknown.

XXX collects assistant messages admitted by its stateful transport membership
model for the owned session and unchanged root request. Internal compaction and
continuation IDs are separate; they never replace that root. Per-model entries
retain `origin=stage` or `origin=compaction` through run aggregation; compaction's
actual model may differ and its requested model is not inferred from the stage.
For each message it sums unique `step-finish` parts, or uses completed
assistant-message metadata when steps are absent. Repeated snapshots replace
earlier snapshots; message totals and step totals are never added together.
A reported `tokens.total` wins. Otherwise total is computed as
`input + cache.read + cache.write + output`, assuming the fork preserves OpenCode's
convention that reasoning is included in output; that total is marked estimated.
The final validated history establishes complete reported usage. Earlier snapshots
and usage retained after a backend failure or interruption are partial.

CLI pipe logs and already observed HTTP snapshots can preserve usage after errors,
timeouts and signals. Missing final counters leave unknown remaining spend; no
extra model calls, session recovery, or extended cleanup budgets are used to obtain
it. Complete means the available backend accounting is complete, not that it has
been reconciled against an invoice or includes unreported backend activity.

OpenCode V2 runs `run --standalone --format json`, with prompts on stdin and a
private agent allowing only read/glob/grep (no tools for comparison). Its final
JSON comes from the last completed assistant step with finish reason `stop`;
errors, incomplete output and fenced or otherwise invalid JSON are rejected.
OpenCode 2.0.23 can omit the terminal event even after emitting the full answer.
After a successful CLI exit with final text but no finish event, the runner makes
one local `session export --standalone` call within the same stage budget. It
requires the same session, agent, latest assistant message and exact answer text,
plus successful session/idle outcomes, a completion timestamp and `finish: stop`.
The private export logs are retained. Failed, mismatched or incomplete exports
remain failures; no model prompt is repeated or session resumed.
Schema, binding, evidence and source checks still run locally. V1's HTTP/native
StructuredOutput contract applies only to XXX. OpenCode V2 makes one model call per
attempt and ignores the retained `opencode_format_retries` and
`structured_output_repair_attempts` settings. Substantive revision rounds still
apply. Session history uses OpenCode's standard local storage.

OpenCode CLI usage comes from distinct `step_finish` parts, deduplicated by session,
message and part ID, with the same token normalization as the HTTP collector.
A verified export supplies the missing final message's usage once, separately
labelled `opencode.session_export`; it is not added when that step was reported.
Incomplete/error streams retain partial counters. The CLI does not report the
actual model in these events, so that field remains unknown. Counters describe
reported steps; internal title generation or other unreported activity may be absent.

## Git source preparation

Existing `mode: "git"` configurations use internal source snapshots by default;
there are no dirty/untracked/snapshot options. The original repository is read-only
throughout preparation and analysis. No stash, reset, clean, checkout, switch,
index update or restoration is performed by the analyzer. Only owned temporary
sources and invocation data are removed. Folder mode retains its original behavior.

The current branch is matched by its immediate symbolic HEAD identity, never by
SHA alone. When selected, it uses working bytes. Other selected branches use pinned
commits. An unselected current branch or detached HEAD with analyzable local changes
adds a labeled working-tree revision. Ignored-only changes add none. Baseline and
requested branch comparisons remain unchanged. Deltas compare snapshot contents
recursively, including additions, removals, modes and submodules; renames appear as
deletion plus addition. A base SHA is provenance, not a working snapshot identity.

Git selects cached and non-ignored untracked paths with NUL-delimited output and
`--exclude-standard`. Standard root/nested rules, negation, info/exclude and global
excludes apply. A read-only Git config query imports only the effective
`core.excludesFile`; normal Git commands retain the hardened runtime config.
Tracked files matching an ignore pattern stay in scope. Missing files are recorded
as deletions. Partial staging never substitutes index bytes for disk bytes. Index
mode/object IDs and status are preserved separately. No hard links are used.
Symlinks are metadata only, excluded from readable files and source coverage.

Recursive submodules use their actual checkout for working snapshots, including
local modifications and untracked files. Metadata distinguishes the parent's base
gitlink, staged gitlink and actual child HEAD. Commit snapshots use the gitlinks of
each pinned commit. Required objects must exist locally. Changed submodule paths
or logical names, uninitialized nodes, conflicts, unfinished operations, sparse
checkout, unsafe metadata and configured filters remain rejected.

Preparation scans the allowed paths and ignore rules before, during and after
copying, with descriptor-relative no-follow reads and index/HEAD guards. A mismatch
fails with SOURCE_CHANGED rather than publishing mixed states. Ignored contents
are never fingerprinted. Every later stage—including substantive revisions,
review and evidence checks—reads the same independent copy. Its fingerprint is
checked at stage boundaries. These checks do not guarantee continuous OS-level
immutability or sandbox arbitrary native CLI code. Claude denies reads of the
original checkout through its native Read rules; OpenCode/XXX deny external
directory access; Codex retains its native read-only sandbox. Git environment
variables that redirect repository paths are removed from source invocations. `--check` prepares and discards the same sources, with no model calls.

## Evidence locators

An evidence record has `id`, `source_id`, `path`, `start_line`, `end_line`, `quote`.
IDs are local `E-001` etc.; references use `study:E-001` or `review:E-001`.
Prefixed references are canonical. The shared normalization rule below qualifies
bare references only when their definitions identify one namespace. Review
requires explicit namespaces when its own evidence and study evidence share an ID.
The orchestrator assigns `source-001` to the main source and subsequent IDs to
submodules sorted by root-relative path. Each nested source must be cited through
its own source ID. Git snapshot identity contains source_type (`commit` or
`working_tree`), original repository, branch (null for detached HEAD), base_commit,
snapshot_id and fingerprint. Submodule identities share the snapshot identity and
add their root path and pinned/actual HEAD. A working snapshot is not identified
by its base commit alone.
The internal folder identity is its fingerprint and canonical directory; the model
receives `source_directory` and `source_snapshot_id` instead. Folder mode does not
call Git. The resolver uses no network.

Paths use `/`, relative to the selected source root. Empty components, `.`, `..`,
absolute paths, drive paths, backslashes, NUL/control characters and `.git`
components are rejected. Every path component is opened relative to an already
opened directory descriptor with `O_NOFOLLOW`; directory/file identities are
checked before/after access. No symlink target or special file is read, including
during component replacement races. Detected source changes are fatal integrity
failures, not recoverable model format failures.
Before an agent call the runner inventories source-file hashes under the source
guards. In Git mode this is the prepared copy, not the original checkout. Evidence
paths must belong to the allowed inventory before any content read. Resolved bytes
must match that inventory, including nested sources. Working copies preserve the
actual disk bytes; commit copies use raw Git blobs without checkout transforms.
Inventories do not enter prompts and do not count as agent source inspection.

Lines are positive integers (booleans are not integers), inclusive and ordered.
Source decoding is strict; there is no replacement of invalid bytes. Lines split
on LF only; CRLF becomes LF; a lone CR stays data. A final nonterminated line
counts, and an empty file has zero lines. Document locators use the same rules.
File SHA-256 covers the original bytes. Fragment SHA-256 covers normalized UTF-8
bytes for the complete range, including its terminating LF when present. `quote`
may be `""`; otherwise it must equal that complete normalized range exactly.
There is no fuzzy correction. Document quotes must be nonblank and exact.

Limits per stage: 256 evidence records; 16 KiB canonical JSON per record; 8 MiB
per file; 200 lines and 64 KiB per fragment; 32 MiB total unique file bytes read
per resolver call (successful cache hits do not count again). Reaching/exceeding a limit that prevents full resolution gives
`LIMIT_EXCEEDED`, never successful resolution of truncated data. No source fragment
is copied into generated tables. Model-supplied quotes remain in private wire data.

Each resolution stores namespaced ID, source identity, path, range, raw file hash,
fragment hash, used encoding and status. Hashes not obtained remain null. `RESOLVED` means only
that the locator resolved. Other statuses distinguish `INVALID_POINTER`,
`UNKNOWN_SOURCE`, `SOURCE_SCOPE_MISMATCH`, `UNSAFE_PATH`, `NOT_FOUND`,
`ACCESS_DENIED`, `READ_ERROR`, `OUT_OF_RANGE`, `DECODE_ERROR`, `ENCODING_MISMATCH`, `QUOTE_MISMATCH`,
`LIMIT_EXCEEDED`. Bad/inaccessible pointers prevent positive policy acceptance
without discarding an otherwise well-formed report. A real fragment may be
irrelevant to its architecture claim; the resolver never assigns `SUPPORTED`.

`source_decoding.rules` contains `{path, encoding}` rules relative to the common
source root, including submodules. A path selects a file or directory subtree;
`.` selects the whole tree. Matching respects component boundaries and the most
specific path wins. Duplicate/unsafe paths and unknown encodings fail configuration
before model calls. Rules support UTF-8, UTF-16/32 with BOM or explicit LE/BE,
CP1251, CP1252, CP866 and Latin-1. Without a rule, a recognized Unicode BOM is used,
otherwise strict UTF-8. A compatible BOM is removed before line splitting; a
conflicting BOM is ENCODING_MISMATCH. Raw file hashes include BOM bytes; fragment
hashes follow decoding and existing LF/CRLF normalization. Normalized rules enter
the frozen review plan and change its identity. Agents receive them as context,
but CLI reading capabilities are independent; no UTF-8 copies are generated.

The resolver caches successful reads/decoding only within one call; its unique-byte
budget also includes files read before a decoding failure. Every repeated
reference still walks the path with O_NOFOLLOW and checks component identity and
file state. Replacement, mutation or disappearance raises SourceChanged rather
than silently reading a newer version. Cache data never crosses stages, attempts
or branches. Pinned raw hashes and stage-boundary source guards remain mandatory.

## Evidence ID normalization and diagnostics

`study_normalization.normalize_evidence(stage, value, context, mode='git')` is a pure
function returning `(candidate, changes)`. The candidate is a deep copy, including
when no changes are needed. It first requires the complete internal stage schema,
task, matching pinned Git/folder identity and, for review, unchanged frozen
context and exact target. The caller verifies transport before calling it.
It never repairs the schema.
The rule is `EVIDENCE_IDS` in both strict and compromise, shared by Codex,
Claude Code, OpenCode and XXX after their transport checks.

Evidence definitions may pad one/two numeric digits (`E-1`, `E-01` -> `E-001`)
and remove the stage's own namespace (`study:E-001` in study or `review:E-001`
in review -> `E-001`). Three or more digits are preserved (`E-0001` stays so).
An explicit original-definition -> canonical-ID mapping is built first. Duplicate
definitions and collisions (including `E-1` plus `E-001`) reject the entire
transformation. Structured references to mapped definitions are updated atomically,
including review findings. References already using the canonical ID are retained.
Study qualifies bare references to defined local IDs with `study:`. Review
qualifies references to its own unique definitions with `review:` and exact bare
references to unique frozen study definitions with `study:`. If the canonical ID
exists in both sets, all its bare aliases remain invalid, including short own-ID
spellings such as `E-1`; explicit namespaces are required. Review never changes
frozen study evidence or C-*.

All other fields, authored blocks, source quotations/code, source identities,
claim content and record/reference order and counts stay unchanged. Unknown IDs,
case, whitespace, punctuation and foreign namespaces are never
guessed or corrected. Aliases must derive from actual definitions; an unmatched
short spelling of an otherwise canonical definition is not invented. Repeated
normalization is idempotent and returns no changes. References are never
deduplicated: `["E-001", "study:E-001"]` becomes two identical references and is
then rejected by strict validation. `validate_result()` itself remains strict
and does not normalize or ignore unknown references.

Live processing preserves the existing source/cleanup guards:

1. Check backend completion, transport/session/request identity, and native
   StructuredOutput equality with the original completed tool input. Native
   failures retain the raw envelope privately in `response.json`; they cannot
   normalize it or admit it to local validation.
2. Retain the original object in private `extracted.json`. Verify model identity
   bindings and write `expanded.json` and `binding.json`. An unknown, foreign or
   stale binding is an identity failure and cannot be repaired or recovered.
   For study/review, check prerequisites on the expanded object, create the
   candidate, and write private `normalized.json` and `normalization.json`
   separately. These files are required even for zero edits.
3. Fully validate the internal candidate, including the study block/claim graph.
   Materialize study into required `materialized.json` and `provenance.json` and
   strictly check all computed links and hashes. A failure on a valid wire input
   is a program materialization defect, not a model line-counting error. `validation.json`
   identifies `validated_object` and includes the normalization provenance when
   available. A successful normalization is the separate `study_normalized` / `review_normalized`
   event with its replacement count, never a schema/semantic failure or model
   repair request. A later failure retains the candidate and its actual error.
4. Finish cleanup and source guards, resolve evidence against pinned bytes,
   compute policy, and freeze the registry/plan from the processed candidate.
   Recheck source and review targets at existing boundaries, then publish only
   after all required artifact and invocation writes succeed.

The private journal is a provenance object plus `changes`, for example:

```json
{
  "rule": "EVIDENCE_IDS",
  "path": "$.claims[0].evidence_ids[0]",
  "before": "E-001",
  "after": "study:E-001"
}
```

Each change has the above shape. The provenance object contains `rule`,
`hash_format: "canonical-json-utf8"`, `replacement_count`, `input_sha256`
and `normalized_sha256`. The input is `expanded.json`, after verified identity
expansion. Both SHA-256 hashes cover the respective **entire internal objects**,
serialized with Python `json.dumps(ensure_ascii=False, sort_keys=True,
separators=(',', ':'), allow_nan=False)` and UTF-8 encoded, without BOM or trailing
newline. They do not hash the pretty-printed artifact file bytes. Unicode and
array order are unchanged. With zero edits both hashes are identical.
The same provenance summary is carried in invocation metadata and every new
published study/review; direct library callers may omit that summary, but every
new Runner publication requires the normalization artifacts.
It is never requested from the model or admitted by the wire schema. Successful
normalization does not change `completion_status`, establish content accuracy,
satisfy policy, or imply publication.

Contract failures retain their broad `failure_kind` (`SCHEMA_ERROR`,
`SEMANTIC_ERROR`, or `IDENTITY_MISMATCH`) for existing control flow. Specific,
closed `details.code` values and schema-owned JSON paths distinguish:

| Code | Meaning |
| --- | --- |
| `UNKNOWN_EVIDENCE_REFERENCE` | No definition resolves the exact reference |
| `DUPLICATE_EVIDENCE_REFERENCE` | Repeated reference in one list |
| `INVALID_EVIDENCE_NAMESPACE` | Explicit namespace is unavailable at this stage |
| `CLAIMS_TYPE_MISMATCH` | `$.claims` is not an array; expected/actual types are recorded |
| `INVALID_RECORD_ID`, `DUPLICATE_RECORD_ID`, `UNKNOWN_CLAIM_ID` | Invalid, repeated or unregistered record identity |
| `DOCUMENT_LOCATOR_MISMATCH`, `INVALID_EVIDENCE_LINE_RANGE` | Invalid document binding or evidence line range |
| `TASK_IDENTITY_MISMATCH`, `SOURCE_IDENTITY_MISMATCH`, `TARGET_IDENTITY_MISMATCH` | Task, source or frozen review target mismatch |
| `MODEL_BINDING_IDENTITY_MISMATCH` | Unknown, foreign, stale or incorrectly scoped model identity |
| `UNKNOWN_REFERENCE`, `DUPLICATE_REFERENCE` | Non-evidence relations such as finding/claim links |
| `UNKNOWN_OMISSION_AREA_ID`, `DUPLICATE_OMISSION_AREA_ID` | Unknown or repeated review-plan area |

Diagnostics never echo arbitrary response values, source content or invalid IDs.
Paths contain trusted schema keys and numeric indices. Journal IDs are validated
local IDs and JSON encoded. Public retention messages state the contract reason,
that text was retained, that policy checks were not completed, and the agent's
self-assessment; they do not infer insufficient architectural evidence from a
structure error.

A string-valued `claims`, including large strings that strict JSON parsing cannot
decode, stays a string in private `extracted.json` alongside original authored blocks.
It is rejected with `CLAIMS_TYPE_MISMATCH at $.claims; expected array, got string`.
No nested decoding, partial extraction, empty-array substitution or model registry
reconstruction is performed. In compromise mode only the existing unvalidated
text-retention path applies; it never makes an accepted registry. The application
received a wrong type; this does not establish whether the model, runtime or
serialization produced it. Any future regeneration/reconstruction must be a
separate result revision with full review.

### Offline candidate checking versus a new acceptance pass

An offline caller may load an **expanded internal** study using `strict_json`, supply its
expected identity, call `normalize_evidence('study', value, context, mode)`, and run `validate_result('study',
candidate, context, mode)`. This reads no sources, calls no model, publishes
nothing and grants no new COMPLETE. Keep any outputs in a separate location;
never overwrite the original `extracted.json`, manifest, material or review.

## Frozen registry and review plan

Study claims have `id`, `statement`, `scope`, `epistemic_kind`, `evidence_ids`,
`uncertainty` on the wire. `report_sections` has exactly ten sections, with keys
`scope`, `context`, `components`, `startup_and_flows`, `data_and_state`,
`cross_cutting`, `constraints`, `change_navigation`, `unknowns`, `evidence_basis`
in that order. Each has `title` and `blocks`, each block `markdown` and `claim_ids`.
Titles and narrative use output_language. There are no model block IDs, reverse
claim.block_ids, hashes, provenance, report_markdown or document locators.
Sections and Markdown together are rejected as a hybrid wire response.

All material narrative/table/scenario claims
must be registered by the authoring agent. This requirement is not a claim that
Python can find every factual assertion in free text, or prove a block's semantic
correspondence to its claims. Definitions must be unique, block references must
exist and be unique within each block, and every claim must be linked. Multiple
claims per block and multiple occurrences per claim are supported. Empty claim_ids
are allowed for nonmaterial prose; they do not waive the registration requirement.

`src/reports/document_rendering.py` purely produces materialized `report_markdown`, `block_map`
and claims with `document_locators: [...]`, one exact line range/quote per linked
block. No Markdown AST, quote search or fuzzy match is used. `report_sections` is
not retained as a second narrative in the materialized/saved result. Its original
and normalized objects remain in the private wire artifacts.

For example, the first section `{key: "scope", title: "Область", blocks:
[{markdown: "Вход — Dispatcher.", claim_ids: ["C-001"]}]}` writes
`## 1. Область\n\nВход — Dispatcher.\n\n`; C-001 receives
`document_locators: [{start_line: 3, end_line: 3, quote: "Вход — Dispatcher.\n"}]`.
Further occurrences receive separate locators, never a spanning range.
Always `quote == "".join(lines(report_markdown)[start_line-1:end_line])`.

Materialization provenance uses `study-blocks-lf` and records distinct hashes
of normalized wire, exact UTF-8 document, final registry and block map. Combined
private provenance also records expanded/normalized object hashes and the ID edit
journal separately. Identical canonical input yields identical text/locators/hashes.
Saved schemas use MATERIALIZED_CLAIM, never the locator-free wire CLAIM.

Before review, `claim.registry.json` and `review.plan.json` fix the source catalog,
document SHA-256, registry SHA-256, required IDs, mandatory omission areas, and
study eligibility, normalized decoding rules and immutable coverage plan. Every configured priority scenario becomes another mandatory
omission area. The model echoes `review_target_id`, which the orchestrator resolves
to the internal `target` containing all four hashes (source, document, registry,
plan). Repeating the opaque ID does not prove reading the document.

Canonical JSON uses Python `json.dumps(ensure_ascii=False, sort_keys=True,
separators=(',', ':'), allow_nan=False)` encoded as UTF-8, without a BOM/newline.
Array order is preserved, Unicode is not normalized. Registry hashing covers the
entire ordered claims array. Plan hashing excludes its own `plan_sha256`; no
timestamps, invocation IDs or random values enter it. Source hashing covers the
catalog. Any changed statement, scope, evidence link, document byte, source or
plan requires a new review identity; old acceptance does not transfer.

Review `claims` contain only `id`, `outcome`, `evidence_ids`, `limitation`.
The renderer takes original statements/scope from the frozen registry. Findings
have the only canonical claim relation: `findings[].claim_ids`. Reverse links
are generated.
Omissions reference findings without replacing required claim responses.

| Frozen kind | Allowed outcomes | Rule |
| --- | --- | --- |
| FACT | SUPPORTED | Agent assessment with at least one resolved cited pointer |
| FACT | CONTRADICTED | Evidence plus linked HIGH/MEDIUM finding |
| FACT | UNVERIFIABLE | Concrete limitation plus linked HIGH/MEDIUM finding; cannot pass |
| Any | NOT_CHECKED | Concrete limitation; cannot pass |
| HYPOTHESIS / UNKNOWN | CAVEAT_ACCEPTABLE | Caveat assessed as adequate; does not establish a fact |
| HYPOTHESIS / UNKNOWN | CAVEAT_INADEQUATE | Concrete limitation plus linked HIGH/MEDIUM finding |

Hypotheses/unknowns need concrete uncertainty or missing checks in study, but do
not automatically require runtime evidence or a fabricated material finding.
Every finding needs evidence and a concrete proposed correction. Missing review
responses are valid partial material, not invented agent answers. Duplicate or
unknown IDs/references and incompatible outcomes are contract violations.

Registry coverage retains expected, received, assessed, unchecked and missing
IDs/counts. Its denominator is the frozen registry only; an empty denominator is
null, never 100%. Document linkage counts only registered claims. Omission search
tracks every required area and reported inspected/partial/uninspected state;
these are agent reports, not measured completeness of source exploration.

Positive policy requires valid identities/contracts, passed source guards,
complete published study/review, nonempty linked registry, all evidence resolved,
one assessed response per required claim, no unfinished/missing mandatory omission
area, COMPLETE self-assessments, no blocking findings, and only SUPPORTED facts or
CAVEAT_ACCEPTABLE uncertainties. Python computes PASS, CHANGES_REQUIRED (material
agent findings), or INCONCLUSIVE. An empty registry or unsupported material fact
cannot pass. Neither policy nor these offline tests measure factual correctness.

## Comparison and validation

Comparison is reports-only. A structured reference identifies branch, artifact
(`study`/`review`), selected revision ID, claim ID and `review_target_id`. The
orchestrator verifies the branch, selected revision and artifact role, then expands
the binding to the internal document and registry hashes. Strong differences
need references resolving on **both distinct sides** and policy-accepted inputs
on both sides. Each side needs a referenced FACT assessed as SUPPORTED; acceptance
of a hypothesis's caveat is not factual support for a strong contrast.
Recovered/incomplete inputs stay unverified. Missing reports
do not prove absent components, and missing comparison does not mean no differences.
An unresolved or unverified difference prevents positive comparison policy even
when the agent reports COMPLETE. A response with no listed differences is still
an agent assessment of the supplied reports, not proof of identical behavior.

`strict` rejects wire contract violations. `compromise` may retain completed text
with matching task/source identity as unvalidated material, but never invents
claims/evidence/positive assessments. A wrong review target, incomplete transport,
source mutation or cleanup failure cannot be repaired into usable acceptance.
Custom prompts must emit the current model wire schema. Contract diagnostics retain
the failure kind and schema-owned violation paths. In compromise, usable study
`report_sections` blocks may be retained as unvalidated narrative, labeled program
assembly of authored blocks. A study wire response without `report_sections` is rejected and
cannot enter recovery, review or format repair. No partial registry is presented
as accepted, and empty-registry review is explicitly limited/ineligible.
An existing manifest with missing or unsupported `contract_id` / `artifact_format`
causes `UNSUPPORTED_ARTIFACT_FORMAT` before any artifact writes. Use a new run directory.
Ordinary successful execution uses catalog → study → review, optionally one
revised study → full review, then selection → compare (comparison only for
multiple Git branches). Format repairs stay bounded and may
not change already valid facts/evidence/assessments. Orchestrator format repairs apply
only to XXX; OpenCode V2 uses final JSON with local validation.

## Compact model context

A pure projection builds the model request without mutating internal context.
Review receives the whole document Markdown once and claim_registry once. Claim
coordinates retain start/end lines without duplicate quotes; block_map, duplicated
registries and private provenance are omitted. Comparison, substantive revision
and format-repair contexts use the same principle. Findings, evidence, limitations,
identifiers and diagnostics remain available; narrative is neither truncated nor
summarized. Full saved locators, registry hashes and document hashes remain intact.
Projection removes service hash fields by structure throughout nested documents,
evidence resolutions, plans, previous revisions, comparison inputs and private
provenance; it never searches prose for hexadecimal strings. Git commit IDs,
including commits from repositories using SHA-256, remain unchanged.

Model folder identities use `source_snapshot_id`; model reviews and comparison
references use `review_target_id`. The orchestrator generates opaque random IDs
with `S-` / `T-` prefixes and 16 following characters outside the pure projection.
Bindings are stable within a run and across format attempts for the same snapshot
or frozen revision. New runs receive new IDs. Private mappings retain full source
and target identities. An ID must match its purpose, source, revision and reference
role; unknown, foreign or stale IDs fail identity checks without repair or recovery.

Format repair compares original model responses before binding expansion and cannot
change facts. Full original and invalid responses remain private; the repair prompt
removes known service hash fields even from malformed response objects, preserving
prose, quotes and diagnostics. Equal original/invalid objects are sent once with
both roles identified. Recovery uses only the first original response after its
identity has been verified; it never promotes contract-invalid text to acceptance.

Invocation metadata records `context_format: "compact-context"`.
`input.prompt.txt` contains the exact request actually sent, with its hash recorded
in invocation metadata. The backend receives the model schema, while local
validation and publication use expanded internal and saved schemas.

## Subsystem catalog and coverage

The catalog stage uses the same read-only source scope and boundary guards as study.
Its wire task is architecture_catalog, with source identity, completion_status,
limitations, subsystems `{id,name,purpose,paths}` and exclusions `{path,reason}`.
The agent receives source information and a compact directory summary; Python
checks selectors against the full file inventory. Explicit exclusions and symlinks
remain represented, and links are never followed. Unallocated entries become
UNCLASSIFIED. The resulting coverage.plan.json is immutable for the snapshot and
shared by all document revisions; there is still only one study per version.

In compromise mode an invalid catalog produces DIRECTORY_FALLBACK areas from
top-level directories and root files, retaining explicit limitations. In strict
mode that failure stops the current source. continue_on_error controls continuation
and false stops further calls in both policies. Integrity, cleanup and publication
errors stop the run regardless of policy. An incomplete or fallback catalog and
remaining UNCLASSIFIED prevent COMPLETE even if usable documentation is retained.

Study adds coverage entries `{area_id,status,evidence_ids,limitation}` with status
INSPECTED, PARTIALLY_INSPECTED or NOT_INSPECTED for every area. A missing response
blocks acceptance; unfinished inspection blocks acceptance only for required areas.
Empty or fully excluded areas may report NOT_INSPECTED with a limitation. INSPECTED
needs a resolved source pointer inside that area. This is evidence of support, not
proof of exhaustive reading. Review omission_search includes required catalog areas as well
as thematic scopes and user scenarios; missing/unfinished mandatory responses block
acceptance. Final output separately reports file allocation and agent assessments.
Neither is called completeness of system understanding; semantic_quality stays
NOT_MEASURED.

## Bounded revision and selected artifacts

execution.max_revision_rounds is 0 or 1, default 1. Revision starts only after a
published, strictly valid study and review, when review completion_status is
COMPLETE and there are HIGH/MEDIUM findings. LOW-only findings, incomplete review,
transport failures and recovered contract-invalid material do not trigger it.
prompts.revise supplies a separate prompt to the existing study agent; no revision
backend profile is introduced. stage_agents.catalog inherits study settings.
prompts.catalog controls catalog. Each substantive stage has its own budget,
shared with its format-repair attempts, for at most five stages per snapshot.

The program compares registries for added, removed and changed claims. Coordinate
changes alone are not semantic edits; unchanged assertions retain their IDs.
Previous claims/findings are addressed by revision plus local ID. Repeat review
receives the old substantive obligations and returns prior_findings records
`{revision_id,finding_id,status,explanation}` with RESOLVED, UNRESOLVED or NOT_CHECKED.
Initial review returns an empty list. The entire new registry and all mandatory
areas are reviewed again. Missing or unresolved obligations and new material
findings block acceptance. Only reviewer assessment can close an issue by deleting
an erroneous statement; deleting necessary explanation leaves an issue or omission.
The loop ends after the second review regardless of its result.

revisions/001 and revisions/002 hold separate immutable study/review states and
artifacts. Selection prefers an accepted pair; otherwise it uses the latest
published pair whose review has completion_status COMPLETE; PARTIAL does not count
as completed. An incomplete new review keeps the previous
completed pair selected and exposes the newer study separately as unverified.
Without any completed review, the latest usable study is selected with a warning.
The manifest records selected_revision and revision history. Comparison uses only
selected pairs. Top-level ARCHITECTURE.md, study.json and companion files are exact
copies of the selected artifacts, published after selection. Frozen-document checks
run within each revision; a new study can never inherit an older review.

XXX uses the shared inspected OpenCode envelope checks: successful `stop` and
`tool-calls` were already supported. Closed diagnostics distinguish FINISH_MISSING,
FINISH_INVALID_TYPE, FINISH_UNKNOWN, FINISH_TRUNCATED (`length`), FINISH_ERROR
(`error`) and UNFINISHED_TOOL_CALL. No other success reason is enabled. Raw finish
and envelope remain private; classification is separate. Session/request/agent,
completion timestamp, errors, pending tools, native structured result, completed
StructuredOutput/input equality and exact final history snapshot equality remain mandatory.
The XXX transition model compares a role-specific projection of protected fields:
IDs/session/role/parent/agent, task settings and creation time remain immutable.
`user.summary` is mutable metadata with required FileDiff array `diffs` and optional
string `title`/`body`; `assistant.summary` is a protected boolean. Unknown message
fields require an explicit profile update. JSON booleans and numbers remain distinct.
Raw HTTP objects are preserved; the final envelope must match the full final history.
Text/reasoning may append while open and trim only trailing ECMAScript whitespace
when part time acquires `end`, including a final chunk coalesced with completion.
Content replacement, non-whitespace truncation and changes after completion fail.
Empty/incomplete compaction summaries can remain pending within the original budget.
Production continuation accepts only the auto/non-overflow service request, its
linked completed summary and exact reference continuation shape with unchanged
settings and original `format`. Missing/changed format yields a factual refusal;
no external certificate or developer live smoke is required. The wire checks do
not prove backend actions before a model call. Overflow/replay and other service
forms remain unsupported. Only verified membership contributes to usage; summary
metadata/repeats do not extend idle time or create attempts, and native transitions
never restart the stage budget. See [XXX history and compaction](docs/xxx-compaction.md).
A complete-looking JSON never overrides these checks. Compare failure preserves
prior study/review and the diagnostic summary with a nonzero exit and no assertion
that differences are absent.

The small local schema subset implements exact object/array/string/boolean/integer
types, enum, required/properties/additionalProperties, items, minLength, minimum,
minItems. Artifact-only schemas also use number/null type unions and typed maps
through additionalProperties. Booleans never satisfy integer/number types. Wire schemas avoid dialect-specific
extensions; explicit local rules enforce whitespace, IDs, typed links, locators,
identity, kind/outcome combinations and policy. Empty findings/evidence lists and
empty optional limitation/quote/uncertainty strings remain legal where specified.

Exit codes remain operational: 0 means processing/review policy satisfied (or
successful `--check`), 2 means partial material under the chosen policy, 1 means
failure, 130 means interruption. `--check` checks local prerequisites only, makes
no model call, tests no provider authorization/model availability and creates no
fictional review. English CLI and configurable report language use equivalent
attribution: “supported according to the agent”, “agent reported a contradiction”,
“policy checks satisfied; factual correctness is not established”. Prompts cannot
programmatically guarantee honest wording throughout arbitrary model prose.
