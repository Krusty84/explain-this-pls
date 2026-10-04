# explain-this-pls

## What is this?

Analyzes legacy code and explains how the damn thing actually works.

explain-this-pls uses your coding agent to document an existing codebase, then
reviews the documented claims against the code in a separate session. Analyze a
folder, study a Git branch, or compare several branches. Agents are instructed to
read the code without running the project's builds or tests.

## Features

- **Architecture reports** explaining the system's main parts and workflows,
  with references to the source code.
- **PlantUML diagrams** of components and data flows, embedded as source in the reports.
- **Report review** identifying unsupported claims, contradictions and missing information.
- **Subsystem coverage** showing which areas the agent reports inspecting and where gaps remain.
- **Report revision** allowing one correction round and a new review while retaining earlier versions.
- **Branch comparison** showing differences from a branch you choose as a baseline.
- **Folder analysis** for source code that does not need to be in Git.
- **Shareable results** in Markdown and JSON, in your preferred language.

## Who is it for?

Anyone who needs to understand an unfamiliar codebase, document an older system,
or compare versions before planning changes.

## Instructions

### Prerequisites

- macOS or Linux.
- Python 3.11 or newer.
- Git 2.34.1 or newer, only for Git mode.
- An installed and configured coding-agent CLI: Codex CLI, Claude Code, or the proprietary XXX CLI.
  Sign in before using explain-this-pls.

The OpenCode backend is currently unavailable in this package.

### Get Your Input Data

Download this package, or clone it outside the source directory you want to inspect:

```sh
git clone https://github.com/Krusty84/explain-this-pls.git
cd explain-this-pls
```

Choose how to provide your source code:

- **Git mode:** use a standalone local clone with one or more local branches.
  Choose a baseline for comparison. Fetch the branches you need before starting.
  The clone must be clean, with no unfinished Git operations, uncommitted changes,
  untracked files, or ignored files.
- **Folder mode:** use an existing directory containing your source files.
  Git and a clean checkout are not required. Hidden files are included too.

If the project uses submodules, prepare them with
`git submodule update --init --recursive --checkout` in the source repository.
All submodules, including nested ones, must be clean and at their recorded versions.
Versions needed by the selected branches must be available locally, and submodule
names and paths must match across those branches and the original checkout.

Keep this package outside the source directory. Do not edit the source or switch
branches while analysis is running.

### Configuration

Copy the example for your agent to `config.jsonc`:

- [Codex CLI](config.example.jsonc)
- [Claude Code](config.claude-code.example.jsonc)
- [XXX](config.xxx.example.jsonc)

For example, with Codex CLI:

```sh
cp config.example.jsonc config.jsonc
```

Open `config.jsonc` in a text editor and replace the example paths and branch names
with your own. Paths are relative to the configuration file.
Keep source and report directories separate, with neither inside the other.

This example analyzes two branches and writes reports in English:

```json
{
  "project_description": "An ERP system originally developed in 1995",
  "mode": "git",
  "git_mode": {
    "repository": "../legacy-erp",
    "branches": ["main", "customer-version"],
    "baseline_branch": "main"
  },
  "reports_dir": "../architecture-reports/legacy-erp",
  "output_language": "English",
  "agent": {
    "backend": "codex",
    "executable": "codex"
  },
  "priority_scenarios": ["Order creation", "Inventory updates"]
}
```

For a single branch, set `git_mode.branches` to `["main"]` and
`git_mode.baseline_branch` to `"main"`. Comparison is skipped. Git mode restores
the original checkout after analysis.

For folder analysis, replace the source settings with:

```json
{
  "mode": "folder",
  "folder_mode": {
    "path": "../legacy-erp"
  }
}
```

Keep the report and agent settings. Folder mode creates the architecture report
and review without requiring Git; `git_mode` settings are ignored.

#### Main settings

| Field                 | Purpose                                                      |
| --------------------- | ------------------------------------------------------------ |
| `mode`                | Analyze Git branches (`"git"`) or a directory (`"folder"`).  |
| `git_mode`            | Source repository, local branches and the baseline branch.   |
| `folder_mode.path`    | Source directory for folder mode.                            |
| `reports_dir`         | Destination for results; each run gets a separate subfolder. |
| `project_description` | Optional introduction to the system's purpose and history.   |
| `output_language`     | Report language; defaults to `"Russian"`.                    |
| `priority_scenarios`  | Optional workflows or areas to focus on.                     |

#### Agent settings

| Field              | Purpose                                                                     |
| ------------------ | --------------------------------------------------------------------------- |
| `agent.backend`    | Coding agent: `codex`, `claude-code`, or `xxx`.                             |
| `agent.executable` | CLI command or path; defaults to `codex`, `claude`, or `xxx`, respectively. |
| `agent.model`      | Optional model name; omit it or use `null` for the CLI's configured model.  |

The tool uses your CLI's existing login and settings. No separate API key is needed
in this configuration.

#### Optional settings

The [commented configuration](config.example.jsonc) describes additional options:

- **Execution limits:** each stage has a one-hour limit by default. One correction
  round and repeat review are allowed after substantive review findings. Adjust
  `execution.stage_timeout_seconds` or `execution.max_revision_rounds` as needed.
  Revisions and optional response-format retries can increase model usage.
- **Different agents per stage:** use `stage_agents` to customize cataloging,
  analysis, review or comparison. Otherwise the shared agent settings apply.
- **Legacy encodings:** use `source_decoding.rules` to declare encodings such as CP1251.
  These rules affect checks of cited source text; the agent must also be able to
  read the files with its own tools.
- **Continuing after errors:** `continue_on_error` defaults to `true`. Set it to
  `false` to stop further agent calls after an error. Detected source changes always
  stop the run.

#### Custom prompts

The included prompts are ready to use. To customize them, copy an existing
[catalog](prompts/catalog.md), [study](prompts/study.md), [revision](prompts/revise.md),
[review](prompts/review.md), or [comparison](prompts/compare.md)
template, keep its required response format, and set its path in `prompts`.
Store custom templates outside the source directory. See the
[commented configuration](config.example.jsonc) for examples.

### Run explain-this-pls

First, check your configuration, source files, and CLI setup:

```sh
python3 explain.py --config config.jsonc --check
```

This checks your local setup without calling a model or switching branches.
It does not test your agent login or model availability.

Then start the analysis:

```sh
python3 explain.py --config config.jsonc
```

Progress messages show the current branch or folder and analysis stage. The final
summary shows the outcome and paths to the results. Console messages are in English;
`output_language` controls the reports.

Use `--no-progress` to hide the spinner and waiting messages, `--output json` for a
script-friendly summary, or `python3 explain.py --help` to see all options.

### Read the results

Each run saves results in a new subfolder of `reports_dir`. Start with these files:

| Report                   | Contents                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------- |
| `FINAL_REPORT.md`        | Start here: studies, reviews, comparison and limitations in one document.             |
| `SUBSYSTEM_CATALOG.md`   | Subsystems, purposes, paths, exclusions, limitations and unclassified source entries. |
| `ARCHITECTURE.md`        | How the system works, with component and data-flow diagrams.                          |
| `ARCHITECTURE_REVIEW.md` | Tables of review assessments, source references, findings and gaps.                   |
| `BRANCH_COMPARISON.md`   | A table of differences from the baseline, for two or more Git branches.               |

`FINAL_REPORT.md` is directly in the run folder. In Git mode, `ARCHITECTURE.md`
and `ARCHITECTURE_REVIEW.md` are under `branches/<branch-id>/`, and the comparison,
when applicable, is under `comparison/`. In folder mode, the architecture report
and review are directly in the run folder.
`SUBSYSTEM_CATALOG.md` is saved alongside `catalog.json` in the run folder for
folder mode, or under `branches/<branch-id>/` for Git mode. It is generated from
the validated catalog and coverage plan without another model call.

#### FINAL_REPORT.md — consolidated results

- **Run summary:** overall status and limitations affecting the results.
- **Results for each branch or folder:** document versions, review findings,
  subsystem coverage and the full architecture and review text. Additional revision
  material appears when available.
- **Comparison:** differences between branches and the agent's detailed explanation,
  or a note explaining why comparison is unavailable or unnecessary.
- **Run diagnostics:** errors encountered during analysis.

#### ARCHITECTURE.md — system architecture

The report has ten sections; headings use the configured report language.

| Section                           | What it explains                                                             |
| --------------------------------- | ---------------------------------------------------------------------------- |
| Scope and evidence basis          | The source studied, exclusions and limits of the investigation.              |
| System context and overview       | The system's purpose, external boundaries, processes and integrations.       |
| Component map                     | Main components, responsibilities, dependencies and a PlantUML overview.     |
| Startup and runtime flows         | How the system starts and how important scenarios execute.                   |
| Data and state                    | Storage, readers and writers, transactions and a PlantUML data-flow diagram. |
| Cross-cutting mechanisms          | Configuration, security, error handling, logging and concurrency.            |
| Constraints and legacy exceptions | Enforced restrictions, documented rules and observed conventions.            |
| Change navigation                 | Where to start common changes and which related tests to inspect.            |
| Unknowns and coverage             | Areas studied, gaps and next steps for verification.                         |
| Evidence basis and limitations    | Why the cited sources support the explanation and what remains uncertain.    |

The bundled prompts require both diagrams as PlantUML source blocks inside the
Markdown. Missing evidence must be explained. Viewing them as graphics requires a
PlantUML-capable viewer; this tool does not render images.

#### ARCHITECTURE_REVIEW.md — review results

- **Checks and assessments:** review outcome, limitations and how many of the
  report's registered claims were checked.
- **Claim assessments:** conclusions about individual statements and their cited evidence.
- **Source references:** files and lines used to support the review.
- **Omission search:** areas checked for missing architectural information.
- **Findings:** problems, their impact and proposed corrections; later reviews also
  show whether earlier findings were resolved.

The agent's written explanation is included in `FINAL_REPORT.md` and saved
separately as `review.original.md` alongside the review.

#### BRANCH_COMPARISON.md — branch differences

This file contains processing checks, limitations and a table of reported
differences with the agent's assessment and rationale. Comparison uses the existing
studies and reviews.

The agent's detailed comparison in `FINAL_REPORT.md` and
`comparison/compare.original.md` covers:

- **Executive comparison:** the main differences and important caveats.
- **Inputs and comparability:** branches, available reports and coverage gaps.
- **Baseline architecture:** a summary of the reference branch.
- **Each other branch:** changes, unchanged aspects, consequences and uncertainty.
- **Cross-branch matrix:** capabilities and components compared across branches.
- **Documentation defects:** gaps in the reports distinguished from code differences.
- **Open questions:** follow-up checks needed to resolve uncertainty.

### Understand the outcome

By default, useful partial results are kept even if a stage fails
(`result_policy: "compromise"`). Unreviewed material and missing inputs are labeled
in the final report. If a revision is produced, the results identify the selected
version and retain earlier material.

- **COMPLETE:** processing and review checks passed.
- **PARTIAL:** useful material is available, with unresolved issues or missing coverage.
- **FAILED:** no usable study was produced, or a critical error prevented completion.

Read the limitations and findings before relying on the reports. Source references
and automated review help assess the explanations, but do not prove correctness or
exhaustive coverage. Project builds and tests are not run.

For response formats, validation rules and artifact storage details, see
[CONTRACTS.md](CONTRACTS.md).
