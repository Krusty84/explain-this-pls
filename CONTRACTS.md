# Evidence ledger contract

## Optional review and processing success

`execution.review_enabled` is a strict boolean and defaults to `false` in JSON
and JSONC configurations. It applies to every source and backend in the run.
With review disabled, the source pipeline is catalog → planned study; comparison remains
available for multiple selected Git branches. Review/revision agents and prompts
are inactive, and `max_revision_rounds` has no effect. Set `review_enabled: true`
explicitly to retain the reviewed workflow, including its bounded revision loop.

New manifests, source results and revisions record `review_enabled`. Disabled
reviews have `review: null` and `review_skipped: "disabled_by_config"`; progress
and metrics record SKIPPED with no model attempts or usage. No review result or
invocation artifacts are fabricated. The study's frozen registry and
`review.plan.json` remain necessary for integrity and comparison identities.

`accepted` retains its study-AND-review meaning. `workflow_satisfied` is computed
separately by Python: a complete, valid, published study satisfying evidence,
coverage and identity checks is sufficient when review is disabled; with review
enabled the existing accepted-pair policy applies. Run success additionally
requires successful applicable comparison and no blocking run errors.
Both strict and compromise modes can finish COMPLETE / exit 0 without review.
Recovery material, coverage gaps and integrity failures do not gain acceptance.
Old saved results and library contexts without `review_enabled` retain the prior
review-required interpretation; this differs intentionally from the new config default.

Comparison contexts include review mode and computed workflow results. Disabled
review alone does not make an input unresolved. Without review, differences use
REPORTED_UNVERIFIED; successful processing requires selected, valid FACT references
with resolved source evidence on both sides of each difference. Insufficient
support or INSUFFICIENT_EVIDENCE prevents positive processing policy. The existing
CONFIRMED_DIFFERENCE requirement for supported facts in reviewed, accepted pairs
is unchanged and that classification is forbidden in review-disabled runs.
An empty difference list still requires complete eligible input studies on all
requested branches. Success is not factual verification. Agent response schemas
and artifact formats are unchanged.

The review and revision sections below describe the opt-in reviewed workflow.

The orchestrator selects the required response schema for each stage and validates
its structure locally.
`schemas/{catalog,study,study-shard,synthesis,review,compare}.schema.json` and the
available `folder-` variants
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
| Synthesized study `evidence`, `claims`, `coverage` | Python deep-copies the frozen, remapped records from validated shards; the synthesis agent cannot supply them |
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
For synthesis, expansion also inserts the three frozen registry arrays after
model identity and synthesis schema validation. `extracted.json` has only the
model-authored response; `expanded.json` has the complete study input. The same
binding hashes cover these distinct objects. Direct studies and revisions still
supply their own registry arrays under the full study wire schema.
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
`SUBSYSTEM_CATALOG.md` is generated from the validated or recovered catalog and its frozen
coverage plan, with subsystem tables, exclusions, limitations and unclassified
paths. It is included in the catalog's `artifact_hashes`; the catalog's
`report_sha256` continues to identify `catalog.json`.
The final report places limitations and assessments before narrative,
and keeps contradictory assessments visible. It summarizes the selected architecture
revision and its corresponding review when that optional stage is enabled.
Generated Markdown evidence tables include ID, source, path and lines, resolution
status and encoding, without service hash columns or values. JSON keeps the pinned
file hashes. Prompts prohibit service fingerprints, hashes and binding IDs in narrative;
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
the contents of `FINAL_REPORT.md`. OpenCode V2 uses CLI events; XXX uses verified
CLI session exports, with partial pipe-event accounting after a failure.

Each attempt's `invocation.json` adds `metrics` with `usage`, `by_model`,
`attempts: 1` and `duration_seconds`. Its enclosing invocation still identifies
the backend, requested/actual model and `invocation_id`. The new duration measures
only that attempt. The older top-level `duration_seconds` retains its existing
meaning: elapsed time against the stage budget.
Do not sum that older field across attempts.

The run manifest and final stdout JSON contain the same `metrics` snapshot:

- `duration_seconds`: monotonic wall time of this run, including local preparation,
  processing, checkout restoration and report publication up to final metrics capture.
- `attempts`: orchestrator attempts, including failed attempts;
  it is not a count of the backend's internal model requests or tool calls.
- `usage`: aggregate counters and their availability, described below.
- `by_backend` and `by_model`: usage breakdowns. Model entries distinguish
  `model_actual` from `model_requested`; an unknown actual model stays null.
- `stages`: ordered stage records with branch/folder, revision, stage, status,
  wall duration, attempts and usage breakdowns. Revised study is named `revise`.
  Skipped reviews and locally generated blocked comparisons consume no model tokens.

Attempts are registered once by `invocation_id`. All revisions, all branches,
and failed calls count, regardless of which report revision is
selected. Manifest aliases of the selected revision are never additional usage.
Attempt, stage and run durations are independently measured, not summed.
Preflight probes are excluded from model attempts. A `--check` run has zero attempts
and zero usage. XXX readiness checks invoke only version/help commands.

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

XXX (OpenCode 1.2.27) accounts for the assistant messages in its verified final
session export. It sums unique step-finish counters for each message, using
assistant totals only when no steps exist. Export and stdout counters are never
added together. Actual models come from assistant metadata; summary-model usage
has origin=compaction, separate from origin=stage. The stage's actual model is the
final non-summary assistant model. Missing counters remain unavailable. Pipe-only
usage after a failure, timeout or interruption remains partial.

Complete accounting means the available backend accounting is complete, not that
it has been reconciled against an invoice or includes unreported activity.

### XXX CLI profile

backend: "xxx" selects opencode-v1.2.27-cli, including branded executables.
expected_version retains exact comparison with the executable's version output;
branding does not switch backend or model. Preflight checks --version, run --help,
export --help and session delete --help. It creates no session or model request
and does not prove provider readiness.

Each substantive stage runs:
    xxx run --format json --agent <private-agent> --title <invocation-title>
An explicit model adds --model provider/model; otherwise the configured default
is preserved. The prompt and schema arrive through stdin. No standalone, attach,
continue or session option is sent. The transient V1 config uses agent/permission,
allows read/glob/grep/list for source stages, and denies tools for compare.
Source stages retain external_directory denial. User authentication, configuration,
compaction and hooks stay intact; automatic sharing is disabled for this child.
No installed binary, global configuration or other backend adapter is modified.

The CLI emits NDJSON events, not a native schema response. Its final answer must
be one JSON object, optionally inside the single JSON fence described below. After one successful run,
one local export <sessionID> confirms session/title/directory, original prompt,
agent, model, message/part identities and exact final text. The V1 export shape is
messages[].info/parts. The final assistant must be completed, finish with stop,
have no error or unfinished tools, and not be a compaction summary. A missing
CLI step_finish can be confirmed by this export; a contradictory terminal event
cannot. Conflicting duplicate events, foreign identities and invalid JSON fail.

Automatic compaction runs inside the same CLI process and stage budget.
The exporter verifies the service/summary/continuation chain. Fence normalization
applies only to the verified final response, before the existing compaction-summary
fallback selection. Summary parsing is unchanged. No HTTP recovery or extra format-correction prompt exists.
Local schema, binding, normalization, semantic and source checks stay authoritative.
strict rejects invalid results; compromise may retain eligible material with
warnings, without treating it as accepted or requesting another model answer.
Substantive review/revise remains a separate, unchanged pipeline operation.

Legacy http_timeout_seconds, api_doc_timeout_seconds, opencode_format_retries
and structured_output_repair_attempts remain accepted config keys but have no
effect on XXX execution. retry_policy reports zero orchestrator retries and null
format_retries_requested; no native retry-enforcement guarantee is claimed.

Private attempt directories retain input.prompt.txt, schema.json, stdout.log,
stderr.log, session-export logs, extracted/expanded/validation records and metadata.
Stage/idle budgets cover run and export; stdout/stderr count as CLI activity.
Owned process groups are stopped on timeout or interruption. A separate five-second
cleanup budget exports an interrupted session once if needed, then calls
session delete only after ownership is proven. Unknown sessions are never guessed,
listed or deleted. Cleanup errors are recorded, block acceptance, and never mask
the original failure. Reports are published after cleanup and integrity checks.

The pinned CLI source is OpenCode v1.2.27 commit
4ee426ba549131c4903a71dfb6259200467aca81, cli/cmd/run.ts, export.ts and session.ts.
Protocol fixtures are synthetic and use no model. Installed-XXX compaction smoke
remains explicit opt-in and may incur provider cost; it is never part of --check.

OpenCode V2 runs `run --standalone --format json`, with prompts on stdin and a
private agent allowing only read/glob/grep (no tools for comparison). Its final
JSON comes from the last completed assistant step with finish reason `stop`;
errors, incomplete output and invalid JSON are rejected. The same single JSON
fence normalization applies after transport verification.
OpenCode 2.0.23 can omit the terminal event even after emitting the full answer.
After a successful CLI exit with final text but no finish event, the runner makes
one local `session export --standalone` call within the same stage budget. It
requires the same session, agent, latest assistant message and exact answer text,
plus successful session/idle outcomes, a completion timestamp and `finish: stop`.
The private export logs are retained. Failed, mismatched or incomplete exports
remain failures; no model prompt is repeated or session resumed.
Schema, binding, evidence and source checks still run locally. OpenCode V2 makes one model call per
attempt and ignores the retained `opencode_format_retries` and
`structured_output_repair_attempts` settings. Substantive revision rounds still
apply. Session history uses OpenCode's standard local storage.

XXX and OpenCode V2 first parse the complete final response with `strict_json()`.
If parsing fails, they accept exactly one triple-backtick code block labelled
`json` (case-insensitive), with opening and closing fences on separate lines.
Surrounding spaces, tabs and blank lines, and LF, CRLF or CR line endings are allowed.
The payload is sliced without content changes and parsed with `strict_json()`;
the result must be an object. Prose outside the block, multiple or nested blocks,
missing fences, other labels, arrays, scalars, malformed JSON, duplicate keys and
non-finite numbers remain invalid. There is no substring search or JSON repair.

This is transport compatibility, not relaxed contract validation. Schema, source
identity, semantics, evidence and security checks remain authoritative. It does
not change claims, acceptance policy or self-assessments, and adds no model call.
Codex and Claude Code parsing is unchanged.

Private `stdout.log` and session-export logs retain the original transport bytes,
including the exact response text. `extracted.json` separately retains the parsed
wire object. Invocation `provider_metadata.response_normalization` records
`kind: "markdown_json_fence"` and zero-based, end-exclusive character offsets
`payload_start` / `payload_end` into the joined final response. These offsets
recover the exact payload, including its whitespace, from the logged response.
XXX also records the final `message_id` in this provenance, including when invalid
final JSON uses the unchanged summary fallback. If payload parsing fails without
a fallback, the same record is in the error details. Successful
normalization is not a validation failure or compromise recovery.

OpenCode CLI usage comes from distinct `step_finish` parts, deduplicated by session,
message and part ID, with the existing native token normalization.
A verified export supplies the missing final message's usage once, separately
labelled `opencode.session_export`; it is not added when that step was reported.
Incomplete/error streams retain partial counters. The CLI does not report the
actual model in these events, so that field remains unknown. Counters describe
reported steps; internal title generation or other unreported activity may be absent.

## Git source access and lifecycle

`mode: "git"` analyzes requested branches sequentially in the original repository.
There is no copy-based mode or source strategy setting. Folder mode is unchanged.
The root and every recursive submodule must start clean, with each submodule HEAD
matching its parent's gitlink. Ignored untracked files are permitted; other
untracked files and staged or tracked modifications are rejected.

Preflight records original refs and SHAs, resolves all requested branch SHAs and
recursive gitlinks, and checks required commit/tree/blob objects locally. Submodule
paths and logical names must match the original hierarchy. Uninitialized nodes,
conflicts, unfinished operations, sparse checkout, unsafe metadata, and configured
filters remain rejected. No fetch, initialization, or custom submodule update runs.
`--check` performs these checks and CLI capability checks without source inventories,
checkout switches, source copies, or model calls.

For each branch, the orchestrator switches the root and then its submodules using
`git switch --detach <pinned-sha>`, with hooks and recursion disabled, network
transports denied, and ignored-file overwrite protection. Only a verified switch
updates expected HEAD/index metadata. Each source stage runs in the original root.
Catalog, file assignment, Study, revisions, Review, and evidence validation complete
before the next branch. Source and Git guards remain active at stage boundaries.

Git selects tracked paths with NUL-delimited output. Recursive submodule paths use
the common root. Configured exclusions apply before content hashing. Tracked files
matching ignore patterns stay in scope; `.git` and untracked files are excluded.
Root/nested Git ignore rules, info/exclude, and effective global core.excludesFile
still determine whether untracked files make a checkout dirty. The hardened Git
runtime imports only the global ignore setting. Symlinks remain metadata only.

One inventory hashes the checked-out bytes per branch, including normal Git
checkout transformations such as line endings. Descriptor-relative no-follow reads,
pinned filesystem metadata, and Git HEAD/index/config/ref/ignore-rule checks detect
unexpected changes. Later stage guards reuse the inventory hashes. Source access
is read-only under each CLI's native permissions; it is not full filesystem isolation.
Repository-redirecting Git environment variables are removed from source invocations.

Branch inventories and validated artifacts are saved outside the repository before
switching. Comparison uses those artifacts and deltas from saved filtered inventories
and pinned submodule commits. Deltas include nested additions, removals, content,
symlink targets, and modes; renames appear as deletion plus addition. Earlier
inventories are never checked against a later checkout's files.

The manifest records `pins`, `checkout_plans`, `original_checkout`,
`original_hierarchy`, `switch_journal`, and `restoration`. It has no copy provenance,
working-tree revision, or temporary-source cleanup fields. Each attempted switch is
journaled before execution. `finally` restores children before parents, using the
original branch names or detached SHAs. Every safe node is attempted independently,
and original refs, commits, and cleanliness are verified. A restoration failure
makes the run fail and retains both primary and restoration diagnostics. An
interrupted run retains exit code 130. Restoration never forces checkout or uses
stash, reset, or clean; unexpected edits can prevent restoration.

## Evidence locators

An evidence record has `id`, `source_id`, `path`, `start_line`, `end_line`, `quote`.
IDs are local `E-001` etc.; references use `study:E-001` or `review:E-001`.
Prefixed references are canonical. The shared normalization rule below qualifies
bare references only when their definitions identify one namespace. Review
requires explicit namespaces when its own evidence and study evidence share an ID.
The orchestrator assigns `source-001` to the main source and subsequent IDs to
submodules sorted by root-relative path. Each nested source must be cited through
its own source ID. Git source identity contains `mode`, the requested `branch`,
and its pinned `commit`. Submodule identities contain their pinned `commit`,
`parent_commit`, and root-relative `submodule_path`.
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
The runner pins one inventory per analyzed branch or Folder source. Folder mode
also creates its inventory in preflight-only runs; Git preflight does not.
Each inventory hashes source bytes once. Later guards check path membership, device/inode, type,
permissions, size, modification/change timestamps and symlink targets without
reading or hashing file contents. Access times are excluded. Metadata-only changes,
including timestamp touches, invalidate the snapshot; guards never refresh the
baseline or retry hashing. Private filesystem metadata does not enter serialized
fingerprints or model prompts.

Evidence paths must belong to the allowed inventory before any content read.
Confined reads check the pinned file and directory metadata before and after
access, including nested sources, then reuse the initial file hash. In Git mode
these checks cover the active checkout. Hashes describe actual checked-out bytes,
including Git checkout transformations.
Inventories do not enter prompts and do not count as agent source inspection.

Lines are positive integers (booleans are not integers), inclusive and ordered.
Source decoding is strict; there is no replacement of invalid bytes. Lines split
on LF only; CRLF becomes LF; a lone CR stays data. A final nonterminated line
counts, and an empty file has zero lines. Document locators use the same rules.
File SHA-256 covers the original bytes and is calculated only during initial
pinning. Evidence snippets are not hashed. `quote`
may be `""`; otherwise it must equal that complete normalized range exactly.
There is no fuzzy correction. Document quotes must be nonblank and exact.

Limits per stage: 256 evidence records; 16 KiB canonical JSON per record; 8 MiB
per file; 200 lines and 64 KiB per fragment; 32 MiB total unique file bytes read
per resolver call (successful cache hits do not count again). Reaching/exceeding a limit that prevents full resolution gives
`LIMIT_EXCEEDED`, never successful resolution of truncated data. No source fragment
is copied into generated tables. Model-supplied quotes remain in private wire data.

Each resolution stores namespaced ID, source identity, path, range, pinned raw file
hash, used encoding and status. Unavailable file hashes remain null. New resolutions
omit `fragment_sha256`; saved schemas accept it as optional legacy data so older
reports remain readable. `RESOLVED` means only
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
conflicting BOM is ENCODING_MISMATCH. Pinned raw file hashes include BOM bytes;
quote checks follow decoding and existing LF/CRLF normalization. Normalized rules enter
the frozen review plan and change its identity. Agents receive them as context,
but CLI reading capabilities are independent; no UTF-8 copies are generated.

The resolver caches successful reads/decoding only within one call; its unique-byte
budget also includes files read before a decoding failure. Every repeated
reference still walks the path with O_NOFOLLOW and checks component identity and
file state. Replacement, mutation or disappearance raises SourceChanged rather
than silently reading a newer version. Cache data never crosses stages, attempts
or branches. The initial inventory hashes and metadata pins do span all stages and
revisions of that snapshot. Standalone resolver calls without metadata pins hash
each distinct file once within the call, including files that fail decoding.
Generated reports, prompts, registries and plans retain their artifact hashes.

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

1. Check backend completion and transport identity using that adapter's contract.
   XXX additionally verifies the original request and final answer against the
   local export. Failed transports remain private evidence and cannot enter
   normalization or local result validation.
2. Retain the original object in private `extracted.json`. Verify model identity
   bindings and write `expanded.json` and `binding.json`. An unknown, foreign or
   stale binding is an identity failure and cannot be repaired or recovered.
   Synthesis must pass its model schema before expansion copies frozen registries.
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

Direct-study and revision claims have `id`, `statement`, `scope`, `epistemic_kind`,
`evidence_ids`, `uncertainty` on the wire. Synthesis references frozen shard claims
and does not return claim records. `report_sections` has exactly ten sections, with keys
`scope`, `context`, `components`, `startup_and_flows`, `data_and_state`,
`cross_cutting`, `constraints`, `change_navigation`, `unknowns`, `evidence_basis`
in that order. Each has `title` and `blocks`, each block `markdown` and `claim_ids`.
Titles and narrative use output_language. There are no model block IDs, reverse
claim.block_ids, hashes, provenance, report_markdown or document locators.
Sections and Markdown together are rejected as a hybrid wire response.

All material narrative/table/scenario claims
must be registered by the authoring agent (the shard agents for synthesis). This requirement is not a claim that
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

Positive reviewed acceptance requires valid identities/contracts, passed source guards,
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
With review enabled, an unresolved or unverified difference prevents positive
comparison policy even when the agent reports COMPLETE. With review disabled,
REPORTED_UNVERIFIED can satisfy processing checks under the requirements above.
A response with no listed differences is still
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
Default execution uses catalog → planned study → selection → compare. With review enabled,
execution uses catalog → study → review, optionally one revised study → full review,
then selection → compare (comparison only for
multiple Git branches). XXX and OpenCode V2 use final JSON with local validation;
no orchestrator format-repair prompt is sent.

## Compact model context

A pure projection builds the model request without mutating internal context.
Review receives the whole document Markdown once and claim_registry once. Claim
coordinates retain start/end lines without duplicate quotes; block_map, duplicated
registries and private provenance are omitted. Comparison and substantive revision
contexts use the same principle. Findings, evidence, limitations,
identifiers and diagnostics remain available; narrative is neither truncated nor
summarized. Full saved locators, registry hashes and document hashes remain intact.
Projection removes service hash fields by structure throughout nested documents,
evidence resolutions, plans, previous revisions, comparison inputs and private
provenance; it never searches prose for hexadecimal strings. Git commit IDs,
including commits from repositories using SHA-256, remain unchanged.

Model folder identities use `source_snapshot_id`; model reviews and comparison
references use `review_target_id`. The orchestrator generates opaque random IDs
with `S-` / `T-` prefixes and 16 following characters outside the pure projection.
Bindings are stable within a run for the same snapshot
or frozen revision. New runs receive new IDs. Private mappings retain full source
and target identities. An ID must match its purpose, source, revision and reference
role; unknown, foreign or stale IDs fail identity checks without repair or recovery.

Compromise recovery uses only the original response after its identity has
been verified; it never promotes contract-invalid text to acceptance.

Invocation metadata records `context_format: "compact-context"`.
`input.prompt.txt` contains the exact request actually sent, with its hash recorded
in invocation metadata. The backend receives the model schema, while local
validation and publication use expanded internal and saved schemas.

## Subsystem catalog and coverage

The catalog stage uses the same read-only source scope and boundary guards as study.
Its wire task is architecture_catalog, with source identity, completion_status,
limitations, subsystems `{id,name,purpose,paths}` and exclusions `{path,reason}`.
The agent receives source information and a compact directory summary; Python
checks selectors against the full file inventory. Subsystems are provisional
navigation hints, not established architecture or indivisible scheduling units.
Unknown purpose is not an exclusion reason. Explicit exclusions and symlinks
remain represented, and links are never followed. Unallocated entries become
UNCLASSIFIED; their regular files still receive primary study assignments.
The resulting coverage.plan.json is immutable for the snapshot and
shared by all document revisions. Large sources can use multiple study sessions
before the first global document is assembled.

In compromise mode, catalog recovery first checks response structure, pinned source
identity, IDs, selector syntax and exclusion rules. Only UNKNOWN_CATALOG_PATH is
recoverable: selectors absent from the frozen inventory are removed without replacement.
Valid assignments and exclusions remain. Subsystems with no remaining paths are
removed; unallocated entries become UNCLASSIFIED through normal reconciliation.
Traversal and other unsafe selectors are never recovered or authorized. Symlink
targets are never followed. Strict mode continues to reject unknown paths.

Coverage origin AGENT identifies a catalog that passed validation without recovery.
AGENT_SALVAGED identifies a catalog with retained subsystems after unknown selectors
were removed. Its saved completion_status and coverage catalog_status are PARTIAL,
even if the agent reported COMPLETE. If none remain, DIRECTORY_FALLBACK groups
top-level directories and root files while retaining valid exclusions and catalog
limitations. Other catalog failures can use the existing directory fallback in
compromise mode; catalog identity failures stop analysis for that source.

The original extracted.json and expanded.json are unchanged. Private recovered.json
holds the filtered catalog; recovery.json records rejected selectors with their
JSON locations, original self-assessment, provenance and canonical input/output hashes.
These artifacts are pinned and checked with the other attempt files. validation.json
identifies recovered.json as its validated object and records original_valid=false.
The saved program_checks retain the original completion_self_assessment. Rejected
selector values are not copied into public reports or later model inputs.

Recovered and fallback plans require PARTIAL status, limitations and
policy_satisfied=false; their provenance is included in the frozen plan hash.
Successful analysis cannot restore positive catalog acceptance. In strict mode a
catalog failure stops the current source. continue_on_error=false stops further
calls after a stage failure or catalog recovery; the recovered catalog is retained.
Deterministic recovery is not a retry. Integrity, cleanup and publication errors
stop the run regardless of policy. An incomplete, salvaged
or fallback catalog and remaining UNCLASSIFIED prevent overall COMPLETE even if
usable documentation is retained.

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

## Multi-session study

The top-level `multi_session` setting is a strict boolean, default `true` when
omitted. `false` forces the ordinary single-study path. The top-level
`max_source_bytes_per_session` setting defaults to `262144` (256 KiB). It accepts
only positive integers; zero, negative numbers, booleans, floats, strings and null
are invalid. The same setting applies to all backends. Catalog always uses one
invocation. The backend-independent planner starts with this session target:

```
N_target = max(1, ceil(areas / 4), ceil(source_files / 300), ceil(source_bytes / max_source_bytes_per_session))
```

Areas include UNCLASSIFIED. File batching can increase the target when groups do
not fit; unused sessions are removed. The area and file thresholds remain code
constants; the byte threshold is configurable. One resulting session uses the
existing study call, with no shard or synthesis calls. Multiple sessions use study shards in stable
R-001, R-002, ... order, then one synthesis call. There is no concurrency. Shards
and synthesis inherit the effective `stage_agents.study` selection. Their fixed
internal prompts are `study-shard.md` and `synthesis.md`; direct study and revision
keep the existing configurable prompts.

Size metrics describe **eligible regular files**. The byte limit measures the total
size of source files assigned to a session, not bytes actually read by the LLM.
Inventory has no authoritative language classification. Directories and symlinks
do not count; no new extension/vendor/generated heuristics are applied. Git source
preparation retains its existing ignored-untracked exclusions. Catalog exclusions
affect area membership and analysis file/byte totals. The complete inventory and
coverage counts retain excluded files with their explicit paths and reasons.
Planning `source_bytes` sums the frozen inventory's `size` values. The planner
does not reread source files or add filesystem scans. The inventory retains
`source_lines`, which counts LF bytes plus one for a nonempty unterminated final line.
Blank lines count; CRLF counts once; standalone CR is not a separator. Binary
bytes use the same rule. Empty files have zero lines. Counts are collected during
the existing hash read (or reused from Git source preparation), without another
source-content scan. Metadata guards reuse the frozen counts.

`analysis.plan.json` is saved beside `coverage.plan.json` and `source.inventory.json`.
It contains the enable flag, exact session count, thresholds, metric definition,
global totals, area sizes, shard primary_file_paths, oversized_file_paths and loads.
The existing subsystem fields include the UNCLASSIFIED area when present. It binds the inventory
and coverage-plan hashes. Local validation rebuilds the plan and compares canonical
JSON using the expected configured byte limit, checking every assignment, count,
ID, capacity flag and hash. The effective limit is frozen in
`thresholds.max_source_bytes_per_session`; totals, areas and shards use `source_bytes`.
The metric is `nonexcluded_regular_files; sum_inventory_size_bytes`.
A changed effective limit invalidates the plan, even if assignments remain the same.
Old line-based plans must be regenerated. Once study
starts, every boundary checks the frozen plan bytes; changes are integrity failures.

Area cost is max(1/4, files/300, bytes/max_source_bytes_per_session). Descending cost, then area ID,
determines placement order. Sorted file paths form batches within the file and
byte limits. Each batch goes to the least-loaded shard that can hold it, with
shard order breaking ties; a new shard is added when none fits. Shard load uses
the same maximum with its area count. Rational arithmetic determines placement;
normalized_load is a JSON number. Each inventory path counts once per shard
despite overlapping areas. Global totals also count paths once; sums across
shards can exceed global totals. Oversized individual files remain intact in
their own batches and set `over_capacity: true`; their paths are explicit.
Unused sessions are removed. Areas without regular files share an existing
session for their coverage report. No assignment is treated as inspection.

The shard schema carries task/source/shard identity, assigned subsystem IDs,
components, flows, state, constraints, cross-area relationships, evidence, claims,
coverage, limitations and completion. Observation records link claims; relationships
link a primary subsystem to an inventoried common-root-relative path. The model
receives only its primary coverage areas and assignment, with source identity and
normal study context. primary_file_paths gives the exact common-root-relative file
scope. Coverage for an area concerns its intersection with these paths, even when
other shards share the area ID. INSPECTED requires resolved evidence in that
intersection. Other locations may be read to understand interfaces and support
dependency claims, but cannot establish primary coverage.
Model scope changes are identity failures. No shard produces a global report.

Each shard must pass backend completion, binding, schema, semantics, source
integrity, evidence resolution and primary coverage checks. There is no shard
revision or compromise promotion. Validated results, logs and per-attempt metrics
are preserved in `study-shards/R-NNN/`. Manifest `study_shards` records PLANNED,
RUNNING, SUCCEEDED or FAILED, each with its own invocation metadata. With
continue_on_error=true, recoverable failure permits later planned shards to run;
false stops them, leaving their state PLANNED. Any failed shard blocks synthesis
under both result policies. Integrity failures always stop the run. Multi-session
analysis accepts AGENT, AGENT_SALVAGED and DIRECTORY_FALLBACK coverage plans after
the same structural, inventory and frozen-plan verification. Catalog provenance
does not relax any shard validation or evidence requirement.

Synthesis runs as a study invocation with prompt_variant=synthesis, a neutral
working directory and the adapters' existing reports-only tool restrictions.
Inputs include the available catalog, validated shards, frozen plan, description, priority
scenarios and source identity. Evidence and claims receive deterministic global
E-/C- IDs in shard/ID order. `shard_id_mappings` records the remapping in the saved
input prompt. The model uses `synthesis.schema.json` or `folder-synthesis.schema.json`:
only task/source identity, completion_status, limitations and report_sections are
permitted. Every registered claim must appear in a block's claim_ids, and all ten
sections must retain their required order. Registry arrays and other unexpected
fields are rejected, even if they match the inputs.
Python merges repeated area coverage into one record, retaining all evidence
references and limitations. A partial or uninspected contribution cannot become
INSPECTED through this merge. Coverage completeness remains unmeasured.

After checking model identity and the synthesis schema, Python deep-copies
`synthesis_evidence`, `synthesis_claims` and `synthesis_coverage` from the frozen
binding context into the complete study. Missing inputs are never fabricated.
The unchanged full study contract validates the assembled object. Local checks
also require exact evidence, claim and coverage equality, including array order,
with the remapped inputs before publication. The model cannot mint source evidence
or change a claim's scope/certainty. Source evidence
resolutions are reused from validated shards, under the same boundary guards.
The shard artifacts are pinned and checked before synthesis publication.

The global study uses existing materialization, saved schemas, review plans,
optional review/revision, selection and publication. Unclassified/incomplete
catalog policy still prevents global success. A successful synthesis from a salvaged
or fallback catalog remains usable and is selected and published through the usual
report pipeline. Original catalog limitations and provenance remain visible in
study.annotated.md and FINAL_REPORT.md. ARCHITECTURE.md retains the authored study.
The overall result is PARTIAL (exit 2 in
compromise mode), accepted=false and coverage_plan.policy_satisfied=false, even
when all shards, synthesis and optional review finish. Study completion_status
remains the model's self-assessment; it does not override the overall result.
Successful processing does not establish architectural correctness. Model-facing
study, study-shard and synthesis schemas are unchanged. A failed synthesis retains shards
and diagnostics but publishes no successful or recovered global study. Manifest
`study_origin` distinguishes direct_single_session from multi_session_synthesis;
it also records source_metrics, required_sessions, analysis_plan_path,
synthesis_status and synthesis_invocation. Metrics retain one row per shard and
one for synthesis. Later ordinary revisions use the same frozen coverage and
analysis plans; they do not rerun or revise shards.

## Bounded revision and selected artifacts

execution.max_revision_rounds is 0 or 1, default 1. Revision starts only after a
published, strictly valid study and review, when review completion_status is
COMPLETE and there are HIGH/MEDIUM findings. LOW-only findings, incomplete review,
transport failures and recovered contract-invalid material do not trigger it.
prompts.revise supplies a separate prompt to the existing study agent; no revision
backend profile is introduced. stage_agents.catalog inherits study settings.
prompts.catalog controls catalog. Each substantive invocation has its own budget.
Single-session analysis has at most five invocations per snapshot; multi-session
analysis adds the planned shards and uses synthesis for the first global study.

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
Without any completed review, the latest usable study is selected. A missing-review
warning applies only when review was enabled; an intentional skip is labeled as such.
The manifest records selected_revision and revision history. Comparison uses only
selected pairs. Top-level ARCHITECTURE.md, study.json and companion files are exact
copies of the selected artifacts, published after selection. Frozen-document checks
run within each revision; a new study can never inherit an older review.
