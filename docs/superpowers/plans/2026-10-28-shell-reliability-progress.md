# SDD ledger — plan: docs/superpowers/plans/2026-10-28-shell-reliability.md

- Ruling: execute in the user's existing checkout, leaving changes uncommitted — user approved direct execution in this session; avoid additional worktree approval loops.
- Ruling: share argument/configuration/HTTP helpers in `file-transfer-common.sh` — two entry points keep consistent validation and old-curl behavior. Deploy all three Shell files together.
- Baseline: original two Shell tests passed in Git Bash 5.3.15. Windows sandbox prevented Python temporary-directory operations; rerun with workspace-local TEMP/TMP.
- RED: new regression failed with `FAIL: old manifest overwritten` against old upload script.
- Task 1/2 implementation: isolated upload scratch, atomic publication, finite retry, manifest validation, isolated locked download state, verified reuse and atomic output replacement prepared.
- Ruling: omit Bash nounset in production scripts — Bash 4.2 reports empty arrays as unset; explicit validation and errexit/pipefail preserve predictable empty-file behavior.
- Task 1: complete. Nine-group offline suite covers upload failure preservation, chunk isolation, retries, options, unique response URL, empty files and cross-platform names.
- Task 2: complete. Offline suite covers target preservation, verified resume, metadata/CRLF compatibility and malformed manifests; real HTTP tests cover corrupt cache, retries, auth and lock behavior.
- Review: independent reviewer identified curl URL globbing. Added literal bracket/brace URL test, confirmed RED, fixed with `-g`, confirmed GREEN. No other review findings.
- Additional RED/GREEN: Windows reserved source basename initially produced a Python-incompatible manifest; now rejected consistently with Python.
- Environment diagnosis: Windows native curl emits CRLF in write-out. Normalize status/redirect line endings; real same-origin and cross-origin redirect tests pass.
- Verification: full combined Python suite passed: 62 tests, 0 failures (original 50 plus 12 real HTTP integration tests). Final nine-group offline suite passed with `BASH_COMPAT=42`; syntax checks and `git diff --check` passed. `git diff --exit-code -- file-transfer.py` confirms the Python implementation is unchanged.
- Task 3: complete. README documents deployment of all three Shell files, CLI/environment configuration, retries, recovery, locking, expiration and runtime limitations. Changes left uncommitted for user review.
- Limitation: Git Bash compatibility mode is not a substitute for execution on real CentOS 7 Bash 4.2/curl 7.29. No target Linux distribution was available in this workspace.
