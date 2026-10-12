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

| Component | Responsibility |
|---|---|
| [cli.main](src/cli.py) | Selects TUI, audit, `--check`, or the `--models` catalog. |
| [run_audit](src/runtime/audit.py) | Loads configuration, creates the run directory, locks the source, and handles signals and shutdown. Shared by CLI and TUI. |
| [Runner](src/runtime/runner.py) | Coordinates source modes, branches, comparison, Git restoration, and final publication. |
| [RunSession](src/runtime/session.py) | Holds configuration, source state, bindings, prepared context, metrics, and the critical-failure flag. |
| [SourcePipeline](src/runtime/pipeline.py) | Runs study, optional review, and at most one revision round. Selects the published revision. |
| [ModelInvoker](src/model/invocation.py) | Prepares the prompt and schema, calls the backend, records the attempt, validates the response, and requests publication. |
| [Repository](src/analysis/repository.py), [Folder](src/analysis/folder.py) | Guard source state. Repository manages the Git hierarchy; Folder builds the inventory and pins file state. |
| [ReportPublisher](src/reports/publication.py) | Validates and saves reports, claim registries, review plans, and the selected revision. |
| [Reporter](src/runtime/reporting.py), [RunMetrics](src/runtime/metrics.py) | Emit events, console output, and `run.log`; aggregate time, attempts, and available model usage. |

Packages group responsibilities. Dependencies cross package boundaries:
`analysis`, `contracts`, and `reports` call each other.

The TUI runs the same audit in a separate process through
[AuditProcess](src/tui/worker.py). A pipe carries events and the result;
[RunState](src/tui/state.py) updates the tables. Cancellation sends SIGTERM,
which enters normal cleanup. `explain.config` separately stores the config-folder
preference.

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
   [SourceContextBuilder](src/analysis/source_context.py) prepares symbol maps
   and exact source fragments within the configured budget.
4. ModelInvoker combines a `prompts/` template, stage context, and JSON Schema.
   [BindingRegistry](src/model/model_boundary.py) binds responses to the source
   snapshot and review target. The CLI receives the prompt through stdin.
   Study, review, and revise run in the source directory; compare runs in a
   temporary directory.
5. The adapter extracts a completed JSON response. [ResultValidator](src/model/validation.py)
   checks identity, schema, and data consistency. Study/review evidence IDs are
   normalized; `compromise` also permits local citation repair. Study blocks become
   Markdown with computed claim coordinates.
6. [prepare_result](src/analysis/ledger.py) resolves evidence against the pinned
   source and computes `program_checks`. The document, claim registry, and review
   plan are frozen before review. A completed study/review pair with `HIGH` or
   `MEDIUM` findings can trigger one revision and a new full review.
7. For multiple branches, Runner passes selected report revisions and baseline-relative
   [inventory changes](src/analysis/git_sources.py) to compare. Compare does not
   inspect source. If comparison is unavailable, the orchestrator publishes
   `BLOCKED` without a model call. Finally, it restores the Git checkout,
   writes `FINAL_REPORT.md`, and commits the final `manifest.json`.

## Contracts and State

[contracts.py](src/contracts/contracts.py) defines response schemas.
[saved_contracts.py](src/contracts/saved_contracts.py) defines enriched saved data.
Files in `schemas/` match these definitions;
[test_result_regressions.py](tests/test_result_regressions.py) checks consistency.

Study returns ten ordered `report_sections`, blocks linked through `claim_ids`,
`claims`, `evidence`, status, and limitations. Claims use `FACT`, `HYPOTHESIS`,
or `UNKNOWN`. Evidence specifies a source, relative path, line range, and quote.
[document_rendering.py](src/reports/document_rendering.py) computes Markdown,
`block_map`, and `document_locators`; the agent supplies no final document coordinates.

Review receives a frozen claim registry and omission-search plan. The program
computes the verdict from assessments, findings, and checks.
[revisions.py](src/analysis/revisions.py) computes registry changes and selects
one revision. Acceptance requires study and review from the same revision.
Without review, branch differences use `REPORTED_UNVERIFIED`.

| Field | Meaning |
|---|---|
| `completion_status` | Agent self-assessment: `COMPLETE`, `PARTIAL`, or `BLOCKED`. |
| `program_checks` | Schema, evidence, and processing-policy checks. |
| `accepted` | Study and its matching review satisfy acceptance checks. Remains `false` when review is disabled. |
| `workflow_satisfied` | Enabled stages satisfy strict policy checks. |
| `processing_completed` | Processing finished; `compromise` permits recorded citation warnings. |
| `publication_complete` | Publication recorded; independent of content quality. |

## Storage and Publication

State lives in RunSession memory and per-run files. Each run directory has a
UTC timestamp and random suffix.

| Files | Purpose |
|---|---|
| `config.snapshot.json`, `manifest.json`, `run.log` | Configuration, stage state, diagnostics, metrics, and events. |
| `source.inventory.json`, `coverage.plan.json` | Pinned source inventory and analysis scope. |
| `revisions/001/`, optional `revisions/002/` | Separate study/review revisions in Markdown and JSON. |
| `claim.registry.json`, `review.plan.json`, `registry.diff.json` | Claim registry, review target, and revision changes. |
| `study.logs/attempt-001/` and equivalent stage paths | Prompt, schema, stdout/stderr, extracted and transformed responses, validation, bindings, and tool observations. |
| `FINAL_REPORT.md` | Consolidated result, selected reports, limitations, and errors. |

Folder mode publishes selected reports at the run root. Git mode uses
`branches/<branch-id>/` per source and `comparison/` for comparison.
`ARCHITECTURE_REVIEW.md` exists when review is published.

The publisher keeps original text separate from reader-facing output. Published
revision files have hash checks. The selected revision is copied to the source's
report directory; earlier revisions remain available.
[atomic](src/runtime/artifacts.py) replaces each file atomically.
The file set is not a transaction; the final manifest write records publication.

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
- **Failure policy is explicit.** Default `compromise` can retain useful text after
  certain contract failures and continue with warnings. `strict` disables this
  recovery. Source changes, artifact integrity failures, and cleanup errors stop
  further calls.
- **Each stage owns its processes.** [process.py](src/runtime/process.py) manages
  process groups, pipes, and cleanup. [Budget](src/runtime/execution.py) limits
  stage time to 3600 seconds by default. A source lock prevents concurrent audits
  by this orchestrator on the same path.
- **Repeated reads have separate observation.** [ToolObserver](src/runtime/tool_observer.py)
  stops a stage with `NO_PROGRESS` after 50 consecutive confirmed repeats spanning
  at least 120 seconds. Unknown or incomplete telemetry cannot prove a loop.
  Events remain available for replay.

## External Dependencies and Integrations

| Backend | Protocol and boundary |
|---|---|
| [Codex](src/backends/codex.py) | `exec --ephemeral --sandbox read-only --output-schema --json`; final response comes from a separate file. Compare disables the shell tool. |
| [OpenCode V2](src/backends/opencode_cli.py) | `run --standalone --format json`; a temporary profile permits source reads and searches. Events confirm completion; session export verifies a missing final event. |
| [XXX / OpenCode 1.2.27](src/backends/xxx.py) | One `run --format json`, mandatory export, and history verification including compaction. Confirmed owned sessions are deleted. |

CLIs use the user's existing configuration and authentication.
[preflight.py](src/backends/preflight.py) checks versions and required commands.
[codex_models.py](src/backends/codex_models.py) provides the Codex catalog and
explicit access checks outside the analysis pipeline.

[requirements.txt](requirements.txt) pins `textual` for the TUI, `pathspec` for
`.gitignore`, `ast-grep-py` for JS/TS, JSX/TSX, Go, C/C++, Java, Rust, and Swift
symbol maps, and `markdown-it-py` for Markdown validation. Python uses standard
`ast`. Git is required only in `git` mode.

## Setup and Validation

[install-explain.py](install-explain.py) creates or reuses `.venv` and installs
dependencies. No separate build step is required. From the repository root:

```sh
python3 install-explain.py
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use temporary sources and fake CLIs. Main checks cover the
[pipeline](tests/test_model_pipeline.py), [Git and submodules](tests/test_submodules.py),
[bindings](tests/test_model_boundary.py), [publication](tests/test_publication.py),
[response conversion](tests/test_conversion_integrity.py), and [TUI](tests/test_tui.py).
Installed OpenCode and XXX smoke tests require `EXPLAIN_OPENCODE_SMOKE=1` and
`EXPLAIN_XXX_COMPACTION_SMOKE=1`, respectively. The XXX test calls a real model.

`explain.py --config <file> --check` checks local setup without model calls or
branch switches. It does not test model access or authentication.
[replay_tool_observation.py](scripts/replay_tool_observation.py) replays saved
tool events without a model. [compare_read_context.py](compare_read_context.py)
compares context variants on frozen scope; model calls require `--live`.

## Known Constraints

- Requires macOS/Linux and Python 3.11+. Git mode requires Git 2.34.1+ and a clean
  standalone checkout. Linked worktrees, sparse/partial clones, external object
  alternates, and Git filters are unsupported. Submodules must be initialized
  locally with matching structure across snapshots.
- Source guards run at stage boundaries and during evidence reads. After initial
  hashing, Folder checks metadata without rehashing all files. This is not a backup
  or continuous immutability guarantee; changed source files are not restored.
- `static-only` prompts prohibit execution of analyzed code, builds, and tests.
  Native CLI permissions also restrict access. Filters and the compare working
  directory do not provide full filesystem isolation.
- `COMPLETE`, resolved evidence, and positive review do not prove factual accuracy
  or exhaustive inspection. Coverage measures citation distribution.
  `review_quality` and `semantic_quality` remain `NOT_MEASURED`.
- Prepared context defaults to 65536 bytes; fragments contain at most 120 lines.
  Unsupported syntax retains text reading. [source_decoding](src/analysis/source_decoding.py)
  uses a BOM, explicit rule, or UTF-8. These rules do not change CLI decoding.
- A successful Study requires PlantUML blocks for components and data flow.
  Checks cover block structure and claim links. Diagrams are not rendered;
  full syntax and semantic correctness are not established.
- Token, cost, and actual-model metrics depend on backend data. Unknown values
  remain unavailable. Private attempt logs can contain source code.
