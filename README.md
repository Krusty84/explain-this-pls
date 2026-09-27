# explain-this-pls

## What is this?

Analyzes legacy code and explains how the damn thing actually works

explain-this-pls is a set of Python scripts and prompts for researching and
documenting the implemented architecture of a legacy codebase across Git branches.
It uses your configured Codex CLI, Claude Code, or OpenCode to inspect source files
and produce reports.

For each selected branch, it creates an architecture document and reviews its
claims against the source in a separate session. It then compares the branch
reports against a chosen baseline. The investigation is static: the prompts
instruct agents to inspect code without running the project's builds or tests.

## Features

- **Architecture documentation** covering components, data flows, integrations,
  and important execution paths, with references to source evidence.
- **Independent review** of documented claims, highlighting unsupported conclusions,
  contradictions, and gaps in coverage.
- **Branch comparison** against a baseline, using reports and Git metadata tied
  to commit IDs pinned at the start of the run.
- **Choice of coding agent** with Codex CLI, Claude Code, and OpenCode support,
  including per-stage executable and model settings.
- **Markdown and JSON results** with validated output contracts, invocation logs,
  a run manifest, and a configuration snapshot.
- **macOS and Linux support** using Python's standard library, with no additional
  Python packages to install.

## Who is it for?

Developers, maintainers, and architects who need to understand an unfamiliar legacy
system, document how it is implemented, or compare customer-specific and historical
branches before planning changes.

## Instructions

### Prerequisites

- macOS or Linux.
- Python 3.11 or newer.
- Git with support for `git switch`.
- At least one installed and authenticated coding-agent CLI: Codex CLI, Claude Code,
  or OpenCode.

### Get Your Input Data

Clone this package outside the repository you want to inspect:

```sh
git clone https://github.com/Krusty84/explain-this-pls.git
cd explain-this-pls
```

Prepare a standalone local Git checkout of the legacy system, with at least two
local branches and one of them selected as the comparison baseline. The checkout
must have no staged or unstaged changes, untracked files, or ignored files. The
runner does not automatically stash or clean it, and does not fetch remote branches.

Keep this package and the reports outside the inspected repository. Prepare a short
description of the system's purpose and history for `project_description`; for
example, an ERP system originally developed in 1995.

### Configuration

The file passed with `--config` must contain a JSON object, without comments or
trailing commas. Copy one of the examples to `config.json`:
[config.example.json](config.example.json),
[config.claude-code.example.json](config.claude-code.example.json), or
[config.opencode.example.json](config.opencode.example.json).

```sh
cp config.example.json config.json
```

Set the repository and report paths, branch names, and agent settings for your system.
For example, this configuration documents three local branches and produces reports
in English:

```json
{
  "repository": "../legacy-erp",
  "reports_dir": "../architecture-reports/legacy-erp",
  "project_description": "An ERP system originally developed in 1995",
  "branches": ["master", "lockheed_ver10.2a.0", "general_ver10.0"],
  "baseline_branch": "master",
  "output_language": "English",
  "agent": {
    "backend": "codex",
    "executable": "codex",
    "model": null,
    "expected_version": null
  },
  "stage_agents": {},
  "priority_scenarios": ["Order creation", "Inventory updates"],
  "timeout_seconds": 1800,
  "max_input_bytes": 800000,
  "max_output_bytes": 16000000,
  "continue_on_error": true,
  "prompts": {}
}
```

#### Project and execution settings

| Field                 | Required / default | Description                                                                                                                                                                                                                                         |
| --------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `repository`          | Required           | Path to the root of the Git checkout to inspect. It must be a standalone checkout, not a linked worktree or bare repository.                                                                                                                        |
| `reports_dir`         | Required           | Directory for results. Each run creates a new subdirectory containing reports, logs, a manifest, and a configuration snapshot.                                                                                                                      |
| `project_description` | Recommended; `""`  | A short introduction to the system's purpose and history, passed to all three stages. It is user-provided background, not verified evidence about the implementation.                                                                               |
| `branches`            | Required           | At least two unique, nonempty local branch names, processed in the listed order. No fetch or pull is performed; commit IDs are pinned at startup.                                                                                                   |
| `baseline_branch`     | Required           | The reference branch for comparison. Must be included in `branches`.                                                                                                                                                                                |
| `output_language`     | `"Russian"`        | Language of the generated reports. Set `"English"` for English output.                                                                                                                                                                              |
| `agent`               | Required           | Default CLI settings for all stages; see below.                                                                                                                                                                                                     |
| `stage_agents`        | `{}`               | Overrides of `agent` for `document`, `review`, or `compare`.                                                                                                                                                                                        |
| `priority_scenarios`  | `[]`               | An array of strings describing flows or areas to prioritize during documentation and review.                                                                                                                                                        |
| `timeout_seconds`     | `1800`             | Maximum duration of each document, review, or comparison CLI call, in seconds. Must be a positive integer. CLI version/help checks have a separate 30-second limit.                                                                                 |
| `max_input_bytes`     | `800000`           | Maximum UTF-8 size of the complete prompt, context, and output schema sent to a stage. Must be a positive integer. Oversized input fails the stage before a model call; it is never silently truncated.                                             |
| `max_output_bytes`    | `16000000`         | Maximum combined stdout and stderr size per CLI call. Must be a positive integer. Exceeding it terminates the call.                                                                                                                                 |
| `continue_on_error`   | `true`             | Continue with other branches after a stage fails. Set `false` to stop on the first operational failure and attempt to restore the original checkout. Unexpected checkout changes always stop the run; review findings are not operational failures. |
| `prompts`             | `{}`               | Custom prompt paths for `document`, `review`, and `compare`. Unspecified stages use the bundled templates.                                                                                                                                          |

Relative repository, report, prompt, and explicit executable paths are resolved
against the configuration file's directory. `~` is expanded. The repository and
report directories must be separate: neither may contain the other. Prompt files
must exist outside the inspected repository.

Leading and trailing whitespace is removed from `project_description`. If the field
is missing, empty, or contains only whitespace, both normal runs and `--check` print
one warning and continue without asking for input. Its absence does not by itself
lower the report's completion status. Non-string values, including `null`, are rejected.

#### Agent settings

| Field              | Required / default | Description                                                                                                                                                                                                   |
| ------------------ | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend`          | Required           | One of `codex`, `claude-code`, or `opencode`.                                                                                                                                                                 |
| `executable`       | Backend default    | Command name or executable path. Defaults to `codex`, `claude`, or `opencode`, respectively. Command names are looked up in the inherited `PATH`; use `./name` for a path relative to the configuration file. |
| `model`            | `null`             | Optional model identifier passed to the CLI. Omit it or use `null` to let the configured CLI choose.                                                                                                          |
| `expected_version` | `null`             | Optional exact version string returned by the CLI's `--version` command. A mismatch stops the run before branch analysis.                                                                                     |

Configure and authenticate the CLI before starting the runner. The runner inherits
your environment and profile, including HOME, PATH, XDG settings, and CLI-specific
configuration directories. It does not require a separate API key, start a login
flow, or rewrite saved credentials. Every stage starts a new session rather than
resuming a previous conversation.

Each stage uses native CLI permissions and the user's profile. The runner does not
provide filesystem or profile isolation.

#### Per-stage overrides

Each entry in `stage_agents` is merged with the default `agent` object. This fragment
selects Claude Code for the independent review while leaving the other stages on
the default backend:

```json
{
  "stage_agents": {
    "review": {
      "backend": "claude-code",
      "executable": "claude",
      "model": null,
      "expected_version": null
    }
  }
}
```

When changing backends, explicitly override `executable`, `model`, and
`expected_version` if they are set in `agent`; otherwise their values are inherited.

#### Custom prompts

Use the `prompts` object to replace individual stage templates:

```json
{
  "prompts": {
    "document": "./custom-prompts/document.md",
    "review": "./custom-prompts/review.md"
  }
}
```

Here, `compare` still uses the bundled template. Custom prompts must follow the
existing output contracts: the runner appends the stage context and required JSON
Schema, and the agent must return the report in `report_markdown` rather than write
files itself.

### Run explain-this-pls

First, validate the configuration, Git state, and CLI capabilities:

```sh
python3 explain.py --config config.json --check
```

The `--check` option checks CLI availability, versions, and required capabilities
without calling a model or switching branches. It writes a manifest and configuration
snapshot under `reports_dir`.

Then start the investigation:

```sh
python3 explain.py --config config.json
```

The runner locks the repository, pins the selected branch commits, and checks out
each commit to run the `document` and `review` stages. It restores the original
checkout before running `compare` in a new session outside the repository. Avoid
editing or switching the inspected checkout while the run is in progress.

Each run creates a separate directory under `reports_dir`. Successful stages produce:

| Path within the run directory                          | Contents                                                                     |
| ------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `branches/<branch-id>/ARCHITECTURE.md`                 | Architecture documentation for one branch.                                   |
| `branches/<branch-id>/ARCHITECTURE_REVIEW.md`          | Independent review of that document.                                         |
| `branches/<branch-id>/document.json` and `review.json` | Structured versions of the branch reports.                                   |
| `comparison/BRANCH_COMPARISON.md` and `compare.json`   | Baseline comparison in Markdown and JSON.                                    |
| `comparison/inputs.json`                               | The reports and Git metadata supplied to the comparison stage.               |
| `manifest.json`                                        | Run status, pinned commits, stage results, and checkout restoration details. |
| `config.snapshot.json`                                 | The configuration used for this run.                                         |

Stage logs are stored in `document.logs`, `review.logs`, and `compare.logs` alongside
their respective reports. The terminal prints a JSON summary with the run status
and manifest path.
