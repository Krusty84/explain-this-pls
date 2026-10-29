# Evidence ledger contract

## Optional review and processing success

`execution.review_enabled` is a strict boolean and defaults to `true` in JSON
and JSONC configurations. It applies to every source and backend in the run.
With review disabled, the source pipeline is study; comparison remains
available for multiple selected Git branches. Review/revision agents and prompts
are inactive, and `max_revision_rounds` has no effect. Explicit `review_enabled: false`
selects that workflow. Omitting the setting runs review and its bounded revision loop.

New manifests, source results and revisions record `review_enabled`. Disabled
reviews have `review: null` and `review_skipped: "disabled_by_config"`; progress
and metrics record SKIPPED with no model attempts or usage. No review result or
invocation artifacts are fabricated. The study's frozen registry and
`review.plan.json` remain necessary for integrity and comparison identities.

`accepted` retains its study-AND-review meaning. `workflow_satisfied` is computed
separately by Python: a complete, valid, published study satisfying evidence,
document and identity checks is sufficient when review is disabled; with review
enabled the existing accepted-pair policy applies. Run success additionally
requires successful applicable comparison and no blocking run errors.
Both strict and compromise modes can finish COMPLETE / exit 0 without review.
Recovery material and integrity failures do not gain acceptance. Auxiliary
coverage diagnostics do not determine document publication or acceptance.
Saved results and library contexts without `review_enabled` retain their
review-required interpretation.

Comparison contexts include review mode and computed workflow results. Disabled
review alone does not make an input unresolved. Without review, differences use
REPORTED_UNVERIFIED; successful processing requires selected, valid FACT references
with resolved source evidence on both sides of each difference. Insufficient
support or INSUFFICIENT_EVIDENCE prevents positive processing policy. The existing
CONFIRMED_DIFFERENCE requirement for supported facts in reviewed, accepted pairs
is unchanged and that classification is forbidden in review-disabled runs.
An empty difference list still requires complete eligible input studies on all
requested branches. Success is not factual verification.

The review and revision sections below describe the default reviewed workflow.
Review errors, incomplete responses and skipped review cannot set `accepted=true`.
Published usable studies remain available after review failure. The final report,
CLI and TUI distinguish accepted review, material findings, incomplete review and
explicit disablement. Policy acceptance does not establish safe code modification.
Completed review without pair acceptance is shown separately from unfinished
review, including partial-study cases. CLI JSON exposes per-source `reviews`
with source, selected revision, outcome and summary; this is presentation metadata.

The orchestrator selects the required response schema for each stage and validates
its structure locally.
Initial Study has two internal phases: `collect` returns identity, status,
limitations, claims and evidence; `write` returns identity, status, limitations
and report sections. The `study-collect` and `study-write` wire schemas and their
`folder-` variants define these responses. The full study schema remains the
contract for revision responses and the program-assembled initial Study.
`schemas/{study,review,compare}.schema.json` and the
available `folder-` variants
are **model wire** schemas generated from `MODEL_SCHEMAS` / `MODEL_FOLDER_SCHEMAS`
in `src/contracts/contracts.py`. Internal `SCHEMAS` / `FOLDER_SCHEMAS` validate the expanded
representation, which retains full integrity identities. Separate `saved-*.schema.json`
files are generated from `src/contracts/saved_contracts.py` and locally validated before publication.
The `evidence-ledger` contract requires the saved schema or explicitly marked
recovery material for rendering.
Unsupported results are rejected without modifying their artifacts.

Both initial phases use the Study agent and one stage timeout. The fixed collector
prompt investigates the pinned source. After normalization and optional citation
correction, Python resolves evidence and freezes `study.evidence.json` in the
initial revision directory. Its bytes and both attempts' private artifacts are
guarded by the Study invocation hashes. A collection contract or identity failure
does not invoke the writer or create recovered report material.

The writer receives the package and task context without a source index or symbol
maps. It uses a neutral directory, disabled source shell, and no user CLI config
or rules. Explicit agent model and effort settings still apply; null settings use
the isolated CLI defaults. `prompts.study` controls this authoring call;
`prompts.revise` retains the full source-reading revision contract.

Python copies the frozen registry into the authored Study, unions limitations,
and chooses the worse collection/author status. Unresolved evidence forces at
least PARTIAL under both result policies. This status is set before materializing
the document; `completion_self_assessment` retains the author's original status.
The writer's original response, assembled Study and collection package remain
distinct artifacts. Assembly provenance binds their hashes; the final normalization
and materialization hashes cover the full assembled Study. Later successful
revision may return COMPLETE. Matching source text still does not establish
semantic support.

Internal contract, schema, format and processing-rule identifiers are permanent
and carry no version numbers. Keep these identifiers when schemas change; update
the single implementation and its validation. Do not add version fields,
migrations or legacy aliases. External tool, protocol, model and parser versions
and document revision numbers remain valid.

## Data provenance and storage

| Data | Authority |
| --- | --- |
| Narrative, claims, evidence pointers, findings, assessments, relevance observations, omission-search activity | Agent |
| `completion_status` | Agent self-assessment; Python bounds the initial assembled Study by both phases and evidence resolution |
| Transport completion/error/timeout/interruption and cleanup | Backend adapter and runner |
| Expected source identities, review target, contract/artifact identifiers in invocation metadata and manifest | Orchestrator |
| `program_checks` (including coverage and diagram checks), `verdict`, frozen plan, hashes, file distribution, reverse links, tables | Python, after local wire validation |
| `model_requested` | Configuration |
| `reasoning_effort_requested` | Resolved stage configuration, or null; does not confirm actual model effort |
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
Initial collection supplies registry arrays; Python adds them to the authored
sections. Revisions supply their own arrays under the full study wire schema.
Transport/stdout and stderr remain private attempt artifacts. A saved `study.json`,
`review.json` or `compare.json` is a separate representation with processed wire
fields and `program_checks`. Study narrative is authored as sections/blocks;
the program computes the Markdown, block map and every document locator.
Contract/artifact identifiers (`contract_id:
"evidence-ledger"`, `artifact_format: "architecture-artifacts"`) belong only
to invocation metadata, the manifest and the immutable coverage plan, not agent
responses or the root of saved results. The coverage plan records both identifiers
and `origin: "SOURCE_INVENTORY"`; it is also embedded in saved review plans.
Study adds `review_plan` and `normalization_provenance` (including
the normalization rule). Review also adds `review_plan`, the
unchanged `claim_registry`, and the Python-derived `verdict`. These extra fields
are forbidden in wire output. The saved representation must not be re-submitted
as an agent response. Stored `program_checks` are execution records, not signatures
against later manual tampering with report files.

`program_checks` separates `execution`, `contract`, `source`, `evidence`,
`completion_self_assessment`, `document_links`, `diagrams`, `registry_coverage`,
`omission_coverage`, `finding_ids_by_claim` and `policy_satisfied` as applicable.
Failed attempts retain invocation/validation diagnostics; they do not get a
successful result. Source guards compare state at prescribed boundaries; they
are not a continuous immutability guarantee. Orchestrator reads do not establish
which files the agent read.

`study.json` retains structured judgments and checks; `study.original.md` retains
canonical `report_markdown` for Review. `ARCHITECTURE.md` is a separate deterministic
presentation with fixed headings and source references after each linked block.
It need not start with the canonical document bytes. Rendering never changes
`report_markdown`, its SHA-256, `document_locators`, the frozen registry or
`review_target`. Publication hashes cover all published presentation bytes;
`assert_review_files()` recreates those bytes with the same renderer before use.
Existing published reports are never rewritten.
The pure serializer normalizes CRLF to LF in authored blocks,
preserving internal spaces, tabs, bare CR and Unicode without NFC/NFKC. It writes
`## N. title` followed by two LF, then each block with a final LF if absent and
one additional separator LF. Existing blank lines are preserved. Coordinates are
counted while writing, using `evidence.lines()` semantics. Locators cover only
serialized blocks, including their final LF, never headings or separators.
Quotes use that same normalized line view of the serialized fragment, including
the edge case where an authored trailing bare CR touches a program-added LF.
After materialization no whitespace or newline changes to canonical text are allowed
before hashing or review. Publication preserves each serialized authored block
byte for byte, including tables, diagrams and code fences. Generated elements
are separate; restoring canonical headings and removing generated elements
recovers `report_markdown`. `study.annotated.md` contains generated checks and the registry.
`ARCHITECTURE_REVIEW.md` and `BRANCH_COMPARISON.md` contain generated tables;
`study.original.md` retains the canonical program assembly of authored blocks.
`review.original.md` and `compare.original.md` preserve original prose.
`coverage.plan.json` is generated deterministically from the filtered pinned
inventory before Study. Each Study publication hashes its plan copy; the common
source-level plan remains immutable across Study, Review and Revision. Model
classifications and relevance observations cannot change its membership.

The final report places limitations and assessments before narrative,
and keeps contradictory assessments visible. It summarizes the selected architecture
revision and its corresponding review when that optional stage is enabled.
Generated Markdown evidence tables include ID, source, path and lines, resolution
status and encoding, without service hash columns or values. JSON keeps the pinned
file hashes. Prompts prohibit service fingerprints, hashes and binding IDs in narrative;
source-system hash semantics and Git commit IDs remain meaningful content.
No regular-expression cleanup or other postprocessing rewrites authored prose.

### Deterministic architecture publication

Study and Revision share the same pure renderer. Its inputs are canonical
`report_markdown`, `block_map`, `claims`, `evidence`, `program_checks.evidence`,
`review_plan.sources` and the report language. It reads no files, uses no network
and makes no model call. Identical inputs produce identical UTF-8 bytes without
mutating the Study. One initial Study still runs per pinned source snapshot.

The published document has exactly five H2 headings, in the section-key order
below. Python constants define their numbered titles for Russian and English,
including supported aliases. Other languages without a fixed dictionary are
configuration errors before any model call. Model `report_sections[].title`
remains in the wire response and canonical assembly, but never selects a
published heading.

| section_key | Russian title | English title |
| --- | --- | --- |
| `scope` | Область исследования | Study scope |
| `context` | Контекст системы | System context |
| `components` | Компоненты системы | System components |
| `startup_and_flows` | Запуск и выполнение | Startup and execution |
| `data_and_state` | Данные и состояние | Data and state |

For each `block_map` record, references follow only
`claim_ids` → `claims[].evidence_ids` → `evidence` → source catalog.
They retain first-occurrence order through claims and their evidence lists,
deduplicate canonical evidence IDs within that block, and appear after the
complete block with a blank-line separator. Repeated text is never searched or
matched; the same evidence appears beside every block that links it. No reference
limit removes available evidence. A block with no claims needs no source reference.
HYPOTHESIS/UNKNOWN without evidence receive no fabricated source; unresolved
support remains an explicit limitation. Missing definitions or required relations
remain strict contract errors, not quote warnings.

Every reference displays only its evidence ID and links to a stable internal
evidence anchor. An H3 source index (`Реестр источников` in Russian) follows all
five sections, recording evidence ID, displayed relative file path with inclusive
line range, source ID and locator/quote status. Source identities appear once per
referenced source in a separate H3 table. Submodule display paths include
`sources[].root`; source identity retains its pinned commit.
Paths are untrusted data and are escaped for Markdown
and HTML. External source URLs are not inferred from Git remotes; a verified
explicit base and a pinned Git commit would be required, with percent-encoded
path segments.

Reference status comes from the existing resolver results. `RESOLVED` means the
file/range locator resolved; an empty quote does not establish quote verification.
`QUOTE_MISMATCH` carries an explicit warning. `OUT_OF_RANGE`, `NOT_FOUND` and
other resolution failures display their status without claiming verified support.
Detailed citation-warning tables remain in `study.annotated.md` and
`FINAL_REPORT.md`. Architecture warnings stay beside their blocks or under H3
inside the relevant section; they never add an H2.

A pinned CommonMark parser validates every authored block and the final document.
Blocks may contain H3–H6, paragraphs, lists, tables, inline code, links, quotes
and closed fenced code including PlantUML. ATX/Setext H1/H2 in any container,
raw HTML h1/h2 outside code, and unclosed fences are rejected. Literal heading
markers in fenced/inline code or escaped text remain valid. Diagnostics identify
the `section_key`, `block_index` and violation type; headings are never renamed,
removed or converted to text. The final check requires the exact H2 count, titles
and order before publication, whether Review is enabled or disabled.

`strict` rejects invalid Markdown. `compromise` may retain eligible original
material through existing recovery, but cannot publish it as a valid
`ARCHITECTURE.md`. Formatting errors never trigger another architectural Study.
Presentation changes do not require a new Review; canonical document changes
always require a new review identity and cannot reuse an earlier Review.

### Required PlantUML diagrams

COMPLETE Study and Revision require at least one valid PlantUML diagram in each
of `components` and `data_and_state`. Other sections cannot satisfy these
requirements. Extra diagrams are allowed. Each required diagram occupies one
`report_sections[].blocks[]` record containing only a standalone, closed
CommonMark fence. Backticks and tildes are accepted. The info string is exactly
`plantuml`, case-insensitive, with no parameters. Lists, blockquotes, inline code,
ordinary text and other fence languages do not supply a required diagram.

Pure required-diagram inspection applies structural checks within `components`
and `data_and_state`.
Other sections remain subject to the common Markdown and claim-link checks.
It uses the existing Markdown parser, not a fence substring search. It checks
exactly one `@startuml` before exactly one `@enduml`, nonempty content other than
comments, and nonempty links to registered claims. External directives from the
`!include` family and `!import` are forbidden. Comments and quoted literal
examples are not executable directives. This is basic structure checking, not a
PlantUML parser, compiler or architecture assessment.

Missing diagrams reject COMPLETE. PARTIAL/BLOCKED may omit them with nonempty
`limitations`; the author must explain the gap and information to verify in its
section and limitations. Python records the missing sections without keyword
tests of the explanation. A malformed present required diagram fails the
contract for every completion status. Python does not invent diagram content.

Before materialization, the orchestrator checks every PlantUML block in Study
and Revision using local `plantuml -charset UTF-8 -syntax` with stdin and the
SANDBOX profile. Every diagram block must contain one standalone fence; includes
and imports remain forbidden. PlantUML >= 1.2020.11 and Java are mandatory;
install-explain.py installs a checksum-verified PlantUML JAR and launcher in .venv,
using an existing Java 11+ runtime. Runtime lookup uses the launcher beside the
running Python first, then PATH, so activation is optional.
Graphviz is not needed. Preflight records the version and tests valid and invalid
input. Syntax errors are read from the output protocol, including with exit 0;
reported source lines are converted from zero-based to one-based coordinates.

At most two additional calls to the study agent correct all currently defective
diagrams in a batch. Calls use the same stage budget and model settings, with
tools disabled, and do not consume architectural revision rounds. The internal
plantuml_repair response echoes the source identity and a request-specific
repair_id, with diagrams containing section_key, block_index and source. Targets
must match exactly. Only fence bodies change; claims, evidence, prose and all
other fields stay fixed. Full contract and syntax validation precede hashing,
materialization and Review. Schema, identity, integrity and process failures do
not trigger syntax corrections. Exhaustion uses the existing strict rejection or
compromise material recovery, with the diagram location in the diagnostic.

Private plantuml-validation.json records each check and validator version.
plantuml-corrected.json and provenance record replacements without overwriting
the original extracted, expanded or normalized response. After a successful
correction, plantuml-normalization.json records the no-op evidence normalization
of the corrected candidate; the public normalization hash still equals the
materialization input hash. The initial normalization remains in its original
journal and private provenance. Public Study/Review schemas are unchanged.

The orchestrator alone writes saved `program_checks.diagrams`:
`required_sections`, `present_sections`, `missing_sections`, `complete` and
`scope: "STRUCTURAL_ONLY"`. Required sections are always `components` and
`data_and_state`; `complete` is true only when both contain valid diagrams.
COMPLETE cannot satisfy processing policy without this check. The model Study
wire schema is unchanged.

Materialized validation reads each block through its validated `block_map`
locator and applies the same checks. It requires five canonical H2 headings and
five ordered block groups with matching section and block indexes. Saved block
keys use the same five-value enum as Study responses. Publication validates the
five sections and checks diagram content and placement against the canonical document before
writing `ARCHITECTURE.md`. These checks make no model calls or external requests,
and do not change canonical Markdown, locators, provenance or Review identity.
Review still assesses component truth, dependencies, data-flow directions,
evidence links, text consistency and the reasons for incomplete diagrams. A
format rejection is not a substantive architectural conclusion.

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
the contents of `FINAL_REPORT.md`.

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
and zero usage. Readiness checks invoke only version/help commands.

`usage` contains `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, `total_tokens` and `cost_usd`.
Normalized input includes cache reads and writes. Cache and reasoning are
breakdowns, not additional tokens to add to total. Each counter has an independent
entry in `coverage`: `complete`, `partial` (known subtotal), or `unavailable`.
Missing or invalid numbers remain null, not zero. Optional unavailable breakdowns
do not make an otherwise reported total incomplete. `sources` records the consumed
metadata fields; `total_tokens_estimated` records whether the total is estimated.
`cost_is_estimate` describes backend-reported costs; no tariffs are fetched or
applied by the runner. A reported zero cost does not establish a free invocation.

Codex uses `--json` for private JSONL events and `--output-last-message` for the
separate schema response. `turn.completed.usage` supplies tokens; total is input
plus output, with cached input and optional reasoning already included. No monetary
cost or actual model identity is inferred from the configured model.

Complete accounting means the available backend accounting is complete, not that
it has been reconciled against an invoice or includes unreported activity.

Legacy `http_timeout_seconds`, `api_doc_timeout_seconds` and
`structured_output_repair_attempts` remain accepted configuration keys but have
no effect on Codex execution. The orchestrator sends no general format-repair
request. The bounded PlantUML syntax corrections above are independent of these keys.

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
Study, file assignment, revisions, Review, and evidence validation complete
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

Source resolution has no program limits on file size, range length, fragment or
record size, record count, or total bytes read. It reads complete source files
and checks full requested ranges without truncation. Path, snapshot, hash, range
and quote checks still apply. Stage timeouts and native CLI/model limits remain.
No source fragment is copied into generated tables. Model-supplied quotes remain
in private wire data.

Each resolution stores namespaced ID, source identity, path, range, pinned raw file
hash, used encoding and status. Unavailable file hashes remain null. New resolutions
omit `fragment_sha256`; saved schemas accept it as optional legacy data so older
reports remain readable. `RESOLVED` means only
that the locator resolved. Other statuses distinguish `INVALID_POINTER`,
`UNKNOWN_SOURCE`, `SOURCE_SCOPE_MISMATCH`, `UNSAFE_PATH`, `NOT_FOUND`,
`ACCESS_DENIED`, `READ_ERROR`, `OUT_OF_RANGE`, `DECODE_ERROR`, `ENCODING_MISMATCH`
and `QUOTE_MISMATCH`. `LIMIT_EXCEEDED` remains valid for historical results; new
source resolution does not emit it for source volume. Bad/inaccessible pointers
prevent positive policy acceptance without discarding an otherwise well-formed
report. A real fragment may be
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
the frozen review plan and change its identity. The rules apply to evidence
verification. When `source_context` is enabled, they also decode files for the
symbol map. Agents receive the rules and the map's used encoding. Native file
reads depend on the CLI's capabilities; no UTF-8 copies are generated on disk.
Agents must report a gap if they cannot read the original text exactly.

The resolver caches successful reads/decoding only within one call. Every repeated
reference still walks the path with O_NOFOLLOW and checks component identity and
file state. Replacement, mutation or disappearance raises SourceChanged rather
than silently reading a newer version. Cache data never crosses stages, attempts
or branches. The initial inventory hashes and metadata pins do span all stages and
revisions of that snapshot. Standalone resolver calls without metadata pins hash
each distinct file once within the call, including files that fail decoding.
Generated reports, prompts, registries and plans retain their artifact hashes.

## Prepared source navigation

Enabled source context uses `mode: "index-only"` and `fragments: []`. Study,
Review and Revision prepare every regular file from the pinned, filtered inventory
and every declaration found by the selected parser. File entries identify the
source, relative path, parser version, language, encoding, line count and map
status; unavailable values are null. Unknown languages, partial parses and read
failures remain visible. Comparison receives no source map.

The native full model view uses `file_columns` in this order: `source_id`, `path`, `parser`,
`language`, `encoding`, `total_lines`, `map_status`, `map_reason`, `symbols`.
`files` contains rows of values in that order. Each `symbols` value contains rows
in `symbol_columns` order: `name`, `kind`, `parent`, `start_line`, `end_line`.
All values and list ordering remain intact. Internal maps, cache entries and
manifests retain named fields. The manifest transfer size measures the model view.

Maps are ordered deterministically and are not cut to a size quota. The run-local
cache holds only maps and metadata and validates pinned state before reuse.
The manifest records completeness, reasons, preparation time and transfer size;
`transmitted_text_bytes` is zero. A full map can be larger than the source text.

The map provides navigation, not source implementations or proof of inspection.
Agents must read implementations with the selected source tools, check relationships
and cite source. Index-only delivery adds no read or delivered source lines to
tool-observation metrics. Backend capacity failures remain explicit without
silent truncation.

### MCP navigation and source delivery

MCP uses the same complete prepared index and saved maps. Its initial
`source_context` keeps `mode: "index-only"`, `index_format: "dictionary"`,
`delivery: "mcp"` and `fragments: []`. It contains source IDs/roots, counts,
bounded directory/type summaries, language counts, map/read/decoding failure
counts and tool instructions. It has no file or declaration rows. Aggregated
summary labels never remove files from the inventory. The full index, coverage
plan and maps stay with the orchestrator. Native and full delivery are unchanged.

Only `search_symbols`, `search_text`, `list_files`, `read_source` and
`read_symbol` are source tools. Registration, temporary approval settings,
effective configuration and actual inference tool schemas must agree on this
set. Native shell, write, network and project execution tools remain forbidden.
The source server advertises no resources and starts no external process.

`list_files` returns every regular inventory file, ordered by source ID and path,
with language, encoding, total lines, map status/reason and declaration count.
Unknown metadata is null. `source_id` and `path` filter by source and exact
file/directory subtree. List/search limits are 1–1,000 records, default 100.
Every page has `has_more` and `cursor`. No count or byte limit silently drops rows.

`search_text` finds literal substrings in all selected text files regardless of
AST status. It uses strict configured/BOM decoding and LF/CRLF rules, including
legacy encodings. Case sensitivity defaults to true; false uses Unicode casefold.
Multiline and overlapping matches are supported. Results are ordered by source
ID, path and original character offset. Each result includes source ID, path,
one-based line/column and end line/column (exclusive), encoding and a snippet of
at most 160 characters with `snippet_truncated`. Snippets are navigation.
Matches and errors share the page limit. `errors` identifies failed files and
codes; `incomplete` persists across continuation pages after any error.
Search `complete` requires the last page and no file errors. No matches in an
incomplete search do not prove absence. `SOURCE_CHANGED` aborts and blocks the run.

`search_symbols` adds `symbol_id` without changing stored maps. The ID hashes the
snapshot, source ID, path, declaration ordinal and prepared declaration record.
It is stable for the same snapshot/map and separates duplicate names and overloads.
`read_symbol(symbol_id, cursor?)` reads the original pinned file through the
shared `read_source` mechanism. Each page contains `symbol_id`, source ID/path,
actual `start_line`/`end_line`, `declaration_start_line`/`declaration_end_line`,
`total_lines`, encoding, raw-file SHA-256, text, `complete`, `has_more`,
`next_start_line` and `cursor`. It never reads past the declaration's inclusive
bounds. `complete` means that the declaration end was reached; previous pages
are still necessary. Whole lines are returned. `LINE_TOO_LARGE` is explicit.
Missing symbols, unavailable coordinates and map integrity failures are errors;
manual reading remains available through `read_source`. No request reparses AST.

All result JSON bodies retain the 65,536-byte limit. Cursors bind the tool,
snapshot, attempt and complete parameter set, including limit. An in-memory HMAC
key rejects modified cursors; restart expires them. Use unchanged parameters
for continuation. Reads always check the raw source SHA-256, inventory ownership,
pinned metadata and symlink confinement. Source changes remain blocking.

All five tools use exact `SERVED` receipts. ToolObserver checks completed text
results against receipts. Only `read_source` and `read_symbol` add source lines;
symbol reads also check identity and bounds against the prepared map. Source
delivery keeps `INSPECTED_UNVERIFIED`. Full successful read results, including
every symbol page, must also occur in actual model tool-output input. Missing,
metadata-only or truncated text fails with `MCP_DELIVERY`. Search/AST metadata
does not establish reading, understanding or architectural evidence. Existing
evidence and publication checks remain unchanged.

## Evidence ID normalization and diagnostics

`study_normalization.normalize_evidence(stage, value, context, mode='git')` is a pure
function returning `(candidate, changes)`. The candidate is a deep copy, including
when no changes are needed. It first requires the complete internal stage schema,
task, matching pinned Git/folder identity and, for review, unchanged frozen
context and exact target. The caller verifies transport before calling it.
It never repairs the schema.
The rule is `EVIDENCE_IDS` in both strict and compromise, applied after Codex
transport checks.

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
   Failed transports remain private evidence and cannot enter
   normalization or local result validation.
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
   available. A successful normalization is the separate `study_normalized` or `review_normalized`
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
expansion, or `quote-corrected.json` when local citation repair changed it.
Both SHA-256 hashes cover the respective **entire internal objects**,
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

### Quote-only continuation

Only `result_policy: "compromise"` permits local citation repair. Exact existing
resolution runs first. For QUOTE_MISMATCH, search the same pinned file with each
range boundary allowed to change by at most three lines. Prefer a unique exact
quote; otherwise permit only a unique replacement of the common leading indent.
Relative indentation, internal whitespace, code values and identifiers stay exact.
Revalidate every proposed correction with the ordinary resolver. Ambiguous matches
remain unresolved. No project-wide search, fuzzy match, quote removal or model
retry is used. `strict` keeps its previous evidence rules.

`extracted.json` and `expanded.json` remain unchanged. Corrections are recorded in
private `quote-corrected.json` and `quote-corrections.json`; `quote-diagnostics.json`
retains full quote/source diffs, including unresolved mismatches. These files join
the existing attempt hashes. The provenance chain is expanded response, optional
quote correction, evidence-ID normalization, then materialization. Only safe IDs,
source paths, ranges, reasons, actions and affected claims enter ordinary logs.

Optional saved `program_checks.citation_warnings` and `continuation_allowed`
separate permission to continue from strict `policy_satisfied`. Unresolved quotes
remain QUOTE_MISMATCH, cannot confirm citations or differences, and prevent strict
acceptance. Continuation permits only quote mismatches; all identity, schema,
source, publication,
timeout, NO_PROGRESS and cleanup checks remain in force. Saved reports without
these fields retain their previous meaning and are not rewritten.

Citation warnings pass from study into review; affected claims require manual
verification. Comparison can use such inputs only as REPORTED_UNVERIFIED, never
as proof of CONFIRMED_DIFFERENCE. Otherwise completed runs exit 0 with
COMPLETE_WITH_WARNINGS and processing_completed=true. Strict workflow_satisfied
and accepted remain separate; an unresolved quote cannot make either true.
CLI/TUI, FINAL_REPORT.md and annotated reports display citation warnings.

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
| `UNEXPECTED_TOP_LEVEL_HEADING` | Authored block contains a forbidden H1/H2 |
| `INVALID_MARKDOWN_STRUCTURE` | Authored block has invalid Markdown structure, including an unclosed fence |
| `REPORT_SECTION_COUNT_MISMATCH` | Canonical or published architecture does not have exactly five sections |
| `REPORT_HEADING_ORDER_MISMATCH` | Block groups or published heading titles differ from the required order |
| `REQUIRED_DIAGRAM_MISSING` | Required section has no valid diagram in a COMPLETE Study |
| `INVALID_PLANTUML_FENCE` | PlantUML fence is invalid or not a standalone block |
| `INVALID_PLANTUML_BOUNDARY` | PlantUML boundary markers are missing, repeated or reversed |
| `EMPTY_PLANTUML_DIAGRAM` | No diagram content remains after blank lines and comments |
| `FORBIDDEN_PLANTUML_DIRECTIVE` | Diagram uses an external include/import directive |
| `UNLINKED_REQUIRED_DIAGRAM` | Required diagram has no valid registered claim links |
| `DIAGRAM_CHECKS_MISMATCH` | Saved diagram checks differ from the canonical document |
| `DIAGRAM_PUBLICATION_MISMATCH` | Published diagrams differ from the checked canonical content or location |

Diagram diagnostics include section_key, diagram_type and the trusted path;
block errors also include block_index. They never copy PlantUML content.
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
`evidence_ids`, `uncertainty` on the wire. `report_sections` has exactly five sections, with keys
`scope`, `context`, `components`, `startup_and_flows`, `data_and_state`
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
block. Coordinates are written directly, without quote search or fuzzy match;
Markdown parsing only validates structure. `report_sections` is
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

Thematic omission areas retain `A-001` through `A-004` for context, components,
startup_and_flows and data_and_state; `A-005` adds scope. Each area requires one
omission_search response under the existing review policy. Missing or unfinished
scope inspection prevents positive acceptance, as for the other mandatory areas.

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
Default execution uses study → review, optionally one revised study → full review,
then selection → compare. Explicitly disabling review uses study → selection →
compare (comparison only for
multiple Git branches). Codex uses final JSON with local validation; no
orchestrator format-repair prompt is sent.

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
Context and schema JSON omit optional separator spaces. Input measurements record
exact character and UTF-8 byte counts; they do not estimate tokens or answer quality.
With a source map, `input_measurements.source_index` records `original_utf8_bytes`,
`transmitted_utf8_bytes`, `saved_utf8_bytes` and `reduction_percent`. The original
uses named fields and legacy separator spaces; the transmitted view uses compact rows.
`input.prompt.txt` contains the exact request actually sent, with its hash recorded
in invocation metadata. The backend receives the model schema, while local
validation and publication use expanded internal and saved schemas.

## Study scope and coverage

The strict Study document contains source identity, `completion_status`,
`limitations`, `report_sections`, `claims` and `evidence`. Subsystems, exclusions,
coverage assessments and relevance notes are auxiliary observations. Their
absence, wrong types or nonexistent paths cannot reject a valid document in
either result policy. Python preserves the original response and records auxiliary
errors in diagnostics without repairing model paths, changing claims, evidence or
authored text. This independent validation does not use compromise text recovery.
The optional saved-only `advisory` sidecar contains `raw_json` (the unchanged
recognized auxiliary values encoded as JSON) and `diagnostics` records with
`path`, `code` and `message`. Attempts also retain `advisory.json` and invocation
`advisory_diagnostics`. The complete original response remains in `extracted.json`;
expanded and normalized private responses retain auxiliary values too.
`study.annotated.md` and the consolidated report show auxiliary warning tables;
`ARCHITECTURE.md` keeps the authored report text unchanged.

Only the user's `source_filter` and Git rules determine the available source set.
Python records that set and snapshot in `source.inventory.json`, then creates
`coverage.plan.json` with `origin: "SOURCE_INVENTORY"`. Directory groups are
deterministic navigation aids, not model-declared components. The plan records the
source identity, inventory hash, areas and unique file, symbolic-link and directory
counts. Symlink targets are never followed. Model suggestions to exclude `src/`,
`vendor/` or another path remain observations; those files stay in the inventory
and evidence references to them receive the usual strict checks.

Study, Review and Revision share the same frozen inventory-derived plan. The same
snapshot and filter produce the same scope regardless of model classification.
Component boundaries and relevance judgments belong in `components`; investigated
areas, missing checks and uncertainty belong in `scope`, the relevant retained
section, and `limitations`.
Python requires each of four scope groups exactly once as a top-level H3 Markdown
heading: Investigated, Partially investigated, Unverified and Out of scope.
Russian labels are Исследовано, Частично исследовано, Не проверено and
Вне области исследования. Each group needs content; an explicit absence
with a reason is valid. Headings in code, quotes or ordinary text do not count.
The same check applies before materialization, to saved text and Scope blocks,
and at publication. Structural
validity does not establish semantic truth or complete investigation. These groups
remain agent assessments; no paths, defects or evidence need be invented to fill them.
Selected scenarios describe their trigger, components/path, branches and alternative
entries, source-supported links, unfinished paths and next checks. Each material
gap includes reason, potential impact and a concrete next check. User exclusions
and unselected investigation areas stay distinct; only source filters remove files.
Effects, alternate writers, configuration conditions and failure paths need source
evidence or HYPOTHESIS/UNKNOWN with missing checks. Absent behavior is "not established";
the prompts do not require invented retries, transactions or guarantees. Revision
retains uncertainty until new evidence resolves it. These requirements use the
existing claims, sections and limitations; no schema fields are added.
A structurally valid PARTIAL document is published with its complete registry and
limitations. Its self-assessment still prevents a COMPLETE workflow result; it
does not make the document contract invalid.

`program_checks.coverage` is diagnostic. `cited_ids` identifies areas with a
resolved source citation, and `not_cited_ids` identifies the rest. `NOT_CITED`
does not mean `NOT_INSPECTED`: this diagnostic does not record agent file reads. All areas
remain `UNVERIFIED` for inspection (`unverified_ids`), and
`completeness_measured` is false. No missing classification or citation changes
document acceptance. Review omission search still checks the frozen source areas,
thematic scopes (including the separate `scope` area) and user scenarios; mandatory
review responses and substantive findings retain their review policy rules.
`semantic_quality` stays `NOT_MEASURED`.
`INSPECTED` records the reviewer's reported activity, not actual completeness.
Source-tool observations can establish text delivery, not understanding.
`INSPECTED_UNVERIFIED` is not semantic verification; source-based FACT is not runtime
verification. Absence of a found counterexample does not establish absence of an
alternative path. No citation count establishes full path coverage.

`FINAL_REPORT.md` renders the selected revision's scope blocks, HYPOTHESIS/UNKNOWN
claims and their uncertainty/checks, review omission search, frozen coverage plan
and citation diagnostics. Source result metadata retains the pinned inventory's
`source_filter` settings and exclusion records. The summary labels these as program
records and keeps them distinct from author/reviewer assessments. Initial Study
metadata retains collection `tool_observation` and `source_access` in
`phase_invocations`; unavailable observations do not establish that no read occurred.
Rendering does not alter claims, source locators, hashes or acceptance checks.

`program_checks.coverage.metadata_verified_empty_paths` records regular files
whose pinned size and content hash establish empty content. This invents no line
evidence and proves no agent inspection. An invalid evidence line pointer still
fails, including a pointer into an empty file. Metadata never waives the strict
document or evidence requirements.

The inventory retains `source_lines`, which counts LF bytes plus one for a nonempty
unterminated final line. Blank lines count; CRLF counts once; standalone CR is not
a separator. Binary bytes use the same rule. Empty files have zero lines. Counts
are collected during the existing hash read, or reused from Git source preparation,
without another source-content scan. Metadata guards reuse the frozen counts.

## Bounded revision and selected artifacts

execution.max_revision_rounds is 0 or 1, default 1. Revision starts only after a
published, strictly valid study and review, when review completion_status is
COMPLETE and there are HIGH/MEDIUM findings. LOW-only findings, incomplete review,
transport failures and recovered contract-invalid material do not trigger it.
prompts.revise supplies a separate prompt to the existing study agent; no revision
backend profile is introduced. Each stage has its own budget; initial collection
and writing share the Study budget. Analysis has at most four stage runs per
snapshot: study and, when enabled, review plus one revision and repeat review.
These stage runs make at most five model calls because initial Study has two phases.
The new review default changes normal use from two to three calls per snapshot,
or five with revision and repeat review. Explicit `false` keeps two calls.
Eligible branch comparison adds one call per run. Failures can stop calls early.

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
artifacts. Selection prefers an accepted pair. In compromise it next prefers a
pair satisfying quote-only continuation. Otherwise it uses the latest
published pair whose review has completion_status COMPLETE; PARTIAL does not count
as completed. An incomplete new review keeps the previous
completed pair selected and exposes the newer study separately as unverified.
Without any completed review, the latest usable study is selected. A missing-review
warning applies only when review was enabled; an intentional skip is labeled as such.
The manifest records selected_revision and revision history. Comparison uses only
selected pairs. Top-level ARCHITECTURE.md, study.json and companion files are exact
copies of the selected artifacts, published after selection. Frozen-document checks
run within each revision; a new study can never inherit an older review.

The removed Catalog stage is not accepted in stage_agents or prompts. Configuration
errors identify the key to remove. The permanent artifact format is
`architecture-artifacts`; unsupported run formats and coverage plans
are rejected without migration or modification of existing reports.
