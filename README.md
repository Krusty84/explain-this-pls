# explain-this-pls

## What is this?

Analyzes legacy code and explains how the damn thing actually works

explain-this-pls is a set of Python scripts and prompts for researching and
documenting the implemented architecture of a legacy codebase in a source folder
or across Git branches.
It uses your configured Codex CLI, Claude Code, or OpenCode to inspect source files
and produce reports.

It creates an architecture document and reviews its claims against the source in
a separate session. Git mode does this for each selected branch and then compares
the reports against a chosen baseline. Folder mode inspects one existing directory
without requiring Git. The investigation is static: the prompts
instruct agents to inspect code without running the project's builds or tests.

## Features

- **Architecture documentation** covering components, data flows, integrations,
  and important execution paths, with references to source evidence.
- **Independent review** of documented claims, highlighting unsupported conclusions,
  contradictions, and gaps in coverage.
- **Branch comparison** against a baseline, using reports and Git metadata tied
  to commit IDs pinned at the start of the run.
- **Plain-folder research** with file inventories and SHA-256 fingerprints checked
  at stage boundaries, without creating a repository or copying the source.
- **Commented JSONC configuration** showing both modes and all available options,
  with support for existing JSON configurations.
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
- Git with support for `git switch`, only when using git mode.
  `--trust-repository` requires Git 2.38 or newer, which supports `safe.directory`
  in command-line protected configuration (see the
  [Git 2.38 configuration documentation](https://git-scm.com/docs/git-config/2.38.0)).
- At least one installed and authenticated coding-agent CLI: Codex CLI, Claude Code,
  or OpenCode.

### Get Your Input Data

Download this package, or clone it outside the source directory you want to inspect:

```sh
git clone https://github.com/Krusty84/explain-this-pls.git
cd explain-this-pls
```

For git mode, prepare a standalone local checkout of the legacy system, with at least two
local branches and one of them selected as the comparison baseline. The checkout
must have no staged or unstaged changes, untracked files, or ignored files. The
runner does not automatically stash or clean it, and does not fetch remote branches.

For folder mode, prepare an existing directory with the source files and subdirectories.
There is no requirement for `.git`, branches, or a clean checkout. All entries,
including hidden files, are inventoried; there are no configurable exclusions.
Symbolic links are recorded without following their targets. Unreadable entries
and special files such as FIFOs stop the run with an error.

Keep this package and the reports outside the inspected source directory. Prepare a short
description of the system's purpose and history for `project_description`; for
example, an ERP system originally developed in 1995.

### Configuration

Copy one of the fully commented examples to `config.jsonc`:
[config.example.jsonc](config.example.jsonc),
[config.claude-code.example.jsonc](config.claude-code.example.jsonc), or
[config.opencode.example.jsonc](config.opencode.example.jsonc).

```sh
cp config.example.jsonc config.jsonc
```

Files ending in `.jsonc` support `//` and `/* ... */` comments and trailing commas.
Ordinary `.json` files remain strict JSON. Both formats reject duplicate keys and
non-finite numbers. Configuration snapshots and generated reports remain plain JSON.

Choose `mode`, fill in its source section, and set the report and agent settings.
Both modes are visible in the examples; only the selected section is required and
used. An inactive section must be an object, but its contents and paths are not
validated. The presence of `.git` never changes the selected mode automatically.

This shorter example documents three local branches and produces reports in English.
Change `mode` to `"folder"` to research `folder_mode.path` without a branch comparison:

```json
{
  "project_description": "An ERP system originally developed in 1995",
  "mode": "git",
  "git_mode": {
    "repository": "../legacy-erp",
    "branches": ["master", "lockheed_ver10.2a.0", "general_ver10.0"],
    "baseline_branch": "master"
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
  "timeout_seconds": 1800,
  "max_input_bytes": 800000,
  "max_output_bytes": 16000000,
  "continue_on_error": true,
  "prompts": {}
}
```

Existing flat configurations with top-level `repository`, `branches`, and
`baseline_branch` still run in git mode. To migrate, move these three fields into
`git_mode` and add `"mode": "git"`. Do not mix flat source fields with the new format.

#### Project and execution settings

| Field                 | Required / default | Description                                                                                                                                                                                                                                         |
| --------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mode` | Required in grouped format | `"git"` or `"folder"`; only the selected source section is used. |
| `git_mode.repository` | Required in git mode | Root of a standalone checkout, not a linked worktree or bare repository. |
| `folder_mode.path` | Required in folder mode | Existing source directory; no Git installation or repository is required. |
| `reports_dir`         | Required           | Directory for results. Each run creates a new subdirectory containing reports, logs, a manifest, and a configuration snapshot.                                                                                                                      |
| `project_description` | Recommended; `""` | Introduction to the system's purpose and history, passed to every active stage as user background, not verified implementation evidence. |
| `git_mode.branches` | Required in git mode | At least two unique, nonempty local branch names, processed in order. No fetch or pull is performed; commit IDs are pinned at startup. |
| `git_mode.baseline_branch` | Required in git mode | Reference branch for comparison. Must be included in `git_mode.branches`. |
| `output_language`     | `"Russian"`        | Language of the generated reports. Set `"English"` for English output.                                                                                                                                                                              |
| `agent`               | Required           | Default CLI settings for all stages; see below.                                                                                                                                                                                                     |
| `stage_agents` | `{}` | Overrides of `agent` for `document`, `review`, or `compare`. Only active stages are resolved and checked. |
| `priority_scenarios`  | `[]`               | An array of strings describing flows or areas to prioritize during documentation and review.                                                                                                                                                        |
| `timeout_seconds`     | `1800`             | Maximum duration of each document, review, or comparison CLI call, in seconds. Must be a positive integer. CLI version/help checks have a separate 30-second limit.                                                                                 |
| `max_input_bytes`     | `800000`           | Maximum UTF-8 size of the complete prompt, context, and output schema sent to a stage. Must be a positive integer. Oversized input fails the stage before a model call; it is never silently truncated.                                             |
| `max_output_bytes`    | `16000000`         | Maximum combined stdout and stderr size per CLI call. Must be a positive integer. Exceeding it terminates the call.                                                                                                                                 |
| `continue_on_error` | `true` | Git mode: continue with other branches after a stage fails. `false` stops on the first operational failure and attempts checkout restoration. Integrity failures always stop. Folder mode always stops on an operational failure; review findings are not operational failures. |
| `prompts` | `{}` | Custom prompt paths for `document`, `review`, and `compare`. Unspecified active stages use bundled templates; `compare` is ignored in folder mode. |

Relative source, report, prompt, and explicit executable paths are resolved
against the configuration file's directory. `~` is expanded. The source and
report directories must be separate: neither may contain the other. Prompt files
must exist outside the inspected source directory.

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
The full JSONC examples show these fields as comments in all three stage blocks.
Uncomment only the fields you want to override. Folder mode never resolves or
checks `stage_agents.compare`, so its CLI does not need to be installed.

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

Here, `compare` uses the bundled template in git mode and is unused in folder mode.
Custom prompts must follow the
existing output contracts: the runner appends the stage context and required JSON
Schema, and the agent must return the report in `report_markdown` rather than write
files itself.
Git document/review contracts remain version `2.0` with `branch` and `source_commit`.
Folder contracts use version `3.0` with `source_directory` and `source_fingerprint`.
Custom document/review prompts must handle the chosen source mode. See the
[folder document schema](schemas/folder-document.schema.json) and
[folder review schema](schemas/folder-review.schema.json).

### Run explain-this-pls

Root and ordinary users use the same commands in both git and folder modes,
inside or outside containers. Root needs no extra option, environment variable,
or confirmation. When the effective UID is 0, the runner prints this warning once
to stderr, without changing the exit status or the JSON summary on stdout:

```text
WARNING: Running as root; child CLIs inherit root privileges.
```

First, validate the configuration, source state, and CLI capabilities:

```sh
python3 explain.py --config config.jsonc --check
```

The `--check` option checks CLI availability, versions, and required capabilities
without calling a model or switching branches. It writes a manifest and configuration
snapshot under `reports_dir`. In folder mode it also inventories and fingerprints
the source tree, and checks only the CLIs used for document and review.

Then start the investigation:

```sh
python3 explain.py --config config.jsonc
```

Git ownership checks apply equally to root and ordinary users. If Git rejects a
checkout owned by another UID, run with the checkout owner's UID, or explicitly
trust that specific checkout for this run:

```sh
python3 explain.py \
  --config config.jsonc \
  --trust-repository
```

To check the same configuration and checkout without calling a model:

```sh
python3 explain.py \
  --config config.jsonc \
  --trust-repository \
  --check
```

`--trust-repository` applies only to the checkout selected by the active Git
configuration. Each runner Git command receives an empty `safe.directory` entry
to reset the exception list, followed by the checkout's canonical absolute path
(including symlink resolution). Paths with spaces are supported. The runner does
not change system, user, or repository Git configuration, trust other checkouts,
or pass this exception to external agents. Its Git processes continue to ignore
system and global configuration, so a global Git exception is not a substitute.
In folder mode, this option is a configuration error before results are created.

Trust does not fix filesystem permissions, an inaccessible HOME, missing CLIs, or
authentication errors. A trusted checkout must still pass all source integrity
checks, including the clean working tree requirement. Root support in the runner
also does not guarantee that Codex CLI, Claude Code, or OpenCode permits running
as root. Their sandbox and tool restrictions remain in effect; the runner does
not bypass their own startup policies or change the process user.

In git mode, the runner locks the repository, pins the selected branch commits, and checks out
each commit to run the `document` and `review` stages. It restores the original
checkout before running `compare` in a new session outside the repository. Avoid
editing or switching the inspected checkout while the run is in progress.

In folder mode, the runner locks the source directory and runs document and review
in place, in separate sessions. It records relative paths, entry types, permissions,
file hashes, and symlink targets, and checks the tree fingerprint before and after
each stage. A detected change fails the run before publishing that stage's report;
files are not restored. These checks do not provide a backup or continuous
immutability: a change reverted between checks may go undetected. Reading all file
contents for each fingerprint takes additional I/O time on large trees.

Each run creates a separate directory under `reports_dir`. Successful Git stages produce:

| Path within the run directory                          | Contents                                                                     |
| ------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `branches/<branch-id>/ARCHITECTURE.md`                 | Architecture documentation for one branch.                                   |
| `branches/<branch-id>/ARCHITECTURE_REVIEW.md`          | Independent review of that document.                                         |
| `branches/<branch-id>/document.json` and `review.json` | Structured versions of the branch reports.                                   |
| `comparison/BRANCH_COMPARISON.md` and `compare.json`   | Baseline comparison in Markdown and JSON.                                    |
| `comparison/inputs.json`                               | The reports and Git metadata supplied to the comparison stage.               |
| `manifest.json`                                        | Run status, pinned commits, stage results, and checkout restoration details. |
| `config.snapshot.json`                                 | The configuration used for this run.                                         |

Folder runs place `ARCHITECTURE.md`, `ARCHITECTURE_REVIEW.md`, `document.json`,
`review.json`, `manifest.json`, and `config.snapshot.json` directly in the run
directory. `source.inventory.json` records the complete inventory and fingerprint;
the inventory is not automatically included in the model prompt. No comparison
report is created. Missing Git history alone does not lower report completion status.

Stage logs are stored in `document.logs`, `review.logs`, and `compare.logs` alongside
their respective reports, for active stages only. The terminal prints a JSON summary
with the run status and manifest path. Exit codes are `0` for success, `2` for
incomplete/unaccepted results without an operational failure, `1` for an operational
failure, and `130` for an interrupted run.
