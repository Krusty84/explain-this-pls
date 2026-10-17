# explain-this-pls Architecture

## Overview

`explain-this-pls` is a local Python orchestrator for legacy code analysis.
It calls Codex, OpenCode V2, or XXX based on OpenCode 1.2.27 and saves Markdown
and JSON reports. Model access uses installed CLIs.

`folder` mode analyzes a directory. `git` mode analyzes selected local branches.
Stages and branches run sequentially. `study` is the main stage.
`execution.review_enabled` enables `review` (default: `false`).
Document revision uses `study` with the `revise` template.
Multiple Git branches enable `compare`.
With review disabled, revisions and review/revision prompts and review-agent
settings are inactive. Reports explicitly state that no separate review ran.
Existing configs without `review_enabled` now skip review. Comparison remains
available as unverified differences requiring factual references from both
selected studies.

This file describes the orchestrator. Reports named `ARCHITECTURE.md` under
`reports_dir` describe the analyzed system. See [README.md](README.md) for usage
and [CONTRACTS.md](CONTRACTS.md) for evidence ledger rules.

## Repository Structure

```text
explain.py                 CLI and TUI entry point
install-explain.py         .venv setup and dependency installation
compare_read_context.py    Context preparation experiments
src/
  cli.py                   Arguments and interface selection
  runtime/                 Lifecycle, processes, limits, events, metrics
  analysis/                Sources, evidence, coverage, document revisions
  model/                   Context, agent invocation, response validation
  backends/                CLI adapters and Codex model catalog
  contracts/               Schemas and data consistency checks
  reports/                 Document assembly and publication
  tui/                     Textual interface and audit worker process
prompts/                   study, review, revise, compare templates
schemas/                   Response and saved-artifact JSON Schema files
tests/                     unittest; tests/fixtures/ contains fake CLIs
scripts/                   Prompt measurement and log replay
config.*.example.jsonc     Configuration examples for three backends
```

## Main Runtime Components

| Component                                                                  | Responsibility                                                                                                             |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| [cli.main](src/cli.py)                                                     | Selects TUI, audit, `--check`, or the `--models` catalog.                                                                  |
| [run_audit](src/runtime/audit.py)                                          | Loads configuration, creates the run directory, locks the source, and handles signals and shutdown. Shared by CLI and TUI. |
| [Runner](src/runtime/runner.py)                                            | Coordinates source modes, branches, comparison, Git restoration, and final publication.                                    |
| [RunSession](src/runtime/session.py)                                       | Holds configuration, source state, bindings, prepared context, metrics, and the critical-failure flag.                     |
| [SourcePipeline](src/runtime/pipeline.py)                                  | Runs study, optional review, and at most one revision round. Selects the published revision.                               |
| [ModelInvoker](src/model/invocation.py)                                    | Prepares the prompt and schema, calls the backend, records the attempt, validates the response, and requests publication.  |
| [Repository](src/analysis/repository.py), [Folder](src/analysis/folder.py) | Guard source state. Repository manages the Git hierarchy; Folder builds the inventory and pins file state.                 |
| [ReportPublisher](src/reports/publication.py)                              | Validates and saves reports, claim registries, review plans, and the selected revision.                                    |
| [Reporter](src/runtime/reporting.py), [RunMetrics](src/runtime/metrics.py) | Emit events, console output, and `run.log`; aggregate time, attempts, and available model usage.                           |

Packages group responsibilities. Dependencies cross package boundaries:
`analysis`, `contracts`, and `reports` call each other.

The TUI runs the same audit in a separate process through
[AuditProcess](src/tui/worker.py). A pipe carries events and the result;
[RunState](src/tui/state.py) updates the tables. Cancellation sends SIGTERM,
which enters normal cleanup.

### TUI Configuration and Model Selection

On first launch, the TUI saves the selected config folder's absolute path in
`explain.config` beside `explain.py`. This Git-excluded settings file requires a
writable installation directory. The picker scans JSON/JSONC files directly inside
the folder and excludes example configs and subfolders. For config selection and
model-selection controls, see [README.md](README.md#run-explain-this-pls).

The Codex **Models** dialog's **Use for** list maps **global** to `agent.model`
and **study**, **review**, or **compare** to `stage_agents.<stage>.model`. Inactive stages remain
selectable; setting a model does not enable them. Stage settings inherit the
shared agent settings unless overridden. Models with `visibility: hide` are
omitted. **Access via API** reports API support; **Access via ChatGPT** reports an
explicit check. **Check ChatGPT Access** checks ChatGPT login, then sends a short,
ephemeral, read-only request using the selected model and the user's Codex
allowance. **Use** requires a successful response. The specific ChatGPT-account
unsupported-model error marks a model unavailable; other errors leave access
unverified.

[config_edit.py](src/tui/config_edit.py) atomically updates only the selected model
field and creates a missing stage section. It preserves JSONC comments, formatting
and other settings. **Refresh** reloads the file and catalog and clears access
checks; changing the target field also clears them. An external edit requires
refresh before applying. **Cancel**, Escape or Ctrl+C closes the dialog and stops
a pending request. Catalog requests time out after 30 seconds; model checks after
60 seconds.

No-argument launches and `--tui` require interactive stdin/stdout and `TERM` other
than `dumb`.
`--no-progress` disables elapsed-time refresh but keeps stage updates; `--verbose`
adds diagnostics. With `--tui --check`, **Run** performs setup checks; the Models
dialog can still make an explicit access-check request. `--trust-repository`
trusts only the configured Git checkout and verified submodules despite ownership
mismatches. Explicit `--output text` or `--output json` cannot combine with `--tui`.

## Data Flow

1. [load_config](src/runtime/config.py) reads JSON/JSONC, validates fields,
   resolves paths relative to the config file, and merges `stage_agents` overrides
   into `agent`. Stage configuration checks apply to active stages.
   The source and `reports_dir` must be disjoint.
2. Preflight checks source state and CLI capabilities without model calls.
   Git mode pins branch commits, original HEAD/ref, and recursive submodules,
   then switches the checkout to each pinned commit for analysis.
   Folder mode records an inventory with content SHA-256 and file metadata.
3. [source_filter](src/analysis/source_filter.py) applies exclusions before
   inventory, hashing, and context preparation. The inventory defines an immutable
   [coverage plan](src/analysis/coverage_plan.py) shared by all document revisions.
   [SourceContextBuilder](src/analysis/source_context.py) prepares a full file/symbol
   map without source fragments or size quotas. Its cache stores maps and metadata.
4. ModelInvoker combines a `prompts/` template, stage context, and JSON Schema.
   [BindingRegistry](src/model/model_boundary.py) binds responses to the source
   snapshot and review target. The CLI receives the prompt through stdin.
   Study, review, and revise run in the source directory; compare runs in a
   temporary directory. All three adapters receive the same
   [source navigation index](#full-source-index) when source context is enabled.
5. The adapter extracts a completed JSON response. [ResultValidator](src/model/validation.py)
   checks identity, schema, and data consistency. Study/review evidence IDs are
   normalized; `compromise` also permits local citation repair. Study blocks become
   Markdown with computed claim coordinates.
6. [prepare_result](src/analysis/ledger.py) resolves evidence against the pinned
   source and computes `program_checks`. The document, claim registry, and review
   plan are frozen before review. A completed study/review pair with `HIGH` or
   `MEDIUM` findings can trigger one revision and a new full review.
   `execution.max_revision_rounds` accepts `0` or `1` (default: `1`);
   `0` disables revision while retaining enabled review.
7. For multiple branches, Runner passes selected report revisions and baseline-relative
   [inventory changes](src/analysis/git_sources.py) to compare. Compare does not
   inspect source. If comparison is unavailable, the orchestrator publishes
   `BLOCKED` without a model call. Finally, it restores the Git checkout,
   writes `FINAL_REPORT.md`, and commits the final `manifest.json`.

## Source Inventory and Navigation

### Filtering and Pinned State

`source_filter` applies before hashing, inventory, navigation, coverage planning
and study assignments. `follow_gitignore` defaults to `false`; `exclude_paths`
defaults to `[]`. See the [configuration example](README.md#configuration).

Folder mode always skips entries named `.git` at any depth before reading metadata
or contents, including directories, worktree files and symbolic links.

`exclude_paths` applies in both modes and each Git branch checkout. Paths are
exact, case-sensitive and relative to the common source root, with `/` separators.
An entry excludes a file or directory and its descendants. `build` does not match
`builder` or `packages/web/build`; nested paths must be explicit. Absolute paths,
empty components, `.`, `..`, backslashes, duplicates and glob patterns are invalid.
Use `.gitignore` for patterns. Configured exclusions override negation.

`follow_gitignore` must be a JSON boolean. In Folder mode, `true` enables root and
nested `.gitignore` files without requiring `.git`. Rules use their containing
directory as the base and support wildcards, anchors, directory rules and negation
through [pathspec](https://python-path-specification.readthedocs.io/en/v0.12.1/readme.html).
An ignored parent is pruned; child negation cannot reopen it. Rule files inside
pruned directories and symbolic-link rule files are not read. Explicit exclusion
of a `.gitignore` disables that rule file. A rule file that ignores itself remains
active as filter metadata. With no Folder index, rules apply to every matching
source file.

This flag controls additional Folder filtering; it does not disable Git ignore
handling. Git includes tracked files unless `exclude_paths` removes them and
excludes ignored untracked files. Existing Git rule and submodule control-file
checks remain active throughout a run.

Example configs contain commented optional exclusions for Java, JS/TS, C/C++,
Rust, Go, Python and Swift. Names such as `target`, `build` and `dist` can also hold
required code or resources. There is no framework detection or default artifact
exclusion list.

Artifacts record filter settings and excluded paths or pruned roots with `CONFIG`
or `GITIGNORE` origins. Folder `source.inventory.json` also records active rule-file
hashes and each matching rule's file and line. Git inventories record filter
exclusions. The manifest records pinned commits, recursive checkout plans, switch
attempts and verified restoration. Source filters, Git rules and the mandatory
Folder `.git` exclusion determine selection; agent relevance notes cannot change
the inventory or evidence checks. Active Folder rule files stay pinned even if an
edit would not change selection. Changes confined to excluded content do not
invalidate a filtered Folder snapshot. Directory guards pin device, inode and
permissions. Included entry membership and full file metadata remain checked.
Adding or removing excluded `.git` entries between checks does not invalidate
the snapshot.

Filtering is not a security boundary. Native CLI tools can access the original
directory, including excluded paths, under the CLI's permissions.

### Initial Study and Coverage

One initial Study runs per Folder source or selected Git branch, with source tools
enabled. Its pinned, filtered inventory defines scope. Python builds one
deterministic coverage plan for Study, Review and all revisions. Study returns
architecture sections, claims, evidence, completion status and limitations.
Component boundaries, relevance and investigation gaps belong in the report;
classification errors cannot reject a valid document or remove source files.
Enabled review and revision use the resulting study.

`coverage.plan.json` groups the filtered inventory by top-level directory and
source root. Citation diagnostics appear in the consolidated report.
`NOT_CITED` means no resolved
citation, not `NOT_INSPECTED`; Python leaves file inspection `UNVERIFIED`.

### Full Source Index

`source_context.enabled` is a strict boolean and defaults to `true`.
`source_context.mode` accepts `"on-demand"` (default) or `"full"`. Existing configs
without `mode` use `on-demand`; `full` restores inline delivery of all symbols.
When disabled, neither map preparation nor export runs. When enabled, the builder
parses the complete pinned inventory using the decoding rules before each stage;
on-demand delivery reduces prompt size, not source parsing work. Compare receives
no prepared source context.

Study, Review and Revision receive every file's source ID, relative path,
parser/version, language, encoding, line count and map status. Unknown values are
null; parse or read failures stay explicit entries. File order is deterministic;
the inventory is not truncated. The wire payload retains `mode: "index-only"` and
`fragments: []`, with `index_format: "dictionary-v1"` and a `delivery` field.

Internal maps and cache entries retain named fields. The model view defines
`file_columns` and, where symbols are present, `symbol_columns`; records are rows
in the supplied column order. Repeated file metadata and symbol `kind` and `parent`
values use zero-based table references. Tables follow first occurrence; all values,
nulls, empty values, list order and inclusive source line coordinates remain intact.
Context and schema JSON omit optional separator spaces.

With `delivery: "full"`, symbols stay inline. With `delivery: "on-demand"`, file
rows contain `symbol_count` and `symbols_ref`. Each file with symbols has one
multiline `M-000001.json`-style map under the attempt's `source-maps/` directory;
other files have a null reference. Each map contains complete declarations and
its own value tables, without source bodies. The payload supplies one absolute
`maps_root`. Agents read maps as needed through native tools with `offset`/`limit`
pages, then verify claims against original source. Every stage can read every map,
regardless of earlier agents' selections. A failed map read remains an analysis
gap; it does not trigger a hidden full-index fallback.

The attempt and maps exist before the final prompt is built. AttemptStore saves
maps atomically and records their byte sizes and SHA-256 values. Verification
before launch and publication rejects changed, missing or substituted maps. Codex
retains its read-only sandbox; OpenCode and XXX allow the current attempt's map
directory without opening neighboring reports or logs.

The run-local cache keeps maps and metadata, not source text, and validates pinned
state before reuse. Each attempt saves `source-context.manifest.json` with processing
status, map gaps, preparation time and sizes. Sizes distinguish the full named
index, packed full index, initial transmitted index and stored map files. Stored
map bytes are not bytes transmitted to the model; subsequent tool reads determine
the total. Source text fragments are absent, so `transmitted_text_bytes` is zero.
A full map can be larger than the source text. Source reads and evidence records
have no program size or count limits. Stage timeouts, source integrity checks and
native CLI/model limits remain. Capacity errors do not cause silent truncation.

`scripts/measure_prompt_sizes.py` measures complete synthetic Python indexes with
1, 1,000 and 5,000 files, both delivery modes, and the full stage prompts. It also
checks a large file and a long symbol name. It calls no model and makes no token
or answer-quality claims.

Python uses standard `ast`. The pinned `ast-grep-py` package supplies JavaScript,
TypeScript, JSX/TSX, Go, C/C++, Java, Rust and Swift grammars, including SwiftUI
through Swift. No project code or parser configuration runs. Other text formats
and syntax errors remain available through native source tools. Agents locate
files through the map, read implementations, check relationships, then cite source.
The map does not establish inspection or prove behavior.

`source_decoding.rules` applies to evidence checks and file decoding for the map;
it can declare legacy encodings such as CP1251. Decoding uses a BOM, an explicit
rule, or strict UTF-8. The map shows the encoding, but these rules do not change
native CLI decoding. No UTF-8 file copies are created on disk. An agent must report
a gap if it cannot read the original text exactly.

## Contracts and State

[contracts.py](src/contracts/contracts.py) defines response schemas.
[saved_contracts.py](src/contracts/saved_contracts.py) defines enriched saved data.
Files in `schemas/` match these definitions;
[test_result_regressions.py](tests/test_result_regressions.py) checks consistency.

Study returns five ordered `report_sections`, blocks linked through `claim_ids`,
`claims`, `evidence`, status, and limitations. Claims use `FACT`, `HYPOTHESIS`,
or `UNKNOWN`. Evidence specifies a source, relative path, line range, and quote.
[document_rendering.py](src/reports/document_rendering.py) computes Markdown,
`block_map`, and `document_locators`; the agent supplies no final document coordinates.

Review receives a frozen claim registry and omission-search plan. The program
computes the verdict from assessments, findings, and checks.
[revisions.py](src/analysis/revisions.py) computes registry changes and selects
one revision. Acceptance requires study and review from the same revision.
Without review, branch differences use `REPORTED_UNVERIFIED`.

| Field                  | Meaning                                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------- |
| `completion_status`    | Agent self-assessment: `COMPLETE`, `PARTIAL`, or `BLOCKED`.                                       |
| `program_checks`       | Schema, evidence, and processing-policy checks.                                                   |
| `accepted`             | Study and its matching review satisfy acceptance checks. Remains `false` when review is disabled. |
| `workflow_satisfied`   | Enabled stages satisfy strict policy checks.                                                      |
| `processing_completed` | Processing finished; `compromise` permits recorded citation warnings.                             |
| `publication_complete` | Publication recorded; independent of content quality.                                             |

### Failure Policy and Citation Diagnostics

The default `result_policy: "compromise"` keeps eligible partial material and labels
unreviewed or missing inputs. `strict` disables citation repair and recovery of
contract-invalid responses as partial report material. Both policies publish
structurally valid studies with valid source identity despite auxiliary
classification errors; those errors stay diagnostic. Evidence failures remain
explicit and prevent strict policy success. A valid `PARTIAL` study keeps
its claims and limitations but cannot satisfy a complete workflow. Required
document, evidence and source-integrity checks remain separate from permission
to continue. Detected source changes always stop the run; `continue_on_error`
defaults to `true` for other independent work.

Citation warnings show the evidence ID, source, path, lines, reason and affected
claims in CLI/TUI output and report tables. `UNRESOLVED` means the citation was not
verified; `CORRECTED` records a repaired range or common leading indentation.
Authored blocks and FACT labels stay unchanged. Problem statuses appear beside
linked architecture blocks; the H3 source index follows all five sections. Detailed
warning tables stay in `study.annotated.md` and `FINAL_REPORT.md`, without a sixth H2.
Warnings do not make unresolved citations proof. Structurally valid documents
can be saved with unresolved evidence under either policy. `strict` blocks
successful processing and continuation. `compromise` can complete when
`QUOTE_MISMATCH` is the only remaining evidence failure and other required checks
pass; strict evidence quality and acceptance remain unsatisfied.

Local repair under `compromise` searches only the same pinned file, within three
lines of each original boundary, and requires a unique match. It does not
approximate internal whitespace, identifiers or values. Original responses remain
unchanged; private attempt files retain corrections and full diffs. No extra model
call runs. `structured_output_repair_attempts` is a legacy setting ignored by all
backends; the orchestrator sends no format-repair request.

### Document Contract Migration

Former Catalog stage keys and old artifact formats are unsupported. Remove
`stage_agents.catalog` and `prompts.catalog`. Custom Study prompts must use the
current document contract; subsystem, exclusion and coverage arrays are no longer
required. The artifact format is `evidence-ledger-five-sections-v4-artifacts`.
Start a new run directory; existing reports remain unchanged. See
[CONTRACTS.md](CONTRACTS.md) for response, validation and storage contracts.

## Storage and Publication

State lives in RunSession memory and per-run files. Each run directory has a
UTC timestamp and random suffix.

| Files                                                           | Purpose                                                                                                          |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `config.snapshot.json`, `manifest.json`, `run.log`              | Configuration, stage state, diagnostics, metrics, and events.                                                    |
| `source.inventory.json`, `coverage.plan.json`                   | Pinned source inventory and analysis scope.                                                                      |
| `revisions/001/`, optional `revisions/002/`                     | Separate study/review revisions in Markdown and JSON.                                                            |
| `claim.registry.json`, `review.plan.json`, `registry.diff.json` | Claim registry, review target, and revision changes.                                                             |
| `study.logs/attempt-001/` and equivalent stage paths            | Prompt, schema, stdout/stderr, extracted and transformed responses, validation, bindings, and tool observations. |
| `FINAL_REPORT.md`                                               | Consolidated result, selected reports, limitations, and errors.                                                  |

Folder mode publishes selected reports at the run root. Git mode uses
`branches/<branch-id>/` per source and `comparison/` for comparison.
`ARCHITECTURE_REVIEW.md` exists when review is published.

The publisher keeps original text separate from reader-facing output. Published
revision files have hash checks. The selected revision is copied to the source's
report directory; earlier revisions remain available.
[atomic](src/runtime/artifacts.py) replaces each file atomically.
The file set is not a transaction; the final manifest write records publication.

### Review, Comparison and Consolidated Reports

`ARCHITECTURE_REVIEW.md` contains review checks, limitations and registered-claim
counts; individual claim/evidence assessments; source paths and lines; omission
search; and findings with impact and proposed corrections. Later reviews also
record whether earlier findings were resolved. The agent's explanation appears
in `FINAL_REPORT.md` and in `review.original.md` alongside the review.

`BRANCH_COMPARISON.md` uses existing studies and reviews to publish processing
checks, limitations and reported differences with assessments and rationale.
The detailed explanation appears in `FINAL_REPORT.md` and
`comparison/compare.original.md`. It covers:

- Main differences and caveats.
- Input branches, available reports and coverage gaps.
- Baseline architecture.
- Changes, unchanged aspects, consequences and uncertainty for each other branch.
- A capability/component matrix across branches.
- Documentation defects distinguished from code differences.
- Open questions and verification steps.

`FINAL_REPORT.md` combines the run summary and limitations, selected document
versions and revision material, citation diagnostics, full architecture/review
text, branch comparison and its explanation, and run errors. It records why
comparison is unavailable or unnecessary when applicable.

### Architecture Document Rendering

The published architecture report has exactly five numbered H2 sections. Python
fixes their names and order for Russian and English; model section titles do not
select published headings. Aliases `ru`, `russian`, `русский`, `en`, `english` and
`английский` are case-insensitive. Unsupported languages fail before a model call.

| Section               | Contents                                                                 |
| --------------------- | ------------------------------------------------------------------------ |
| Study scope           | Source studied, exclusions and investigation limits.                     |
| System context        | Purpose, external boundaries, processes and integrations.                |
| System components     | Components, responsibilities, dependencies and a PlantUML overview.      |
| Startup and execution | Startup and significant execution scenarios.                             |
| Data and state        | Storage, readers/writers, transactions and a PlantUML data-flow diagram. |

Each block with linked evidence is followed by compact evidence-ID links to the
H3 source index at the end of the document. Its four columns show ID, file with
inclusive line range, source ID and locator/quote status. Source identities appear
once per referenced source in a separate H3 table. Evidence is deduplicated per
block in structured reference order. Submodule paths include their source root
and retain pinned source identity. Unresolved locators carry
status warnings; an empty quote checks only the locator. Missing evidence gets
no invented reference.

`study.json` keeps structured claims and checks; `study.original.md` keeps the
canonical report used by Review. Published headings and references preserve each
authored block without changing canonical hashes, claim coordinates or the Review
target. Rendering is local and uses no model call, including with Review disabled.
Existing reports stay unchanged; integrity checks detect published-file changes.

COMPLETE requires a separate claim-linked PlantUML block in both System components
and Data and state. Python checks structure in these two sections (`components`
and `data_and_state`): fences, markers, nonempty content, claim links and forbidden
external directives, even with Review disabled. Extra diagrams are allowed.
PARTIAL/BLOCKED may omit diagrams with explicit limitations and next checks;
malformed diagrams in these two sections still fail validation. Other sections'
diagrams follow common Markdown and claim-link checks. `program_checks.diagrams`
records structure only. Review checks components, dependencies and flows against
evidence. The orchestrator does not compile or render diagrams; graphics require
a PlantUML-capable viewer.

Blocks may use H3–H6 subheadings. Additional H1/H2, including Setext, headings in
containers and raw HTML h1/h2, are rejected outside code. Fenced blocks must close;
literal headings inside code or escaped text are allowed. Errors identify section
and block. Python does not rewrite invalid structure or repeat the study. `strict`
fails publication; `compromise` can retain eligible material as unvalidated data,
without publishing it as a valid `ARCHITECTURE.md`.

## Key Design Decisions

- **Inventory defines scope.** Agent relevance and exclusion notes remain advisory.
  They cannot remove files from verification scope.
- **The program owns identity.** The model receives opaque `source_snapshot_id`
  and `review_target_id` values instead of service hashes.
  [Binding](src/model/model_boundary.py) validates IDs and restores full identities.
  [model_context.py](src/model/model_context.py) removes internal metadata and
  duplicates from prompts while preserving substantive text.
- **The agent authors; Python publishes.** Python assembles documents, computes
  coordinates, and saves results after CLI exit. Each ModelInvoker call uses one
  agent run. Citation repair needs no extra model call.
- **Failure policy is explicit.** [Recovery rules](#failure-policy-and-citation-diagnostics)
  distinguish contract warnings from critical failures. Source changes, artifact
  integrity failures, and cleanup errors stop further calls.
- **Each stage owns its processes.** [process.py](src/runtime/process.py) manages
  process groups, pipes, and cleanup. [Budget](src/runtime/execution.py) limits
  stage time to 3600 seconds by default. A source lock prevents concurrent audits
  by this orchestrator on the same path.
- **Repeated reads have separate observation.** [ToolObserver](src/runtime/tool_observer.py)
  distinguishes source delivery, native reads and repeats. The rules and telemetry
  limits are described below; events remain available for replay.

### Read-loop Observation

`execution.read_loop_guard` is a strict boolean and defaults to `true`. Setting it
to `false` keeps metrics but disables the stop. The observer uses completed tool
results only. At least 50 consecutive confirmed repeats spanning at least 120
seconds stop the owned process group with `NO_PROGRESS`. New material, errors,
incomplete or ambiguous results reset the streak. Model prose, reasoning and stderr
do not affect it. The first native read of source located through the map remains
valid. There is no model retry or format repair after this stop; normal source
checks, cleanup and independent-part continuation apply. Stage and idle timeouts
stay active.

Attempt metrics separate delivered lines, tool-read lines, exact repeats, model
requests and token usage. `tool-events.jsonl` records completed calls with monotonic
observation times for replay. Missing measurements include an availability reason;
overall metrics can remain partial while complete individual results confirm
repeats. Ambiguous or incomplete results do not count. Private logs can contain
source text.
Neither the initial index nor reads of saved symbol maps add delivered or tool-read
source lines. Source coverage comes from reads of the original source files.

OpenCode V2 and XXX/OpenCode V1 observe completed native tools and supported numbered
Read output. XXX keeps its separate session ownership and deletion checks. Codex
uses completed items and exact shell command/result comparisons; shell source-line
coordinates and model request counts are unavailable. Unknown output formats keep
conservative partial metrics.

## External Dependencies and Integrations

| Backend                                      | Protocol and boundary                                                                                                                                              |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Codex](src/backends/codex.py)               | `exec --ephemeral --sandbox read-only --output-schema --json`; final response comes from a separate file. Compare disables the shell tool.                         |
| [OpenCode V2](src/backends/opencode_cli.py)  | `run --standalone --format json`; a temporary profile permits source reads and searches. Events confirm completion; session export verifies a missing final event. |
| [XXX / OpenCode 1.2.27](src/backends/xxx.py) | One `run --format json`, mandatory export, and history verification including compaction. Confirmed owned sessions are deleted.                                    |

CLIs use the user's existing configuration and authentication.
[preflight.py](src/backends/preflight.py) checks versions and required commands.
[codex_models.py](src/backends/codex_models.py) provides the Codex catalog and
explicit access checks outside the analysis pipeline.

[requirements.txt](requirements.txt) pins `textual` for the TUI, `pathspec` for
`.gitignore`, `ast-grep-py` for the [source index](#full-source-index), and
`markdown-it-py` for Markdown validation.

### Codex Model Catalog

`--models` creates no audit reports and runs no access probes. Output groups catalogs
by executable and active stages and includes visible models' `slug`, `display_name`,
`visibility` and `supported_in_api`. `visibility: hide` models are omitted from
both text and JSON. API support does not confirm ChatGPT access.
`--output auto` selects text in a terminal and JSON otherwise; `--models` cannot
combine with `--check` or `--tui`.

An explicit access probe uses the configured Codex executable and consumes the
user's Codex allowance. The TUI checks login first; a direct probe is:

```sh
codex exec --ephemeral --sandbox read-only --skip-git-repo-check \
  --model MODEL 'Ответь только OK. Не используй инструменты.'
```

## Setup and Validation

See README.md for [installation](README.md#get-your-input-data) and
[local setup checks](README.md#run-explain-this-pls). No separate build step is required.
To run developer checks from the repository root after installation:

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use temporary sources and fake CLIs. Main checks cover the
[pipeline](tests/test_model_pipeline.py), [Git and submodules](tests/test_submodules.py),
[bindings](tests/test_model_boundary.py), [publication](tests/test_publication.py),
[response conversion](tests/test_conversion_integrity.py), and [TUI](tests/test_tui.py).
Installed OpenCode and XXX smoke tests require `EXPLAIN_OPENCODE_SMOKE=1` and
`EXPLAIN_XXX_COMPACTION_SMOKE=1`, respectively. The XXX test calls a real model.

### Replay Completed Tool Calls

[replay_tool_observation.py](scripts/replay_tool_observation.py) replays selected
attempts without starting a model:

```sh
python scripts/replay_tool_observation.py /path/to/logs \
  --attempt study.logs/attempt-001 --backend opencode \
  --output /path/to/replay
```

Repeat `--attempt` to select more attempts. The command prefers `tool-events.jsonl`
and falls back to `stdout.log`. `--output` is a file prefix: the example writes
`/path/to/replay.json` and `/path/to/replay.md`. These files contain the possible stop
time, call number, streak, metrics and observation limits. Optional `--context`
accepts saved orchestration context for source ownership and prompt delivery.
Recorded monotonic times reproduce the live rule. Native wall-clock timestamps
provide weaker evidence; logs without timestamps cannot establish the time
threshold. Tests use small fixtures and need no external report directory.

### Compare Source Context and Loop Protection

[compare_read_context.py](compare_read_context.py) compares one study using an
existing `source.inventory.json` and `coverage.plan.json`. `--baseline` points to
their directory. The tool reuses a completed Study's frozen scope and the normal
study prompt, source checks, result validation, publication and Git restoration.

First save significant facts with source references in JSON, for example:

```json
[
  {
    "id": "F-001",
    "statement": "The parser rejects an empty request.",
    "evidence": [
      {
        "id": "E-001",
        "source_id": "source-001",
        "path": "parser.py",
        "start_line": 12,
        "end_line": 15,
        "quote": ""
      }
    ]
  }
]
```

Use actual source references; the tool checks them before the first model call.
An empty quote checks coordinates only. Fact support requires human assessment.

```sh
python compare_read_context.py --config config.jsonc \
  --baseline /path/to/saved/source-report --facts /path/to/facts.json \
  --output /path/to/new/comparison --repetitions 3
```

This command saves the experiment plan without calling a model. Add `--live` and
choose a new output directory to run it. Each attempt runs one study. Review and
revision are outside the experiment scope. Each repetition runs three variants
in sequence:

| Variant   | Tool observation | Automatic stop | Full source index |
| --------- | ---------------- | -------------- | ----------------- |
| `observe` | On               | Off            | Off               |
| `guard`   | On               | On             | Off               |
| `context` | On               | On             | On                |

The source snapshot, inventory, coverage plan, configured model, prompt templates,
validation settings and timeouts stay fixed. A source, template, CLI version or reported model
change stops the comparison. CLI profile settings and an unreported actual model
remain external limitations. No model calls run in ordinary tests.

`comparison.json` and `comparison.md` separate successful, interrupted and failed
attempts. An early stop measures prevented waste, not a faster successful study.
Citation resolution counts do not establish factual correctness. Fact coverage
stays `unavailable` until a human supplies an assessment for each fact:

```json
[
  {
    "variant": "context",
    "repetition": 1,
    "facts": [{ "id": "F-001", "status": "supported", "claim_ids": ["C-001"] }]
  }
]
```

Statuses are `supported`, `omitted`, `unsupported` or `unassessed`. Apply them
without model calls:

```sh
python compare_read_context.py --summarize /path/to/comparison/comparison.json \
  --assessments /path/to/assessments.json
```

## Known Constraints

- In Git mode, linked worktrees, sparse/partial clones, external object alternates,
  and Git filters are unsupported. Submodules must be initialized locally with
  matching structure across snapshots. See [README.md](README.md#prerequisites)
  for system requirements.
- Source guards run at stage boundaries and during evidence reads. After initial
  hashing, Folder checks metadata without rehashing all files. This is not a backup
  or continuous immutability guarantee; changed source files are not restored.
- `static-only` prompts prohibit execution of analyzed code, builds, and tests.
  Native CLI permissions also restrict access. Filters and the compare working
  directory do not provide full filesystem isolation.
- `COMPLETE`, resolved evidence, and positive review do not prove factual accuracy
  or exhaustive inspection. Coverage measures citation distribution.
  `review_quality` and `semantic_quality` remain `NOT_MEASURED`.
- A [full source index](#full-source-index) can exceed native CLI/model capacity.
  Source decoding rules do not change CLI decoding. Capacity and decoding gaps
  stay explicit; maps do not establish inspection.
- [Diagram validation](#architecture-document-rendering) checks structure and
  claim links. Diagrams are not rendered; full syntax and semantic correctness
  are not established.
- Token, cost, and actual-model metrics depend on backend data. Unknown values
  remain unavailable. Private attempt logs can contain source code.
