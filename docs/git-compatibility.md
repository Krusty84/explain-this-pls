# Git compatibility and verification

Git mode requires `MIN_GIT_VERSION = (2, 34, 1)`. The runner resolves the actual
executable through PATH, stores its absolute resolved path, and uses it for the
version probe and all root/submodule commands. The probe uses a private neutral
directory and an allowlisted environment. Distribution suffixes are accepted;
unrecognized versions and versions below the numeric minimum fail before checkout
preflight. Folder mode does not resolve or execute Git.

The minimum describes the command interface, not package security. Install vendor
security updates. A base version cannot identify backported behavior: Ubuntu
[USN-5376-4](https://ubuntu.com/security/notices/USN-5376-4) fixed command-line safety
configuration in `1:2.34.1-1ubuntu1.16`. The runner uses the same mechanisms on every
supported build, without inferring security capabilities from a version triple.

## Command and environment audit

| Mechanism | Git 2.34.1 compatibility and checks |
| --- | --- |
| Version | `git --version`, numeric tuple comparison; distribution suffix parser tests are separate from the real-build matrix. |
| Discovery | `rev-parse --absolute-git-dir`, `--git-common-dir`, `--show-toplevel`; filesystem path/owner checks precede checkout commands. No linked worktrees, external/reused Git directories, symlink directories, or alternates. |
| HEAD | Direct bounded read of the verified `git_dir/HEAD`, including gitfile submodules. `lstat`, no-follow/nonblocking open, regular-file check, fstat/path identity, size/timestamp checks before/after read. Ref syntax uses `check-ref-format`. Detached IDs use `rev-parse --show-object-format` (SHA-1/SHA-256) and commit verification. No dependency on `symbolic-ref --no-recurse` or recursive `--short` output. |
| Restoration | `switch --detach --no-recurse-submodules` and `switch --no-guess --no-recurse-submodules -- <immediate-branch>` work on 2.34.1. Never forced ref movement or manual HEAD writes. Original ref identity and commit must still match. Every node retains the partial-switch journal and integrity checks. |
| Cleanliness | `status --porcelain=v1 -z --untracked-files=all --ignore-submodules=all`, `diff-index --cached --raw -z --ignore-submodules=none --no-ext-diff HEAD --`, and `ls-files` ignored/untracked/entry-flag checks are supported. Children are checked individually. |
| Fsmonitor | `-c core.fsmonitor=` disables the hook on 2.34.1 and modern Git. `false` and `true` are hook paths on old Git. Tests seed a real FSMN index extension, install hook/PATH sentinels, change tracked files, and verify detection without modifying the index/config. |
| Other overrides | `core.hooksPath=/dev/null`, `core.untrackedCache=false`, `submodule.recurse=false`, `core.pager=cat` are supported. Custom submodule update commands are never invoked. |
| Trees and refs | `rev-parse --verify`, `cat-file -t/--batch-check`, `ls-tree -r -z`, `config --no-includes --null --blob`, `check-ref-format`, and raw `diff --no-abbrev --no-renames --no-ext-diff --no-textconv -z` are supported. All requested branch and gitlink commits are pinned before switching. |
| Locality | `GIT_ALLOW_PROTOCOL=''` denies every transport even with repository `protocol.*.allow=always`. `GIT_NO_LAZY_FETCH=1` is supplementary only; 2.34.1 ignores it. Partial-clone/promisor configuration is rejected before resolving HEAD/objects because old Git can write `remote.*.partialclonefilter` before transport denial. Missing local commits/trees/blobs fail with snapshot/path/SHA context. |
| Ownership | Runner effective UID must own checkout root, `.git` entry, and actual admin directory, unless `--trust-repository` was supplied. Root follows the same rule; SUDO_UID is not passed to Git. The check remains effective on upstream 2.34.1 without native protection. |
| Addressed trust | A private global config per validated checkout uses `GIT_CONFIG_GLOBAL`, available in 2.34.1. Empty `safe.directory` followed by one escaped canonical path; no later command-scope reset, wildcards, includes, retries, or unrelated settings. Configs are 0600 in a 0700 temporary directory and removed by `Runner.run` on success/errors/handled interrupts. Agents receive their usual environment without these runner-created settings. |
| Remaining environment | Only PATH/LANG/LC_ALL are inherited by Git. `GIT_CONFIG_NOSYSTEM=1`, replacement global config (otherwise `/dev/null`), `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, `GIT_PAGER=cat`, and `GIT_NO_REPLACE_OBJECTS=1` are supported by 2.34.1. Inherited Git config overrides, object directories, executable paths and test switches are excluded. |

Sources: [2.34.1 fsmonitor configuration implementation](https://github.com/git/git/blob/v2.34.1/config.c),
[2.34.1 transport implementation](https://github.com/git/git/blob/v2.34.1/transport.c),
[2.34.1 promisor implementation](https://github.com/git/git/blob/v2.34.1/promisor-remote.c),
[Git fsmonitor documentation](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsmonitor),
[Git configuration environment](https://git-scm.com/docs/git#Documentation/git.txt-GITCONFIGGLOBAL).

## Real-build CI matrix

Every variant runs the full suite:

```sh
python3 -B -m unittest discover -s tests -v
```

| Variant | Reproducible input / diagnostics |
| --- | --- |
| Upstream 2.34.1 | Original release tarball, SHA-256 `3a0755dd1cfab71a24dd96df3498c29cd0acd13b04f3d08bf933e81286db802c`, compiled in Ubuntu 22.04. |
| Ubuntu 22.04 updated | `jammy-updates`/`jammy-security`; full installed Git/git-man package revision and architecture logged each run. |
| Security-patched Ubuntu without command-scope trust | Snapshot `20260201T000000Z`, exact Git/git-man `1:2.34.1-1ubuntu1.15`; installation fails if unavailable. The real foreign-owner test requires command-scope trust to fail and private global trust to succeed. |
| Modern Git | Existing Linux/macOS jobs with Python 3.11–3.14, plus Debian bookworm/Python 3.13 root container with its package revision logged. |

The container jobs run tests in a separate network namespace after installing
tools. The runner and fake CLIs do not download objects or contact model services.
`git-build.log`/`git-tests.log` are uploaded for the 2.34.1 matrix, including real
executable paths, versions, and behavioral ownership probes. Package preparations
may access the network; analysis does not.

Tests invoke `explain.py` as a subprocess, load JSONC configuration, execute fake
CLI version/help and analysis stages, inspect actual HEADs and source contents,
and validate reports/restoration. Tests cover all three backend contracts, nested
SHA/content changes, symbolic chains, partial switch errors/signals, external
modifications, and byte-preserving `--check` failures. Folder tests remove Git
from PATH. Parser shims and injected file races are labeled as such; they are not
substitutes for real Git builds or OS ownership tests.

Actual root and foreign-owner tests are skipped explicitly without UID 0. Foreign
fixtures are created by an unprivileged child in isolated test directories; the
runner never changes its own UID, ownership, permissions, or SUDO_UID to bypass
rejection. Behavioral package probes are enabled by `AUDIT_GIT_BUILD` in CI only.
This variable configures tests, not runner permissions.

The Git manifest field records `executable`, `version_string`, numeric `version`,
`minimum_version`, and `compatibility`. Agent response schemas are unchanged.

## Local verification, 2026-09-28

On macOS with Python 3.13.2, the full suite passed on the compiled upstream 2.34.1
release and installed Git 2.54.0: 123 tests per build, with six explicit skips for
actual root/foreign-owner tests. After the final diagnostic and transport/wildcard
regressions were added, eight HEAD/version/transport tests and the native-ownership
diagnostic test passed separately on each build. The current suite has 125 tests.
The workflow YAML and preparation script passed syntax checks.

The pinned Ubuntu revision was verified in the snapshot package index. Ubuntu
binaries and actual root tests were **not run locally** because Docker access was
declined. Their jobs and behavioral checks are prepared in CI; this local record
does not claim that the entire CI matrix has passed.
