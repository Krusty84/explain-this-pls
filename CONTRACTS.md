# Evidence ledger contract

Response and saved-result schemas contain no version fields. The orchestrator
selects the required schema for each stage and validates its structure locally.
`schemas/{study,review,compare}.schema.json` and their `folder-` variants
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
| `completion_status`, narrative, claims, evidence pointers, findings, assessments, omission-search activity | Agent; completion is a self-assessment |
| Transport completion/error/timeout/interruption and cleanup | Backend adapter and runner |
| Expected source identities, review target, contract/artifact identifiers in invocation metadata and manifest | Orchestrator |
| `program_checks`, `verdict`, frozen plan, hashes, coverage, reverse links, tables | Python, after local wire validation |
| `model_requested` | Configuration |
| `model_actual`, `model_actual_source` | Backend metadata, or null with a reason; never inferred from executable or model prose |
| `review_quality` / `semantic_quality: NOT_MEASURED` | No substantive evaluation of this execution profile has been performed |

An attempt's private `extracted.json` preserves the original wire object.
Transport/stdout and stderr remain private attempt artifacts. A saved `study.json`,
`review.json` or `compare.json` is a separate representation with original wire
fields and `program_checks`. Version identifiers (`contract_version:
"evidence-ledger-v1"`, `artifact_version: "evidence-ledger-artifacts-v1"`) belong only
to invocation metadata and the manifest, not agent responses, saved-result schemas,
or the review plan. Study adds `review_plan`. Review also adds `review_plan`, the
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

`ARCHITECTURE.md` contains exactly the UTF-8 bytes of study `report_markdown`.
No trailing whitespace or newline normalization is applied before hashing or
publication. `study.annotated.md` contains generated checks and the registry.
`ARCHITECTURE_REVIEW.md` and `BRANCH_COMPARISON.md` contain generated tables;
`study.original.md`, `review.original.md` and `compare.original.md` preserve original
prose. The final report places limitations and assessments before original prose,
and keeps contradictory assessments visible. It is a study and automated review
summary, not an automatically corrected architecture.

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
UTF-8 decoding is strict; there is no replacement of invalid bytes. Lines split
on LF only; CRLF becomes LF; a lone CR stays data. A final nonterminated line
counts, and an empty file has zero lines. Document locators use the same rules.
File SHA-256 covers the original bytes. Fragment SHA-256 covers normalized UTF-8
bytes for the complete range, including its terminating LF when present. `quote`
may be `""`; otherwise it must equal that complete normalized range exactly.
There is no fuzzy correction. Document quotes must be nonblank and exact.

Limits per stage: 256 evidence records; 16 KiB canonical JSON per record; 8 MiB
per file; 200 lines and 64 KiB per fragment; 32 MiB total file bytes read (repeated
files count again). Reaching/exceeding a limit that prevents full resolution gives
`LIMIT_EXCEEDED`, never successful resolution of truncated data. No source fragment
is copied into generated tables. Model-supplied quotes remain in private wire data.

Each resolution stores namespaced ID, source identity, path, range, raw file hash,
fragment hash and status. Hashes not obtained remain null. `RESOLVED` means only
that the locator resolved. Other statuses distinguish `INVALID_POINTER`,
`UNKNOWN_SOURCE`, `SOURCE_SCOPE_MISMATCH`, `UNSAFE_PATH`, `NOT_FOUND`,
`ACCESS_DENIED`, `READ_ERROR`, `OUT_OF_RANGE`, `DECODE_ERROR`, `QUOTE_MISMATCH`,
`LIMIT_EXCEEDED`. Bad/inaccessible pointers prevent positive policy acceptance
without discarding an otherwise well-formed report. A real fragment may be
irrelevant to its architecture claim; the resolver never assigns `SUPPORTED`.

## Frozen registry and review plan

Study claims have `id`, `statement`, `scope`, `epistemic_kind`, `evidence_ids`,
`uncertainty`, `document_locator`. All material narrative/table/scenario claims
must be registered by the authoring agent. This requirement is not a claim that
Python can find every factual assertion in free text. A document locator is an
exact line range and quotation; no Markdown AST or fuzzy match is used.

Before review, `claim.registry.json` and `review.plan.json` fix the source catalog,
document SHA-256, registry SHA-256, required IDs, mandatory omission areas, and
study eligibility. Every configured priority scenario becomes another mandatory
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
(`study`/`review`), claim ID, document hash and registry hash. Strong differences
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
diagnostic. Ordinary successful execution still uses study → review → compare
(comparison only for multiple Git branches). Format repairs stay bounded and may
not change already valid facts/evidence/assessments. Backend eligibility gates are
unchanged, including the unavailable OpenCode native retry capability.

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
