# explain-this-pls

## What is this?

Analyzes legacy code and explains how the damn thing actually works.

explain-this-pls uses your coding agent to document an existing codebase, with an
optional separate review of documented claims against the code. Review is disabled
by default. Analyze a folder, study a Git branch, or compare several branches.
Agents are instructed to read the code without running the project's builds or
tests.

## Features

- **Architecture reports** explaining the system's main parts and workflows,
  with references to the source code.
- **PlantUML diagrams** of components and data flows, embedded as source in the reports.
- **Optional report review** identifying unsupported claims, contradictions and missing information.
- **Source coverage diagnostics** showing cited areas without treating missing citations as unread files.
- **Report revision**, when review is enabled, allowing one correction round and a new review while retaining earlier versions.
- **Branch comparison** showing differences from a branch you choose as a baseline.
- **Folder analysis** for source code that does not need to be in Git.
- **Shareable results** in Markdown and JSON, in your preferred language.
- **Usage summaries** with reported tokens, estimated costs, elapsed time and attempts
  for each stage and the whole run.

## Who is it for?

Anyone who needs to understand an unfamiliar codebase, document an older system,
or compare versions before planning changes.

## Instructions

### Prerequisites

- macOS or Linux.
- Python 3.11 or newer.
- Git 2.34.1 or newer, only for Git mode.
- An installed and configured coding-agent CLI: Codex CLI, OpenCode V2, or OpenCode 1.2.27 (XXX).
  Sign in before using explain-this-pls.

### Get Your Input Data

Download this package, or clone it outside the source directory you want to inspect:

```sh
git clone https://github.com/Krusty84/explain-this-pls.git
cd explain-this-pls
python3 install-explain.py
source .venv/bin/activate
```

The installer creates or reuses `.venv` beside `explain.py` and installs
`requirements.txt` into it. Run it again after updating the package. It also prints
a command to launch the app without activating the environment.

Choose how to provide your source code:

- **Git mode:** use a clean standalone local clone with one or more local branches.
  Choose a baseline for comparison. All required commits must exist locally.
  Staged changes, tracked changes, non-ignored untracked files, conflicts, and
  unfinished Git operations are rejected. Ignored untracked files are excluded.
- **Folder mode:** use an existing directory containing your source files.
  Git and a clean checkout are not required. Hidden files are included too.

### Configuration

Copy the example for your agent to `config.jsonc`, or use a project-specific name
such as `config_prj1.jsonc` or `config_superErp.jsonc`:

- [Codex CLI](config.codex.example.jsonc)
- [OpenCode V2](config.opencode.example.jsonc)
- [OpenCode 1.2.27 / XXX](config.xxx.example.jsonc)

For example, with Codex CLI:

```sh
cp config.codex.example.jsonc config.jsonc
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
`git_mode.baseline_branch` to `"main"`. Comparison is skipped.

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
and, when enabled, a review without requiring Git; `git_mode` settings are ignored.

Use `source_filter` to exclude files or directories from analysis:

```json
"source_filter": {
  "follow_gitignore": false,
  "exclude_paths": []
}
```

Add exact paths relative to the source root to `exclude_paths`. A directory entry
also excludes its contents. Set `follow_gitignore` to `true` to use `.gitignore`
rules in Folder mode. Git mode still includes tracked files unless you exclude
them explicitly. There are no default artifact exclusions; review the commented
examples before adding paths.

Exclusions define the analysis scope. Agent file tools can still access excluded
paths in the original directory.

#### Main settings

| Field                       | Purpose                                                                                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `mode`                      | Analyze Git branches (`"git"`) or a directory (`"folder"`).                                                               |
| `git_mode`                  | Source repository, local branches and the baseline branch.                                                                |
| `folder_mode.path`          | Source directory for folder mode.                                                                                         |
| `reports_dir`               | Destination for results; each run gets a separate subfolder.                                                              |
| `project_description`       | Optional introduction to the system's purpose and history.                                                                |
| `output_language`           | Report language: `"Russian"` (default) or `"English"`.                                                                    |
| `priority_scenarios`        | Optional workflows or areas to focus on.                                                                                  |

#### Agent settings

| Field              | Purpose                                                                       |
| ------------------ | ----------------------------------------------------------------------------- |
| `agent.backend`    | Coding agent: `codex`, `opencode` (V2), or `xxx` (OpenCode 1.2.27).           |
| `agent.executable` | CLI command or path; defaults to `codex`, `opencode`, or `xxx`, respectively. |
| `agent.model`      | Optional model name; omit it or use `null` for the CLI's configured model.    |

The tool uses your CLI's existing login and settings. No separate API key is needed
in this configuration.

#### Optional settings

**Review is opt-in.** Enable it to check the architecture report against the code:

```json
{
  "execution": {
    "review_enabled": true,
    "max_revision_rounds": 1
  }
}
```

Without this setting, each folder or branch gets an architecture study. Multiple
Git branches also get a comparison. Review can trigger one correction round and
a repeat review, which can increase model usage. Earlier report versions remain
available.

The [commented configuration](config.codex.example.jsonc) describes other options:

- **Stage timeouts:** `execution.stage_timeout_seconds` defaults to one hour.
- **Different agents per stage:** use `stage_agents` for study, review or comparison.
- **Legacy encodings:** use `source_decoding.rules` for encodings such as CP1251.
- **Continuing after errors:** `continue_on_error` defaults to `true`. Set it to
  `false` to stop further agent calls after an error. Source changes always stop the run.

#### Custom prompts

The included prompts are ready to use. To customize them, copy an existing
[study](prompts/study.md), [revision](prompts/revise.md),
[review](prompts/review.md), or [comparison](prompts/compare.md)
template, keep its required response format, and set its path in `prompts`.
Store custom templates outside the source directory. See the
[commented configuration](config.codex.example.jsonc) for examples.

### Run explain-this-pls

To use the interactive interface, run without arguments:

```sh
python3 explain.py
```

On first launch, select the folder containing your JSON/JSONC configurations.
The app remembers this folder. Use **Change folder** to choose another location,
or **Refresh** after editing or adding configs.

Select a config to preview its source, branches, agents, review setting and report
destination. Invalid configs show validation errors and cannot run. Use **Check
setup** to check prerequisites, then **Run** to start analysis. Selecting a config
alone does not call a model.

For Codex agents, **Models** lets you choose a model for the shared agent or an
individual stage through **Apply Scope**. Use **Check ChatGPT Access**, then
**Apply** after a successful check. The access check makes a short model request
and uses your Codex allowance.
Refresh before applying if you changed the config in another editor.

The **Steps** table shows progress, elapsed time and available usage estimates.
Select a row for details and report paths. Scroll horizontally for additional
columns on narrow terminals. **Final result** stays open after completion with
the outcome, totals and paths. **Configs** returns to configuration selection.

Use **Cancel** or Ctrl+C to stop a run and wait for cleanup. **Quit** also waits
for cleanup when analysis is active. Use Tab, arrow keys and Enter to navigate.
The interactive interface requires a terminal.

To open a particular config without changing the saved folder:

```sh
python3 explain.py --tui --config config_prj1.jsonc
```

Existing command-line usage remains available. First, check your configuration,
source files, and CLI setup:

```sh
python3 explain.py --config config.jsonc --check
```

This checks your local setup without calling a model or switching branches.
It does not test your agent login or model availability.

To list Codex models without an audit or access probes:

```sh
python3 explain.py --config config.jsonc --models --output text
python3 explain.py --config config.jsonc --models --output json
```

Use the interactive **Models** dialog to check access.

Then start the analysis:

```sh
python3 explain.py --config config.jsonc
```

Progress messages show the current branch or folder and analysis stage. The final
summary shows the outcome and paths to the results.
`output_language` controls the reports.

Use `--no-progress` to hide the spinner and waiting messages, `--output json` for a
script-friendly summary, or `python3 explain.py --help` to see all options.

### Read the results

Each run saves results in a new subfolder of `reports_dir`. Start with these files:

| Report                   | Contents                                                                  |
| ------------------------ | ------------------------------------------------------------------------- |
| `FINAL_REPORT.md`        | Start here: studies, reviews, comparison and limitations in one document. |
| `ARCHITECTURE.md`        | How the system works, with component and data-flow diagrams.              |
| `ARCHITECTURE_REVIEW.md` | Tables of review assessments, source references, findings and gaps.       |
| `BRANCH_COMPARISON.md`   | A table of differences from the baseline, for two or more Git branches.   |

`FINAL_REPORT.md` is directly in the run folder. In Git mode, `ARCHITECTURE.md`
and `ARCHITECTURE_REVIEW.md` are under `branches/<branch-id>/`, and the comparison,
when applicable, is under `comparison/`. In folder mode, the architecture report
and review are directly in the run folder.

Review findings and citation warnings appear in `FINAL_REPORT.md`. Reports include
references to source files and lines. PlantUML diagrams are included as source;
use a PlantUML-capable viewer to display them.

### Understand the outcome

By default, useful partial results are kept even if a stage fails
(`result_policy: "compromise"`). Unreviewed material and missing inputs are labeled
in the final report. If a revision is produced, the results identify the selected
version and retain earlier material.

- **COMPLETE:** checks for all enabled stages passed, review is required only when enabled.
- **COMPLETE_WITH_WARNINGS:** processing completed with citation warnings; read them before relying on the affected claims.
- **PARTIAL:** useful material is available, with unresolved issues or missing required review checks.
- **FAILED:** no usable material, a critical failure, or a required-stage error
  under `strict`. Earlier valid reports may still be available.

Read the limitations and findings before relying on the reports. Source references
and automated review help assess the explanations, but do not prove correctness or
exhaustive coverage.

For implementation, validation, storage and development tools, see
[ARCHITECTURE.md](ARCHITECTURE.md). For response formats and evidence rules, see
[CONTRACTS.md](CONTRACTS.md).
