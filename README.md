# explain-this-pls

## What is this?

Analyzes legacy code and explains how the damn thing actually works.

explain-this-pls uses your coding agent to document an existing codebase, then
independently reviews the report against the code. Analyze a folder, study a Git
branch, or compare several branches. Agents are instructed to read the code
without running the project's builds or tests.

## Features

- **Architecture reports** explaining the system's main parts and workflows,
  with references to the source code.
- **Independent review** highlighting unsupported claims and missing information.
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
- An installed and configured coding-agent CLI: Codex CLI, Claude Code, or special proprietary XXX.
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
- [OpenCode](config.opencode.example.jsonc)
- [XXX](config.xxx.example.jsonc)

For example, with Codex CLI:

```sh
cp config.example.jsonc config.jsonc
```

Open `config.jsonc` in a text editor and replace the example paths and branch names
with your own. Paths are relative to the configuration file.
Keep source and report directories separate, with neither inside the other.

This complete example analyzes two branches and writes reports in English:

```json
{
  "project_description": "An ERP system originally developed in 1995",
  "mode": "git",
  "git_mode": {
    "repository": "../legacy-erp",
    "branches": ["main", "customer-version"],
    "baseline_branch": "main"
  },
  "folder_mode": {
    "path": "../legacy-erp"
  },
  "reports_dir": "../architecture-reports/legacy-erp",
  "output_language": "English",
  "agent": {
    "backend": "codex",
    "executable": "codex",
    "model": null,
    "expected_version": null
  },
  "stage_agents": {},
  "priority_scenarios": ["Order creation", "Inventory updates"],
  "continue_on_error": true,
  "prompts": {}
}
```

To analyze only `master`, keep `mode` set to `"git"` and use:

```json
"git_mode": {
  "repository": "../legacy-erp",
  "branches": ["master"],
  "baseline_branch": "master"
}
```

This creates an architecture report and review, then restores the original Git
checkout. `baseline_branch` must match the selected branch. Comparison is skipped.

For source code outside Git, change `mode` to `"folder"` and set `folder_mode.path`.
Folder mode creates an architecture report and review `git_mode` settings are ignored.

#### Project and execution settings

| Field                      | What to enter                                                                     |
| -------------------------- | --------------------------------------------------------------------------------- |
| `mode`                     | `"git"` to analyze one or more branches or `"folder"` for source outside Git.     |
| `git_mode.repository`      | Path to your standalone local clone, required in Git mode.                        |
| `git_mode.branches`        | One or more distinct local branch names, required in Git mode.                    |
| `git_mode.baseline_branch` | Required member of `branches` used as the baseline when comparing branches.       |
| `folder_mode.path`         | Path to your source directory, required in folder mode.                           |
| `reports_dir`              | Required destination for reports. Each run gets its own subfolder.                |
| `project_description`      | A short description of the system's purpose and history. Optional.                |
| `output_language`          | Report language, such as `"English"`. Default: `"Russian"`.                       |
| `agent`                    | Required settings for your coding agent see below.                               |
| `stage_agents`             | Optional agent settings for individual stages. Default: `{}`.                     |
| `priority_scenarios`       | Workflows or areas to focus on. Default: `[]`.                                    |
| `continue_on_error`        | Continue with other branches after a stage fails. Default: `true`. Git mode only. |
| `prompts`                  | Optional paths to your own analysis instructions. Default: `{}`.                  |

Detected source changes always stop the run, even with `continue_on_error` enabled.

Optional execution settings and their defaults:

```json
"execution": {
  "stage_timeout_seconds": 3600,
  "idle_timeout_seconds": null,
  "opencode_format_retries": 2,
  "structured_output_repair_attempts": 0,
  "http_timeout_seconds": 5,
  "api_doc_timeout_seconds": 30
}
```

Timeouts are in seconds and must be greater than zero.

- `stage_timeout_seconds`: maximum time for each analysis, review or comparison stage.
- `idle_timeout_seconds`: stop after this much inactivity `null` disables this limit.
- `http_timeout_seconds`: time allowed for service requests to XXX or OpenCode.
- `api_doc_timeout_seconds`: time allowed to check the agent's API compatibility.
- `structured_output_repair_attempts`: allow up to 1 or 2 extra model requests to
  correct response formatting with XXX or OpenCode. Default `0` disables corrections.
- `opencode_format_retries`: format retries for OpenCode, from 0 to 2 ignored by XXX.

The XXX and OpenCode examples enable one correction attempt. Extra requests may
increase model usage, but must fit within the original stage time limit.

#### Agent settings

| Field              | What to enter                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------- |
| `backend`          | Required: `codex`, `claude-code`, `opencode`, or `xxx`.                                                 |
| `executable`       | CLI command or path. Defaults to `codex`, `claude`, `opencode`, or `xxx`, respectively.                 |
| `model`            | Optional model name. Leave `null` to use your CLI's configured model.                                   |
| `expected_version` | Optional exact CLI version from `--version`, a mismatch stops the run. Leave `null` to skip this check. |

The tool uses your CLI's existing login and settings. No separate API key is needed
in this configuration.

#### Per-stage overrides

Leave `stage_agents` empty to use one agent throughout. To choose a different agent
for analysis (`study`), review (`review`), or comparison (`compare`), edit the
corresponding block in the [commented example](config.example.jsonc).

Unspecified settings use the values from `agent`. When changing the backend, also
update `executable` and clear or replace any inherited `model` and `expected_version`.
The `compare` stage is used only in Git mode with two or more selected branches.

#### Custom prompts

The included prompts are ready to use. To customize them, copy an existing
[study](prompts/study.md), [review](prompts/review.md), or [comparison](prompts/compare.md)
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
summary shows the outcome and paths to the results. Console messages are in English
`output_language` controls the reports.

Add these optional flags to either command as needed:

| Option               | Purpose                                                                     |
| -------------------- | --------------------------------------------------------------------------- |
| `--output auto`      | Default: readable text in a terminal, JSON when redirected.                 |
| `--output text`      | Always show a readable final summary.                                       |
| `--output json`      | Return the final result as JSON for scripts.                                |
| `--verbose`          | Show extra diagnostic details.                                              |
| `--no-progress`      | Hide periodic waiting messages keep stage updates and errors.              |
| `--trust-repository` | Allow a Git checkout owned by another user, if you trust it. Git mode only. |

Each run saves results in a new subfolder of `reports_dir`. Start with these files:

| Report                   | Contents                                                               |
| ------------------------ | ---------------------------------------------------------------------- |
| `FINAL_REPORT.md`        | Start here: consolidated results, coverage, discrepancies and caveats. |
| `ARCHITECTURE.md`        | How the system works.                                                  |
| `ARCHITECTURE_REVIEW.md` | Review findings and gaps in the architecture report.                   |
| `BRANCH_COMPARISON.md`   | Differences from the baseline, with two or more selected Git branches. |

In Git mode, the first two reports are under `branches/<branch-id>/`, and the
comparison, when applicable, is under `comparison/`. In folder mode, the first two
reports are directly in the run folder.

The default `"result_policy": "compromise"` preserves usable material even when
individual stages fail. The orchestrator assembles `FINAL_REPORT.md` without an
additional model request, in both folder and Git modes. Original reports are not
rewritten. A study without a completed review is explicitly unverified; review
inconsistencies and missing inputs appear before the affected material.

Strictly validated results retain their existing filenames and JSON contracts.
Completed study/review text with a valid task and source identity but other contract
defects is retained separately in `study.material.json` / `review.material.json`.
It never becomes an accepted result. Malformed JSON, foreign identities, incomplete
transport responses, and results from changed sources are not recovered.

The existing comparison stage can use a usable baseline study and at least one
other study without requiring review PASS. Comparisons involving unaccepted inputs
remain unverified. If comparison fails or inputs are missing, the final report still
contains the available studies and reviews. With no usable study, it is explicitly
a diagnostic summary. `--check` does not create a final report.

Compromise exit codes: `0` / `COMPLETE` for full strict acceptance, `2` / `PARTIAL`
for usable material with caveats or individual stage failures, `1` / `FAILED` for no
usable study or critical source-integrity, restoration, cleanup, or publication
failure. Interruption remains `130`. `continue_on_error: false` stops further agent
calls but still assembles previously completed material. Configured format repairs
remain bounded; semantic errors do not trigger extra model requests.
