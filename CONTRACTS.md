# Evidence ledger contract

Response schemas contain no version fields. The orchestrator
selects the required schema for each stage and validates its structure locally.
`schemas/{catalog,study,review,compare}.schema.json` and their `folder-` variants
are **wire** schemas generated from `contracts.py`. Separate `saved-*.schema.json`
files are generated from `saved_contracts.py` and locally validated before publication.
Historical formats without the required ledger structure are legacy. Absence of a
version field is normal and does not identify a legacy response. There is no
automatic upgrade or import system.
Old reports remain readable as original agent text, with the label “Legacy format;
new checks were not performed”. They cannot receive positive acceptance.

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

An attempt's private `extracted.json` preserves the original wire object.
Transport/stdout and stderr remain private attempt artifacts. A saved `study.json`,
`review.json` or `compare.json` is a separate representation with processed wire
fields and `program_checks`. Study narrative is authored as sections/blocks;
the program computes the Markdown, block map and every document locator.
Contract/artifact identifiers (`contract_version:
"evidence-ledger-v3"`, `artifact_version: "evidence-ledger-artifacts-v3"`) belong only
to invocation metadata and the manifest, not agent responses or the root of saved
results. The immutable coverage plan also records contract_version and is embedded
in saved review plans. Study adds `review_plan` and `normalization_provenance` (including
the normalization rule version). Review also adds `review_plan`, the
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
`study.original.md` retains the canonical program assembly of authored blocks
(the historical filename does not mean model-authored Markdown).
`review.original.md` and `compare.original.md` preserve original prose.
The final report places limitations and assessments before narrative,
and keeps contradictory assessments visible. It is a study and automated review
summary of the selected architecture revision and its corresponding review.

Each file is replaced atomically with private permissions. Multiple files are
**not** one atomic transaction. A stage is published only after all stage writes
and successful invocation metadata writes, then its result is recorded in the
manifest. `publication_complete` in invocation metadata and the last run manifest
is the explicit completion record. A stray Markdown file from a failed write is
not a completed artifact group and is not advertised by the CLI. A failed run may
still retain successfully published earlier stages and a diagnostic final report.

## Evidence locators

An evidence record has `id`, `source_id`, `path`, `start_line`, `end_line`, `quote`.
IDs are local `E-001` etc.; references use `study:E-001` or `review:E-001`.
Prefixed references are canonical. Study has the narrowly scoped compatibility
rule below; review always requires explicit namespaces because both its own
evidence and study evidence are available.
The orchestrator assigns `source-001` to the main source and subsequent IDs to
submodules sorted by root-relative path. Each nested source has its exact commit,
root path and containing main commit; it must be cited through its own source ID.
The folder identity is its fingerprint and canonical directory; folder mode does
not call Git. The resolver uses no network.

Paths use `/`, relative to the selected source root. Empty components, `.`, `..`,
absolute paths, drive paths, backslashes, NUL/control characters and `.git`
components are rejected. Every path component is opened relative to an already
opened directory descriptor with `O_NOFOLLOW`; directory/file identities are
checked before/after access. No symlink target or special file is read, including
during component replacement races. Detected source changes are fatal integrity
failures, not recoverable model format failures.
Before an agent call the runner inventories source-file hashes under the source
guards (excluding Git control directories in Git mode). Resolved bytes must match
that private inventory, including nested sources. This uses actual checkout bytes
so legitimate Git EOL transformations do not produce false blob mismatches.
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

## Evidence ID compatibility and diagnostics

`study_normalization.normalize_evidence(stage, value, context, mode='git')` is a pure
function returning `(candidate, changes)`. The candidate is a deep copy, including
when no changes are needed. It first requires the complete stage wire schema,
task, matching pinned Git/folder identity and, for review, unchanged frozen
context and exact target. The caller verifies transport before calling it.
It never repairs the schema. `normalize_study` remains a convenience wrapper.
The rule version is `EVIDENCE_IDS_V3` in both strict and compromise.

Evidence definitions may pad one/two numeric digits (`E-1`, `E-01` -> `E-001`)
and remove the stage's own namespace (`study:E-001` in study or `review:E-001`
in review -> `E-001`). Three or more digits are preserved (`E-0001` stays so).
An explicit original-definition -> canonical-ID mapping is built first. Duplicate
definitions and collisions (including `E-1` plus `E-001`) reject the entire
transformation. Structured references to mapped definitions are updated atomically,
including review findings. References already using the canonical ID are retained.
Study alone also qualifies bare references to defined local IDs with `study:`.
Review requires explicit namespaces and never changes frozen study evidence or C-*.

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
2. Retain the original object in private `extracted.json`. For study/review, check
   prerequisites, create the candidate, and write private `normalized.json` and
   `normalization.json` separately. These files are required even for zero edits.
3. Fully validate the wire candidate, including the study block/claim graph.
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
  "rule": "EVIDENCE_IDS_V3",
  "path": "$.claims[0].evidence_ids[0]",
  "before": "E-001",
  "after": "study:E-001"
}
```

Each change has the above shape. The provenance object contains `rule`,
`hash_format: "canonical-json-utf8-v1"`, `replacement_count`, `extracted_sha256`
and `normalized_sha256`. Both SHA-256 hashes cover the respective **entire wire
objects**, serialized with Python `json.dumps(ensure_ascii=False, sort_keys=True,
separators=(',', ':'), allow_nan=False)` and UTF-8 encoded, without BOM or trailing
newline. They do not hash the pretty-printed artifact file bytes. Unicode and
array order are unchanged. With zero edits both hashes are identical.
The same provenance summary is carried in invocation metadata and every new
published study/review; direct library callers may omit that summary, but every
new Runner publication requires the normalization artifacts. Older artifacts are
viewed in their historical format, not validated against the v3 saved schema.
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
| `UNKNOWN_REFERENCE`, `DUPLICATE_REFERENCE` | Non-evidence relations such as finding/claim links |
| `UNKNOWN_OMISSION_AREA_ID`, `DUPLICATE_OMISSION_AREA_ID` | Unknown or repeated review-plan area |

Diagnostics never echo arbitrary response values, source content or invalid IDs.
Paths contain trusted schema keys and numeric indices. Journal IDs are validated
local IDs and JSON encoded. Public retention messages state the contract reason,
that text was retained, that policy checks were not completed, and the agent's
self-assessment; they do not infer insufficient architectural evidence from a
structure error.

A string-valued `claims`, including large strings that strict JSON parsing cannot
decode, stays a string in private `extracted.json` alongside original authored blocks
(or legacy Markdown).
It is rejected with `CLAIMS_TYPE_MISMATCH at $.claims; expected array, got string`.
No nested decoding, partial extraction, empty-array substitution or model registry
reconstruction is performed. In compromise mode only the existing unvalidated
text-retention path applies; it never makes an accepted registry. The application
received a wrong type; this does not establish whether the model, runtime or
serialization produced it. Any future regeneration/reconstruction must be a
separate result revision with full review.

### Offline candidate checking versus a new acceptance pass

An offline caller may load a saved **wire** study using `strict_json`, supply its
expected identity, call `normalize_study`, and run `validate_result('study',
candidate, context, mode)`. This reads no sources, calls no model, publishes
nothing and grants no new COMPLETE. Keep any outputs in a separate location;
never overwrite the historical `extracted.json`, manifest, material or review.

Full reuse additionally requires the corresponding source snapshot, submodule
state and evidence checks, all cleanup/publication guards, and a newly frozen
review plan. Changing registry references changes its hash and requires a new
review; an old review of an empty/different registry cannot be made applicable by
replacing target hashes. No resume/import subsystem is implemented here.

For the investigated run `20261001T120459Z-bdbe271e91`, the reported master
`claims` was a 21,295-character string whose nested strict JSON decoding failed;
its actual contents were not supplied and are not classified as truncated JSON,
Markdown or an array. The 29 shown GLM violations were existing local IDs without
prefixes. Of DeepSeek's 48 reported violations, only 30 were shown with that
pattern; the remaining 18 are not assumed identical. Fixing that representation
may expose further contract, source or policy failures. No historical artifacts
or acceptance records are changed, and these observations do not establish
whether the architecture conclusions are correct.

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

`document_rendering.py` purely produces materialized `report_markdown`, `block_map`
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

Materialization provenance uses `study-blocks-lf-v1` and records distinct hashes
of normalized wire, exact UTF-8 document, final registry and block map. Combined
private provenance also records extracted/normalized wire hashes and the ID edit
journal separately. Identical canonical input yields identical text/locators/hashes.
Saved schemas use MATERIALIZED_CLAIM, never the locator-free wire CLAIM.

Before review, `claim.registry.json` and `review.plan.json` fix the source catalog,
document SHA-256, registry SHA-256, required IDs, mandatory omission areas, and
study eligibility, normalized decoding rules and immutable coverage plan. Every configured priority scenario becomes another mandatory
omission area. The response `target` must match all four hashes (source, document,
registry, plan). Repeating a hash does not prove reading the document.

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
are generated. Old two-direction wire relations are rejected, not reconciled.
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

## Comparison, compatibility and validation

Comparison is reports-only. A structured reference identifies branch, artifact
(`study`/`review`), selected revision ID, claim ID, document hash and registry hash. Strong differences
need references resolving on **both distinct sides** and policy-accepted inputs
on both sides. Each side needs a referenced FACT assessed as SUPPORTED; acceptance
of a hypothesis's caveat is not factual support for a strong contrast.
Legacy/recovered/incomplete inputs stay unverified. Missing reports
do not prove absent components, and missing comparison does not mean no differences.
An unresolved or unverified difference prevents positive comparison policy even
when the agent reports COMPLETE. A response with no listed differences is still
an agent assessment of the supplied reports, not proof of identical behavior.

`strict` rejects wire contract violations. `compromise` may retain completed text
with matching task/source identity as unvalidated material, but never invents
claims/evidence/positive assessments. A wrong review target, incomplete transport,
source mutation or cleanup failure cannot be repaired into usable acceptance.
Custom prompts that still emit legacy objects receive an explicit expected-contract
diagnostic. Migrate study prompts to ten structured sections and blocks[].claim_ids;
remove report_markdown and claims[].document_locator, keep source evidence ranges
and quotation rules. Review/compare keep their own Markdown. In compromise a legacy
Markdown or usable new blocks may be retained only as unvalidated narrative; new
blocks are labeled program assembly of authored blocks. No partial registry is
presented as accepted, and empty-registry review is explicitly limited/ineligible.
Historical v1 manifest, registry, target and acceptance records are read-only.
Use a new run directory; no automatic reacceptance/import is implemented.
Ordinary successful execution uses catalog → study → review, optionally one
revised study → full review, then selection → compare (comparison only for
multiple Git branches). Format repairs stay bounded and may
not change already valid facts/evidence/assessments. Backend eligibility gates are
unchanged, including the unavailable OpenCode native retry capability.

## Compact model context

A pure projection builds the model request without mutating internal context.
Review receives the whole document Markdown once and claim_registry once. Claim
coordinates retain start/end lines without duplicate quotes; block_map, duplicated
registries and private provenance are omitted. Comparison, substantive revision
and format-repair contexts use the same principle. Findings, evidence, limitations,
identifiers and diagnostics remain available; narrative is neither truncated nor
summarized. Full saved locators, registry hashes and document hashes remain intact.
Invocation metadata records the compact-context format version, and input.prompt.txt
contains the exact request actually sent.

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
Historical v1/v2 results remain readable without automatic hash migration or status
promotion. Custom prompts must adopt the v3 wire schemas; old outputs stay legacy
or explicitly unvalidated material under the existing policy.

For run `20261002T091648Z-95474db027`, only the reported error codes/paths and
generic unsupported-finish message are known. Actual bad ID spellings, Markdown /
locator pairs and `info.finish` were not supplied. `E-1`, `E-01` and prefixed
definitions in regression tests are synthetic, not observations from that run.
Its specific finish incompatibility remains unverified.

XXX uses the shared inspected OpenCode envelope checks: successful `stop` and
`tool-calls` were already supported. Closed diagnostics distinguish FINISH_MISSING,
FINISH_INVALID_TYPE, FINISH_UNKNOWN, FINISH_TRUNCATED (`length`), FINISH_ERROR
(`error`) and UNFINISHED_TOOL_CALL. No other success reason is enabled. Raw finish
and envelope remain private; classification is separate. Session/request/agent,
completion timestamp, errors, pending tools, native structured result, completed
StructuredOutput/input equality and exact final history snapshot equality remain mandatory.
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
