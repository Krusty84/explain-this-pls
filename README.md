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
- **Choice of coding agent** with Codex CLI, Claude Code, and OpenCode support,
  including per-stage executable and model settings.
- **Markdown and JSON results** with validated output contracts, invocation logs,
  a run manifest, and a configuration snapshot.

## Who is it for?

Developers, maintainers, and architects who need to understand an unfamiliar legacy
system, document how it is implemented, or compare customer-specific and historical
branches before planning changes.

## Instructions

### Prerequisites

- macOS or Linux.
- Python 3.11 or newer.
- Git 2.34.1 or newer.
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

Recursive Git submodules are supported when they are already initialized. Prepare
them yourself before starting the runner, for example with
`git submodule update --init --recursive --checkout` in a trusted checkout. Fetch
any additional historical submodule commits needed by the selected branches during
that preparation. The runner never fetches, initializes submodules, runs custom
update commands, or advances them to remote tips. Every required commit, tree, and
blob must be available locally.

The original checkout and every selected branch must contain the same submodule
paths and logical names at every level. The runner reads `.gitmodules`
from each pinned parent commit. Missing checkouts or objects are reported with the
snapshot, submodule path, and required SHA.

Every initialized submodule must initially match its parent's gitlink, with no
staged, tracked, untracked, or ignored changes and no unfinished Git operation.
Each working tree is checked directly, even with `submodule.<name>.ignore=all`.

For folder mode, prepare an existing directory with the source files and subdirectories.
There is no requirement for `.git`, branches, or a clean checkout. All entries,
including hidden files, are inventoried; there are no configurable exclusions.

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
  "continue_on_error": true,
  "prompts": {}
}
```

Existing flat configurations with top-level `repository`, `branches`, and
`baseline_branch` still run in git mode. To migrate, move these three fields into
`git_mode` and add `"mode": "git"`. Do not mix flat source fields with the new format.

#### Project and execution settings

| Field                      | Required / default         | Description                                                                                                                                                                                                                                                                     |
| -------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mode`                     | Required in grouped format | `"git"` or `"folder"`; only the selected source section is used.                                                                                                                                                                                                                |
| `git_mode.repository`      | Required in git mode       | Root of a standalone checkout, not a linked worktree or bare repository.                                                                                                                                                                                                        |
| `folder_mode.path`         | Required in folder mode    | Existing source directory; no Git installation or repository is required.                                                                                                                                                                                                       |
| `reports_dir`              | Required                   | Directory for results. Each run creates a new subdirectory containing reports, logs, a manifest, and a configuration snapshot.                                                                                                                                                  |
| `project_description`      | Recommended; `""`          | Introduction to the system's purpose and history, passed to every active stage as user background, not verified implementation evidence.                                                                                                                                        |
| `git_mode.branches`        | Required in git mode       | At least two unique, nonempty local branch names, processed in order. No fetch or pull is performed; commit IDs are pinned at startup.                                                                                                                                          |
| `git_mode.baseline_branch` | Required in git mode       | Reference branch for comparison. Must be included in `git_mode.branches`.                                                                                                                                                                                                       |
| `output_language`          | `"Russian"`                | Language of the generated reports. Set `"English"` for English output.                                                                                                                                                                                                          |
| `agent`                    | Required                   | Default CLI settings for all stages; see below.                                                                                                                                                                                                                                 |
| `stage_agents`             | `{}`                       | Overrides of `agent` for `document`, `review`, or `compare`. Only active stages are resolved and checked.                                                                                                                                                                       |
| `priority_scenarios`       | `[]`                       | An array of strings describing flows or areas to prioritize during documentation and review.                                                                                                                                                                                    |
| `continue_on_error`        | `true`                     | Git mode: continue with other branches after a stage fails. `false` stops on the first operational failure and attempts checkout restoration. Integrity failures always stop. Folder mode always stops on an operational failure; review findings are not operational failures. |
| `prompts`                  | `{}`                       | Custom prompt paths for `document`, `review`, and `compare`. Unspecified active stages use bundled templates; `compare` is ignored in folder mode.                                                                                                                              |

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
Custom document/review prompts must handle the chosen source mode. See the
[folder document schema](schemas/folder-document.schema.json) and
[folder review schema](schemas/folder-review.schema.json).

### Run explain-this-pls

Root and ordinary users use the same commands in both git and folder modes,
inside or outside containers. Root needs no extra option, environment variable,
or confirmation. When the effective UID is 0, the runner prints this warning once
to stderr, without changing the exit status or the selected result format on stdout:

```text
[WARN] Running as root; child CLIs inherit root privileges.
```

First, validate the configuration, source state, and CLI capabilities:

```sh
python3 explain.py --config config.jsonc --check
```

The `--check` option checks CLI availability, versions, and required capabilities
without calling a model or switching branches. It writes a manifest and configuration
snapshot under `reports_dir`. In folder mode it also inventories and fingerprints
the source tree, and checks only the CLIs used for document and review.
In Git mode, it builds and validates the complete recursive plan for the original
checkout and all selected branches. It does not change HEAD, refs, indexes, source
files, or Git configuration, including when the last branch fails validation.

Then start the investigation:

```sh
python3 explain.py --config config.jsonc
```

Console output uses English independently of `output_language`, which controls
only generated reports. The console uses sequential lines with `[RUN]`, `[OK]`,
`[WARN]`, `[FAIL]`, and `[SKIP]`; no terminal control sequences or third-party UI
packages are needed.

| Option | Behavior |
| --- | --- |
| `--output auto` | Default: readable text when **stdout** is a TTY, JSON otherwise. Selected once after argument parsing. |
| `--output text` | One readable final summary on stdout, including duration, branch results, comparison, restoration and existing artifact paths. |
| `--output json` | Exactly one final JSON document on stdout. |
| `--verbose` | Additional technical event context on stderr. Never prints prompts, credentials or raw agent output. |
| `--no-progress` | Suppresses only periodic waiting messages. Stage starts/completions, warnings, errors and the final result remain visible. |

In every mode, the header, progress, warnings and diagnostic explanations go to
**stderr**. Explicit `--output text` or `--output json` overrides TTY detection.
The JSON document retains these four keys:

```json
{"run_id": "20260928T124632Z-a7408eb86f", "status": "PREFLIGHT_OK", "manifest": "/reports/20260928T124632Z-a7408eb86f/manifest.json", "exit_code": 0}
```

Handled errors after argument parsing also produce a result. `manifest` is `null`
when no manifest exists, and `run_id` is `null` if an ID has not yet been assigned.
**Exception:** `--help` and argument syntax errors retain standard argparse
help/usage behavior, without a result JSON, run directory or manifest.

```sh
# Automatic format selection
python3 explain.py --config config.jsonc --check

# Readable text, including Docker logs
python3 explain.py --config config.jsonc --output text

# Machine result separate from diagnostics
python3 explain.py \
  --config config.jsonc \
  --output json \
  > result.json 2> console.log

# Technical details without periodic waiting messages
python3 explain.py \
  --config config.jsonc \
  --output text \
  --verbose \
  --no-progress
```

Each agent stage identifies the branch (or folder), stage and backend.
Approximately every 30 seconds while a CLI is running, stderr reports monotonic
elapsed time and the time since the last observed CLI output, or that no output
has arrived. This is activity information, not a completion percentage or an ETA.
The runner imposes no time or input/output size limits on model calls, CLI checks,
or Git commands. Processes are awaited until they exit or the run is interrupted,
even if a child closes both output pipes. Limits imposed by the CLI or provider
still apply.
A stage is reported complete only after response parsing, contract and source
integrity checks, and publication of its result. A successful CLI exit alone does
not imply an accepted analysis.

`PREFLIGHT_OK` means the local preflight passed; no model calls or analysis took
place. It does not verify authentication, model availability or analysis quality.
`COMPLETE` means the full analysis passed the existing acceptance checks;
`PARTIAL` means an incomplete or unaccepted result without an operational error;
`FAILED` means an operational error (including a continued stage failure).
A handled interruption returns exit code 130. Stop and restoration messages reflect
actual transitions; restoration is reported verified only after its integrity checks.
Errors identify their cause and, when known, a safe next step; no automatic repair
or submodule initialization is performed. The existing manifest `errors` arrays
remain strings; additional `diagnostics` entries contain structured error context.

The runner checks owners of each checkout root, `.git` entry, and actual Git
directory before running Git there. All must belong to the runner's effective UID;
root has no automatic exception for another UID, including `SUDO_UID`.
These checks also apply to old Git builds without native ownership protection.
For a checkout owned by another UID, run with its owner's UID, or explicitly
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

`--trust-repository` applies to the checkout selected by the active Git
configuration and its discovered, path-validated submodules. Each checkout has a
private temporary global config containing an empty `safe.directory` reset followed
by that checkout's canonical absolute path. Only runner Git processes receive it
through `GIT_CONFIG_GLOBAL`; it works even on packages that ignore command-scope
`safe.directory`. Spaces, Unicode, and quoted paths are escaped. Temporary files
are outside the sources, with directory mode 0700 and file mode 0600, and are
removed on completion and handled errors. No wildcard exceptions are used.
The runner does not change system, user, or repository Git configuration, trust
other checkouts, or pass these settings to external agents. Its Git processes
ignore system and real user configuration, so a user's global exception is not a substitute.
In folder mode, this option is a configuration error before results are created.

Trust does not fix filesystem permissions, an inaccessible HOME, missing CLIs, or
authentication errors. A trusted checkout must still pass all source integrity
checks, including the clean working tree requirement. Root support in the runner
also does not guarantee that Codex CLI, Claude Code, or OpenCode permits running
as root. Their sandbox and tool restrictions remain in effect; the runner does
not bypass their own startup policies or change the process user.

In git mode, the runner locks the repository, pins the selected commits and all
recursive gitlinks, and switches the entire hierarchy to detached HEADs before
`document` or `review` can inspect it. It verifies source and Git metadata integrity
before and after analysis, after CLI checks, and after `compare`. An integrity
failure always stops the run, regardless of `continue_on_error`.

Before `compare` runs outside the repository, the runner restores the original
commit and immediate symbolic branch ref (or detached HEAD) of every node,
including `HEAD -> alias -> branch` chains. It reads a bounded regular HEAD file
from the verified Git directory and checks for symlinks and changes during reading.
Restoration uses Git commands; the runner never writes HEAD directly. It also
attempts restoration on agent/Git failures and handled interruptions such as SIGINT
or SIGTERM. A switch journal accounts for partially completed hierarchy changes.
If another process changes files, HEAD, Git metadata, or an original branch ref,
restoration refuses to overwrite the evidence. It never uses forced checkout,
reset, clean, or forced ref movement. The manifest records restoration success or
the failing node and preserves the analysis error alongside restoration errors.
Avoid editing or switching any node while the run is in progress. These are checks
at stage and switch boundaries, not continuous immutability or a backup. Restoration
cannot be promised after SIGKILL, power loss, or termination that bypasses Python
handlers.

Submodule sources are part of the document/review scope. Their root-relative paths,
parent repositories, expected SHAs, and verified actual states are supplied in the
stage contexts and manifest, with original restoration state stored separately.
Comparison inputs include recursive SHA changes against the baseline. A gitlink
change is commit metadata, not a complete file diff of the nested repository.

In folder mode, the runner locks the source directory and runs document and review
in place, in separate sessions. It records relative paths, entry types, permissions,
file hashes, and symlink targets, and checks the tree fingerprint before and after
each stage. A detected change fails the run before publishing that stage's report;
files are not restored. These checks do not provide a backup or continuous
immutability: a change reverted between checks may go undetected. Reading all file
contents for each fingerprint takes additional I/O time on large trees.

Each run creates a separate directory under `reports_dir`. Successful Git stages produce:

| Path within the run directory                          | Contents                                                                                                                      |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `branches/<branch-id>/ARCHITECTURE.md`                 | Architecture documentation for one branch.                                                                                    |
| `branches/<branch-id>/ARCHITECTURE_REVIEW.md`          | Independent review of that document.                                                                                          |
| `branches/<branch-id>/document.json` and `review.json` | Structured versions of the branch reports.                                                                                    |
| `comparison/BRANCH_COMPARISON.md` and `compare.json`   | Baseline comparison in Markdown and JSON.                                                                                     |
| `comparison/inputs.json`                               | The reports and Git metadata supplied to the comparison stage.                                                                |
| `manifest.json`                                        | Run status, Git executable/version/compatibility mechanisms, pinned commits, stage results, and checkout restoration details. |
| `run.log`                                              | UTC structured technical events, full commit IDs and sanitized exception chains; mode 0600. |
| `config.snapshot.json`                                 | The configuration used for this run.                                                                                          |

Folder runs place `ARCHITECTURE.md`, `ARCHITECTURE_REVIEW.md`, `document.json`,
`review.json`, `manifest.json`, and `config.snapshot.json` directly in the run
directory. `source.inventory.json` records the complete inventory and fingerprint;
the inventory is not automatically included in the model prompt. No comparison
report is created. Missing Git history alone does not lower report completion status.

Stage logs are stored in `document.logs`, `review.logs`, and `compare.logs` alongside
their respective reports, for active stages only. The parent opens `stdout.log`
and `stderr.log` before starting the CLI and flushes received blocks during the
call, so these files can be inspected while it is running. Their bytes are also
passed unchanged to the existing response parser. Raw agent output is never
copied into the console or `run.log`, even with `--verbose`. Stage files may contain
sensitive source/model content; result directories remain private (0700), with
log files at 0600.

`run.log` uses standard Python logging with one JSON record per line (UTC time,
level, event name, run ID and applicable branch/stage/backend context). Exception
chains include stack locations but omit arbitrary model-supplied exception values
and known credentials. It does not contain full prompts, the process environment
or full model responses. No file log is created in source directories or the working
directory for early failures. Console/log write failures do not prevent cleanup.

Exit codes are `0` for a passed check or an accepted complete result, `2` for
incomplete/unaccepted results without an operational failure, `1` for an operational
failure, and `130` for a handled interruption. Argument syntax errors keep
argparse's exit behavior.

Run the offline test suite with:

```sh
python3 -B -m unittest discover -s tests -v
```

Tests use real temporary repositories and fake CLIs; progress interval tests use
injected monotonic clocks or short bounded subprocess waits. Actual root and
foreign-owner tests require UID 0 and are otherwise explicitly skipped. The CI
matrix also covers real Git 2.34.1 builds; version shims only test version parsing.
